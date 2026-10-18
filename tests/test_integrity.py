"""Corrupt only disposable fixtures; observe public refusal and no new writes."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.ledger import MaterialLedger
from domain_operational_knowledge_core.model import digest, MAX_REQUEST
from infra_artifact_ledger import open as open_ledger
from helpers import setup, request, material, candidate, review, proposal, encode


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = setup(self.root)
        self.path = self.root / "knowledge/knowledge.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def failure(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def publish(self):
        return self.store.execute(encode(request("publish_material", material())))

    def ledger_counts(self):
        with open_ledger(self.root / "materials.sqlite") as ledger:
            return ledger.verify()["counts"]

    def counts(self):
        with sqlite3.connect(self.path) as db:
            return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("materials", "candidates", "reviews", "versions", "receipts", "pending", "events"))

    def test_task_control_characters_fail_before_intent_or_ledger_writes(self):
        before, ledger_before = self.counts(), self.ledger_counts()
        for control in ("\n", "\t", "\r", "\x01", "\x7f"):
            with self.subTest(control=repr(control)):
                payload = request("publish_material", dict(material(), task_ref="task:bad" + control + "ref"))
                error = self.failure("INVALID_INPUT", lambda: self.store.execute(encode(payload)))
                self.assertEqual(error.commit_state, "not_committed")
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.ledger_counts(), ledger_before)
        payload = request("publish_material", dict(material(), task_ref="任务:合成构建"))
        self.assertEqual(self.store.execute(encode(payload))["status"], "COMMITTED")

    def test_changed_pending_request_cannot_drive_another_publication(self):
        with patch.object(MaterialLedger, "publish", side_effect=KnowledgeError("BUSY", "Synthetic pause")):
            self.failure("BUSY", self.publish)
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT fingerprint,request FROM pending").fetchone()
        before, ledger_before = self.counts(), self.ledger_counts()
        for field in ("text", "request_id", "scope", "material_id", "missing_field"):
            with self.subTest(field=field):
                changed = json.loads(original[1])
                if field == "text":
                    changed["body"]["text"] = "changed payload"
                elif field in ("request_id", "scope"):
                    changed[field] = "different:identity"
                elif field == "missing_field":
                    del changed["body"]["task_ref"]
                else:
                    changed["body"][field] = "material:another"
                # Even a self-consistent digest cannot authorize a different
                # request key or a material ID different from the reservation.
                fingerprint = original[0] if field == "text" else digest(changed)
                damaged = encode(changed).decode()
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE pending SET fingerprint=?,request=?", (fingerprint, damaged))
                reopened = open_store(self.root / "knowledge")
                self.failure("STORE_INVALID", lambda: reopened.pending("example:operator", "request:one"))
                self.failure("STORE_INVALID", lambda: reopened.resume("example:operator", "request:one"))
                self.failure("STORE_INVALID", lambda: reopened.execute(encode(request("publish_material", material()))))
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.ledger_counts(), ledger_before)
                with sqlite3.connect(self.path) as db:
                    self.assertEqual(db.execute("SELECT request FROM pending").fetchone()[0], damaged)
        # A fixture restoration proves the error did not create a permanent lock.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE pending SET fingerprint=?,request=?", original)
        self.assertEqual(self.store.resume("example:operator", "request:one")["status"], "COMMITTED")

    def test_candidate_replacement_cannot_be_accepted_with_old_digest(self):
        self.publish()
        proposed = candidate(self.store)
        before = self.counts()
        with sqlite3.connect(self.path) as db:
            original = json.loads(db.execute("SELECT document FROM candidates").fetchone()[0])
        for field, replacement in (("content", "Unreviewed replacement"), ("conditions", {}),
                                   ("scope", "another:scope"), ("proposal_id", "proposal:other")):
            with self.subTest(field=field):
                value = dict(original, **{field: replacement})
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE candidates SET document=?", (encode(value).decode(),))
                self.failure("STORE_INVALID", lambda: self.store.get("candidate", proposed["candidate_id"]))
                self.failure("STORE_INVALID", lambda: self.store.execute(encode(request("review", review(proposed)))))
                self.failure("STORE_INVALID", lambda: self.store.execute(encode(request("propose", proposal(), "request:again"))))
                self.assertEqual(self.counts(), before)

    def accept(self):
        self.publish()
        proposed = candidate(self.store)
        return self.store.execute(encode(request("review", review(proposed))))["receipt"]["result"]

    def test_knowledge_read_checks_content_review_and_selected_index(self):
        accepted = self.accept()
        before = self.counts()
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT document FROM versions").fetchone()[0]
        for field, replacement in (("content", "Unreviewed replacement"), ("version_id", "version:other"),
                                   ("review_id", "review:missing"), ("conditions", {}), ("material_ids", [])):
            with self.subTest(field=field):
                value = dict(json.loads(original), **{field: replacement})
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE versions SET document=?", (encode(value).decode(),))
                self.failure("STORE_INVALID", lambda: self.store.get("version", accepted["version_id"]))
                self.failure("STORE_INVALID", lambda: self.store.query("build-guidance", {"platform": "linux"}))
                self.assertEqual(self.counts(), before)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE versions SET document=?,conditions='{}'", (original,))
        self.failure("STORE_INVALID", lambda: self.store.query("build-guidance", {"platform": "linux"}))

    def test_review_damage_does_not_become_current_knowledge(self):
        accepted = self.accept()
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT document FROM reviews").fetchone()[0]
            value = json.loads(original)
            value["reason"] = "Changed after acceptance"
            db.execute("UPDATE reviews SET document=?", (encode(value).decode(),))
        self.failure("STORE_INVALID", lambda: self.store.get("review", accepted["review_id"]))
        self.failure("STORE_INVALID", lambda: self.store.query("build-guidance", {"platform": "linux"}))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE reviews SET document=?", (original,))
            db.execute("DELETE FROM versions")
        self.failure("STORE_INVALID", lambda: self.store.get("review", accepted["review_id"]))

    def test_request_boundary_does_not_make_its_persisted_candidate_unreadable(self):
        self.publish()
        body = dict(proposal(), content="")
        empty = request("propose", body, "req")
        body["content"] = "\x01" * ((MAX_REQUEST - len(encode(empty))) // 6)
        payload = encode(empty)
        self.assertLessEqual(len(payload), MAX_REQUEST)
        self.assertGreater(len(payload), MAX_REQUEST - 6)
        proposed = self.store.execute(payload)["receipt"]["result"]
        accepted = self.store.execute(encode(request("review", review(proposed))))["receipt"]["result"]
        self.assertEqual(self.store.get("version", accepted["version_id"])["record"]["content"], body["content"])

    def test_identity_read_permission_error_is_classified_and_recoverable(self):
        identity = self.root / "knowledge/identity.json"
        from unittest.mock import patch
        original = Path.read_bytes

        def blocked(path):
            if path == identity:
                raise PermissionError("Synthetic identity read denial")
            return original(path)

        with patch.object(Path, "read_bytes", blocked):
            error = self.failure("IO_ERROR", lambda: open_store(self.root / "knowledge"))
            self.assertEqual(error.commit_state, "not_applicable")
        self.assertEqual(open_store(self.root / "knowledge").query("build-guidance", {})["versions"], [])

    def test_receipt_body_must_match_lookup_key_store_and_fingerprint(self):
        self.publish()
        before, ledger_before = self.counts(), self.ledger_counts()
        with sqlite3.connect(self.path) as db:
            original = json.loads(db.execute("SELECT document FROM receipts").fetchone()[0])
        for field, replacement in (("request_id", "request:another"), ("scope", "another:scope"),
                                   ("store_id", "another-store"), ("fingerprint", "0" * 64)):
            with self.subTest(field=field):
                changed = dict(original, **{field: replacement})
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE receipts SET document=?", (encode(changed).decode(),))
                self.failure("STORE_INVALID", lambda: self.store.get_operation("example:operator", "publish_material", "request:one"))
                self.failure("STORE_INVALID", lambda: self.store.pending("example:operator", "request:one"))
                self.failure("STORE_INVALID", lambda: self.store.resume("example:operator", "request:one"))
                self.failure("STORE_INVALID", self.publish)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.ledger_counts(), ledger_before)


if __name__ == "__main__":
    unittest.main()
