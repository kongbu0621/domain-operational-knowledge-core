"""Independent processes: transaction races and killed feedback publication."""
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

from domain_operational_knowledge_core import open_store
from helpers import encode, request, material, review
from test_lifecycle import LifecycleFixture, adoption, completion, retirement
import test_processes as process_helpers


class LifecycleProcessTests(LifecycleFixture, unittest.TestCase):
    simultaneous = process_helpers.ProcessTests.simultaneous

    def test_competing_revisions_switch_exactly_one_version(self):
        self.run_op("publish_material", material("material:new"), "request:new")
        candidates = [self.revision("proposal:revision" + str(i), "material:new") for i in range(2)]
        results = self.simultaneous(self.root, [request("review", review(c), "request:review" + str(i)) for i,c in enumerate(candidates)])
        self.assertEqual(sorted(r["status"] for r in results), ["COMMITTED", "ERROR"])
        self.assertEqual(next(r for r in results if r["status"] == "ERROR")["code"], "CONFLICT")
        winner = next(r for r in results if r["status"] == "COMMITTED")["receipt"]["result"]["version_id"]
        self.assertEqual([v["version_id"] for v in self.store.query("build-guidance", {"platform": "linux"})["versions"]], [winner])
        self.assertEqual(self.counts()["versions"], 2)
        self.assertEqual(self.store.get("version", self.version)["record"]["state"], "SUPERSEDED")
        # The losing candidate remains available for an explicit rejection.
        for c in candidates:
            if self.store.get("candidate", c["candidate_id"])["record"]["state"] == "PENDING":
                self.run_op("review", review(c, "REJECT"), "request:reject-stale")

    def test_retirement_racing_adoption_has_a_valid_serial_order(self):
        results = self.simultaneous(self.root, [request("retire", retirement(self.version)), request("start_use", adoption(self.version))])
        self.assertEqual(results[0]["status"], "COMMITTED")
        if results[1]["status"] == "COMMITTED":
            self.assertEqual(self.counts()["usages"], 1)
            self.run_op("complete_use", completion())
        else:
            self.assertEqual(results[1]["code"], "CONFLICT")
            self.assertEqual(self.counts()["usages"], 0)
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])

    def test_retirement_racing_revision_does_not_retire_the_new_version(self):
        self.run_op("publish_material", material("material:new"), "request:new")
        candidate = self.revision(evidence="material:new")
        results = self.simultaneous(self.root, [request("retire", retirement(self.version)), request("review", review(candidate), "request:revision")])
        self.assertEqual(sorted(r["status"] for r in results), ["COMMITTED", "ERROR"])
        self.assertEqual(next(r for r in results if r["status"] == "ERROR")["code"], "CONFLICT")
        active = self.store.query("build-guidance", {"platform": "linux"})["versions"]
        self.assertEqual(len(active), int(results[1]["status"] == "COMMITTED"))

    def test_identical_concurrent_completion_uses_one_material_and_receipt(self):
        self.run_op("start_use", adoption(self.version))
        results = self.simultaneous(self.root, [request("complete_use", completion())] * 2)
        self.assertTrue(all(r["status"] == "COMMITTED" for r in results), results)
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        self.assertEqual((self.counts()["feedback"], self.counts()["usages"]), (1, 1))
        self.assertEqual(self.ledger_counts()["versions"], 2)

    def test_competing_corrections_keep_one_successor_and_no_extra_material(self):
        self.run_op("start_use", adoption(self.version))
        first = self.run_op("complete_use", completion())["receipt"]["result"]["feedback_id"]
        requests = [request("correct_feedback", dict(completion(material_id="material:correction" + str(i), text="Correction " + str(i)),
                      expected_feedback_id=first), "request:correction" + str(i)) for i in range(2)]
        results = self.simultaneous(self.root, requests)
        self.assertEqual(sorted(r["status"] for r in results), ["COMMITTED", "ERROR"])
        self.assertIn(next(r for r in results if r["status"] == "ERROR")["code"], ("PENDING", "CONFLICT"))
        self.assertEqual((self.counts()["feedback"], self.counts()["usages"], self.ledger_counts()["versions"]), (2, 1, 3))
        self.assertEqual(self.counts()["pending"], 0)

    def test_killed_completion_at_each_commit_boundary_resumes_same_feedback(self):
        # Each checkpoint owns a separate use; the same request key is reused only
        # when recovering that use, never to rerun an external task.
        for checkpoint in ("after_create", "after_append", "before_local_commit", "after_local_commit"):
            with self.subTest(checkpoint=checkpoint):
                usage_id = "usage:" + checkpoint
                key = "request:" + checkpoint
                self.run_op("start_use", adoption(self.version, usage_id), key)
                value = request("complete_use", completion(usage_id, "material:" + checkpoint), key)
                payload = self.root / (checkpoint + ".json")
                payload.write_bytes(encode(value))
                marker = self.root / (checkpoint + ".ready")
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("crash_worker.py")),
                    str(self.root / "knowledge"), str(payload), checkpoint, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists(), "Child never reached feedback checkpoint")
                    child.kill()
                    out, err = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, -signal.SIGKILL, out + err)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)
                self.store = open_store(self.root / "knowledge")
                pending = self.store.pending("example:operator", key, operation="complete_use")
                self.assertEqual(pending["state"], "COMMITTED" if checkpoint == "after_local_commit" else "PENDING")
                result = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(self.root / "knowledge"),
                    "resume", "--scope", "example:operator", "--request-id", key, "--operation", "complete_use"], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                receipt = json.loads(result.stdout)["receipt"]
                repeated = self.store.resume("example:operator", key, operation="complete_use")
                self.assertEqual(repeated["receipt"], receipt)
                self.assertTrue(self.store.read_material("material:" + checkpoint)["content_verified"])
                self.assertEqual((self.counts()["pending"], self.counts()["feedback_reservations"]), (0, 0))
        self.assertEqual((self.counts()["feedback"], self.counts()["usages"], self.ledger_counts()["versions"]), (4, 4, 5))

    def test_different_request_ids_cannot_publish_two_initial_completions(self):
        self.run_op("start_use", adoption(self.version))
        results = self.simultaneous(self.root, [request("complete_use", completion(material_id="material:first" + str(i)),
                                                "request:complete" + str(i)) for i in range(2)])
        self.assertEqual(sorted(r["status"] for r in results), ["COMMITTED", "ERROR"])
        self.assertIn(next(r for r in results if r["status"] == "ERROR")["code"], ("PENDING", "CONFLICT"))
        self.assertEqual((self.counts()["feedback"], self.counts()["usages"], self.ledger_counts()["versions"]), (1, 1, 2))

    def test_killed_revision_commit_preserves_one_current_version_and_original_receipt(self):
        self.run_op("publish_material", material("material:new"), "request:new")
        proposed = self.revision(evidence="material:new")
        value = request("review", review(proposed), "request:revision")
        payload = self.root / "revision.json"
        payload.write_bytes(encode(value))
        for checkpoint in ("before_local_commit", "after_local_commit"):
            marker = self.root / (checkpoint + ".ready")
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("crash_worker.py")),
                str(self.root / "knowledge"), str(payload), checkpoint, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            self.store = open_store(self.root / "knowledge")
            active = self.store.query("build-guidance", {"platform": "linux"})["versions"]
            self.assertEqual(len(active), 1)
            if checkpoint == "before_local_commit":
                self.assertEqual(active[0]["version_id"], self.version)
                self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "PENDING")
            else:
                self.assertNotEqual(active[0]["version_id"], self.version)
                receipt = self.store.get_operation("example:operator", "review", "request:revision")["receipt"]
                replay = self.store.execute(encode(value))
                self.assertEqual(receipt, replay["receipt"])
                self.assertEqual(self.counts()["versions"], 2)
