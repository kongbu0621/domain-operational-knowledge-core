"""Check persisted bindings before they can drive a read, review or retry.

These checks detect inconsistent records, not coordinated malicious rewriting
of a trusted local store. No repair or new state is inferred from bad metadata.
"""
from functools import wraps

from . import model as m


def checked(method):
    @wraps(method)
    def validate(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except m.KnowledgeError as exc:
            if exc.code == "NOT_FOUND":
                raise
            raise m.KnowledgeError("STORE_INVALID", "Stored record binding is inconsistent", "not_applicable") from exc
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise m.KnowledgeError("STORE_INVALID", "Stored record cannot be decoded", "not_applicable") from exc
    return validate


def decode(raw):
    return m.decode(raw.encode(), maximum=m.MAX_RESPONSE)


def timestamp(value):
    m.require(type(value) is int and 0 <= value <= 9007199254740991, "Invalid recorded time")


def body(document, names, operation, scope="stored:validation"):
    value = {key: document[key] for key in names.split()}
    m.text(scope, maximum=128)
    m.validate_body(operation, value)
    return value


def related(db, table, identity):
    try:
        return document(db, table, identity)
    except m.KnowledgeError as exc:
        raise m.KnowledgeError("STORE_INVALID", "Stored relationship cannot be verified", "not_applicable") from exc


@checked
def document(db, table, identity):
    # The table is selected internally, never from caller SQL.
    cursor = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,))
    values = cursor.fetchone()
    m.require(values is not None, "Object not found", "NOT_FOUND")
    row = dict(zip((item[0] for item in cursor.description), values))
    value = decode(row["document"])
    kind = dict(materials="material", candidates="candidate", reviews="review", versions="version")[table]
    m.require(value[kind + "_id"] == identity, "Stored row identity differs")
    m.identity(identity)
    timestamp(value["recorded_at"])
    if kind == "material":
        names = "material_id source_ref task_ref context claimed_outcome gaps reference"
        m.fields(value, names + " scope recorded_at")
        body(value, names, "register_material", value["scope"])
        m.sha(row["fingerprint"])
    elif kind == "candidate":
        names = "proposal_id kind content conditions material_ids"
        m.fields(value, names + " candidate_id scope candidate_sha256 recorded_at")
        original = body(value, names, "propose", value["scope"])
        m.require(m.digest(original) == row["fingerprint"] == value["candidate_sha256"]
                  and row["scope"] == value["scope"] and row["proposal_id"] == value["proposal_id"],
                  "Candidate content differs from its original binding")
        m.require(row["state"] in ("PENDING", "ACCEPTED", "REJECTED"), "Invalid candidate state")
    elif kind == "review":
        names = "candidate_id candidate_sha256 decision reviewer reason evidence_material_ids"
        m.fields(value, names + " review_id version_id recorded_at")
        original = body(value, names, "review")
        m.require(m.digest(original) == row["fingerprint"] and row["candidate_id"] == value["candidate_id"],
                  "Review content differs from its original binding")
        candidate = related(db, "candidates", value["candidate_id"])
        m.require(candidate["candidate_sha256"] == value["candidate_sha256"], "Review target differs")
        state = db.execute("SELECT state FROM candidates WHERE id=?", (value["candidate_id"],)).fetchone()[0]
        m.require(state == ("ACCEPTED" if value["decision"] == "ACCEPT" else "REJECTED"), "Review state differs")
        if value["decision"] == "ACCEPT":
            m.identity(value["version_id"])
            version = db.execute("SELECT id,candidate_id FROM versions WHERE id=?", (value["version_id"],)).fetchone()
            m.require(version == (value["version_id"], value["candidate_id"]), "Accepted review version is missing or unrelated")
        else:
            m.require(value["version_id"] is None, "Rejected review has a version")
    else:
        m.fields(value, "version_id lineage_id candidate_id review_id kind content conditions material_ids recorded_at")
        m.identity(value["lineage_id"])
        m.require(row["lineage_id"] == value["lineage_id"] and row["candidate_id"] == value["candidate_id"]
                  and row["kind"] == value["kind"] and decode(row["conditions"]) == value["conditions"]
                  and row["state"] in ("ACTIVE", "SUPERSEDED", "RETIRED"), "Knowledge index differs")
        candidate = related(db, "candidates", value["candidate_id"])
        review = related(db, "reviews", value["review_id"])
        m.require(review["decision"] == "ACCEPT" and review["version_id"] == identity
                  and review["candidate_id"] == value["candidate_id"]
                  and review["recorded_at"] == value["recorded_at"], "Knowledge review differs")
        m.require(all(value[key] == candidate[key] for key in ("kind", "content", "conditions"))
                  and value["material_ids"] == sorted(set(candidate["material_ids"] + review["evidence_material_ids"])),
                  "Knowledge content differs from the accepted candidate")
    return value


@checked
def pending(db, key):
    row = db.execute("SELECT material_id,fingerprint,request FROM pending WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value = m.request(row[2].encode())
    m.require(value["operation"] == "publish_material" and value["body"]["material_id"] == row[0]
              and m.digest(value) == row[1]
              and m.operation_key(value["scope"], value["operation"], value["request_id"]) == key,
              "Pending request differs from its original identity or content")
    return value


@checked
def receipt(db, key, store_id):
    row = db.execute("SELECT fingerprint,document FROM receipts WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value = decode(row[1])
    m.fields(value, "store_id scope operation request_id fingerprint recorded_at result")
    timestamp(value["recorded_at"])
    m.sha(value["fingerprint"])
    m.require(value["store_id"] == store_id and value["fingerprint"] == row[0]
              and m.operation_key(value["scope"], value["operation"], value["request_id"]) == key,
              "Receipt differs from its original request")
    result = value["result"]
    operation = value["operation"]
    if operation in ("publish_material", "register_material"):
        m.fields(result, "material_id reused")
        m.identity(result["material_id"])
    elif operation == "propose":
        m.fields(result, "candidate_id candidate_sha256 reused")
        m.identity(result["candidate_id"])
        m.sha(result["candidate_sha256"])
    else:
        m.fields(result, "review_id version_id reused")
        m.identity(result["review_id"])
        if result["version_id"] is not None:
            m.identity(result["version_id"])
    m.require(type(result["reused"]) is bool, "Invalid receipt reuse flag")
    return value
