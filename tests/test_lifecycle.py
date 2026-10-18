"""Lifecycle invariants through the public library, with synthetic task evidence."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from infra_artifact_ledger import open as open_ledger
from domain_operational_knowledge_core import KnowledgeError, open_store
from domain_operational_knowledge_core.model import canonical
from helpers import setup, encode, request, material, proposal, candidate, review


def adoption(version, usage_id="usage:build"):
    return dict(usage_id=usage_id, consumer_ref="example:consumer", task_ref="task:use-001",
                version_id=version, context={"platform": "linux"})


def completion(usage_id="usage:build", material_id="material:feedback", text="Synthetic observed result: guidance needs a narrower condition."):
    return dict(usage_id=usage_id, material=dict(material_id=material_id, source_ref="example:consumer", task_ref="task:use-001",
                context={"platform": "linux"}, claimed_outcome="FAILED", gaps=[], text=text))


def retirement(version, evidence="material:build"):
    return dict(expected_version_id=version, reviewer="example:reviewer", reason="Evidence no longer supports new uses.",
                evidence_material_ids=[evidence])


class LifecycleFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = setup(self.root)
        self.run_op("publish_material", material())
        proposed = candidate(self.store)
        self.version = self.run_op("review", review(proposed))["receipt"]["result"]["version_id"]

    def tearDown(self):
        self.temp.cleanup()

    def run_op(self, operation, body, key="request:one"):
        return self.store.execute(encode(request(operation, body, key)))

    def failure(self, code, call):
        with self.assertRaises(KnowledgeError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def counts(self):
        with sqlite3.connect(self.root / "knowledge/knowledge.sqlite") as db:
            return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in ("materials", "usages", "feedback", "versions", "retirements", "pending", "feedback_reservations")}

    def ledger_counts(self):
        with open_ledger(self.root / "materials.sqlite") as ledger:
            return ledger.verify()["counts"]

    def revision(self, identity="proposal:revision", evidence="material:feedback", version=None):
        body = dict(proposal(identity), expected_version_id=version or self.version,
                    content="Check the generated configuration only for matching build environments.",
                    material_ids=[evidence])
        return self.run_op("propose_revision", body, "request:" + identity)["receipt"]["result"]


class LifecycleTests(LifecycleFixture, unittest.TestCase):
    def test_feedback_to_reviewed_revision_and_retirement_is_traceable(self):
        use = self.run_op("start_use", adoption(self.version))
        feedback = self.run_op("complete_use", completion())
        fid = feedback["receipt"]["result"]["feedback_id"]
        self.assertEqual(feedback["observation"]["record"]["version_id"], self.version)
        self.assertTrue(self.store.read_material("material:feedback")["content_verified"])
        proposed = self.revision()
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"][0]["version_id"], self.version)
        accepted = self.run_op("review", dict(review(proposed), evidence_material_ids=["material:feedback"]), "request:revision-review")
        new = accepted["receipt"]["result"]["version_id"]
        versions = self.store.query("build-guidance", {"platform": "linux"})["versions"]
        self.assertEqual([v["version_id"] for v in versions], [new])
        old = self.store.get("version", self.version)["record"]
        self.assertEqual(old["state"], "SUPERSEDED")
        self.assertEqual(old["lineage_id"], versions[0]["lineage_id"])
        self.assertEqual(self.store.get("usage", "usage:build")["record"]["latest_feedback_id"], fid)
        replay = self.run_op("start_use", adoption(self.version))
        self.assertEqual(replay["receipt"], use["receipt"])
        self.assertEqual(replay["observation"]["record"]["version_state"], "SUPERSEDED")
        self.run_op("retire", retirement(new, "material:feedback"))
        self.store = open_store(self.root / "knowledge")
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])
        self.assertEqual(self.store.get("version", new)["record"]["state"], "RETIRED")
        self.assertEqual(self.store.get("feedback", fid)["record"]["version_id"], self.version)
        self.assertEqual(self.counts()["usages"], 1)

    def test_query_then_retirement_rejects_new_use_but_allows_late_feedback_and_replay(self):
        self.assertFalse(self.store.query("build-guidance", {"platform": "linux"})["usage_recorded"])
        original = self.run_op("start_use", adoption(self.version))
        retired = self.run_op("retire", retirement(self.version))
        counts = self.counts()
        self.failure("CONFLICT", lambda: self.run_op("start_use", adoption(self.version, "usage:new"), "request:new"))
        self.assertEqual(self.counts(), counts)
        replay = self.run_op("start_use", adoption(self.version))
        self.assertEqual(replay["receipt"], original["receipt"])
        self.assertEqual(replay["observation"]["record"]["version_state"], "RETIRED")
        self.assertTrue(self.run_op("start_use", adoption(self.version), "request:reuse")["receipt"]["result"]["reused"])
        self.assertEqual(self.run_op("retire", retirement(self.version))["receipt"], retired["receipt"])
        late = self.run_op("complete_use", completion())
        self.assertEqual(late["observation"]["record"]["version_state"], "RETIRED")
        self.assertEqual(self.store.query("build-guidance", {"platform": "linux"})["versions"], [])

    def test_adoption_conditions_material_availability_and_stable_identity(self):
        self.failure("CONFLICT", lambda: self.run_op("start_use", dict(adoption(self.version), context={"platform": "other"})))
        with patch.object(self.store.ledger, "read", side_effect=KnowledgeError("MATERIAL_UNAVAILABLE", "Missing bytes")):
            self.failure("MATERIAL_UNAVAILABLE", lambda: self.run_op("start_use", adoption(self.version)))
        self.assertEqual(self.counts()["usages"], 0)
        self.run_op("start_use", adoption(self.version))
        for key in ("request:one", "request:new"):
            self.failure("CONFLICT", lambda: self.run_op("start_use", dict(adoption(self.version), task_ref="task:changed"), key))
        self.assertEqual(self.counts()["usages"], 1)

    def test_completion_dedupe_and_corrections_preserve_all_history(self):
        self.run_op("start_use", adoption(self.version))
        first = self.run_op("complete_use", completion())
        counts, ledger = self.counts(), self.ledger_counts()
        replay = self.run_op("complete_use", completion())
        self.assertEqual(first["receipt"], replay["receipt"])
        self.assertTrue(self.run_op("complete_use", completion(), "request:reuse")["receipt"]["result"]["reused"])
        self.assertEqual((counts, ledger), (self.counts(), self.ledger_counts()))
        self.failure("CONFLICT", lambda: self.run_op("complete_use", completion(material_id="material:another"), "request:other"))
        corrected = dict(completion(material_id="material:correction", text="Synthetic corrected observation."),
                         expected_feedback_id=first["receipt"]["result"]["feedback_id"])
        second = self.run_op("correct_feedback", corrected)
        self.assertTrue(self.run_op("correct_feedback", corrected, "request:reuse")["receipt"]["result"]["reused"])
        self.failure("CONFLICT", lambda: self.run_op("correct_feedback", dict(corrected, material=completion(material_id="material:changed")["material"]), "request:changed"))
        old = self.store.get("feedback", first["receipt"]["result"]["feedback_id"])["record"]
        self.assertEqual(old["latest_feedback_id"], second["receipt"]["result"]["feedback_id"])
        self.assertIsNone(old["predecessor_id"])
        self.assertEqual(self.counts()["usages"], 1)
        self.assertEqual(self.counts()["feedback"], 2)
        self.assertEqual(self.store.get("version", self.version)["record"]["state"], "ACTIVE")

    def test_feedback_uses_original_task_context_and_consumer_before_ledger_writes(self):
        self.run_op("start_use", adoption(self.version))
        ledger = self.ledger_counts()
        for field, value in (("task_ref", "task:other"), ("source_ref", "consumer:other"), ("context", {})):
            changed = completion()
            changed["material"][field] = value
            self.failure("CONFLICT", lambda: self.run_op("complete_use", changed))
            self.assertEqual(self.counts()["pending"], 0)
            self.assertEqual(self.ledger_counts(), ledger)

    def test_unlinked_result_is_material_with_explicit_gap_not_fabricated_usage(self):
        self.failure("NOT_FOUND", lambda: self.run_op("complete_use", completion()))
        body = completion()["material"]
        body["gaps"] = ["MISSING_USAGE_RECORD: usage:build; adoption was not registered"]
        self.run_op("publish_material", body, "request:unlinked")
        observed = self.store.get("material", "material:feedback")["record"]
        self.assertEqual(observed["gaps"], body["gaps"])
        self.assertEqual((self.counts()["usages"], self.counts()["feedback"]), (0, 0))
        self.assertTrue(self.store.read_material("material:feedback")["content_verified"])

    def test_stale_revision_acceptance_rolls_back_but_rejection_still_works(self):
        self.run_op("publish_material", material("material:new"), "request:new-material")
        proposed = self.revision(evidence="material:new")
        self.run_op("retire", retirement(self.version))
        counts = self.counts()
        self.failure("CONFLICT", lambda: self.run_op("review", review(proposed), "request:stale"))
        self.assertEqual(counts, self.counts())
        self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "PENDING")
        self.run_op("review", review(proposed, "REJECT"), "request:reject")
        self.assertEqual(self.store.get("candidate", proposed["candidate_id"])["record"]["state"], "REJECTED")

    def test_revision_requires_new_evidence_and_preserves_kind(self):
        self.failure("CONFLICT", lambda: self.revision(evidence="material:build"))
        self.run_op("publish_material", material("material:new"), "request:new")
        body = dict(proposal(), kind="different", expected_version_id=self.version, material_ids=["material:new"])
        self.failure("CONFLICT", lambda: self.run_op("propose_revision", body))

    def test_feedback_pending_survives_retirement_and_reserves_slot_across_request_ids(self):
        self.run_op("start_use", adoption(self.version))
        original = self.store.ledger.publish
        def fail_after_append(key, body):
            original(key, body)
            raise KnowledgeError("IO_ERROR", "Synthetic response lost", "unknown")
        with patch.object(self.store.ledger, "publish", side_effect=fail_after_append):
            failed = self.failure("IO_ERROR", lambda: self.run_op("complete_use", completion()))
            self.assertEqual(failed.commit_state, "unknown")
        counts = self.ledger_counts()
        self.failure("PENDING", lambda: self.run_op("complete_use", completion(material_id="material:other"), "request:other"))
        self.failure("CONFLICT", lambda: self.run_op("complete_use", completion(text="changed")))
        self.run_op("retire", retirement(self.version))
        self.store = open_store(self.root / "knowledge")
        pending = self.store.pending("example:operator", "request:one", operation="complete_use")
        self.assertEqual(pending["request"]["body"], completion())
        result = self.store.resume("example:operator", "request:one", operation="complete_use")
        self.assertEqual(result["observation"]["record"]["version_state"], "RETIRED")
        self.assertEqual(counts, self.ledger_counts())
        self.assertEqual((self.counts()["pending"], self.counts()["feedback_reservations"]), (0, 0))

    def test_damaged_feedback_metadata_cannot_become_revision_evidence(self):
        self.run_op("start_use", adoption(self.version))
        result = self.run_op("complete_use", completion())
        identity = result["receipt"]["result"]["feedback_id"]
        with sqlite3.connect(self.root / "knowledge/knowledge.sqlite") as db:
            raw = db.execute("SELECT document FROM feedback WHERE id=?", (identity,)).fetchone()[0]
            import json
            value = json.loads(raw)
            value["usage_id"] = "usage:another"
            db.execute("UPDATE feedback SET document=? WHERE id=?", (canonical(value).decode(), identity))
        self.failure("STORE_INVALID", lambda: self.store.read_material("material:feedback"))
        self.failure("STORE_INVALID", lambda: self.revision())
        replay = self.run_op("complete_use", completion())
        self.assertEqual(replay["receipt"], result["receipt"])
        self.assertEqual(replay["observation"]["record_status"], "UNAVAILABLE")

    def test_feedback_index_cannot_rebind_an_equivalent_completion_to_changed_metadata(self):
        self.run_op("start_use", adoption(self.version))
        first = self.run_op("complete_use", completion())
        changed = completion()
        changed["material"]["claimed_outcome"] = "SUCCEEDED"
        from domain_operational_knowledge_core.model import digest
        with sqlite3.connect(self.root / "knowledge/knowledge.sqlite") as db:
            db.execute("UPDATE feedback SET fingerprint=? WHERE id=?",
                       (digest(["example:operator", "complete_use", changed]), first["receipt"]["result"]["feedback_id"]))
        self.failure("STORE_INVALID", lambda: self.run_op("complete_use", changed, "request:changed"))
        self.assertEqual(self.counts()["feedback"], 1)
        observed = self.store.get_operation("example:operator", "complete_use", "request:one")
        self.assertEqual(observed["receipt"], first["receipt"])
        self.assertEqual(observed["observation"]["record_status"], "UNAVAILABLE")

    def test_recovery_operation_type_is_strict_and_does_not_write(self):
        before = self.counts(), self.ledger_counts()
        for operation in ([], {}, None, 1, "start_use"):
            for method in (self.store.pending, self.store.resume):
                self.failure("INVALID_INPUT", lambda: method("example:operator", "request:one", operation=operation))
        self.assertEqual(before, (self.counts(), self.ledger_counts()))
