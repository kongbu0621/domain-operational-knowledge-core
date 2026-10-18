"""Stored identity parsing is bounded before SQLite creates Python values."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.database import read_database_identity
from domain_operational_knowledge_core.model import MAX_REQUEST
from helpers import setup, request, material, encode


MEMORY_WORKER = r'''
from contextlib import closing
import hashlib, json, resource, sqlite3, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from helpers import request, material, encode
from domain_operational_knowledge_core import KnowledgeError, open_store
root = Path(sys.argv[1])
store = open_store(root / "knowledge")
payload = encode(request("publish_material", material("material:next"), "request:next"))
with closing(sqlite3.connect(root / "knowledge/knowledge.sqlite")) as db, db:
    db.execute("UPDATE metadata SET value=cast(zeroblob(67108864) as text) WHERE key='identity'")

def hashes():
    result = {}
    for name in ("knowledge/knowledge.sqlite", "knowledge/identity.json", "materials.sqlite"):
        with (root / name).open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result

resource.setrlimit(resource.RLIMIT_AS, (96 * 1024 * 1024, 96 * 1024 * 1024))
before, results = hashes(), {}
for label, call in (
    ("open", lambda: open_store(root / "knowledge")),
    ("read", lambda: store.get_operation("example:operator", "publish_material", "request:one")),
    ("write", lambda: store.execute(payload))):
    try:
        call()
        results[label] = "UNEXPECTED SUCCESS"
    except KnowledgeError as exc:
        results[label] = exc.as_dict()
results["unchanged"] = hashes() == before
print(json.dumps(results))
'''


class DatabaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = setup(self.root)
        self.first = self.store.execute(encode(request("publish_material", material())))
        self.path = self.root / "knowledge/knowledge.sqlite"
        with closing(sqlite3.connect(self.path)) as db:
            self.original = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0]
        self.payload = encode(request("publish_material", material("material:next"), "request:next"))

    def tearDown(self):
        self.temporary.cleanup()

    def replace_identity(self, value):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM metadata WHERE key='identity'")
            if value is not None:
                db.execute("INSERT INTO metadata VALUES ('identity',?)", (value,))

    def hashes(self):
        result = {}
        for name in ("knowledge/knowledge.sqlite", "knowledge/identity.json", "materials.sqlite"):
            with (self.root / name).open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        return result

    def assert_refused(self):
        before = self.hashes()
        for state, call in (
            ("not_applicable", lambda: open_store(self.path.parent)),
            ("not_applicable", lambda: self.store.query("build-guidance", {})),
            ("not_committed", lambda: self.store.execute(self.payload))):
            with self.assertRaises(KnowledgeError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.commit_state), ("STORE_INVALID", state))
        self.assertEqual(self.hashes(), before)

    def assert_original_receipt(self):
        self.assertEqual(self.store.get_operation("example:operator", "publish_material", "request:one")["receipt"],
                         self.first["receipt"])

    def test_strict_database_identity_rejects_damage_without_writes_and_can_continue(self):
        cases = {
            "duplicate": self.original.replace('"schema":', '"schema":"different","schema":', 1),
            "deep": "[" * 10000 + "0" + "]" * 10000,
            "malformed": "{",
            "bom": "\ufeff" + self.original,
            "surrogate": '{"schema":"\\ud800"}',
            "blob": self.original.encode(),
            "missing": None,
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.replace_identity(value)
                self.assert_refused()
                self.replace_identity(self.original)
                self.assert_original_receipt()
        first = self.store.execute(self.payload)
        before = self.hashes()
        self.assertEqual(self.store.execute(self.payload)["receipt"], first["receipt"])
        self.assertEqual(self.hashes(), before)

    def test_database_identity_size_boundary_and_sqlite_limit_restoration(self):
        boundary = self.original + " " * (MAX_REQUEST - len(self.original.encode()))
        self.replace_identity(boundary)
        reopened = open_store(self.path.parent)
        self.assert_original_receipt()
        self.assertEqual(reopened.query("build-guidance", {})["versions"], [])
        with closing(sqlite3.connect(self.path)) as db:
            limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
            self.assertEqual(read_database_identity(db), self.store.database.identity)
            self.assertEqual(db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), limit)
            # Native failures also restore the caller's prior (smaller) limit.
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
            with self.assertRaises(sqlite3.DataError):
                read_database_identity(db)
            self.assertEqual(db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 4096)
        self.replace_identity(boundary + " ")
        self.assert_refused()
        self.replace_identity(boundary)
        self.assert_original_receipt()

    def test_oversized_sqlite_identity_is_controlled_under_memory_limit(self):
        child = subprocess.run([sys.executable, "-c", MEMORY_WORKER, str(self.root),
                                str(Path(__file__).parent)], capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        result = json.loads(child.stdout)
        self.assertTrue(result["unchanged"])
        for label in ("open", "read", "write"):
            self.assertIsInstance(result[label], dict, result)
            self.assertEqual(result[label]["code"], "STORE_INVALID")
            self.assertEqual(result[label]["commit_state"], "not_committed" if label == "write" else "not_applicable")
        self.replace_identity(self.original)
        self.assert_original_receipt()
        self.assertEqual(self.store.execute(self.payload)["commit_state"], "committed")

    def test_cli_reports_nested_database_identity_as_structured_store_error(self):
        self.replace_identity("[" * 10000 + "0" + "]" * 10000)
        before = self.hashes()
        child = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core",
                                "--store", str(self.path.parent), "get", "--kind", "material",
                                "--id", "material:build"], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual((result["code"], result["commit_state"]), ("STORE_INVALID", "not_applicable"))
        self.assertEqual(self.hashes(), before)
