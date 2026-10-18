"""Test-only kill boundaries; no production environment variable or CLI hooks."""
import contextlib
import json
from pathlib import Path
import signal
import sys
import time

from domain_operational_knowledge_core import open_store
from domain_operational_knowledge_core.ledger import MaterialLedger
from domain_operational_knowledge_core.service import Knowledge

store_path, request_path, checkpoint, marker = sys.argv[1:]


def stop():
    Path(marker).write_text("ready")
    signal.raise_signal(signal.SIGSTOP)
    raise RuntimeError("Parent must kill the stopped test process")


if checkpoint in ("after_create", "after_append"):
    original = MaterialLedger._ensure_operation

    def hook(self, handle, request, payloads=None):
        result = original(self, handle, request, payloads)
        kind = "create_artifact" if checkpoint == "after_create" else "append_version"
        if request["operation_kind"] == kind:
            stop()
        return result

    MaterialLedger._ensure_operation = hook
elif checkpoint == "before_local_commit":
    original = Knowledge._commit

    def hook(self, *args):
        original(self, *args)
        stop()

    Knowledge._commit = hook
elif checkpoint == "after_local_commit":
    def hook(self, receipt):
        stop()
    Knowledge._observation = hook
else:
    raise ValueError(checkpoint)

open_store(store_path).execute(Path(request_path).read_bytes())
