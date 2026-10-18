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
    kind = dict(materials="material", candidates="candidate", reviews="review", versions="version",
                usages="usage", feedback="feedback", retirements="retirement")[table]
    m.require(value[kind + "_id"] == identity, "Stored row identity differs")
    m.identity(identity)
    timestamp(value["recorded_at"])
    if kind == "material":
        names = "material_id source_ref task_ref context claimed_outcome gaps reference"
        m.fields(value, names + " scope recorded_at")
        body(value, names, "register_material", value["scope"])
        m.sha(row["fingerprint"])
    elif kind == "candidate":
        revision = "expected_version_id" in value
        names = "proposal_id kind content conditions material_ids" + (" expected_version_id" if revision else "")
        m.fields(value, names + " candidate_id scope candidate_sha256 recorded_at")
        original = body(value, names, "propose_revision" if revision else "propose", value["scope"])
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
    elif kind == "version":
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

        if "expected_version_id" in candidate:
            parent = db.execute("SELECT lineage_id,kind,state FROM versions WHERE id=?", (candidate["expected_version_id"],)).fetchone()
            m.require(parent == (value["lineage_id"], value["kind"], "SUPERSEDED"), "Revision parent differs")
            m.require(db.execute("SELECT next_id FROM revision_links WHERE previous_id=?", (candidate["expected_version_id"],)).fetchone()
                      == (identity,), "Revision link differs")
        if db.execute("PRAGMA user_version").fetchone()[0] == 2:
            successor = db.execute("SELECT next_id FROM revision_links WHERE previous_id=?", (identity,)).fetchone()
            retired = db.execute("SELECT id FROM retirements WHERE version_id=?", (identity,)).fetchone()
            m.require((row["state"] == "ACTIVE" and not successor and not retired)
                      or (row["state"] == "SUPERSEDED" and successor and not retired)
                      or (row["state"] == "RETIRED" and retired and not successor), "Version transition evidence differs")
            if retired:
                related(db, "retirements", retired[0])
            if successor:
                next_row = db.execute("SELECT lineage_id,kind,candidate_id FROM versions WHERE id=?", successor).fetchone()
                m.require(next_row is not None and next_row[:2] == (value["lineage_id"], value["kind"]),
                          "Successor lineage differs")
                next_candidate = related(db, "candidates", next_row[2])
                m.require(next_candidate.get("expected_version_id") == identity, "Successor revision target differs")
    elif kind == "usage":
        names = "usage_id consumer_ref task_ref version_id context"
        m.fields(value, names + " scope state_at_start recorded_at")
        original = body(value, names, "start_use", value["scope"])
        m.require(m.digest([value["scope"], original]) == row["fingerprint"]
                  and row["version_id"] == value["version_id"] and value["state_at_start"] == "ACTIVE", "Usage binding differs")
        version = related(db, "versions", value["version_id"])
        m.require(all(value["context"].get(k) == v for k,v in version["conditions"].items()), "Usage conditions differ")
    elif kind == "feedback":
        m.fields(value, "feedback_id usage_id version_id predecessor_id material_id scope recorded_at")
        m.text(value["scope"], maximum=128)
        m.sha(row["fingerprint"])
        m.require(m.digest(value) == row["document_sha256"]
                  and all(row[k] == value[k] for k in ("usage_id", "predecessor_id", "material_id")), "Feedback binding differs")
        usage = related(db, "usages", value["usage_id"])
        material = related(db, "materials", value["material_id"])
        m.require(db.execute("SELECT fingerprint FROM materials WHERE id=?", (value["material_id"],)).fetchone()
                  == (row["fingerprint"],), "Feedback and its material bind different original inputs")
        m.require(usage["version_id"] == value["version_id"] and material["task_ref"] == usage["task_ref"]
                  and material["source_ref"] == usage["consumer_ref"] and material["context"] == usage["context"]
                  and material["scope"] == value["scope"], "Feedback source differs")
        if value["predecessor_id"] is not None:
            m.identity(value["predecessor_id"])
            parent = db.execute("SELECT usage_id FROM feedback WHERE id=?", (value["predecessor_id"],)).fetchone()
            m.require(parent == (value["usage_id"],) and value["predecessor_id"] != identity, "Feedback predecessor differs")
    elif kind == "retirement":
        names = "expected_version_id reviewer reason evidence_material_ids"
        m.fields(value, names + " retirement_id version_id recorded_at")
        original = body(value, names, "retire")
        m.require(row["fingerprint"] == m.digest(original) and row["version_id"] == value["version_id"] == value["expected_version_id"],
                  "Retirement binding differs")
        m.require(db.execute("SELECT state FROM versions WHERE id=?", (value["version_id"],)).fetchone() == ("RETIRED",),
                  "Retirement version state differs")
    return value


@checked
def pending(db, key):
    row = db.execute("SELECT material_id,fingerprint,request FROM pending WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value = m.request(row[2].encode())
    m.require(value["operation"] in m.PUBLICATIONS and m.material_body(value)["material_id"] == row[0]
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
    # The append-only event retains the exact receipt committed in the same
    # transaction. Request fingerprints alone do not bind the result IDs.
    m.require(db.execute("SELECT 1 FROM events WHERE receipt=? LIMIT 1", (row[1],)).fetchone() is not None,
              "Receipt differs from its committed event")
    result = value["result"]
    operation = value["operation"]
    if operation in ("publish_material", "register_material"):
        m.fields(result, "material_id reused")
        m.identity(result["material_id"])
    elif operation in ("propose", "propose_revision"):
        m.fields(result, "candidate_id candidate_sha256 reused")
        m.identity(result["candidate_id"])
        m.sha(result["candidate_sha256"])
    elif operation == "review":
        m.fields(result, "review_id version_id reused")
        m.identity(result["review_id"])
        if result["version_id"] is not None:
            m.identity(result["version_id"])
    elif operation == "start_use":
        m.fields(result, "usage_id reused")
        m.identity(result["usage_id"])
    elif operation in ("complete_use", "correct_feedback"):
        m.fields(result, "feedback_id material_id reused")
        m.identity(result["feedback_id"])
        m.identity(result["material_id"])
    elif operation == "retire":
        m.fields(result, "retirement_id reused")
        m.identity(result["retirement_id"])
    m.require(type(result["reused"]) is bool, "Invalid receipt reuse flag")
    return value
