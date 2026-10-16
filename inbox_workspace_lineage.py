#!/usr/bin/env python3
"""Compare two retained LDA operator desks without exposing their contents."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


SCHEMA = "lda-inbox-workspace-lineage/v1"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CORE_COLUMNS = {
    "meta": ("key", "value"),
    "workspaces": ("id", "label", "created", "original"),
    "editions": ("workspace", "revision", "digest", "document", "action", "created"),
    "requests": ("id", "fingerprint", "result"),
}
KNOWN_ACTIONS = {"import", "claim", "renew", "release", "complete"}


class LineageError(ValueError):
    """A selected desk cannot be safely compared."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _limit(value: int) -> int:
    if type(value) is not int or value < 1:
        raise LineageError("max_rows must be a positive integer")
    return value


def _timeout(value: float) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value <= 0):
        raise LineageError("timeout must be finite and positive")
    return float(value)


def _readonly(path: Path, timeout: float) -> sqlite3.Connection:
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise LineageError("Select an existing operator database") from exc
    if not path.is_file() or os.path.islink(path):
        raise LineageError("Select a regular operator database file")
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                             timeout=timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        return db
    except sqlite3.Error as exc:
        raise LineageError("Unable to open an operator database read-only") from exc


def _subject(scope: str, kind: str, identifier: str) -> str:
    return hashlib.sha256(
        f"{SCHEMA}\0{scope}\0{kind}\0{identifier}".encode("utf-8")
    ).hexdigest()


def _schema(db: sqlite3.Connection) -> None:
    objects = db.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    if any(row["type"] not in {"table", "index"} or
           (row["sql"] and "CREATE VIRTUAL TABLE" in row["sql"].upper())
           for row in objects):
        raise LineageError("The desk contains unsupported executable schema objects")
    tables = {row["name"] for row in objects if row["type"] == "table"}
    for name, expected in CORE_COLUMNS.items():
        if name not in tables:
            raise LineageError(f"The desk is missing the operator {name} table")
        actual = tuple(row["name"] for row in db.execute(f'PRAGMA table_info("{name}")'))
        if actual != expected:
            raise LineageError(f"The {name} table uses a different operator schema")
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise LineageError("SQLite reported a damaged operator database")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise LineageError("The desk contains a broken table reference")


def _count(db: sqlite3.Connection, table: str, maximum: int) -> int:
    count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if type(count) is not int or count < 0 or count > maximum:
        raise LineageError(f"The {table} table exceeds max_rows")
    return count


def _snapshot(path: Path, *, max_rows: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    try:
        with closing(_readonly(path, timeout)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute("BEGIN")
            _schema(db)
            scope_rows = db.execute(
                "SELECT value FROM meta WHERE key='scope' ORDER BY value"
            ).fetchall()
            if (len(scope_rows) != 1 or not isinstance(scope_rows[0]["value"], str) or
                    not HEX32.fullmatch(scope_rows[0]["value"])):
                raise LineageError("The desk has no unique valid operator scope")
            scope = scope_rows[0]["value"]
            counts = {table: _count(db, table, max_rows)
                      for table in ("workspaces", "editions", "requests")}
            if sum(counts.values()) > max_rows:
                raise LineageError("The selected desk exceeds max_rows in total")

            workspaces = {}
            for row in db.execute("SELECT id FROM workspaces ORDER BY id"):
                if not isinstance(row["id"], str) or not row["id"]:
                    raise LineageError("A workspace has an invalid identifier")
                workspaces[row["id"]] = []

            for row in db.execute(
                "SELECT workspace,revision,digest,action,created "
                "FROM editions ORDER BY workspace,revision"
            ):
                workspace = row["workspace"]
                revision = row["revision"]
                if workspace not in workspaces:
                    raise LineageError("An edition references an unknown workspace")
                if type(revision) is not int or revision < 1:
                    raise LineageError("An edition has an invalid revision")
                if not isinstance(row["digest"], str) or not HEX64.fullmatch(row["digest"]):
                    raise LineageError("An edition has an invalid digest")
                if row["action"] not in KNOWN_ACTIONS or not isinstance(row["created"], str):
                    raise LineageError("An edition has invalid chronology metadata")
                sequence = workspaces[workspace]
                if revision != len(sequence) + 1:
                    raise LineageError("An edition history is not contiguous")
                sequence.append((revision, row["digest"], row["action"], row["created"]))
            if any(not sequence for sequence in workspaces.values()):
                raise LineageError("A workspace has no retained edition")

            requests = {}
            for row in db.execute("SELECT id,fingerprint,result FROM requests ORDER BY id"):
                identifier = row["id"]
                if not isinstance(identifier, str) or not identifier:
                    raise LineageError("A retained request has an invalid identifier")
                fingerprint = row["fingerprint"]
                result = row["result"]
                if (not isinstance(fingerprint, str) or not HEX64.fullmatch(fingerprint) or
                        not isinstance(result, str)):
                    raise LineageError("A retained request has invalid replay metadata")
                requests[identifier] = (
                    fingerprint,
                    hashlib.sha256(result.encode("utf-8")).hexdigest(),
                )
            db.execute("COMMIT")
            return {"scope": scope, "counts": counts,
                    "workspaces": workspaces, "requests": requests}
    except LineageError:
        raise
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise LineageError("The lineage read exceeded its timeout") from exc
        raise LineageError("Unable to read the operator database") from exc
    except (OSError, sqlite3.Error) as exc:
        raise LineageError("Unable to inspect the operator database read-only") from exc


def compare_databases(baseline: Path, candidate: Path, *, max_rows: int = 1_000_000,
                      timeout: float = 30.0) -> dict:
    """Return a deterministic redacted comparison of two complete desks."""
    maximum = _limit(max_rows)
    timeout = _timeout(timeout)
    before = _snapshot(baseline, max_rows=maximum, timeout=timeout)
    after = _snapshot(candidate, max_rows=maximum, timeout=timeout)
    findings = []
    additions = {"workspaces": 0, "editions": 0, "requests": 0}

    if before["scope"] != after["scope"]:
        findings.append({"kind": "scope_mismatch"})
        status = "unrelated"
    else:
        scope = before["scope"]
        for identifier, sequence in before["workspaces"].items():
            subject = _subject(scope, "workspace", identifier)
            candidate_sequence = after["workspaces"].get(identifier)
            if candidate_sequence is None:
                findings.append({"kind": "workspace_removed", "subject": subject})
                continue
            shared = min(len(sequence), len(candidate_sequence))
            mismatch = next((index for index in range(shared)
                             if sequence[index] != candidate_sequence[index]), None)
            if mismatch is not None:
                findings.append({"kind": "edition_history_rewritten", "subject": subject,
                                 "revision": mismatch + 1})
            elif len(candidate_sequence) < len(sequence):
                findings.append({"kind": "edition_history_rollback", "subject": subject,
                                 "baseline_revision": len(sequence),
                                 "candidate_revision": len(candidate_sequence)})
            else:
                additions["editions"] += len(candidate_sequence) - len(sequence)

        for identifier, sequence in after["workspaces"].items():
            if identifier not in before["workspaces"]:
                additions["workspaces"] += 1
                additions["editions"] += len(sequence)

        for identifier, retained in before["requests"].items():
            subject = _subject(scope, "request", identifier)
            candidate_retained = after["requests"].get(identifier)
            if candidate_retained is None:
                findings.append({"kind": "request_removed", "subject": subject})
            elif candidate_retained != retained:
                findings.append({"kind": "request_rewritten", "subject": subject})
        additions["requests"] = sum(
            identifier not in before["requests"] for identifier in after["requests"]
        )
        status = "diverged" if findings else (
            "forward" if any(additions.values()) else "equal"
        )

    findings.sort(key=lambda item: (item["kind"], item.get("subject", ""), _json(item)))
    report = {
        "schema": SCHEMA,
        "status": status,
        "baseline": {"scope_sha256": hashlib.sha256(before["scope"].encode()).hexdigest(),
                     "counts": before["counts"]},
        "candidate": {"scope_sha256": hashlib.sha256(after["scope"].encode()).hexdigest(),
                      "counts": after["counts"]},
        "additions": additions,
        "findings": findings,
    }
    report["report_sha256"] = hashlib.sha256(_json(report).encode("utf-8")).hexdigest()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True,
                        help="older trusted operator SQLite database")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="candidate operator SQLite database to classify")
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        report = compare_databases(args.baseline, args.candidate,
                                   max_rows=args.max_rows, timeout=args.timeout)
        print(_json(report))
        return 0 if report["status"] in {"equal", "forward"} else 2
    except (LineageError, TypeError, ValueError, OverflowError) as exc:
        print(f"Workspace lineage: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
