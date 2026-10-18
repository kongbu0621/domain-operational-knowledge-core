"""Material -> candidate -> explicit review -> exact active knowledge."""
from __future__ import annotations

import base64
import hashlib
from functools import wraps
import json
import time
import uuid

from . import model as m
from .database import Database, initialize as initialize_database
from .ledger import MaterialLedger, capture_binding


def read_only(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except m.KnowledgeError as exc:
            raise m.KnowledgeError(exc.code, exc.message, "not_applicable", details=exc.details) from exc
    return checked


def writing(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except m.KnowledgeError as exc:
            if exc.commit_state == "not_applicable":
                raise m.KnowledgeError(exc.code, exc.message, "not_committed", details=exc.details) from exc
            raise
    return checked


@writing
def initialize(path, *, ledger_path, anchor_artifact_id):
    """Create only a new local store, bound to an existing public Ledger artifact."""
    binding = capture_binding(ledger_path, anchor_artifact_id)
    return Knowledge(initialize_database(path, binding))


def open_store(path):
    return Knowledge(Database(path))


class Knowledge:
    def __init__(self, database):
        self.database = database
        identity = database.identity
        self.ledger = MaterialLedger(identity["ledger"], identity["store_id"])

    def _receipt(self, db, key, fingerprint):
        receipt = self.database.receipt(db, key)
        if receipt:
            m.require(receipt["fingerprint"] == fingerprint, "Request ID already binds different content", "CONFLICT")
            return receipt

    def _commit(self, db, request, key, fingerprint, result):
        receipt = dict(store_id=self.database.identity["store_id"], scope=request["scope"], operation=request["operation"],
                       request_id=request["request_id"], fingerprint=fingerprint, recorded_at=int(time.time()), result=result)
        encoded = m.canonical(receipt).decode()
        db.execute("INSERT INTO receipts VALUES (?,?,?)", (key, fingerprint, encoded))
        db.execute("INSERT INTO events(receipt) VALUES (?)", (encoded,))
        return receipt

    def _material_snapshot(self, db, identity):
        material = self.database.document(db, "materials", identity)
        # A logical material has one creating request; later keys only reuse it.
        rows = db.execute("""SELECT key FROM receipts
            WHERE CASE WHEN json_valid(document) THEN
                  json_extract(document, '$.operation') IN ('publish_material', 'register_material')
              AND json_extract(document, '$.result.material_id')=?
              AND json_extract(document, '$.result.reused')=0 ELSE 0 END LIMIT 2""", (identity,)).fetchall()
        m.require(len(rows) == 1, "Material creating receipt is missing or ambiguous", "STORE_INVALID")
        origin = self.database.receipt(db, rows[0][0])
        m.require(origin["scope"] == material["scope"], "Material source scope differs", "STORE_INVALID")
        fingerprint = db.execute("SELECT fingerprint FROM materials WHERE id=?", (identity,)).fetchone()[0]
        snapshot = material, origin, fingerprint
        if origin["operation"] == "register_material":
            self._verify_material_binding(snapshot)
        return snapshot

    def _verify_material_binding(self, snapshot, *, text=None):
        material, origin, fingerprint = snapshot
        body = {key: value for key, value in material.items() if key not in ("scope", "recorded_at")}
        if origin["operation"] == "publish_material":
            del body["reference"]
            body["text"] = text
        original = dict(schema=m.SCHEMA, scope=origin["scope"], operation=origin["operation"],
                        request_id=origin["request_id"], body=body)
        m.require(m.digest([origin["scope"], origin["operation"], body]) == fingerprint
                  and m.digest(original) == origin["fingerprint"],
                  "Material differs from its creating request", "STORE_INVALID")
        if origin["operation"] == "publish_material":
            key = m.operation_key(origin["scope"], origin["operation"], origin["request_id"])
            expected = self.ledger.requests(key, body)[3]
            m.require(material["reference"] == expected, "Published material version differs from its original request", "STORE_INVALID")

    def _read_material(self, snapshot):
        material, origin, _ = snapshot
        entries = self.ledger.read(material["reference"], material["task_ref"])
        text = None
        if origin["operation"] == "publish_material":
            m.require(len(entries) == 1 and entries[0][0] == "@blob", "Published material root differs", "STORE_INVALID")
            try:
                text = entries[0][1].decode("utf-8")
            except UnicodeError as exc:
                raise m.KnowledgeError("STORE_INVALID", "Published text cannot be decoded", "not_applicable") from exc
        self._verify_material_binding(snapshot, text=text)
        return entries

    def _material(self, db, identity, *, verify=True):
        snapshot = self._material_snapshot(db, identity)
        if verify:
            self._read_material(snapshot)
        return snapshot[0]

    def _material_identity(self, db, body, fingerprint):
        row = db.execute("SELECT fingerprint FROM materials WHERE id=?", (body["material_id"],)).fetchone()
        if row:
            m.require(row[0] == fingerprint, "Material ID already binds different content", "CONFLICT")
            # A new receipt must not endorse replaced metadata merely because
            # the old logical fingerprint column still matches the input.
            snapshot = self._material_snapshot(db, body["material_id"])
            self._verify_material_binding(snapshot, text=body.get("text"))
            return True
        return False

    def _save_material(self, db, request, reference):
        body = request["body"]
        document = {key: value for key, value in body.items() if key != "text"}
        document.update(reference=reference, scope=request["scope"], recorded_at=int(time.time()))
        db.execute("INSERT INTO materials VALUES (?,?,?)", (body["material_id"], m.digest([request["scope"], request["operation"], body]),
                                                           m.canonical(document).decode()))

    def _publish(self, request, key, fingerprint):
        body = request["body"]
        logical = m.digest([request["scope"], request["operation"], body])
        with self.database.connection(write=True) as db:
            old = self._receipt(db, key, fingerprint)
            if old:
                return old, True
            original = self.database.pending(db, key)
            m.require(original is None or m.digest(original) == fingerprint, "Pending request ID binds different content", "CONFLICT")
            if self._material_identity(db, body, logical):
                return self._commit(db, request, key, fingerprint, dict(material_id=body["material_id"], reused=True)), False
            pending = db.execute("SELECT key,fingerprint FROM pending WHERE material_id=?", (body["material_id"],)).fetchone()
            if pending:
                m.require(pending == (key, fingerprint), "Material has an original pending request; resume that request", "PENDING")
            else:
                db.execute("INSERT INTO pending VALUES (?,?,?,?)", (key, body["material_id"], fingerprint, m.canonical(request).decode()))
        try:
            reference = self.ledger.publish(key, body)
            with self.database.connection(write=True) as db:
                old = self._receipt(db, key, fingerprint)
                if old:
                    return old, True
                pending = self.database.pending(db, key)
                m.require(pending is not None and m.digest(pending) == fingerprint, "Original pending material changed", "STORE_INVALID")
                self._save_material(db, request, reference)
                receipt = self._commit(db, request, key, fingerprint, dict(material_id=body["material_id"], reused=False))
                db.execute("DELETE FROM pending WHERE key=?", (key,))
                return receipt, False
        except m.KnowledgeError as exc:
            # An uncertain local final commit could already include the receipt.
            state = "unknown" if exc.commit_state == "unknown" else "pending"
            try:
                with self.database.connection(write=True) as db:
                    db.execute("INSERT INTO failures(key,document) VALUES (?,?)", (key, m.canonical(dict(
                        at=int(time.time()), code=exc.code, cause_state=exc.commit_state, details=exc.details)).decode()))
            except m.KnowledgeError:
                pass
            raise m.KnowledgeError(exc.code, "Material request retained; inspect or resume the original request", state,
                                   details=exc.details) from exc

    @writing
    def execute(self, request_utf8):
        request = m.request(request_utf8)
        key = m.operation_key(request["scope"], request["operation"], request["request_id"])
        fingerprint = m.digest(request)
        if request["operation"] == "publish_material":
            receipt, replayed = self._publish(request, key, fingerprint)
        else:
            with self.database.connection(write=True) as db:
                receipt = self._receipt(db, key, fingerprint)
                replayed = receipt is not None
                if receipt is None:
                    body = request["body"]
                    if request["operation"] == "register_material":
                        logical = m.digest([request["scope"], request["operation"], body])
                        reused = self._material_identity(db, body, logical)
                        if not reused:
                            m.require(not db.execute("SELECT 1 FROM pending WHERE material_id=?", (body["material_id"],)).fetchone(),
                                      "Material ID is reserved by a pending publication", "PENDING")
                            self.ledger.read(body["reference"], body["task_ref"])
                            self._save_material(db, request, body["reference"])
                        result = dict(material_id=body["material_id"], reused=reused)
                    elif request["operation"] == "propose":
                        result = self._propose(db, request)
                    else:
                        result = self._review(db, body)
                    receipt = self._commit(db, request, key, fingerprint, result)
        return self._committed_result(receipt, replayed)

    def _committed_result(self, receipt, replayed):
        # Observation/output failure cannot change a confirmed historical commit.
        try:
            return m.output(dict(status="COMMITTED", commit_state="committed", replayed=replayed, receipt=receipt,
                                 observation=self._observation(receipt)))
        except m.KnowledgeError as exc:
            raise m.KnowledgeError(exc.code, "Command committed; inspect its original receipt", "committed",
                                   details={key: receipt[key] for key in ("scope", "operation", "request_id")}) from exc

    def _propose(self, db, request):
        body, scope = request["body"], request["scope"]
        fingerprint = m.digest(body)
        row = db.execute("SELECT id,fingerprint FROM candidates WHERE scope=? AND proposal_id=?", (scope, body["proposal_id"])).fetchone()
        if row:
            self.database.document(db, "candidates", row[0])
            m.require(row[1] == fingerprint, "Proposal ID already binds different content", "CONFLICT")
            return dict(candidate_id=row[0], candidate_sha256=fingerprint, reused=True)
        for identity in body["material_ids"]:
            self._material(db, identity)
        identity = "candidate:" + str(uuid.uuid4())
        document = dict(body, candidate_id=identity, scope=scope, candidate_sha256=fingerprint, recorded_at=int(time.time()))
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?)", (identity, scope, body["proposal_id"], fingerprint,
                                                                 m.canonical(document).decode(), "PENDING"))
        return dict(candidate_id=identity, candidate_sha256=fingerprint, reused=False)

    def _review(self, db, body):
        candidate = self.database.document(db, "candidates", body["candidate_id"])
        m.require(candidate["candidate_sha256"] == body["candidate_sha256"], "Review target digest differs", "CONFLICT")
        fingerprint = m.digest(body)
        row = db.execute("SELECT fingerprint,id FROM reviews WHERE candidate_id=?", (body["candidate_id"],)).fetchone()
        if row:
            m.require(row[0] == fingerprint, "Candidate already has a different final decision", "CONFLICT")
            old = self.database.document(db, "reviews", row[1])
            return dict(review_id=old["review_id"], version_id=old["version_id"], reused=True)
        state = db.execute("SELECT state FROM candidates WHERE id=?", (body["candidate_id"],)).fetchone()[0]
        m.require(state == "PENDING", "Review requires a pending candidate", "CONFLICT")
        materials = sorted(set(candidate["material_ids"] + body["evidence_material_ids"]))
        m.require(len(materials) <= 8, "Combined review evidence exceeds eight materials", "LIMIT")
        for identity in materials:
            self._material(db, identity, verify=body["decision"] == "ACCEPT")
        review_id = "review:" + str(uuid.uuid4())
        version_id = "version:" + str(uuid.uuid4()) if body["decision"] == "ACCEPT" else None
        reviewed = dict(body, review_id=review_id, version_id=version_id, recorded_at=int(time.time()))
        db.execute("INSERT INTO reviews VALUES (?,?,?,?)", (review_id, body["candidate_id"], fingerprint, m.canonical(reviewed).decode()))
        if version_id:
            lineage = "knowledge:" + str(uuid.uuid4())
            version = dict(version_id=version_id, lineage_id=lineage, candidate_id=body["candidate_id"], review_id=review_id,
                           kind=candidate["kind"], content=candidate["content"], conditions=candidate["conditions"], material_ids=materials,
                           recorded_at=reviewed["recorded_at"])
            db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)", (version_id, lineage, body["candidate_id"], candidate["kind"],
                        m.canonical(candidate["conditions"]).decode(), "ACTIVE", m.canonical(version).decode()))
        db.execute("UPDATE candidates SET state=? WHERE id=?", ("ACCEPTED" if version_id else "REJECTED", body["candidate_id"]))
        return dict(review_id=review_id, version_id=version_id, reused=False)

    @read_only
    def get(self, kind, identity):
        m.identity(identity)
        tables = dict(material="materials", candidate="candidates", review="reviews", version="versions")
        m.require(type(kind) is str and kind in tables, "Unknown object kind")
        with self.database.connection() as db:
            document = self.database.document(db, tables[kind], identity)
            if kind in ("candidate", "version"):
                document["state"] = db.execute(f"SELECT state FROM {tables[kind]} WHERE id=?", (identity,)).fetchone()[0]
            ids = [identity] if kind == "material" else document.get("material_ids", document.get("evidence_material_ids", []))
            if kind == "review":
                candidate = self.database.document(db, "candidates", document["candidate_id"])
                ids = sorted(set(ids + candidate["material_ids"]))
                document["version_state"] = (db.execute("SELECT state FROM versions WHERE id=?", (document["version_id"],)).fetchone()[0]
                                             if document["version_id"] else None)
            materials = [self._material_snapshot(db, item) for item in ids]
        return m.output(dict(record=document, materials=self._availability(materials), observed_at=int(time.time())))

    def _availability(self, materials):
        observed = []
        for snapshot in materials:
            material = snapshot[0]
            result = dict(material_id=material["material_id"])
            try:
                self._read_material(snapshot)
                result.update(status="VERIFIED", reference=material["reference"])
            except m.KnowledgeError as exc:
                if exc.code == "STORE_INVALID":
                    raise
                result.update(status="UNAVAILABLE" if exc.code in ("IDENTITY_CONFLICT", "INTEGRITY_FAILURE") or
                              exc.details.get("ledger_code") in ("NOT_FOUND", "INTEGRITY_FAILURE") else "UNKNOWN",
                              code=exc.code, details=exc.details)
            observed.append(result)
        return observed

    def _observation(self, receipt):
        result = receipt["result"]
        kind = next(kind for kind in ("material", "candidate", "review") if kind + "_id" in result)
        try:
            return dict(self.get(kind, result[kind + "_id"]), record_status="AVAILABLE")
        except m.KnowledgeError as exc:
            # The receipt was verified separately. Do not report its absence
            # when only today's object/material observation cannot be read.
            return dict(record=None, materials=None, observed_at=int(time.time()),
                        record_status="UNAVAILABLE" if exc.code in ("NOT_FOUND", "STORE_INVALID", "UNSUPPORTED_VERSION") else "UNKNOWN",
                        error=dict(code=exc.code, message=exc.message, details=exc.details))

    @read_only
    def get_operation(self, scope, operation, request_id):
        key = m.operation_key(scope, operation, request_id)
        with self.database.connection() as db:
            receipt = self.database.receipt(db, key)
            m.require(receipt is not None, "Successful operation not found; inspect pending publication separately", "NOT_FOUND")
        return m.output(dict(receipt=receipt, observation=self._observation(receipt)))

    @read_only
    def pending(self, scope, request_id):
        key = m.operation_key(scope, "publish_material", request_id)
        with self.database.connection() as db:
            saved = self._publication_state(db, key)
            if saved["state"] == "COMMITTED":
                return saved
            failures = [json.loads(item[0]) for item in db.execute(
                "SELECT document FROM failures WHERE key=? ORDER BY sequence DESC LIMIT 10", (key,))]
            return m.output(dict(saved, recent_failures=failures))

    def _publication_state(self, db, key):
        request = self.database.pending(db, key)
        if request is not None:
            return dict(state="PENDING", request=request)
        receipt = self.database.receipt(db, key)
        m.require(receipt is not None, "Request not found", "NOT_FOUND")
        return dict(state="COMMITTED", receipt=receipt)

    @writing
    def resume(self, scope, request_id):
        key = m.operation_key(scope, "publish_material", request_id)
        with self.database.connection() as db:
            # Diagnostic report decoding/size does not authorize execution
            # and must not prevent recovery of a verified original intent.
            saved = self._publication_state(db, key)
        if saved["state"] == "COMMITTED":
            return self._committed_result(saved["receipt"], True)
        return self.execute(m.canonical(saved["request"]))

    @read_only
    def read_material(self, identity):
        m.identity(identity)
        with self.database.connection() as db:
            snapshot = self._material_snapshot(db, identity)
        material = snapshot[0]
        entries = self._read_material(snapshot)
        return m.output(dict(material_id=identity, reference=material["reference"], content_verified=True,
                             entries=[dict(entry_key=key, encoding="base64", data=base64.b64encode(raw).decode("ascii")) for key, raw in entries]))

    @read_only
    def query(self, kind, context, *, after="", limit=10):
        m.text(kind, maximum=64)
        m.context(context)
        if after:
            m.identity(after)
        m.require(type(after) is str and type(limit) is int and 1 <= limit <= 20, "Invalid page bounds")
        with self.database.connection() as db:
            rows = db.execute("""SELECT id FROM versions WHERE state='ACTIVE' AND kind=? AND id>?
                AND NOT EXISTS (SELECT 1 FROM json_each(versions.conditions) c
                  WHERE json_extract(?, '$.' || c.key) IS NOT c.value) ORDER BY id LIMIT ?""",
                              (kind, after, m.canonical(context).decode(), limit + 1)).fetchall()
            versions = []
            for (identity,) in rows[:limit]:
                version = self.database.document(db, "versions", identity)
                sources = [self._material(db, item) for item in version["material_ids"]]
                versions.append(dict(version, state="ACTIVE", content_sha256=hashlib.sha256(version["content"].encode()).hexdigest(),
                                     sources=sources))
        return m.output(dict(versions=versions, next_after=rows[limit - 1][0] if len(rows) > limit else None,
                             observed_at=int(time.time()), usage_recorded=False))
