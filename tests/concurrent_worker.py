"""Start real independent CLI writers together, without production hooks."""
from pathlib import Path
import sys
import time

from domain_operational_knowledge_core.cli import main

store, request, ready, release = sys.argv[1:]
Path(ready).write_text("ready")
deadline = time.monotonic() + 10
while not Path(release).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("Parent did not release concurrent calls")
    time.sleep(.005)
raise SystemExit(main(["--store", store, "write", "--request", request]))
