"""Initialization validates representable identity before creating a store."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from domain_operational_knowledge_core import KnowledgeError, initialize, open_store
from helpers import setup, request, material, encode


class InitializationInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        setup(self.root)
        self.ledger = self.root / "materials.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, target, ledger=None):
        return initialize(target, ledger_path=self.ledger if ledger is None else ledger,
                          anchor_artifact_id="artifact:example-anchor")

    def hash_file(self, path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def assert_invalid(self, target, ledger=None):
        with self.assertRaises(KnowledgeError) as caught:
            self.create(target, ledger)
        self.assertEqual((caught.exception.code, caught.exception.commit_state), ("INVALID_INPUT", "not_committed"))
        self.assertFalse(target.exists())

    def test_non_utf8_ledger_path_fails_before_creation_in_library_and_cli(self):
        # Linux permits these filename bytes; the persisted Ledger binding
        # is UTF-8 JSON and cannot represent them losslessly as a string.
        renamed = self.root / os.fsdecode(b"materials-\xff.sqlite")
        before = self.hash_file(self.ledger)
        self.ledger.rename(renamed)
        target = self.root / "new-knowledge"
        self.assert_invalid(target, renamed)
        child = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core",
                                "--store", str(target), "init", "--ledger-db", str(renamed),
                                "--anchor-artifact", "artifact:example-anchor"],
                               capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        response = json.loads(child.stdout)
        self.assertEqual((response["code"], response["commit_state"]), ("INVALID_INPUT", "not_committed"))
        self.assertFalse(target.exists())
        self.assertEqual(self.hash_file(renamed), before)
        # Correcting the input leaves the original target available for init.
        renamed.rename(self.ledger)
        created = self.create(target)
        self.assertEqual(open_store(target).database.identity, created.database.identity)
        self.assertEqual(self.hash_file(self.ledger), before)

    def test_nul_in_either_path_is_a_structured_precreation_error(self):
        before = self.hash_file(self.ledger)
        self.assert_invalid(self.root / "new\x00knowledge")
        target = self.root / "new-knowledge"
        self.assert_invalid(target, self.root / "materials\x00.sqlite")
        self.assertEqual(self.hash_file(self.ledger), before)
        self.assertEqual(self.create(target).query("build-guidance", {})["versions"], [])

    def test_unicode_ledger_and_native_store_paths_remain_usable(self):
        renamed = self.root / "材料库.sqlite"
        self.ledger.rename(renamed)
        for target_name in ("知识库", os.fsdecode(b"knowledge-\xff")):
            with self.subTest(target=ascii(target_name)):
                target = self.root / target_name
                store = self.create(target, renamed)
                result = store.execute(encode(request("publish_material", material())))
                reopened = open_store(target)
                self.assertEqual(reopened.get_operation("example:operator", "publish_material", "request:one")["receipt"],
                                 result["receipt"])
                self.assertTrue(reopened.read_material("material:build")["content_verified"])
