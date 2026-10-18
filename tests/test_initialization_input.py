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

    def cli_init(self, target, ledger):
        return subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core",
                               "--store", str(target), "init", "--ledger-db", str(ledger),
                               "--anchor-artifact", "artifact:example-anchor"],
                              capture_output=True, timeout=10)

    def assert_io_error(self, target, ledger=None):
        with self.assertRaises(KnowledgeError) as caught:
            self.create(target, ledger)
        self.assertEqual((caught.exception.code, caught.exception.commit_state), ("IO_ERROR", "not_committed"))
        child = self.cli_init(target, self.ledger if ledger is None else ledger)
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual((result["code"], result["commit_state"]), ("IO_ERROR", "not_committed"))

    def test_non_utf8_ledger_path_fails_before_creation_in_library_and_cli(self):
        # Linux permits these filename bytes; the persisted Ledger binding
        # is UTF-8 JSON and cannot represent them losslessly as a string.
        renamed = self.root / os.fsdecode(b"materials-\xff.sqlite")
        before = self.hash_file(self.ledger)
        self.ledger.rename(renamed)
        target = self.root / "new-knowledge"
        self.assert_invalid(target, renamed)
        child = self.cli_init(target, renamed)
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

    def test_inaccessible_or_overlong_target_is_a_precreation_io_error(self):
        if os.geteuid() == 0:
            self.skipTest("Permission denial requires an ordinary user")
        parent = self.root / "restricted"
        parent.mkdir(mode=0o700)
        before = self.hash_file(self.ledger)
        target = parent / "target"
        parent.chmod(0)
        try:
            self.assert_io_error(target)
        finally:
            parent.chmod(0o700)
        self.assertEqual(list(parent.iterdir()), [])
        overlong = parent / ("k" * (os.pathconf(parent, "PC_NAME_MAX") + 1))
        self.assert_io_error(overlong)
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(self.hash_file(self.ledger), before)
        self.assertEqual(self.create(target).query("build-guidance", {})["versions"], [])

    def test_ledger_link_loop_is_reported_and_corrected_alias_can_retry(self):
        link = self.root / "ledger-loop"
        link.symlink_to(link.name)
        before = self.hash_file(self.ledger)
        target = self.root / "new-knowledge"
        self.assert_io_error(target, link)
        self.assertFalse(target.exists())
        self.assertEqual(link.readlink(), Path(link.name))
        self.assertEqual(self.hash_file(self.ledger), before)
        link.unlink()
        link.symlink_to(self.ledger.name)
        store = self.create(target, link)
        self.assertEqual(store.database.identity["ledger"]["path"], str(self.ledger.resolve()))
        self.assertEqual(open_store(target).database.identity, store.database.identity)
        self.assertEqual(self.hash_file(self.ledger), before)

    def test_removed_working_directory_keeps_read_and_write_error_states(self):
        # Change the CWD only in a child, retaining valid absolute paths so
        # recovery does not depend on an inaccessible relative location.
        worker = r'''
import json, os, sys
from pathlib import Path
from domain_operational_knowledge_core import KnowledgeError, initialize, open_store
root = Path(sys.argv[1])
cwd = root / "removed-cwd"
cwd.mkdir()
os.chdir(cwd)
cwd.rmdir()
results = {}
for label, call in (
    ("init", lambda: initialize("target", ledger_path=root / "materials.sqlite",
                                anchor_artifact_id="artifact:example-anchor")),
    ("open", lambda: open_store("target"))):
    try:
        call()
        results[label] = "UNEXPECTED SUCCESS"
    except KnowledgeError as exc:
        results[label] = exc.as_dict()
os.chdir(root)
store = initialize("target", ledger_path=root / "materials.sqlite", anchor_artifact_id="artifact:example-anchor")
results["recovered"] = open_store("target").database.identity == store.database.identity
print(json.dumps(results))
'''
        before = self.hash_file(self.ledger)
        child = subprocess.run([sys.executable, "-c", worker, str(self.root)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual((result["init"]["code"], result["init"]["commit_state"]), ("IO_ERROR", "not_committed"))
        self.assertEqual((result["open"]["code"], result["open"]["commit_state"]), ("IO_ERROR", "not_applicable"))
        self.assertTrue(result["recovered"])
        self.assertEqual(self.hash_file(self.ledger), before)
