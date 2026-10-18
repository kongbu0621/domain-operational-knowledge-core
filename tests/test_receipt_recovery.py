"""Historical facts remain separate from fresh observations and diagnostics."""
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError, initialize, open_store
from domain_operational_knowledge_core import cli
from domain_operational_knowledge_core import database
from domain_operational_knowledge_core.ledger import MaterialLedger
from domain_operational_knowledge_core.model import MAX_RESPONSE, canonical
from infra_artifact_ledger import open as open_ledger
from helpers import setup, request, material, candidate, review, encode


class ReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = setup(self.root)
        self.path = self.root / "knowledge/knowledge.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, identity="material:build", key="request:one"):
        return self.store.execute(encode(request("publish_material", material(identity=identity), key)))

    def failure(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_missing_current_object_does_not_hide_confirmed_receipt(self):
        first = self.publish()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM materials")
        before = self.path.read_bytes()
        current = self.failure("NOT_FOUND", lambda: self.store.get("material", "material:build"))
        self.assertEqual(current.commit_state, "not_applicable")
        for result in (self.store.get_operation("example:operator", "publish_material", "request:one"),
                       self.store.resume("example:operator", "request:one"), self.publish()):
            self.assertEqual(result["receipt"], first["receipt"])
            self.assertEqual(result["observation"]["record_status"], "UNAVAILABLE")
            self.assertEqual(result["observation"]["error"]["code"], "NOT_FOUND")
            self.assertIsNone(result["observation"]["record"])
            self.assertIsNone(result["observation"]["materials"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_temporary_observation_error_preserves_old_review_but_not_new_admission(self):
        self.publish()
        proposed = candidate(self.store)
        original = self.store.execute(encode(request("review", review(proposed))))
        with patch.object(self.store, "get", side_effect=KnowledgeError("BUSY", "Synthetic read failure", "not_applicable")):
            observed = self.store.get_operation("example:operator", "review", "request:one")
            replay = self.store.execute(encode(request("review", review(proposed))))
        for result in (observed, replay):
            self.assertEqual(result["receipt"], original["receipt"])
            self.assertEqual(result["observation"]["record_status"], "UNKNOWN")
            self.assertEqual(result["observation"]["error"]["code"], "BUSY")
        second = candidate(self.store, "proposal:second")
        with patch.object(MaterialLedger, "read", side_effect=KnowledgeError("BUSY", "Synthetic material read failure", "not_applicable")):
            error = self.failure("BUSY", lambda: self.store.execute(encode(request("review", review(second), "request:second"))))
        self.assertEqual(error.commit_state, "not_committed")
        self.assertEqual(self.store.get("candidate", second["candidate_id"])["record"]["state"], "PENDING")

    def test_corrupt_current_candidate_is_unavailable_but_original_acceptance_remains(self):
        self.publish()
        proposed = candidate(self.store)
        original = self.store.execute(encode(request("review", review(proposed))))
        with sqlite3.connect(self.path) as db:
            value = json.loads(db.execute("SELECT document FROM candidates").fetchone()[0])
            value["content"] = "Different from the reviewed content"
            db.execute("UPDATE candidates SET document=?", (encode(value).decode(),))
        result = self.store.get_operation("example:operator", "review", "request:one")
        self.assertEqual(result["receipt"], original["receipt"])
        self.assertEqual(result["observation"]["record_status"], "UNAVAILABLE")
        self.assertEqual(result["observation"]["error"]["code"], "STORE_INVALID")
        self.failure("STORE_INVALID", lambda: self.store.query("build-guidance", {"platform": "linux"}))

    def test_replaced_receipt_result_or_missing_event_cannot_be_reported_as_original(self):
        original = self.publish()
        self.publish("material:other", "request:other")
        with sqlite3.connect(self.path) as db:
            key, raw = db.execute("SELECT key,document FROM receipts WHERE json_extract(document,'$.request_id')='request:one'").fetchone()
        for field, replacement in (("material_id", "material:other"), ("reused", True)):
            changed = json.loads(raw)
            changed["result"][field] = replacement
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE receipts SET document=? WHERE key=?", (canonical(changed).decode(), key))
            before = self.path.read_bytes()
            for call in (lambda: self.store.get_operation("example:operator", "publish_material", "request:one"),
                         lambda: self.store.resume("example:operator", "request:one"), self.publish):
                self.failure("STORE_INVALID", call)
            self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE receipts SET document=? WHERE key=?", (raw, key))
        self.assertEqual(self.publish()["receipt"], original["receipt"])
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM events WHERE receipt=?", (raw,))
        self.failure("STORE_INVALID", lambda: self.store.get_operation("example:operator", "publish_material", "request:one"))

    def test_diagnostic_decode_failure_cannot_permanently_block_original_resume(self):
        with patch.object(MaterialLedger, "publish", side_effect=KnowledgeError("BUSY", "Synthetic pending publication")):
            self.failure("BUSY", self.publish)
        with sqlite3.connect(self.path) as db:
            original_request = db.execute("SELECT request FROM pending").fetchone()[0]
            db.execute("UPDATE failures SET document='{broken'")
        self.failure("STORE_INVALID", lambda: self.store.pending("example:operator", "request:one"))
        reopened = open_store(self.root / "knowledge")
        result = reopened.resume("example:operator", "request:one")
        self.assertEqual(result["receipt"]["request_id"], json.loads(original_request)["request_id"])
        self.assertTrue(reopened.read_material("material:build")["content_verified"])
        with open_ledger(self.root / "materials.sqlite") as ledger:
            counts = ledger.verify()["counts"]
            self.assertEqual((counts["artifacts"], counts["versions"]), (2, 1))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT document FROM failures").fetchone()[0], "{broken")

    def test_diagnostic_response_limit_cannot_block_original_resume(self):
        with patch.object(MaterialLedger, "publish", side_effect=KnowledgeError("BUSY", "Synthetic pending publication")):
            self.failure("BUSY", self.publish)
        large = json.dumps(dict(at=1, code="BUSY", cause_state="not_applicable", details=dict(log="x" * MAX_RESPONSE)))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE failures SET document=?", (large,))
        self.failure("LIMIT", lambda: self.store.pending("example:operator", "request:one"))
        self.assertEqual(self.store.resume("example:operator", "request:one")["commit_state"], "committed")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT document FROM failures").fetchone()[0], large)

    def test_cli_initialization_and_read_response_limit_have_correct_commit_states(self):
        target = self.root / "cli-knowledge"
        result = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(target),
                                 "init", "--ledger-db", str(self.root / "materials.sqlite"),
                                 "--anchor-artifact", "artifact:example-anchor"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["commit_state"], "committed")
        self.assertEqual(open_store(target).database.identity["store_id"], json.loads(result.stdout)["data"]["store_id"])
        # A valid library-sized result can exceed the limit after CLI wrapping.
        context = self.root / "context.json"
        context.write_text("{}")
        response = dict(value="x" * (MAX_RESPONSE - 30))
        self.assertLess(len(canonical(response)) + 1, MAX_RESPONSE)
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch.object(cli, "open_store", return_value=self.store), patch.object(self.store, "query", return_value=response), patch.object(sys, "stdout", output):
            status = cli.main(["--store", str(self.path.parent), "query", "--kind", "build-guidance", "--context", str(context)])
        self.assertEqual(status, 2)
        error = json.loads(output.buffer.getvalue())
        self.assertEqual((error["code"], error["commit_state"]), ("LIMIT", "not_applicable"))

    def test_initialization_failures_distinguish_before_during_and_after_commit(self):
        before = self.root / "before-init"
        error = self.failure("MATERIAL_UNAVAILABLE", lambda: initialize(before,
            ledger_path=self.root / "materials.sqlite", anchor_artifact_id="artifact:missing"))
        self.assertEqual(error.commit_state, "not_committed")
        self.assertFalse(before.exists())
        partial = self.root / "partial-init"
        durable = database.durable_file

        def interrupt_marker(path, raw):
            if path == partial / "identity.json":
                raise OSError("Synthetic marker write failure")
            return durable(path, raw)

        with patch.object(database, "durable_file", interrupt_marker):
            error = self.failure("IO_ERROR", lambda: initialize(partial,
                ledger_path=self.root / "materials.sqlite", anchor_artifact_id="artifact:example-anchor"))
        self.assertEqual(error.commit_state, "unknown")
        self.assertTrue((partial / "knowledge.sqlite").exists())
        self.failure("STORE_INVALID", lambda: open_store(partial))
        complete = self.root / "complete-init"
        with patch.object(database, "Database", side_effect=KnowledgeError("BUSY", "Synthetic post-commit open failure", "not_applicable")):
            error = self.failure("BUSY", lambda: initialize(complete,
                ledger_path=self.root / "materials.sqlite", anchor_artifact_id="artifact:example-anchor"))
        self.assertEqual(error.commit_state, "committed")
        self.assertEqual(open_store(complete).database.identity["store_id"], error.details["store_id"])
