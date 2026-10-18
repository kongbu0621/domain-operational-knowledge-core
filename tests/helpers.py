"""Synthetic independent requests. No P or Local Hand imports or running services."""
import json

from infra_artifact_ledger import initialize as initialize_ledger
from domain_operational_knowledge_core import initialize


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def setup(root):
    ledger = root / "materials.sqlite"
    with initialize_ledger(ledger) as handle:
        handle.execute(encode(dict(profile="bounded-local-v0.1", contract_version="0.1.0", operation_kind="create_artifact",
            idempotency_scope_ref="example:anchor", idempotency_key="anchor", body=dict(artifact=dict(
                artifact_id="artifact:example-anchor", namespace_ref="synthetic:knowledge",
                type_ref=dict(namespace="example", type_id="store-anchor", type_version="1"))))))
    return initialize(root / "knowledge", ledger_path=ledger, anchor_artifact_id="artifact:example-anchor")


def request(operation, body, key="request:one", scope="example:operator"):
    return dict(schema="operational-knowledge-v1", scope=scope, operation=operation, request_id=key, body=body)


def material(identity="material:build", text="Synthetic build failed: missing generated configuration.\n"):
    return dict(material_id=identity, source_ref="example:build-tool", task_ref="task:synthetic-build",
                context=dict(platform="linux", language="python"), claimed_outcome="FAILED", gaps=[], text=text)


def proposal(identity="proposal:configuration"):
    return dict(proposal_id=identity, kind="build-guidance", content="Check generated configuration before repeating this build.",
                conditions=dict(platform="linux"), material_ids=["material:build"])


def review(candidate, decision="ACCEPT"):
    return dict(candidate_id=candidate["candidate_id"], candidate_sha256=candidate["candidate_sha256"],
                decision=decision, reviewer="example:reviewer", reason="Synthetic example reviewed for its stated scope.",
                evidence_material_ids=["material:build"])


def candidate(store, identity="proposal:configuration"):
    return store.execute(encode(request("propose", proposal(identity), "request:" + identity)))["receipt"]["result"]
