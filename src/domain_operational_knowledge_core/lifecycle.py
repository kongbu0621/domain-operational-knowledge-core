"""Transactional adoption, feedback, revision preconditions and retirement.

The caller observes external work. These records do not execute or authorize it.
"""
import time
import uuid

from . import model as m


class Lifecycle:
    def _active_version(self, db, identity):
        version = self.database.document(db, "versions", identity)
        m.require(db.execute("SELECT state FROM versions WHERE id=?", (identity,)).fetchone() == ("ACTIVE",),
                  "Expected knowledge version is no longer active", "CONFLICT")
        return version

    def _start_use(self, db, request):
        body = request["body"]
        fingerprint = m.digest([request["scope"], body])
        row = db.execute("SELECT fingerprint FROM usages WHERE id=?", (body["usage_id"],)).fetchone()
        if row:
            self.database.document(db, "usages", body["usage_id"])
            m.require(row[0] == fingerprint, "Usage ID binds another adoption", "CONFLICT")
            return dict(usage_id=body["usage_id"], reused=True)
        version = self._active_version(db, body["version_id"])
        m.require(all(body["context"].get(k) == v for k, v in version["conditions"].items()),
                  "Knowledge conditions do not match this use", "CONFLICT")
        for identity in version["material_ids"]:
            self._material(db, identity)
        value = dict(body, scope=request["scope"], state_at_start="ACTIVE", recorded_at=int(time.time()))
        db.execute("INSERT INTO usages VALUES (?,?,?,?)", (body["usage_id"], body["version_id"], fingerprint,
                                                          m.canonical(value).decode()))
        return dict(usage_id=body["usage_id"], reused=False)

    def _feedback_head(self, db, usage_id):
        rows = db.execute("""SELECT f.id FROM feedback f WHERE f.usage_id=? AND
            NOT EXISTS (SELECT 1 FROM feedback next WHERE next.predecessor_id=f.id) LIMIT 2""", (usage_id,)).fetchall()
        m.require(len(rows) <= 1, "Feedback history has multiple heads", "STORE_INVALID")
        if rows:
            self.database.document(db, "feedback", rows[0][0])
            return rows[0][0]
        return None

    @staticmethod
    def _feedback_slot(request):
        body = request["body"]
        return m.digest([body["usage_id"], body.get("expected_feedback_id")])

    def _feedback_existing(self, db, request):
        body = request["body"]
        row = db.execute("SELECT id,fingerprint FROM feedback WHERE usage_id=? AND predecessor_id IS ?",
                         (body["usage_id"], body.get("expected_feedback_id"))).fetchone()
        if row is None:
            return None
        old = self.database.document(db, "feedback", row[0])
        m.require(row[1] == m.digest([request["scope"], request["operation"], body]),
                  "This completion or correction already binds different content", "CONFLICT")
        snapshot = self._material_snapshot(db, old["material_id"])
        self._verify_material_binding(snapshot, text=body["material"]["text"])
        return dict(feedback_id=old["feedback_id"], material_id=old["material_id"], reused=True)

    def _feedback_precondition(self, db, request):
        body = request["body"]
        usage = self.database.document(db, "usages", body["usage_id"])
        material = body["material"]
        m.require(material["source_ref"] == usage["consumer_ref"] and material["task_ref"] == usage["task_ref"]
                  and material["context"] == usage["context"], "Feedback must describe the original consumer, task and context", "CONFLICT")
        head = self._feedback_head(db, body["usage_id"])
        if request["operation"] == "complete_use":
            m.require(head is None, "Usage already has completion feedback", "CONFLICT")
        else:
            previous = self.database.document(db, "feedback", body["expected_feedback_id"])
            m.require(previous["usage_id"] == body["usage_id"] and head == body["expected_feedback_id"],
                      "Correction requires the current feedback of this use", "CONFLICT")
        # Do not require the old version still ACTIVE or its old bytes readable:
        # the original usage is historical; the newly reported evidence is saved.
        return usage

    def _save_feedback(self, db, request):
        body = request["body"]
        usage = self.database.document(db, "usages", body["usage_id"])
        identity = "feedback:" + str(uuid.uuid4())
        value = dict(feedback_id=identity, usage_id=body["usage_id"], version_id=usage["version_id"],
                     predecessor_id=body.get("expected_feedback_id"), material_id=body["material"]["material_id"],
                     scope=request["scope"], recorded_at=int(time.time()))
        db.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?)", (identity, value["usage_id"], value["predecessor_id"],
                    value["material_id"], m.digest([request["scope"], request["operation"], body]),
                    m.digest(value), m.canonical(value).decode()))
        return dict(feedback_id=identity, material_id=value["material_id"], reused=False)

    def _retire(self, db, body):
        row = db.execute("SELECT id,fingerprint FROM retirements WHERE version_id=?", (body["expected_version_id"],)).fetchone()
        fingerprint = m.digest(body)
        if row:
            self.database.document(db, "retirements", row[0])
            m.require(row[1] == fingerprint, "Version has a different retirement decision", "CONFLICT")
            return dict(retirement_id=row[0], reused=True)
        version = self._active_version(db, body["expected_version_id"])
        for identity in body["evidence_material_ids"]:
            self._material(db, identity)
        identity = "retirement:" + str(uuid.uuid4())
        value = dict(body, retirement_id=identity, version_id=version["version_id"], recorded_at=int(time.time()))
        db.execute("INSERT INTO retirements VALUES (?,?,?,?)", (identity, version["version_id"], fingerprint,
                                                               m.canonical(value).decode()))
        db.execute("UPDATE versions SET state='RETIRED' WHERE id=? AND state='ACTIVE'", (version["version_id"],))
        return dict(retirement_id=identity, reused=False)
