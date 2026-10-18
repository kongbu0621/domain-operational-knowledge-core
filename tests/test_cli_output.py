"""Actual failed stdout channels cannot erase a committed operation."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import cli
from domain_operational_knowledge_core.model import MAX_RESPONSE
from helpers import setup, request, material, encode


class CliOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = setup(self.root)
        self.prefix = [sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(self.root / "knowledge")]

    def tearDown(self):
        self.temp.cleanup()

    def test_full_stdout_handles_error_and_preserves_successful_write(self):
        payload = self.root / "request.json"
        payload.write_bytes(encode(request("publish_material", material())))
        for args in (["get", "--kind", "material", "--id", "material:missing"], ["write", "--request", str(payload)]):
            with self.subTest(command=args[0]), open("/dev/full", "wb") as sink:
                result = subprocess.run(self.prefix + args, stdout=sink, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(b"inspect the original request", result.stderr)
            self.assertNotIn(b"Traceback", result.stderr)
            self.assertNotIn(b"Exception ignored", result.stderr)
        old = self.store.get_operation("example:operator", "publish_material", "request:one")
        replay = self.store.execute(payload.read_bytes())
        self.assertEqual(old["receipt"], replay["receipt"])
        self.assertTrue(replay["replayed"])
        self.assertTrue(self.store.read_material("material:build")["content_verified"])

    def test_closed_pipe_handles_error_output(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, "wb") as sink:
            result = subprocess.run(self.prefix + ["get", "--kind", "material", "--id", "material:missing"],
                                    stdout=sink, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b"Exception ignored", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_oversized_parser_diagnostic_is_bounded_json(self):
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch.object(sys, "stdout", output):
            result = cli.main(self.prefix[3:] + ["get", "--kind", "material", "--id", "material:missing"] + ["x" * 65536] * 20)
        raw = output.buffer.getvalue()
        self.assertEqual(result, 2)
        self.assertLessEqual(len(raw), MAX_RESPONSE)
        self.assertEqual(raw.count(b"\n"), 1)
        error = json.loads(raw)
        self.assertEqual((error["code"], error["commit_state"]), ("LIMIT", "not_committed"))
