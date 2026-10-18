"""One bounded JSON result per command; runtime data stays in caller storage."""
from __future__ import annotations

import argparse
import os
import stat
import sys

from . import initialize, open_store
from . import model as m


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise m.KnowledgeError("INVALID_INPUT", message)


def read_input(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        m.require(stat.S_ISREG(info.st_mode), "Input must be a regular file")
        m.require(info.st_size <= m.MAX_REQUEST, "Input exceeds 256 KiB", "LIMIT")
        raw = stream.read(m.MAX_REQUEST + 1)
        m.require(len(raw) <= m.MAX_REQUEST, "Input exceeds 256 KiB", "LIMIT")
        return raw


def parser():
    root = Parser(description="Operational knowledge in one trusted local data domain")
    root.add_argument("--store", required=True)
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--ledger-db", required=True)
    init.add_argument("--anchor-artifact", required=True)
    write = commands.add_parser("write")
    write.add_argument("--request", required=True)
    get = commands.add_parser("get")
    get.add_argument("--kind", required=True, choices=["material", "candidate", "review", "version"])
    get.add_argument("--id", required=True)
    query = commands.add_parser("query")
    query.add_argument("--kind", required=True)
    query.add_argument("--context", required=True)
    query.add_argument("--after", default="")
    query.add_argument("--limit", type=int, default=10)
    material = commands.add_parser("read-material")
    material.add_argument("--id", required=True)
    for name in ("operation", "pending", "resume"):
        child = commands.add_parser(name)
        child.add_argument("--scope", required=True)
        child.add_argument("--request-id", required=True)
        if name == "operation":
            child.add_argument("--operation", required=True, choices=sorted(m.OPERATIONS))
    return root


def _run(argv=None):
    command = None
    try:
        args = parser().parse_args(argv)
        command = args.command
        if args.command == "init":
            store = initialize(args.store, ledger_path=args.ledger_db, anchor_artifact_id=args.anchor_artifact)
            result = dict(store_id=store.database.identity["store_id"], schema=m.SCHEMA)
        else:
            store = open_store(args.store)
            if args.command == "write":
                result = store.execute(read_input(args.request))
            elif args.command == "get":
                result = store.get(args.kind, args.id)
            elif args.command == "query":
                result = store.query(args.kind, m.decode(read_input(args.context)), after=args.after, limit=args.limit)
            elif args.command == "read-material":
                result = store.read_material(args.id)
            elif args.command == "operation":
                result = store.get_operation(args.scope, args.operation, args.request_id)
            else:
                result = getattr(store, args.command)(args.scope, args.request_id)
        if args.command not in ("write", "resume"):
            result = dict(status="OK", commit_state="committed" if command == "init" else "not_applicable", data=result)
        sys.stdout.buffer.write(m.canonical(m.output(result)) + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except m.KnowledgeError as exc:
        if command in ("get", "query", "read-material", "operation", "pending"):
            exc = m.KnowledgeError(exc.code, exc.message, "not_applicable", details=exc.details)
        elif command in ("init", "write", "resume") and exc.commit_state == "not_applicable":
            exc = m.KnowledgeError(exc.code, exc.message, "not_committed", details=exc.details)
        try:
            result = m.output(exc.as_dict())
        except m.KnowledgeError:
            # Parser diagnostics can include arbitrarily many CLI arguments.
            # Preserve the known commit state when the error itself is too big.
            exc = m.KnowledgeError("LIMIT", "Error response exceeds 1 MiB; inspect the original request",
                                   exc.commit_state)
            result = exc.as_dict()
        sys.stdout.buffer.write(m.canonical(result) + b"\n")
        sys.stdout.buffer.flush()
        return {"NOT_FOUND": 3, "CONFLICT": 4, "IDENTITY_CONFLICT": 4, "BUSY": 6}.get(exc.code, 2)


def main(argv=None):
    try:
        return _run(argv)
    except (OSError, ValueError):
        # A failed output channel may provide no JSON. Never imply a rollback.
        # Prevent a second buffered flush at interpreter shutdown from
        # replacing this handled I/O exit with an unhandled exit code 120.
        try:
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        try:
            sys.stderr.write("I/O failure; inspect the original request before retrying.\n")
        except (OSError, ValueError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
