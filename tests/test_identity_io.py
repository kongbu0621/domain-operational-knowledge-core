"""Bound marker reads in retained handles and in filesystem change windows."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.model import MAX_REQUEST
from helpers import setup, request, material, encode


WORKER = r'''
import json, os, resource, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[2])
from helpers import request, material, encode
from domain_operational_knowledge_core import KnowledgeError, open_store
root, mode = Path(sys.argv[1]), sys.argv[3]
store = open_store(root / "knowledge")
payload = encode(request("publish_material", material("material:next"), "request:next"))
marker = root / "knowledge/identity.json"
saved, inode = marker.read_bytes(), marker.stat().st_ino
changed, results = [], {}
fstat, lstat = os.fstat, Path.lstat

def enlarge():
    with marker.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    changed.append(True)

def grow_after_size_check(fd):
    info = fstat(fd)
    if info.st_ino == inode and not changed:
        enlarge()
    return info

def replace_after_type_check(path, *args, **kwargs):
    info = lstat(path, *args, **kwargs)
    if path == marker and not changed:
        marker.unlink()
        os.mkfifo(marker, 0o600)
        changed.append(True)
    return info

resource.setrlimit(resource.RLIMIT_AS, (96 * 1024 * 1024, 96 * 1024 * 1024))
try:
    if mode == "oversized":
        enlarge()
    with patch.object(os, "fstat", grow_after_size_check if mode == "growing" else fstat), \
         patch.object(Path, "lstat", replace_after_type_check if mode == "fifo" else lstat):
        for label, call in (
            ("open", lambda: open_store(root / "knowledge")),
            ("read", lambda: store.get_operation("example:operator", "publish_material", "request:one")),
            ("write", lambda: store.execute(payload))):
            try:
                call()
                results[label] = "UNEXPECTED SUCCESS"
            except KnowledgeError as exc:
                results[label] = exc.as_dict()
            except MemoryError:
                results[label] = "UNHANDLED MemoryError"
    results["change_exercised"] = bool(changed)
finally:
    if mode == "fifo" and changed:
        marker.unlink()
    marker.write_bytes(saved)
    marker.chmod(0o600)
print(json.dumps(results))
'''


class IdentityIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = setup(self.root)
        self.first = self.store.execute(encode(request("publish_material", material())))

    def tearDown(self):
        self.temporary.cleanup()

    def retained_bytes(self):
        return {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in ("knowledge/knowledge.sqlite", "knowledge/identity.json", "materials.sqlite")}

    def worker(self, mode):
        before = self.retained_bytes()
        child = subprocess.run([sys.executable, "-c", WORKER, str(self.root),
                                str(Path(__file__).parent), mode], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertFalse(child.stderr)
        result = json.loads(child.stdout)
        self.assertTrue(result["change_exercised"])
        for label in ("open", "read", "write"):
            self.assertIsInstance(result[label], dict, result)
            self.assertEqual(result[label]["code"], "STORE_INVALID")
            self.assertEqual(result[label]["commit_state"], "not_committed" if label == "write" else "not_applicable")
        self.assertEqual(self.retained_bytes(), before)
        self.assertEqual(self.store.get_operation("example:operator", "publish_material", "request:one")["receipt"],
                         self.first["receipt"])

    def test_existing_or_growing_oversized_marker_cannot_exhaust_process_memory(self):
        for mode in ("oversized", "growing"):
            with self.subTest(mode=mode):
                self.worker(mode)
        result = self.store.execute(encode(request("publish_material", material("material:next"), "request:next")))
        self.assertEqual(result["commit_state"], "committed")

    def test_marker_changed_to_fifo_after_type_check_cannot_block_reader(self):
        self.worker("fifo")

    def test_identity_size_boundary_preserves_normal_read_and_original_binding(self):
        marker = self.root / "knowledge/identity.json"
        raw = marker.read_bytes()
        boundary = raw + b" " * (MAX_REQUEST - len(raw))
        marker.write_bytes(boundary)
        store = open_store(self.root / "knowledge")
        self.assertEqual(store.get_operation("example:operator", "publish_material", "request:one")["receipt"],
                         self.first["receipt"])
        marker.write_bytes(boundary + b" ")
        for call in (lambda: open_store(marker.parent), lambda: store.query("build-guidance", {})):
            with self.assertRaises(KnowledgeError) as caught:
                call()
            self.assertEqual((caught.exception.code, caught.exception.commit_state), ("STORE_INVALID", "not_applicable"))
        marker.write_bytes(boundary)
        self.assertEqual(store.get_operation("example:operator", "publish_material", "request:one")["receipt"],
                         self.first["receipt"])
