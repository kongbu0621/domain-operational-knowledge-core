"""Synthetic full lifecycle using public APIs; output directory must be new."""
import base64
import json
import os
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
        request = dict(schema="operational-knowledge-v1", scope="example:operator", operation=operation, request_id=key, body=body)
        raw = encode(request)
        fd = os.open(root / (key.split(":")[-1] + ".json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        return store.execute(raw)["receipt"]["result"]

    write("publish_material", "request:material", dict(material_id="material:build", source_ref="example:build-tool",
        task_ref="task:synthetic-build", context=dict(platform="linux"), claimed_outcome="FAILED", gaps=[],
        text="Synthetic build log: generated configuration was absent."))
    proposed = write("propose", "request:candidate", dict(proposal_id="proposal:configuration", kind="build-guidance",
        content="Check generated configuration before repeating this build.", conditions=dict(platform="linux"), material_ids=["material:build"]))
    assert not store.query("build-guidance", {"platform": "linux"})["versions"]

    def accept(candidate, key, evidence):
        return write("review", key, dict(candidate_id=candidate["candidate_id"], candidate_sha256=candidate["candidate_sha256"],
            decision="ACCEPT", reviewer="example:reviewer", reason="Explicit synthetic review for the stated context.",
            evidence_material_ids=[evidence]))["version_id"]

    original = accept(proposed, "request:review", "material:build")
    write("start_use", "request:usage", dict(usage_id="usage:build", consumer_ref="example:consumer", task_ref="task:synthetic-use",
        version_id=original, context=dict(platform="linux")))
    # No task is executed here. This string is explicitly synthetic feedback.
    text = "Synthetic outcome: configuration checks helped only for generated builds."
    feedback = write("complete_use", "request:feedback", dict(usage_id="usage:build", material=dict(material_id="material:feedback",
        source_ref="example:consumer", task_ref="task:synthetic-use", context=dict(platform="linux"), claimed_outcome="UNKNOWN",
        gaps=["Synthetic example; no real production task executed"], text=text)))
    content = store.read_material(feedback["material_id"])
    assert base64.b64decode(content["entries"][0]["data"]).decode() == text
    revision = write("propose_revision", "request:revision", dict(proposal_id="proposal:narrower-guidance", kind="build-guidance",
        content="For generated builds, check generated configuration before retrying.", conditions=dict(platform="linux", build="generated"),
        material_ids=[feedback["material_id"]], expected_version_id=original))
    current = accept(revision, "request:revision-review", feedback["material_id"])
    assert [v["version_id"] for v in store.query("build-guidance", {"platform": "linux", "build": "generated"})["versions"]] == [current]
    write("retire", "request:retirement", dict(expected_version_id=current, reviewer="example:reviewer",
        reason="Synthetic example ends this guidance; keep its history.", evidence_material_ids=[feedback["material_id"]]))
    reopened = open_store(root / "knowledge")
    usage = reopened.get("usage", "usage:build")["record"]
    assert usage["version_id"] == original and usage["version_state"] == "SUPERSEDED"
    assert reopened.get("version", current)["record"]["state"] == "RETIRED"
    assert not reopened.query("build-guidance", {"platform": "linux", "build": "generated"})["versions"]
    print(json.dumps(dict(synthetic=True, original_version=original, revised_version=current,
        usage_id=usage["usage_id"], feedback_id=feedback["feedback_id"], feedback_material=content["reference"],
        exact_feedback_verified=True, old_usage_retained=True, revised_version_retired=True, reopened=True), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
