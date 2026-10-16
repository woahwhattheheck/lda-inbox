#!/usr/bin/env python3
"""Audit duplicate task identity and authority across retained LDA workspaces.

This command is deliberately read-only.  It reports cross-workspace conflicts;
it never chooses a winning copy, changes a lease, or grants execution authority.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from task_protocol import (
    TaskProtocolError,
    document_sha256,
    parse_protocol_text,
    task_spec_sha256,
)


SCHEMA = "lda.inbox.workspace-integrity.v1"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FINDING_ORDER = {
    "spec_divergence": 0,
    "concurrent_live_leases": 1,
    "stale_executable_after_completion": 2,
    "completion_conflict": 3,
    "multiple_executable_copies": 4,
}


class IntegrityError(ValueError):
    """The retained desk cannot be audited safely."""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip() == value and value:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise IntegrityError("as_of must be an ISO-8601 timestamp") from exc
    else:
        raise IntegrityError("as_of must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrityError("as_of must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    rendered = value.astimezone(timezone.utc).isoformat(
        timespec="microseconds" if value.microsecond else "seconds")
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _state(task: dict, as_of: datetime) -> tuple[str, bool, int | None, str | None]:
    execution = task.get("execution")
    if task["done"]:
        result_sha = hashlib.sha256(task["result"].encode("utf-8")).hexdigest()
        return "completed", False, execution.get("attempt") if execution else None, result_sha
    if execution is None:
        return "pending", False, None, None
    attempt = execution["attempt"]
    if execution["state"] == "available":
        return "available", False, attempt, None
    if execution["state"] == "completed":
        # parse_protocol_text rejects this state when the task is not done.  Keep
        # this guard local so an optimized interpreter cannot change the audit.
        raise IntegrityError("A nonterminal task carries completed execution state")
    expires = _instant(execution["lease_expires_at"])
    live = as_of < expires
    return ("leased" if live else "expired"), live, attempt, None


def _open_database(path: Path) -> sqlite3.Connection:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IntegrityError("Select an existing operator database") from exc
    if not resolved.is_file():
        raise IntegrityError("Select a regular operator database")
    database = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True,
                               timeout=10, isolation_level=None)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    database.execute("PRAGMA trusted_schema=OFF")
    return database


def _occurrences(database: sqlite3.Connection, as_of: datetime) -> tuple[str, list[dict]]:
    tables = {row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"meta", "workspaces", "editions"}.issubset(tables):
        raise IntegrityError("Database does not contain the operator workspace schema")
    scope = database.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
    if scope is None or not isinstance(scope[0], str) or not re.fullmatch(r"[0-9a-f]{32}", scope[0]):
        raise IntegrityError("Database has no valid operator scope")
    missing = database.execute(
        "SELECT w.id FROM workspaces w WHERE NOT EXISTS "
        "(SELECT 1 FROM editions e WHERE e.workspace=w.id) LIMIT 1").fetchone()
    if missing is not None:
        raise IntegrityError("A retained workspace has no edition")
    rows = database.execute(
        "SELECT w.id,e.revision,e.digest,e.document "
        "FROM workspaces w JOIN editions e ON e.workspace=w.id "
        "AND e.revision=(SELECT max(v.revision) FROM editions v WHERE v.workspace=w.id) "
        "ORDER BY w.id").fetchall()
    found: list[dict] = []
    for row in rows:
        workspace = row["id"]
        if not isinstance(workspace, str) or not IDENTIFIER.fullmatch(workspace):
            raise IntegrityError("A retained workspace has an invalid identity")
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise IntegrityError("A retained workspace has an invalid revision")
        try:
            document = parse_protocol_text(row["document"])
        except (TaskProtocolError, TypeError, RecursionError) as exc:
            raise IntegrityError(f"Workspace {workspace} has an invalid latest edition") from exc
        if document_sha256(document) != row["digest"]:
            raise IntegrityError(f"Workspace {workspace} latest edition differs from its digest")
        for task in document["tasks"]:
            state, live, attempt, result_sha = _state(task, as_of)
            execution = task.get("execution")
            authority = None
            if live:
                authority = _digest({
                    "worker_id": execution["worker_id"],
                    "lease_id": execution["lease_id"],
                    "attempt": execution["attempt"],
                })
            found.append({
                "task_id": task["id"],
                "workspace": workspace,
                "revision": row["revision"],
                "edition_sha256": row["digest"],
                "spec_sha256": task_spec_sha256(task),
                "state": state,
                "attempt": attempt,
                "live": live,
                "authority_sha256": authority,
                "result_sha256": result_sha,
            })
    return scope[0], found


def _finding(kind: str, task_id: str, occurrences: list[dict], **fields) -> dict:
    value = {
        "kind": kind,
        "task_id": task_id,
        "workspaces": sorted(item["workspace"] for item in occurrences),
    }
    value.update(fields)
    return value


def audit_database(path: Path, as_of: str | datetime) -> dict:
    """Return one deterministic, redacted cross-workspace integrity report."""
    observed = _instant(as_of)
    with closing(_open_database(path)) as database:
        database.execute("BEGIN")
        scope, occurrences = _occurrences(database, observed)
    groups: dict[str, list[dict]] = {}
    for item in occurrences:
        groups.setdefault(item["task_id"], []).append(item)
    findings: list[dict] = []
    repeated = 0
    for task_id in sorted(groups):
        items = sorted(groups[task_id], key=lambda item: item["workspace"])
        if len(items) < 2:
            continue
        repeated += 1
        specs = sorted({item["spec_sha256"] for item in items})
        executable = [item for item in items if item["state"] != "completed"]
        completed = [item for item in items if item["state"] == "completed"]
        live = [item for item in items if item["live"]]
        if len(specs) > 1:
            findings.append(_finding("spec_divergence", task_id, items,
                                     spec_sha256=specs))
        if len(live) > 1:
            findings.append(_finding(
                "concurrent_live_leases", task_id, live,
                authorities=sorted({item["authority_sha256"] for item in live}),
                attempts=sorted(item["attempt"] for item in live),
            ))
        if completed and executable:
            findings.append(_finding(
                "stale_executable_after_completion", task_id, items,
                completed_workspaces=sorted(item["workspace"] for item in completed),
                executable_workspaces=sorted(item["workspace"] for item in executable),
            ))
        results = sorted({item["result_sha256"] for item in completed})
        if len(results) > 1:
            findings.append(_finding("completion_conflict", task_id, completed,
                                     result_sha256=results))
        if len(executable) > 1:
            findings.append(_finding(
                "multiple_executable_copies", task_id, executable,
                states=sorted({item["state"] for item in executable}),
            ))
    findings.sort(key=lambda item: (item["task_id"], FINDING_ORDER[item["kind"]]))
    report = {
        "schema": SCHEMA,
        "scope": scope,
        "as_of": _stamp(observed),
        "status": "issues" if findings else "clean",
        "workspace_count": len({item["workspace"] for item in occurrences}),
        "task_occurrence_count": len(occurrences),
        "task_id_count": len(groups),
        "repeated_task_id_count": repeated,
        "finding_count": len(findings),
        "findings": findings,
    }
    report["report_sha256"] = _digest(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="existing private operator SQLite database")
    parser.add_argument("--as-of", required=True,
                        help="explicit ISO-8601 instant used to classify live leases")
    args = parser.parse_args(argv)
    try:
        report = audit_database(args.db, args.as_of)
        print(_canonical(report))
        return 2 if report["findings"] else 0
    except (IntegrityError, OSError, sqlite3.Error, TypeError, ValueError,
            OverflowError, RecursionError) as exc:
        print(f"Workspace integrity: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
