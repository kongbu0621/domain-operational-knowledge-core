"""Test-only process stop before or after schema-upgrade commit."""
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from domain_operational_knowledge_core import open_store
from domain_operational_knowledge_core.database import install_lifecycle

store_path, checkpoint, marker = sys.argv[1:]


def stop():
    Path(marker).write_text("ready")
    signal.raise_signal(signal.SIGSTOP)
    raise RuntimeError("Parent must kill the test process")


store = open_store(store_path)
if checkpoint == "before_commit":
    def hook(db):
        install_lifecycle(db)
        stop()
    with patch("domain_operational_knowledge_core.service.install_lifecycle", hook):
        store.upgrade()
else:
    store.upgrade()
    stop()
