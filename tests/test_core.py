import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from infra_artifact_ledger import initialize as initialize_ledger, open as open_ledger
from domain_operational_knowledge_core import initialize, open_store, KnowledgeError
from domain_operational_knowledge_core.ledger import MaterialLedger
from helpers import setup, request, material, proposal, review, candidate, encode


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = setup(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, operation, body, key="request:one"):
        return self.store.execute(encode(request(operation, body, key)))

    def publish(self):
        return self.execute("publish_material", material())

    def counts(self):
        with open_ledger(self.root / "materials.sqlite") as handle:
            return handle.verify()["counts"]

    def failure(self, code, call):
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_complete_material_candidate_review_query_and_exact_read(self):
        self.publish()
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])
        proposed = candidate(self.store)
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])
        accepted = self.execute("review", review(proposed))
        result = open_store(self.root / "knowledge").query("build-guidance", {"platform": "linux"})
        self.assertFalse(result["usage_recorded"])
        self.assertEqual(len(result["versions"]), 1)
        self.assertEqual(result["versions"][0]["version_id"], accepted["receipt"]["result"]["version_id"])
        self.assertEqual(result["versions"][0]["content"], proposal()["content"])
        read = self.store.read_material("material:build")
        self.assertEqual(base64.b64decode(read["entries"][0]["data"]), material()["text"].encode())
        self.assertEqual(self.store.query("build-guidance", {"platform": "windows"})["versions"], [])
        self.assertEqual(self.store.query("build-guidance", {})["versions"], [])

    def test_rejection_is_final_and_produces_no_active_version(self):
        self.publish()
        proposed = candidate(self.store)
        first = self.execute("review", review(proposed, "REJECT"))
        self.assertIsNone(first["receipt"]["result"]["version_id"])
        self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "REJECTED")
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])
        self.failure("CONFLICT", lambda: self.execute("review", review(proposed), "request:accept-later"))

    def test_request_replay_and_same_id_changed_content(self):
        first = self.publish()
        counts = self.counts()
        replay = open_store(self.root / "knowledge").execute(encode(request("publish_material", material())))
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["receipt"], replay["receipt"])
        self.assertEqual(counts, self.counts())
        self.failure("CONFLICT", lambda: self.execute("publish_material", material(text="different")))

    def test_material_logical_identity_survives_new_request_id(self):
        first = self.publish()
        counts = self.counts()
        second = self.execute("publish_material", material(), "request:again")
        self.assertTrue(second["receipt"]["result"]["reused"])
        self.assertEqual(first["receipt"]["result"]["material_id"], second["receipt"]["result"]["material_id"])
        self.assertEqual(counts, self.counts())
        self.failure("CONFLICT", lambda: self.execute("publish_material", material(text="changed"), "request:new"))

    def test_committed_resume_observation_failure_preserves_commit_state(self):
        original = self.publish()
        counts = self.counts()
        with patch.object(self.store, "get", side_effect=KnowledgeError("BUSY", "Synthetic observation failure", "not_applicable")):
            error = self.failure("BUSY", lambda: self.store.resume("example:operator", "request:one"))
            self.assertEqual(error.commit_state, "committed")
            self.assertEqual(error.details["request_id"], "request:one")
        resumed = open_store(self.root / "knowledge").resume("example:operator", "request:one")
        self.assertTrue(resumed["replayed"])
        self.assertEqual(resumed["receipt"], original["receipt"])
        self.assertEqual(self.counts(), counts)

    def test_proposal_logical_identity_and_review_equivalence(self):
        self.publish()
        proposed = candidate(self.store)
        repeated = self.execute("propose", proposal(), "request:new-key")["receipt"]["result"]
        self.assertEqual(proposed["candidate_id"], repeated["candidate_id"])
        changed = dict(proposal(), content="Changed reasoning")
        self.failure("CONFLICT", lambda: self.execute("propose", changed, "request:changed"))
        accepted = self.execute("review", review(proposed))
        repeated = self.execute("review", review(proposed), "request:review-again")
        self.assertEqual(accepted["receipt"]["result"]["version_id"], repeated["receipt"]["result"]["version_id"])
        self.assertEqual(len(self.store.query("build-guidance", {"platform": "linux"})["versions"]), 1)
        for key, value in [("reason", "A different decision"), ("reviewer", "another:reviewer"), ("candidate_sha256", "0" * 64)]:
            with self.subTest(field=key):
                self.failure("CONFLICT", lambda: self.execute("review", dict(review(proposed), **{key: value}), "request:changed"))

    def test_missing_material_does_not_partially_create_candidate(self):
        self.failure("NOT_FOUND", lambda: candidate(self.store))
        self.publish()
        self.assertIsNotNone(candidate(self.store)["candidate_id"])

    def test_strict_json_and_resource_bounds_before_any_ledger_write(self):
        raw = encode(request("publish_material", material()))
        invalid = [raw.replace(b'"schema":', b'"schema":"duplicate","schema":'), b'{"n":NaN}',
                   b'\xef\xbb\xbf' + raw, br'{"x":"\ud800"}', b'{"x":1.0}', b'[' * 30 + b'0' + b']' * 30]
        before = self.counts()
        for value in invalid:
            with self.subTest(raw=value[:40]), self.assertRaises(KnowledgeError):
                self.store.execute(value)
        for body in [dict(material(), extra=True), material(text="x" * 65537),
                     dict(material(), context={"../unsafe": "yes"}), dict(material(), claimed_outcome=True)]:
            with self.subTest(body=list(body)), self.assertRaises(KnowledgeError):
                self.execute("publish_material", body)
        self.assertEqual(before, self.counts())

    def test_unknown_or_missing_existing_store_is_not_initialized(self):
        self.failure("STORE_INVALID", lambda: open_store(self.root / "absent"))
        self.assertFalse((self.root / "absent").exists())
        self.failure("CONFLICT", lambda: initialize(self.root / "knowledge", ledger_path=self.root / "materials.sqlite",
                                                    anchor_artifact_id="artifact:example-anchor"))
        (self.root / "knowledge/knowledge.sqlite").unlink()
        self.failure("STORE_INVALID", lambda: open_store(self.root / "knowledge"))
        self.assertFalse((self.root / "knowledge/knowledge.sqlite").exists())

    def test_registration_checks_artifact_pair_digest_and_task_reference(self):
        self.publish()
        original = self.store.get("material", "material:build")["record"]
        body = {key: value for key, value in original.items() if key not in ("scope", "recorded_at")}
        body["material_id"] = "material:reference"
        for field, bad in [("artifact_id", "artifact:example-anchor"), ("version_id", "version:missing")]:
            changed = copy.deepcopy(body)
            changed["reference"][field] = bad
            with self.subTest(field=field), self.assertRaises(KnowledgeError):
                self.execute("register_material", changed)
        changed = copy.deepcopy(body)
        changed["reference"]["entries"][0]["sha256"] = "0" * 64
        self.failure("INTEGRITY_FAILURE", lambda: self.execute("register_material", changed))
        self.failure("IDENTITY_CONFLICT", lambda: self.execute("register_material", dict(body, task_ref="task:unrelated")))
        self.failure("NOT_FOUND", lambda: self.store.get("material", "material:reference"))
        self.execute("register_material", body)
        self.assertEqual(self.store.read_material("material:reference")["entries"], self.store.read_material("material:build")["entries"])

    def test_payload_damage_does_not_rewrite_prior_commit_or_admit_new_knowledge(self):
        original = self.publish()
        proposed = candidate(self.store)
        # Corrupt a disposable fixture through SQLite, never a running deployment.
        with sqlite3.connect(self.root / "materials.sqlite") as db:
            original_payload = db.execute("SELECT data FROM payloads").fetchone()[0]
            db.execute("UPDATE payloads SET data=?", (b"broken",))
        historical = self.execute("publish_material", material())
        self.assertTrue(historical["replayed"])
        self.assertEqual(original["receipt"], historical["receipt"])
        self.assertEqual(historical["observation"]["materials"][0]["status"], "UNAVAILABLE")
        failure = self.failure("MATERIAL_UNAVAILABLE", lambda: self.execute("review", review(proposed)))
        self.assertEqual(failure.commit_state, "not_committed")
        self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "PENDING")
        with sqlite3.connect(self.root / "materials.sqlite") as db:
            db.execute("UPDATE payloads SET data=?", (original_payload,))
        self.execute("review", review(proposed))
        with sqlite3.connect(self.root / "materials.sqlite") as db:
            db.execute("UPDATE payloads SET data=?", (b"broken",))
        self.failure("MATERIAL_UNAVAILABLE", lambda: self.store.query("build-guidance", {"platform": "linux"}))
        history = self.store.get_operation("example:operator", "review", "request:one")
        self.assertEqual(history["observation"]["record"]["version_state"], "ACTIVE")
        self.assertEqual(history["observation"]["materials"][0]["status"], "UNAVAILABLE")

    def test_rejection_can_record_unusable_material_without_reading_corrupt_payload(self):
        self.publish()
        proposed = candidate(self.store)
        with sqlite3.connect(self.root / "materials.sqlite") as db:
            db.execute("UPDATE payloads SET data=?", (b"broken",))
        self.execute("review", review(proposed, "REJECT"))
        self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "REJECTED")

    def test_partial_ledger_create_resume_retains_original_request(self):
        original = MaterialLedger._ensure_operation

        def failed(adapter, handle, selected, payloads=None):
            if selected["operation_kind"] == "append_version":
                raise KnowledgeError("BUSY", "Synthetic failure before append")
            return original(adapter, handle, selected, payloads)

        with patch.object(MaterialLedger, "_ensure_operation", failed):
            error = self.failure("BUSY", self.publish)
        self.assertEqual(error.commit_state, "pending")
        self.assertEqual(self.counts()["versions"], 0)
        saved = self.store.pending("example:operator", "request:one")
        self.assertEqual(saved["state"], "PENDING")
        self.assertEqual(saved["request"]["body"], material())
        self.failure("CONFLICT", lambda: self.execute("publish_material", material(identity="material:changed")))
        self.failure("PENDING", lambda: self.execute("publish_material", material(), "request:replacement"))
        resumed = open_store(self.root / "knowledge").resume("example:operator", "request:one")
        self.assertEqual(resumed["receipt"]["result"]["material_id"], "material:build")
        self.assertEqual(self.counts()["versions"], 1)
        self.assertEqual(self.counts()["artifacts"], 2)
        self.assertEqual(self.store.pending("example:operator", "request:one")["state"], "COMMITTED")

    def test_database_busy_is_recoverable_without_external_write(self):
        db = sqlite3.connect(self.root / "knowledge/knowledge.sqlite", isolation_level=None)
        db.execute("BEGIN EXCLUSIVE")
        try:
            error = self.failure("BUSY", self.publish)
            self.assertEqual(error.commit_state, "not_committed")
        finally:
            db.close()
        self.assertEqual(self.counts()["versions"], 0)
        self.publish()
        self.assertEqual(self.counts()["versions"], 1)

    def test_active_query_pagination_is_deterministic_and_context_is_exact(self):
        self.publish()
        for index in range(3):
            proposed = candidate(self.store, "proposal:" + str(index))
            self.execute("review", review(proposed), "request:review-" + str(index))
        cursor, ids = "", []
        while True:
            page = self.store.query("build-guidance", {"platform": "linux", "extra": "allowed"}, after=cursor, limit=1)
            ids.extend(version["version_id"] for version in page["versions"])
            cursor = page["next_after"]
            if cursor is None:
                break
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, sorted(set(ids)))
        self.failure("INVALID_INPUT", lambda: self.store.query("build-guidance", {}, limit=True))
        self.failure("INVALID_INPUT", lambda: self.store.query("build-guidance", {1: "linux"}))

    def test_cli_fresh_process_reads_exact_material_and_reports_json_error(self):
        self.publish()
        prefix = [sys.executable, "-m", "domain_operational_knowledge_core", "--store", str(self.root / "knowledge")]
        success = subprocess.run(prefix + ["read-material", "--id", "material:build"], capture_output=True, timeout=10)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(json.loads(success.stdout)["data"]["content_verified"])
        error = subprocess.run(prefix + ["get", "--kind", "version", "--id", "version:missing"], capture_output=True, timeout=10)
        self.assertEqual(error.returncode, 3)
        self.assertEqual(json.loads(error.stdout)["commit_state"], "not_applicable")

    def test_manifest_closure_is_exact_and_entry_keys_are_not_filesystem_paths(self):
        self.publish()
        previous = self.store.get("material", "material:build")["record"]
        blob = previous["reference"]["entries"][0]
        new_bytes = b"\xff\x00binary evidence"
        entries = [dict(entry_key="../do-not-extract", blob_ref=blob["blob_ref"]),
                   dict(entry_key="binary", blob_ref="blob:binary")]
        manifest_bytes = json.dumps(dict(entries=entries), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        with open_ledger(self.root / "materials.sqlite") as handle:
            handle.execute(encode(dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind="append_version",
                idempotency_scope_ref="example:manifest", idempotency_key="append-manifest", body=dict(
                    version=dict(version_id="version:manifest", artifact_id=previous["reference"]["artifact_id"],
                                 content_root_ref="root:manifest", parent_version_refs=[], external_source_refs=[material()["task_ref"]],
                                 capture_refs=[], handling_policy_refs=[]),
                    content_roots=[dict(content_root_ref="root:manifest", kind="manifest", manifest_ref="manifest:example")],
                    blobs=[dict(blob_ref="blob:binary", byte_length=len(new_bytes), payload_availability="available",
                                digest=dict(algorithm="sha256", value=hashlib.sha256(new_bytes).hexdigest()))],
                    manifests=[dict(manifest_ref="manifest:example", entries=entries,
                                    digest=dict(algorithm="sha256", value=hashlib.sha256(manifest_bytes).hexdigest()))],
                    provenance_links=[]))), payloads={"blob:binary": new_bytes})
        reference = dict(artifact_id=previous["reference"]["artifact_id"], version_id="version:manifest", entries=[
            dict(blob, entry_key=entries[0]["entry_key"]), dict(entry_key="binary", blob_ref="blob:binary", byte_length=len(new_bytes),
                                                            sha256=hashlib.sha256(new_bytes).hexdigest())])
        body = {key: value for key, value in previous.items() if key not in ("scope", "recorded_at")}
        body.update(material_id="material:manifest", reference=reference)
        incomplete = copy.deepcopy(body)
        incomplete["reference"]["entries"].pop()
        self.failure("IDENTITY_CONFLICT", lambda: self.execute("register_material", incomplete))
        self.execute("register_material", body)
        result = self.store.read_material("material:manifest")
        self.assertEqual([base64.b64decode(entry["data"]) for entry in result["entries"]], [material()["text"].encode(), new_bytes])
        self.assertFalse((self.root.parent / "do-not-extract").exists())

    def test_replacing_configured_ledger_does_not_silently_accept_same_path(self):
        self.publish()
        anchor = self.store.database.identity["ledger"]["anchor"]
        other = self.root / "other.sqlite"
        with initialize_ledger(other) as ledger:
            ledger.execute(encode(dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind="create_artifact",
                idempotency_scope_ref="other", idempotency_key="anchor", body=dict(artifact={
                    key: value for key, value in dict(anchor, namespace_ref="another-domain").items() if key != "recorded_at"}))))
        os.replace(other, self.root / "materials.sqlite")
        self.failure("IDENTITY_CONFLICT", lambda: self.store.read_material("material:build"))
        replay = self.publish()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["observation"]["materials"][0]["status"], "UNAVAILABLE")

    def test_original_operation_fingerprint_conflict_remains_pending(self):
        with patch.object(MaterialLedger, "publish", side_effect=KnowledgeError("BUSY", "Fixture before external writes")):
            self.failure("BUSY", self.publish)
        from domain_operational_knowledge_core.model import operation_key
        key = operation_key("example:operator", "publish_material", "request:one")
        create = self.store.ledger.requests(key, material())[0]
        create["body"]["artifact"]["namespace_ref"] = "wrong:namespace"
        with open_ledger(self.root / "materials.sqlite") as ledger:
            ledger.execute(encode(create))
        failure = self.failure("IDENTITY_CONFLICT", lambda: self.store.resume("example:operator", "request:one"))
        self.assertEqual(failure.commit_state, "pending")
        self.assertEqual(self.counts()["versions"], 0)
        self.assertEqual(self.store.pending("example:operator", "request:one")["state"], "PENDING")


if __name__ == "__main__":
    unittest.main()
