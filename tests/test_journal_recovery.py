"""Real SQLite page spills and killed writers, confined to disposable stores."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.database import Database
from infra_artifact_ledger import open as open_ledger
from helpers import setup, request, material, proposal, encode


class JournalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = setup(self.root)
        self.path = self.root / "knowledge/knowledge.sqlite"
        self.journal = self.path.with_name(self.path.name + "-journal")
        self.first = self.store.execute(encode(request("publish_material", material())))
        self.original_bytes = self.path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def writer(self, value):
        payload, marker = self.root / "request.json", self.root / "paused"
        payload.write_bytes(encode(value))
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("crash_worker.py")),
                                  str(self.path.parent), str(payload), "during_local_write", str(marker)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists(), "Writer did not reach an uncommitted page spill")
            # A nonzero rollback-journal magic proves this is not merely the
            # zero-header journal from a write buffered only in process memory.
            self.assertEqual(self.journal.read_bytes()[:8], bytes.fromhex("d9d505f920a163d7"))
            self.assertNotEqual(self.path.read_bytes(), self.original_bytes)
            yield child
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def kill(self, child):
        child.kill()
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGKILL)

    def failure(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def proposed(self):
        return request("propose", dict(proposal(), content="x" * 65536))

    def test_live_writer_is_busy_then_killed_writer_rolls_back_without_replaying(self):
        with self.writer(self.proposed()) as child:
            attempted_modes = []
            connect = Database._connect_checked

            def observe(db, mode):
                attempted_modes.append(mode)
                return connect(db, mode)

            with patch.object(Database, "_connect_checked", observe):
                self.failure("BUSY", lambda: open_store(self.path.parent))
            self.assertEqual(attempted_modes, ["ro"])
            self.assertIsNone(child.poll())
            self.kill(child)
        # The already-open store must also recover, and its recovery handle
        # must reject domain writes once it is serving this read transaction.
        with self.store.database.connection() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO metadata VALUES ('unrequested-write','forbidden')")
        recovered = open_store(self.path.parent)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(recovered.get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
        self.failure("NOT_FOUND", lambda: recovered.get_operation("example:operator", "propose", "request:one"))
        with recovered.database.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM candidates").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertEqual(recovered.execute(encode(self.proposed()))["status"], "COMMITTED")

    def test_cli_recovery_preserves_pending_input_and_reuses_ledger_append(self):
        body = dict(material("material:pending"), text="x" * 65536, gaps=["g" * 512] * 16,
                    context={"key" + str(i): "v" * 256 for i in range(16)})
        payload = request("publish_material", body, "request:pending")
        with self.writer(payload) as child:
            self.kill(child)
        with open_ledger(self.root / "materials.sqlite") as ledger:
            before = ledger.verify()["counts"]
        result = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(self.path.parent),
                                 "pending", "--scope", "example:operator", "--request-id", "request:pending"],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["commit_state"], "not_applicable")
        self.assertEqual(response["data"]["request"], payload)
        recovered = open_store(self.path.parent)
        self.assertEqual(recovered.resume("example:operator", "request:pending")["status"], "COMMITTED")
        with open_ledger(self.root / "materials.sqlite") as ledger:
            self.assertEqual(ledger.verify()["counts"], before)
        self.assertTrue(recovered.read_material("material:pending")["content_verified"])
        self.assertEqual(recovered.get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])

    def test_recovery_permission_failure_retains_journal_and_can_retry(self):
        if os.geteuid() == 0:
            self.skipTest("Permission denial requires an ordinary user")
        with self.writer(self.proposed()) as child:
            self.kill(child)
        retained = self.path.read_bytes(), self.journal.read_bytes()
        self.path.chmod(0o400)
        try:
            error = self.failure("IO_ERROR", lambda: open_store(self.path.parent))
            self.assertEqual(error.commit_state, "not_applicable")
            self.assertEqual((self.path.read_bytes(), self.journal.read_bytes()), retained)
        finally:
            self.path.chmod(0o600)
        self.assertEqual(open_store(self.path.parent).get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_healthy_read_only_store_never_requests_write_connection(self):
        self.path.chmod(0o400)
        observed = []
        connect = Database._connect_checked

        def only_read(db, mode):
            observed.append(mode)
            self.assertEqual(mode, "ro")
            return connect(db, mode)

        with patch.object(Database, "_connect_checked", only_read):
            store = open_store(self.path.parent)
            self.assertEqual(store.get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
        self.assertTrue(observed)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_recovery_rejects_non_private_journal_without_changing_files(self):
        with self.writer(self.proposed()) as child:
            self.kill(child)
        retained = self.path.read_bytes(), self.journal.read_bytes()
        self.journal.chmod(0o644)
        for expected_state, call in (
                ("not_applicable", lambda: open_store(self.path.parent)),
                ("not_applicable", lambda: self.store.get_operation("example:operator", "publish_material", "request:one")),
                ("not_committed", lambda: self.store.execute(encode(self.proposed())))):
            error = self.failure("STORE_INVALID", call)
            self.assertEqual(error.commit_state, expected_state)
            self.assertEqual((self.path.read_bytes(), self.journal.read_bytes()), retained)
        self.journal.chmod(0o600)
        self.assertEqual(self.store.execute(encode(self.proposed()))["status"], "COMMITTED")
        self.assertEqual(open_store(self.path.parent).get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])

    def test_wrong_format_does_not_trigger_a_write_fallback(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=999")
        before = self.path.read_bytes()
        connect, modes = Database._connect_checked, []

        def observe(db, mode):
            modes.append(mode)
            return connect(db, mode)

        with patch.object(Database, "_connect_checked", observe):
            self.failure("STORE_INVALID", lambda: open_store(self.path.parent))
        self.assertEqual(modes, ["ro"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_journal_links_and_special_files_are_rejected_before_sqlite_access(self):
        target = self.root / "retained-journal"
        target.write_bytes(b"Retained synthetic bytes, not a SQLite journal")
        target.chmod(0o600)
        original = target.read_bytes()
        for kind in ("symlink", "hardlink", "fifo", "directory", "dangling_symlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.journal.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, self.journal)
                elif kind == "fifo":
                    os.mkfifo(self.journal, 0o600)
                elif kind == "directory":
                    self.journal.mkdir(mode=0o700)
                else:
                    self.journal.symlink_to(self.root / "absent")
                try:
                    with patch("sqlite3.connect", side_effect=AssertionError("Unsafe journal reached SQLite")):
                        self.failure("STORE_INVALID", lambda: open_store(self.path.parent))
                        self.failure("STORE_INVALID", lambda: self.store.execute(encode(self.proposed())))
                    self.assertEqual(self.path.read_bytes(), self.original_bytes)
                    self.assertEqual(target.read_bytes(), original)
                finally:
                    if kind == "directory":
                        self.journal.rmdir()
                    else:
                        self.journal.unlink()
        self.assertEqual(self.store.execute(encode(self.proposed()))["status"], "COMMITTED")

    def test_journal_removed_by_completed_writer_does_not_block_open(self):
        # Model a commit removing its zero-header journal just before lstat.
        self.journal.write_bytes(b"\x00" * 512)
        self.journal.chmod(0o600)
        original, removed = Path.lstat, []

        def after_commit(path, *args, **kwargs):
            if path == self.journal and not removed:
                self.journal.unlink()
                removed.append(True)
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", after_commit):
            self.assertEqual(open_store(self.path.parent).get_operation("example:operator", "publish_material", "request:one")["receipt"], self.first["receipt"])
        self.assertTrue(removed)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_open_store_errors_are_read_only_observations(self):
        self.path.parent.chmod(0o755)
        try:
            error = self.failure("STORE_INVALID", lambda: open_store(self.path.parent))
            self.assertEqual(error.commit_state, "not_applicable")
        finally:
            self.path.parent.chmod(0o700)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=999")
        before = self.path.read_bytes()
        error = self.failure("STORE_INVALID", lambda: open_store(self.path.parent))
        self.assertEqual(error.commit_state, "not_applicable")
        self.assertEqual(self.path.read_bytes(), before)

    def test_recovery_rechecks_directory_after_read_only_attempt(self):
        with self.writer(self.proposed()) as child:
            self.kill(child)
        retained = self.path.read_bytes(), self.journal.read_bytes()
        connect, modes = Database._connect_checked, []

        def changed_before_recovery(db, mode):
            modes.append(mode)
            if mode == "rw":
                self.path.parent.chmod(0o755)
            return connect(db, mode)

        try:
            with patch.object(Database, "_connect_checked", changed_before_recovery):
                error = self.failure("STORE_INVALID", lambda: open_store(self.path.parent))
            self.assertEqual(error.commit_state, "not_applicable")
            self.assertEqual(modes, ["ro", "rw"])
            self.assertEqual((self.path.read_bytes(), self.journal.read_bytes()), retained)
        finally:
            self.path.parent.chmod(0o700)
        self.assertEqual(open_store(self.path.parent).get_operation("example:operator", "publish_material", "request:one")["receipt"],
                         self.first["receipt"])
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
