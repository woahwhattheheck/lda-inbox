#!/usr/bin/env python3
"""Back up, inspect and restore a complete local LDA operator desk."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile

FORMAT = "lda-inbox-workspace-archive"
VERSION = 1
DATABASE_NAME = "operator.sqlite"
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MANIFEST_LIMIT = 64 * 1024
CHUNK = 1024 * 1024
COLUMNS = {
    "meta": ("key", "value"),
    "workspaces": ("id", "label", "created", "original"),
    "editions": ("workspace", "revision", "digest", "document", "action", "created"),
    "requests": ("id", "fingerprint", "result"),
}


class ArchiveError(ValueError):
    """The selected archive or destination cannot be used."""


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError("Archive metadata contains a duplicate field")
        result[key] = value
    return result


def _decode(raw: bytes):
    def reject(_):
        raise ArchiveError("Archive metadata contains a non-finite number")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=reject)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ArchiveError("Archive metadata is not valid UTF-8 JSON") from exc


def _limit(value: int) -> int:
    if type(value) is not int or value < 4096:
        raise ArchiveError("max_bytes must be an integer of at least 4096")
    return value


def _timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ArchiveError("timeout must be finite and positive")
    return value


def _readonly(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ArchiveError("Select a regular database file")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout,
                         isolation_level=None)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _summary(path: Path, timeout: float = 30.0) -> dict:
    """Inspect the retained desk without importing source code or executing SQL dumps."""
    deadline = time.monotonic() + timeout
    with closing(_readonly(path, timeout)) as db:
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        objects = db.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        if any(kind not in {"table", "index"} or
               (sql and "CREATE VIRTUAL TABLE" in sql.upper())
               for kind, _, sql in objects):
            raise ArchiveError("This archive version supports ordinary tables and indexes only")
        tables = sorted(name for kind, name, _ in objects if kind == "table")
        for name, columns in COLUMNS.items():
            if name not in tables:
                raise ArchiveError(f"The database is missing the operator {name} table")
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{name}")'))
            if actual != columns:
                raise ArchiveError(f"The {name} table uses a different operator schema")
        if db.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ArchiveError("SQLite reported a damaged database")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ArchiveError("The database has a broken table reference")
        scope = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
        if not scope or not isinstance(scope[0], str) or not re.fullmatch(r"[0-9a-f]{32}", scope[0]):
            raise ArchiveError("The database has no valid operator workspace scope")
        if db.execute("SELECT 1 FROM workspaces w WHERE NOT EXISTS "
                      "(SELECT 1 FROM editions e WHERE e.workspace=w.id) LIMIT 1").fetchone():
            raise ArchiveError("A workspace has no retained edition")
        counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                  for name in ("workspaces", "editions", "requests")}
        return {"scope": scope[0], **counts, "tables": tables,
                "schema_sha256": hashlib.sha256(_json(objects)).hexdigest()}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _destination(path: Path, database: bool = False) -> Path:
    path = path.absolute()
    paths = [path] + ([Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
                      if database else [])
    if any(os.path.lexists(item) for item in paths):
        raise ArchiveError("Choose a new destination; an existing file or database sidecar is present")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _publish(source: Path, destination: Path, database: bool = False) -> None:
    _destination(destination, database)
    with source.open("rb") as stream:
        os.fsync(stream.fileno())
    # Both paths are on the same filesystem. A competing destination is never replaced.
    os.link(source, destination)


def backup(database: Path, archive: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
           timeout: float = 30.0) -> dict:
    """Capture a consistent full desk, including committed WAL contents."""
    _limit(max_bytes)
    _timeout(timeout)
    archive = _destination(archive)
    with tempfile.TemporaryDirectory(prefix=".lda-backup-", dir=archive.parent) as temporary:
        root = Path(temporary)
        snapshot = root / DATABASE_NAME
        deadline = time.monotonic() + timeout
        with closing(_readonly(database, timeout)) as source, closing(sqlite3.connect(snapshot)) as target:
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            def progress(_status, _remaining, total):
                if total * page_size > max_bytes:
                    raise ArchiveError("The database exceeds max_bytes")
                if time.monotonic() > deadline:
                    raise ArchiveError("The database stayed busy beyond the backup timeout")
            source.backup(target, pages=256, progress=progress, sleep=0.1)
        os.chmod(snapshot, 0o600)
        size = snapshot.stat().st_size
        if size > max_bytes:
            raise ArchiveError("The database exceeds max_bytes")
        summary = _summary(snapshot, timeout)
        manifest = {
            "format": FORMAT, "version": VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "database": {"name": DATABASE_NAME, "bytes": size, "sha256": _sha(snapshot)},
            "desk": summary,
        }
        payload = _json(manifest)
        if len(payload) > MANIFEST_LIMIT:
            raise ArchiveError("The database has too much schema metadata for this archive version")
        staged = root / "archive.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            output.writestr(MANIFEST_NAME, payload)
            output.write(snapshot, DATABASE_NAME)
        os.chmod(staged, 0o600)
        _publish(staged, archive)
        return manifest


def _receive(archive: Path, snapshot: Path, *, max_bytes: int, timeout: float) -> dict:
    _limit(max_bytes)
    _timeout(timeout)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) != 2 or {item.filename for item in entries} != {MANIFEST_NAME, DATABASE_NAME}:
            raise ArchiveError("Select a complete workspace archive with exactly its two expected files")
        for item in entries:
            mode = item.external_attr >> 16
            if item.flag_bits & 1 or item.is_dir() or stat.S_ISLNK(mode):
                raise ArchiveError("Encrypted entries, directories and symbolic links are unsupported")
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise ArchiveError("The archive contains a special file")
            if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ArchiveError("Use a stored or deflated workspace archive")
        if source.getinfo(MANIFEST_NAME).file_size > MANIFEST_LIMIT:
            raise ArchiveError("Archive metadata exceeds the size limit")
        manifest = _decode(source.read(MANIFEST_NAME))
        if not isinstance(manifest, dict) or set(manifest) != {"format", "version", "created", "database", "desk"}:
            raise ArchiveError("Archive metadata has an unsupported shape")
        if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
            raise ArchiveError("Unsupported workspace archive version")
        expected = manifest["database"]
        if not isinstance(expected, dict) or set(expected) != {"name", "bytes", "sha256"}:
            raise ArchiveError("Archive database metadata is incomplete")
        size = expected["bytes"]
        if expected["name"] != DATABASE_NAME or type(size) is not int or not 4096 <= size <= max_bytes:
            raise ArchiveError("The archived database name or size is invalid")
        if not isinstance(expected["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]):
            raise ArchiveError("The archived database digest is invalid")
        if source.getinfo(DATABASE_NAME).file_size != size:
            raise ArchiveError("The archived database size does not match its metadata")
        digest = hashlib.sha256()
        count = 0
        deadline = time.monotonic() + timeout
        with source.open(DATABASE_NAME) as data, snapshot.open("xb") as output:
            os.chmod(snapshot, 0o600)
            while block := data.read(CHUNK):
                count += len(block)
                if count > size or time.monotonic() > deadline:
                    raise ArchiveError("The archived database exceeds its size or receiving time limit")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if count != size or digest.hexdigest() != expected["sha256"]:
            raise ArchiveError("The archived database bytes do not match their recorded digest")
    actual = _summary(snapshot, timeout)
    if _json(actual) != _json(manifest["desk"]):
        raise ArchiveError("The archived workspace summary does not match the complete database")
    return manifest


def inspect_archive(archive: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                    timeout: float = 30.0) -> dict:
    """Read the complete archive and report its retained workspace counts."""
    with tempfile.TemporaryDirectory(prefix="lda-inspect-") as temporary:
        return _receive(archive, Path(temporary) / DATABASE_NAME,
                        max_bytes=max_bytes, timeout=timeout)


def restore(archive: Path, database: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
            timeout: float = 30.0) -> dict:
    """Restore exact archived database bytes at a new, never-overwritten path."""
    database = _destination(database, database=True)
    with tempfile.TemporaryDirectory(prefix=".lda-restore-", dir=database.parent) as temporary:
        snapshot = Path(temporary) / DATABASE_NAME
        manifest = _receive(archive, snapshot, max_bytes=max_bytes, timeout=timeout)
        _publish(snapshot, database, database=True)
        return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "inspect", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        if name != "inspect":
            command.add_argument("--db", type=Path, required=True)
        command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
        command.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        _timeout(args.timeout)
        options = {"max_bytes": args.max_bytes, "timeout": args.timeout}
        if args.command == "backup":
            result = backup(args.db, args.archive, **options)
        elif args.command == "restore":
            result = restore(args.archive, args.db, **options)
        else:
            result = inspect_archive(args.archive, **options)
        sys.stdout.buffer.write(_json({"operation": args.command, **result}))
        return 0
    except (ArchiveError, OSError, sqlite3.Error, zipfile.BadZipFile,
            RuntimeError, TypeError, ValueError, OverflowError) as exc:
        print(f"Workspace archive: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
