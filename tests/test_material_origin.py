"""A valid replacement payload cannot take over an existing material identity."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from domain_operational_knowledge_core import KnowledgeError
from domain_operational_knowledge_core.model import canonical, digest
from helpers import setup, request, material, candidate, review, proposal, encode


class MaterialOriginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = setup(self.root)
        self.path = self.root / "knowledge/knowledge.sqlite"
        self.original = self.store.execute(encode(request("publish_material", material())))
        self.document = self.original["observation"]["record"]

    def tearDown(self):
        self.temp.cleanup()

    def replace(self, document, identity="material:build"):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE materials SET document=? WHERE id=?", (canonical(document).decode(), identity))

    def refused(self, call):
        before = self.path.read_bytes(), (self.root / "materials.sqlite").read_bytes()
        with self.assertRaises(KnowledgeError) as caught:
            call()
        self.assertEqual(caught.exception.code, "STORE_INVALID")
        self.assertEqual(before, (self.path.read_bytes(), (self.root / "materials.sqlite").read_bytes()))

    def test_changed_published_metadata_cannot_drive_reads_admission_or_new_reuse(self):
        proposed = candidate(self.store)
        for name, value in (("source_ref", "example:different-source"), ("context", {"platform": "other"}),
                            ("claimed_outcome", "SUCCEEDED"), ("gaps", ["Different gap"]), ("scope", "other:scope")):
            with self.subTest(field=name):
                self.replace(dict(self.document, **{name: value}))
                for call in (lambda: self.store.read_material("material:build"),
                             lambda: self.store.get("material", "material:build"),
                             lambda: self.store.execute(encode(request("review", review(proposed)))),
                             lambda: self.store.execute(encode(request("propose", proposal("proposal:second"), "request:second"))),
                             lambda: self.store.execute(encode(request("publish_material", material(), "request:reuse")))):
                    self.refused(call)
                replay = self.store.execute(encode(request("publish_material", material())))
                self.assertEqual(replay["receipt"], self.original["receipt"])
                self.assertEqual(replay["observation"]["record_status"], "UNAVAILABLE")
        self.replace(self.document)
        self.assertEqual(self.store.execute(encode(request("review", review(proposed))))["status"], "COMMITTED")

    def test_other_valid_version_is_rejected_even_when_bytes_and_task_match(self):
        for text in (material()["text"], "A different valid document"):
            with self.subTest(same_bytes=text == material()["text"]):
                other = self.store.execute(encode(request("publish_material", material("material:" + digest(text), text), "request:" + digest(text))))
                replacement = dict(self.document, reference=other["observation"]["record"]["reference"])
                self.replace(replacement)
                self.refused(lambda: self.store.read_material("material:build"))
                self.refused(lambda: self.store.execute(encode(request("publish_material", material(), "request:reuse"))))
                self.replace(self.document)

    def test_registered_material_checks_original_metadata_and_reference_without_payload_io(self):
        body = {key: value for key, value in material("material:registered").items() if key != "text"}
        body["reference"] = self.document["reference"]
        registered = self.store.execute(encode(request("register_material", body)))
        other = self.store.execute(encode(request("publish_material", material("material:other"), "request:other")))
        for field, value in (("claimed_outcome", "SUCCEEDED"), ("reference", other["observation"]["record"]["reference"])):
            self.replace(dict(registered["observation"]["record"], **{field: value}), "material:registered")
            with patch.object(self.store.ledger, "read", side_effect=AssertionError("Should fail before payload reads")):
                self.refused(lambda: self.store.read_material("material:registered"))
                self.refused(lambda: self.store.execute(encode(request("register_material", body, "request:reuse"))))

    def test_query_rechecks_materials_of_previously_accepted_version(self):
        proposed = candidate(self.store)
        accepted = self.store.execute(encode(request("review", review(proposed))))
        self.replace(dict(self.document, claimed_outcome="SUCCEEDED"))
        self.refused(lambda: self.store.query("build-guidance", {"platform": "linux"}))
        observed = self.store.get_operation("example:operator", "review", "request:one")
        self.assertEqual(observed["receipt"], accepted["receipt"])
        self.assertEqual(observed["observation"]["record_status"], "UNAVAILABLE")

    def test_missing_origin_fails_but_unrelated_bad_receipt_does_not_block_read(self):
        self.store.execute(encode(request("publish_material", material("material:other"), "request:other")))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE receipts SET document='broken' WHERE json_extract(document,'$.request_id')='request:other'")
        self.assertTrue(self.store.read_material("material:build")["content_verified"])
        self.refused(lambda: self.store.read_material("material:other"))
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM receipts")
        self.refused(lambda: self.store.read_material("material:build"))

    def test_recomputed_material_digest_cannot_replace_the_creating_request(self):
        changed_body = dict(material(), claimed_outcome="SUCCEEDED")
        self.replace(dict(self.document, claimed_outcome="SUCCEEDED"))
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE materials SET fingerprint=? WHERE id='material:build'",
                       (digest(["example:operator", "publish_material", changed_body]),))
        self.refused(lambda: self.store.read_material("material:build"))
        self.refused(lambda: self.store.execute(encode(request("publish_material", changed_body, "request:reuse"))))
        original = self.store.get_operation("example:operator", "publish_material", "request:one")
        self.assertEqual(original["receipt"], self.original["receipt"])
        self.assertEqual(original["observation"]["record_status"], "UNAVAILABLE")

    def test_missing_payload_keeps_original_receipt_and_allows_metadata_verified_reuse(self):
        with patch.object(self.store.ledger, "read", side_effect=KnowledgeError("MATERIAL_UNAVAILABLE", "Synthetic missing payload",
                "not_applicable", details={"ledger_code": "NOT_FOUND"})):
            old = self.store.get_operation("example:operator", "publish_material", "request:one")
            self.assertEqual(old["receipt"], self.original["receipt"])
            self.assertEqual(old["observation"]["materials"][0]["status"], "UNAVAILABLE")
            reused = self.store.execute(encode(request("publish_material", material(), "request:reuse")))
            self.assertTrue(reused["receipt"]["result"]["reused"])
            self.assertEqual(reused["observation"]["materials"][0]["status"], "UNAVAILABLE")
        self.assertTrue(self.store.read_material("material:build")["content_verified"])
