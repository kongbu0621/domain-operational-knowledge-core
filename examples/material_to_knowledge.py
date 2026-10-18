"""Synthetic independent consumer. Run with a new private output directory."""
import base64
import json
from pathlib import Path
import sys

from infra_artifact_ledger import initialize as initialize_ledger
from domain_operational_knowledge_core import initialize, open_store


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def main(path):
    root = Path(path).absolute()
    root.mkdir(mode=0o700)
    ledger_path = root / "materials.sqlite"
    anchor = "artifact:example-knowledge-domain"
    with initialize_ledger(ledger_path) as ledger:
        ledger.execute(encode(dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind="create_artifact",
            idempotency_scope_ref="example:domain", idempotency_key="create-anchor", body=dict(artifact=dict(
                artifact_id=anchor, namespace_ref="example:knowledge", type_ref=dict(namespace="example", type_id="domain-anchor", type_version="1"))))))
    store = initialize(root / "knowledge", ledger_path=ledger_path, anchor_artifact_id=anchor)

    def write(operation, key, body):
        return store.execute(encode(dict(schema="operational-knowledge-v1", scope="example:operator", operation=operation,
                                         request_id=key, body=body)))

    text = "Synthetic build failed because its generated configuration was absent.\n"
    material = write("publish_material", "request:material", dict(material_id="material:build", source_ref="example:build-tool",
        task_ref="task:synthetic-build", context=dict(platform="linux"), claimed_outcome="FAILED", gaps=[], text=text))
    candidate = write("propose", "request:candidate", dict(proposal_id="proposal:configuration", kind="build-guidance",
        content="For this build, check whether its generated configuration exists before retrying.",
        conditions=dict(platform="linux"), material_ids=["material:build"]))["receipt"]["result"]
    assert not store.query("build-guidance", dict(platform="linux"))["versions"]
    accepted = write("review", "request:review", dict(candidate_id=candidate["candidate_id"], candidate_sha256=candidate["candidate_sha256"],
        decision="ACCEPT", reviewer="example:reviewer", reason="Synthetic explanation reviewed for the stated build context.",
        evidence_material_ids=["material:build"]))
    reopened = open_store(root / "knowledge")
    result = reopened.query("build-guidance", dict(platform="linux"))
    actual = reopened.read_material("material:build")
    assert base64.b64decode(actual["entries"][0]["data"]) == text.encode()
    assert result["versions"][0]["version_id"] == accepted["receipt"]["result"]["version_id"]
    print(json.dumps(dict(knowledge_version=result["versions"][0]["version_id"], content=result["versions"][0]["content"],
                         material_reference=actual["reference"], exact_material_verified=True, reopened=True,
                         synthetic=True, usage_recorded=False), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
