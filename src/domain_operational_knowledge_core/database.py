"""Private local SQLite transactions. Never initializes a missing existing store."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from . import model as m
from . import records

APP_ID = 0x4B4E5731
MAX_DATABASE = 256 * 1024 * 1024
SQL = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE materials (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE candidates (id TEXT PRIMARY KEY, scope TEXT NOT NULL, proposal_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, document TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('PENDING','ACCEPTED','REJECTED')),
 UNIQUE(scope,proposal_id));
CREATE TABLE reviews (id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
 fingerprint TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE versions (id TEXT PRIMARY KEY, lineage_id TEXT NOT NULL, candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
 kind TEXT NOT NULL, conditions TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ACTIVE','SUPERSEDED','RETIRED')),
 document TEXT NOT NULL);
CREATE UNIQUE INDEX one_active_version ON versions(lineage_id) WHERE state='ACTIVE';
CREATE INDEX query_versions ON versions(state,kind,id);
CREATE TABLE receipts (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE pending (key TEXT PRIMARY KEY, material_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, request TEXT NOT NULL);
CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, receipt TEXT NOT NULL);
CREATE TABLE failures (sequence INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, document TEXT NOT NULL);
"""


def regular(path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        if missing_ok:
            return
        raise m.KnowledgeError("STORE_INVALID", "Existing store file is missing", "not_applicable") from exc
    m.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
              and info.st_mode & 0o077 == 0, "Expected an owned private regular file", "STORE_INVALID")


def durable_file(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def initialize(path, binding):
    root = Path(path).absolute()
    m.require(not root.exists() and not root.is_symlink(), "Store target already exists", "CONFLICT")
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise m.KnowledgeError("CONFLICT", "Store target already exists") from exc
    except OSError as exc:
        raise m.KnowledgeError("IO_ERROR", "Store directory cannot be created") from exc
    identity = dict(schema=m.SCHEMA, store_id=str(uuid.uuid4()), ledger=binding)
    database = root / "knowledge.sqlite"
    try:
        durable_file(database, b"")
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("BEGIN IMMEDIATE;" + SQL)
            db.execute("INSERT INTO metadata VALUES ('identity',?)", (m.canonical(identity).decode(),))
            db.execute(f"PRAGMA application_id={APP_ID}")
            db.execute("PRAGMA user_version=1")
        durable_file(root / "identity.json", m.canonical(identity))
        fd = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        parent_fd = os.open(root.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except (sqlite3.Error, OSError) as exc:
        raise m.KnowledgeError("IO_ERROR", "Initialization incomplete; retain directory for inspection", "unknown") from exc
    try:
        return Database(root)
    except m.KnowledgeError as exc:
        raise m.KnowledgeError(exc.code, "Store initialized; reopening verification failed", "committed",
                               details=dict(store_id=identity["store_id"])) from exc


class Database:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.path = self.root / "knowledge.sqlite"
        try:
            info = self.root.lstat()
            m.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                      "Store must be an owned private directory", "STORE_INVALID")
            regular(self.root / "identity.json")
            m.require((self.root / "identity.json").stat().st_size <= m.MAX_REQUEST,
                      "Store identity exceeds its limit", "STORE_INVALID")
            raw = (self.root / "identity.json").read_bytes()
            self.identity = m.decode(raw)
            m.fields(self.identity, "schema store_id ledger")
            m.require(self.identity["schema"] == m.SCHEMA, "Unsupported store schema", "UNSUPPORTED_VERSION")
            m.require(str(uuid.UUID(self.identity["store_id"])) == self.identity["store_id"],
                      "Invalid store ID", "STORE_INVALID")
            m.fields(self.identity["ledger"], "path anchor profile contract_version")
            m.require(type(self.identity["ledger"]["path"]) is str and Path(self.identity["ledger"]["path"]).is_absolute()
                      and self.identity["ledger"]["profile"] == "bounded-local-v0.1"
                      and self.identity["ledger"]["contract_version"] == "0.1.0"
                      and type(self.identity["ledger"]["anchor"]) is dict,
                      "Invalid Ledger binding", "STORE_INVALID")
            self.identity_bytes = raw
            with self.connection() as db:
                m.require(db.execute("pragma quick_check").fetchone()[0] == "ok", "Database is damaged", "STORE_INVALID")
        except m.KnowledgeError as exc:
            if exc.code in ("INVALID_INPUT", "LIMIT"):
                raise m.KnowledgeError("STORE_INVALID", "Stored identity cannot be decoded", "not_applicable") from exc
            raise
        except FileNotFoundError as exc:
            raise m.KnowledgeError("STORE_INVALID", "Existing store files are missing", "not_applicable") from exc
        except OSError as exc:
            raise m.KnowledgeError("IO_ERROR", "Existing store files cannot be read", "not_applicable") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise m.KnowledgeError("STORE_INVALID", "Stored identity cannot be decoded", "not_applicable") from exc

    def _connect_checked(self, mode):
        # SQLite can recover a hot journal before identity verification on a
        # write-capable connection. Guard every connection, including
        # writes from long-lived handles, before SQLite can access the sidecar.
        # A concurrent commit may have removed it; absence is normal.
        regular(self.path.with_name(self.path.name + "-journal"), missing_ok=True)
        db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode,
                             uri=True, isolation_level=None, timeout=1)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            m.require(db.execute("PRAGMA application_id").fetchone()[0] == APP_ID
                      and db.execute("PRAGMA user_version").fetchone()[0] == 1,
                      "Database format differs", "STORE_INVALID")
            saved = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            m.require(saved is not None and json.loads(saved[0]) == self.identity,
                      "Database identity differs", "STORE_INVALID")
            return db
        except BaseException:
            db.close()
            raise

    @contextmanager
    def connection(self, *, write=False):
        db = None
        committing = False
        try:
            regular(self.path)
            regular(self.root / "identity.json")
            m.require((self.root / "identity.json").read_bytes() == self.identity_bytes,
                      "Store identity changed", "STORE_INVALID")
            try:
                db = self._connect_checked("rw" if write else "ro")
            except sqlite3.Error as exc:
                if write or getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise
                # SQLite found an interrupted transaction whose pages must be
                # rolled back before any reads. Retry once on this same existing
                # store; SQLite owns recovery and locking, never delete journals.
                regular(self.path)
                regular(self.root / "identity.json")
                m.require((self.root / "identity.json").read_bytes() == self.identity_bytes,
                          "Store identity changed", "STORE_INVALID")
                db = self._connect_checked("rw")
            if write:
                pages = MAX_DATABASE // db.execute("PRAGMA page_size").fetchone()[0]
                db.execute(f"PRAGMA max_page_count={pages}")
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
            yield db
            if write:
                committing = True
                db.commit()
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 255
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                name = "BUSY"
            elif code == sqlite3.SQLITE_FULL:
                name = "LIMIT"
            elif code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR):
                name = "IO_ERROR"
            else:
                name = "STORE_INVALID"
            raise m.KnowledgeError(name, "Local database operation failed",
                                   "unknown" if committing else ("not_committed" if write else "not_applicable")) from exc
        except OSError as exc:
            raise m.KnowledgeError("IO_ERROR", "Store file cannot be accessed",
                                   "unknown" if committing else ("not_committed" if write else "not_applicable")) from exc
        except (ValueError, TypeError) as exc:
            raise m.KnowledgeError("STORE_INVALID", "Stored metadata cannot be decoded",
                                   "not_committed" if write else "not_applicable") from exc
        finally:
            if db is not None:
                db.close()

    document = staticmethod(records.document)
    pending = staticmethod(records.pending)

    def receipt(self, db, key):
        return records.receipt(db, key, self.identity["store_id"])
