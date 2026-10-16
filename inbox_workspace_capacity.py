#!/usr/bin/env python3
"""Report bounded storage pressure for a local LDA operator workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import time


SCHEMA = "lda-inbox-workspace-capacity-v1"
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_WARN_AT = 0.80
DEFAULT_MAX_ROWS = 250_000
DEFAULT_TIMEOUT = 10.0
CORE_COLUMNS = {
    "meta": ("key", "value"),
    "workspaces": ("id", "label", "created", "original"),
    "editions": ("workspace", "revision", "digest", "document", "action", "created"),
    "requests": ("id", "fingerprint", "result"),
}


class CapacityError(ValueError):
    """The selected workspace cannot be inspected safely."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _regular_size(path: Path) -> int:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise CapacityError("workspace database does not exist") from exc
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        raise CapacityError("workspace database must be a regular file, not a link")
    return details.st_size


def _columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))


def _bounded_count(db: sqlite3.Connection, table: str, maximum: int) -> int:
    value = int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    if value > maximum:
        raise CapacityError(f"{table} exceeds the {maximum}-row inspection limit")
    return value


def _bytes(db: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> int:
    expression = " + ".join(f"length(CAST(\"{column}\" AS BLOB))" for column in columns)
    return int(db.execute(f'SELECT coalesce(sum({expression}),0) FROM "{table}"').fetchone()[0])


def audit_database(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                   warn_at: float = DEFAULT_WARN_AT,
                   max_rows: int = DEFAULT_MAX_ROWS,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
    if type(max_bytes) is not int or max_bytes < 1:
        raise CapacityError("max_bytes must be a positive integer")
    if type(max_rows) is not int or max_rows < 1:
        raise CapacityError("max_rows must be a positive integer")
    if not isinstance(warn_at, (int, float)) or isinstance(warn_at, bool) or not 0 < warn_at <= 1:
        raise CapacityError("warn_at must be greater than zero and at most one")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise CapacityError("timeout must be positive")

    file_bytes = _regular_size(path)
    sidecars = {}
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            size = _regular_size(candidate)
            if size:
                sidecars[suffix[1:]] = size
    family_bytes = file_bytes + sum(sidecars.values())
    started = time.monotonic()
    deadline = started + timeout
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None) as db:
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        try:
            integrity = db.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise CapacityError("workspace database failed SQLite quick_check")
            for table, expected in CORE_COLUMNS.items():
                if _columns(db, table) != expected:
                    raise CapacityError(f"workspace table {table} has an unsupported schema")
            counts = {table: _bounded_count(db, table, max_rows) for table in CORE_COLUMNS}
            page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
            logical_bytes = page_size * page_count
            used_bytes = page_size * max(0, page_count - free_pages)
            payload_bytes = {
                "workspaces": _bytes(db, "workspaces", ("label", "original")),
                "editions": _bytes(db, "editions", ("digest", "document", "action")),
                "requests": _bytes(db, "requests", ("fingerprint", "result")),
            }
            max_editions = int(db.execute(
                "SELECT coalesce(max(total),0) FROM "
                "(SELECT count(*) AS total FROM editions GROUP BY workspace)"
            ).fetchone()[0])
            max_original = int(db.execute(
                "SELECT coalesce(max(length(CAST(original AS BLOB))),0) FROM workspaces"
            ).fetchone()[0])
            max_edition = int(db.execute(
                "SELECT coalesce(max(length(CAST(document AS BLOB))),0) FROM editions"
            ).fetchone()[0])
            scope = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
            if scope is None or not isinstance(scope[0], str):
                raise CapacityError("workspace scope is missing")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if time.monotonic() > deadline:
        raise CapacityError("workspace inspection exceeded its time limit")

    ratio = logical_bytes / max_bytes
    warnings = []
    if ratio >= 1:
        warnings.append("logical_size_at_or_above_limit")
    elif ratio >= warn_at:
        warnings.append("logical_size_approaching_limit")
    if page_count and free_pages / page_count >= 0.25:
        warnings.append("substantial_free_pages")
    if sidecars.get("wal", 0) > max_bytes * 0.25:
        warnings.append("large_write_ahead_log")

    report = {
        "schema": SCHEMA,
        "scope_sha256": hashlib.sha256(scope[0].encode("utf-8")).hexdigest(),
        "status": "pressure" if warnings else "ok",
        "limits": {"max_bytes": max_bytes, "warn_at": warn_at, "max_rows_per_table": max_rows},
        "storage": {
            "database_file_bytes": file_bytes,
            "sidecar_bytes": sidecars,
            "database_family_bytes": family_bytes,
            "logical_bytes": logical_bytes,
            "used_page_bytes": used_bytes,
            "free_page_bytes": page_size * free_pages,
            "logical_limit_ratio": round(ratio, 6),
        },
        "rows": counts,
        "payload_bytes": payload_bytes,
        "largest_retained_document_bytes": max(max_original, max_edition),
        "max_editions_per_workspace": max_editions,
        "warnings": warnings,
    }
    report["report_sha256"] = _digest(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="existing private operator SQLite database")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="logical database size ceiling")
    parser.add_argument("--warn-at", type=float, default=DEFAULT_WARN_AT,
                        help="fraction of max-bytes that reports pressure")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                        help="per-core-table inspection ceiling")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="maximum inspection time in seconds")
    args = parser.parse_args(argv)
    try:
        report = audit_database(args.db, max_bytes=args.max_bytes, warn_at=args.warn_at,
                                max_rows=args.max_rows, timeout=args.timeout)
        print(_canonical(report))
        return 2 if report["warnings"] else 0
    except (CapacityError, OSError, sqlite3.Error, TypeError, ValueError,
            OverflowError) as exc:
        print(f"Workspace capacity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
