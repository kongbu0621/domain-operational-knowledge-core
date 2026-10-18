"""Bounded material adapter using only the Ledger package's public API."""
from __future__ import annotations

import hashlib
from pathlib import Path

from infra_artifact_ledger import LedgerError, open as open_ledger

from . import model as m


def capture_binding(path, anchor_artifact_id):
    m.text(anchor_artifact_id, maximum=256)
    path = Path(path)
    m.require("\x00" not in str(path), "Ledger path must not contain NUL")
    try:
        path = str(path.resolve(strict=True))
        with open_ledger(path) as handle:
            anchor = handle.get_record("artifact", anchor_artifact_id)
    except LedgerError as exc:
        raise error(exc) from exc
    except OSError as exc:
        raise m.KnowledgeError("IO_ERROR", "Existing Ledger cannot be opened") from exc
    return dict(path=path, anchor=anchor, profile="bounded-local-v0.1", contract_version="0.1.0")


def error(exc):
    return m.KnowledgeError("MATERIAL_UNAVAILABLE", "Ledger material could not be verified", "not_applicable",
                            details=dict(ledger_code=exc.code, ledger_commit_state=exc.commit_state))


class MaterialLedger:
    def __init__(self, binding, store_id):
        self.binding, self.store_id = binding, store_id

    def _bound(self, handle):
        m.require(handle.get_record("artifact", self.binding["anchor"]["artifact_id"]) == self.binding["anchor"],
                  "Configured Ledger anchor differs", "IDENTITY_CONFLICT")

    def read(self, reference, task_ref):
        """Validate full bounded closure, expected bytes and declared task source."""
        m.reference(reference)
        try:
            with open_ledger(self.binding["path"]) as handle:
                self._bound(handle)
                artifact = handle.get_record("artifact", reference["artifact_id"])
                version = handle.get_record("version", reference["version_id"])
                m.require(version["artifact_id"] == artifact["artifact_id"] == reference["artifact_id"],
                          "Artifact and version are unrelated", "IDENTITY_CONFLICT")
                m.require(task_ref in version["external_source_refs"],
                          "Version does not declare the specified task reference", "IDENTITY_CONFLICT")
                root = handle.get_record("content_root", version["content_root_ref"])
                if root["kind"] == "blob":
                    members = [dict(entry_key="@blob", blob_ref=root["blob_ref"])]
                else:
                    manifest = handle.get_record("manifest", root["manifest_ref"])
                    m.require(manifest["digest"] == dict(algorithm="sha256", value=m.digest(dict(entries=manifest["entries"]))),
                              "Manifest digest differs", "INTEGRITY_FAILURE")
                    members = manifest["entries"]
                m.require(members == [{key: item[key] for key in ("entry_key", "blob_ref")}
                                      for item in reference["entries"]],
                          "Expected entries differ from the exact version", "IDENTITY_CONFLICT")
                contents = {}
                for item in reference["entries"]:
                    metadata = handle.get_record("blob", item["blob_ref"])
                    m.require(metadata["byte_length"] == item["byte_length"] and metadata["digest"] ==
                              dict(algorithm="sha256", value=item["sha256"]),
                              "Expected material descriptor differs", "INTEGRITY_FAILURE")
                    if item["blob_ref"] not in contents:
                        raw = handle.read_blob(item["blob_ref"])
                        m.require(len(raw) == item["byte_length"] and hashlib.sha256(raw).hexdigest() == item["sha256"],
                                  "Material bytes differ", "INTEGRITY_FAILURE")
                        contents[item["blob_ref"]] = raw
                return [(item["entry_key"], contents[item["blob_ref"]]) for item in reference["entries"]]
        except LedgerError as exc:
            raise error(exc) from exc

    def requests(self, key, body):
        """Stable IDs and complete original Ledger requests, deterministic on restart."""
        prefix = "k:" + self.store_id + ":" + key
        artifact, version, root, blob = [prefix + ":" + suffix for suffix in ("artifact", "version", "root", "blob")]
        scope = "knowledge-material:" + self.store_id
        raw = body["text"].encode("utf-8")

        def request(kind, payload):
            return dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind=kind,
                        idempotency_scope_ref=scope, idempotency_key=key, body=payload)

        create = request("create_artifact", dict(artifact=dict(artifact_id=artifact, namespace_ref=scope,
                         type_ref=dict(namespace="operational-knowledge", type_id="run-material", type_version="1"))))
        descriptor = dict(blob_ref=blob, digest=dict(algorithm="sha256", value=hashlib.sha256(raw).hexdigest()),
                          byte_length=len(raw), payload_availability="available")
        append = request("append_version", dict(version=dict(version_id=version, artifact_id=artifact, content_root_ref=root,
                         parent_version_refs=[], external_source_refs=[body["task_ref"]], capture_refs=[], handling_policy_refs=[]),
                         content_roots=[dict(content_root_ref=root, kind="blob", blob_ref=blob)], blobs=[descriptor],
                         manifests=[], provenance_links=[]))
        reference = dict(artifact_id=artifact, version_id=version, entries=[dict(entry_key="@blob", blob_ref=blob,
                         sha256=descriptor["digest"]["value"], byte_length=len(raw))])
        return create, append, {blob: raw}, reference

    @staticmethod
    def _fingerprint(request):
        return m.digest({key: request[key] for key in ("contract_version", "idempotency_scope_ref", "operation_kind", "body")})

    def _ensure_operation(self, handle, request, payloads=None):
        def observed():
            result = handle.get_operation(request["idempotency_scope_ref"], request["operation_kind"], request["idempotency_key"])
            record = result["idempotency_record"]
            m.require(record["request_fingerprint"] == dict(algorithm="sha256", value=self._fingerprint(request)),
                      "Original Ledger operation fingerprint differs", "IDENTITY_CONFLICT")
            kind = "artifact" if request["operation_kind"] == "create_artifact" else "version"
            expected = request["body"][kind]
            m.require(all(result["result"].get(key) == value for key, value in expected.items())
                      and record["result_ref"] == expected[kind + "_id"],
                      "Original Ledger operation result differs", "IDENTITY_CONFLICT")
            return result

        try:
            observed()
        except LedgerError as exc:
            if exc.code != "NOT_FOUND":
                raise
            # Both concurrent and interrupted in-process callers use the same
            # original idempotency key/body; Ledger serializes their commits.
            handle.execute(m.canonical(request), **({"payloads": payloads} if payloads is not None else {}))
            observed()

    def publish(self, key, body):
        create, append, payloads, reference = self.requests(key, body)
        try:
            with open_ledger(self.binding["path"]) as handle:
                self._bound(handle)
                self._ensure_operation(handle, create)
                self._ensure_operation(handle, append, payloads)
            self.read(reference, body["task_ref"])
            return reference
        except LedgerError as exc:
            raise error(exc) from exc
