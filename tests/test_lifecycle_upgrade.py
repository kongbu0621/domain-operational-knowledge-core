"""Explicit format-1 -> format-2 upgrade, with rollback and retained intents."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.database import install_lifecycle
from helpers import setup, material, candidate, review, request, encode
from test_lifecycle import adoption, completion


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # The base SQL is exactly the format-1 DDL; initialization adds format-2
        # only through this function. This fixture creates a genuine format-1 DB.
        with patch("domain_operational_knowledge_core.database.install_lifecycle", lambda db: db.execute("PRAGMA user_version=1")):
            self.store = setup(self.root)
        self.path = self.root / "knowledge/knowledge.sqlite"
        self.first = self.store.execute(encode(request("publish_material", material())))
        c = candidate(self.store)
        self.version = self.store.execute(encode(request("review", review(c))))["receipt"]["result"]["version_id"]

    def tearDown(self):
        self.temp.cleanup()

    def legacy_rows(self):
        with sqlite3.connect(self.path) as db:
            return {table: db.execute("SELECT * FROM " + table).fetchall() for table in
                    ("metadata", "materials", "candidates", "reviews", "versions", "receipts", "pending", "events", "failures")}

    def test_reads_do_not_upgrade_and_explicit_upgrade_preserves_all_rows_and_identity(self):
        original = self.legacy_rows()
        identity = (self.root / "knowledge/identity.json").read_bytes()
        db_bytes = self.path.read_bytes()
        store = open_store(self.path.parent)
        self.assertEqual(store.get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
        self.assertTrue(store.read_material("material:build")["content_verified"])
        with self.assertRaises(KnowledgeError) as exc:
            store.execute(encode(request("start_use", adoption(self.version))))
        self.assertEqual((exc.exception.code, exc.exception.commit_state), ("UNSUPPORTED_VERSION", "not_committed"))
        self.assertEqual(db_bytes, self.path.read_bytes())
        result = store.upgrade()
        self.assertTrue(result["upgraded"])
        self.assertEqual(self.legacy_rows(), original)
        self.assertEqual((self.root / "knowledge/identity.json").read_bytes(), identity)
        self.assertFalse(store.upgrade()["upgraded"])
        store.execute(encode(request("start_use", adoption(self.version))))
        store.execute(encode(request("complete_use", completion())))
        self.assertEqual(store.get("usage", "usage:build")["record"]["version_id"], self.version)

    def test_interrupted_upgrade_rolls_back_and_can_retry(self):
        original = self.legacy_rows()
        def fail(db):
            install_lifecycle(db)
            raise sqlite3.OperationalError("Synthetic upgrade failure before commit")
        with patch("domain_operational_knowledge_core.service.install_lifecycle", fail):
            with self.assertRaises(KnowledgeError):
                self.store.upgrade()
        self.assertEqual(self.legacy_rows(), original)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='usages'").fetchone())
        self.assertTrue(open_store(self.path.parent).upgrade()["upgraded"])

    def test_material_pending_before_upgrade_resumes_with_identical_ledger_objects(self):
        original = self.store.ledger.publish
        def fail(key, body):
            original(key, body)
            raise KnowledgeError("IO_ERROR", "Synthetic lost result", "unknown")
        with patch.object(self.store.ledger, "publish", side_effect=fail):
            with self.assertRaises(KnowledgeError):
                self.store.execute(encode(request("publish_material", material("material:pending"), "request:pending")))
        before = self.legacy_rows()
        self.store.upgrade()
        self.assertEqual(before, self.legacy_rows())
        resumed = self.store.resume("example:operator", "request:pending")
        self.assertEqual(resumed["receipt"]["result"]["material_id"], "material:pending")
        self.assertTrue(self.store.read_material("material:pending")["content_verified"])

    def test_process_kill_before_and_after_upgrade_commit_preserves_original_rows(self):
        import signal
        import subprocess
        import sys
        import time
        original = self.legacy_rows()
        for checkpoint in ("before_commit", "after_commit"):
            marker = self.root / (checkpoint + ".ready")
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("upgrade_crash_worker.py")),
                str(self.path.parent), checkpoint, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                child.kill()
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, -signal.SIGKILL, out + err)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
            store = open_store(self.path.parent)
            self.assertEqual(store.get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
            self.assertEqual(self.legacy_rows(), original)
            with sqlite3.connect(self.path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1 if checkpoint == "before_commit" else 2)
        self.assertFalse(store.upgrade()["upgraded"])
