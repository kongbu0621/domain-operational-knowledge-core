import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from infra_artifact_ledger import open as open_ledger
from domain_operational_knowledge_core import open_store
from helpers import setup, request, material, candidate, review, encode


class ProcessTests(unittest.TestCase):
    def test_process_kill_at_four_persistence_boundaries_and_resume(self):
        for checkpoint in ("after_create", "after_append", "before_local_commit", "after_local_commit"):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                setup(root)
                payload = root / "request.json"
                payload.write_bytes(encode(request("publish_material", material())))
                marker = root / "paused"
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("crash_worker.py")),
                                          str(root / "knowledge"), str(payload), checkpoint, str(marker)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists(), "Child did not reach the expected persistence boundary")
                    child.kill()
                    child.communicate(timeout=5)
                    self.assertEqual(child.returncode, -signal.SIGKILL)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)
                store = open_store(root / "knowledge")
                before = store.pending("example:operator", "request:one")
                self.assertEqual(before["state"], "COMMITTED" if checkpoint == "after_local_commit" else "PENDING")
                resumed = subprocess.run([sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(root / "knowledge"),
                                          "resume", "--scope", "example:operator", "--request-id", "request:one"],
                                         capture_output=True, timeout=10)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertEqual(json.loads(resumed.stdout)["receipt"]["result"]["material_id"], "material:build")
                with open_ledger(root / "materials.sqlite") as ledger:
                    counts = ledger.verify()["counts"]
                self.assertEqual((counts["artifacts"], counts["versions"], counts["idempotency_records"]), (2, 1, 3))
                self.assertTrue(store.read_material("material:build")["content_verified"])

    def simultaneous(self, root, requests):
        ready, processes = [], []
        release = root / "release-writers"
        for index, value in enumerate(requests):
            path = root / f"request-{index}.json"
            path.write_bytes(encode(value))
            ready.append(path)
        # Each process must open the store independently. SQLite serializes reviews.
        try:
            for path in ready:
                processes.append(subprocess.Popen([sys.executable, str(Path(__file__).with_name("concurrent_worker.py")),
                                                    str(root / "knowledge"), str(path), str(path.with_suffix(".ready")), str(release)],
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 10
            while not all(path.with_suffix(".ready").exists() for path in ready) and time.monotonic() < deadline:
                self.assertTrue(all(child.poll() is None for child in processes))
                time.sleep(.01)
            self.assertTrue(all(path.with_suffix(".ready").exists() for path in ready))
            release.write_text("go")
            results = []
            for child in processes:
                out, err = child.communicate(timeout=15)
                self.assertFalse(err, err)
                results.append(json.loads(out))
            return results
        finally:
            for child in processes:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_accept_reject_race_has_one_final_decision(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = setup(root)
            store.execute(encode(request("publish_material", material())))
            proposed = candidate(store)
            results = self.simultaneous(root, [request("review", review(proposed, decision), "request:" + decision)
                                                for decision in ("ACCEPT", "REJECT")])
            self.assertEqual(sorted(result["status"] for result in results), ["COMMITTED", "ERROR"])
            self.assertEqual(next(result for result in results if result["status"] == "ERROR")["code"], "CONFLICT")
            active = store.query("build-guidance", {"platform": "linux"})["versions"]
            state = store.get("candidate", proposed["candidate_id"])["record"]["state"]
            self.assertEqual(len(active), 1 if state == "ACCEPTED" else 0)

    def test_two_accepts_have_one_version(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = setup(root)
            store.execute(encode(request("publish_material", material())))
            proposed = candidate(store)
            results = self.simultaneous(root, [request("review", review(proposed), "request:" + str(i)) for i in range(2)])
            self.assertTrue(all(result["status"] == "COMMITTED" for result in results))
            self.assertEqual(len({result["receipt"]["result"]["version_id"] for result in results}), 1)
            self.assertEqual(len(store.query("build-guidance", {"platform": "linux"})["versions"]), 1)

    def test_two_identical_publications_have_one_material_version(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = setup(root)
            results = self.simultaneous(root, [request("publish_material", material())] * 2)
            self.assertTrue(all(result["status"] == "COMMITTED" for result in results), results)
            self.assertEqual(results[0]["receipt"], results[1]["receipt"])
            with open_ledger(root / "materials.sqlite") as ledger:
                counts = ledger.verify()["counts"]
            self.assertEqual((counts["artifacts"], counts["versions"]), (2, 1))
