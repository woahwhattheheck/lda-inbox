#!/usr/bin/env python3
"""Audit the retained LDA operator database without changing it.

The report is intentionally redacted: identifiers, labels, commands, workers,
leases, request IDs, and results never leave the selected database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

from task_protocol import document_sha256, parse_protocol_text
from validate_inbox import MAX_DOCUMENT_BYTES


SCHEMA = "lda.inbox.storage-integrity.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ACTIONS = {"import", "claim", "renew", "release", "complete"}
TRANSITIONS = ACTIONS - {"import"}
CORE_COLUMNS = {
    "meta": ("key", "value"),
    "workspaces": ("id", "label", "created", "original"),
    "editions": ("workspace", "revision", "digest", "document", "action", "created"),
    "requests": ("id", "fingerprint", "result"),
}
MAX_RESULT_BYTES = 8 * MAX_DOCUMENT_BYTES


class StorageIntegrityError(ValueError):
    """The database cannot be inspected under the retained storage contract."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _decode(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject(_):
        raise ValueError("non-finite JSON value")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def _stamp(value: str) -> datetime:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("timestamp is not canonical text")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no offset")
    return parsed


def _subject(kind: str, *values) -> str:
    material = _json([kind, *values]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def _readonly(path: Path) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise StorageIntegrityError("Select a regular operator database")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("BEGIN")
    return db


def _core_schema(db: sqlite3.Connection) -> str:
    objects = db.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    if any(row["type"] not in {"table", "index"} or
           (row["sql"] and "CREATE VIRTUAL TABLE" in row["sql"].upper())
           for row in objects):
        raise StorageIntegrityError("Only ordinary retained tables and indexes are supported")
    for table, expected in CORE_COLUMNS.items():
        actual = tuple(row["name"] for row in db.execute(f'PRAGMA table_info("{table}")'))
        if actual != expected:
            raise StorageIntegrityError(f"The {table} table uses a different operator schema")
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise StorageIntegrityError("SQLite reported a damaged database")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StorageIntegrityError("The database has a broken table reference")
    return hashlib.sha256(_json([tuple(row) for row in objects]).encode()).hexdigest()


def _finding(findings: list[dict], kind: str, subject: str, **details) -> None:
    findings.append({"kind": kind, "subject": subject, **details})


def audit_database(path: Path, *, max_rows: int = 1_000_000) -> dict:
    """Return a deterministic redacted report from one read-only transaction."""
    if type(max_rows) is not int or not 1 <= max_rows <= 10_000_000:
        raise StorageIntegrityError("max_rows must be between 1 and 10000000")
    try:
        with closing(_readonly(path)) as db:
            schema_sha256 = _core_schema(db)
            scope_rows = db.execute("SELECT value FROM meta WHERE key='scope'").fetchall()
            if len(scope_rows) != 1 or not HEX32.fullmatch(scope_rows[0][0] or ""):
                raise StorageIntegrityError("The database has no unique canonical operator scope")
            scope = scope_rows[0][0]
            counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                      for table in ("workspaces", "editions", "requests")}
            if any(value > max_rows for value in counts.values()):
                raise StorageIntegrityError("A retained table exceeds max_rows")
            findings: list[dict] = []
            editions_by_workspace: dict[str, list[sqlite3.Row]] = {}
            edition_index: dict[tuple[str, int], sqlite3.Row] = {}
            for row in db.execute(
                    "SELECT workspace,revision,digest,document,action,created "
                    "FROM editions ORDER BY workspace,revision"):
                editions_by_workspace.setdefault(row["workspace"], []).append(row)
                edition_index[(row["workspace"], row["revision"])] = row

            workspace_count = 0
            for workspace in db.execute(
                    "SELECT id,label,created,original FROM workspaces ORDER BY id"):
                workspace_count += 1
                subject = _subject("workspace", scope, workspace["id"])
                original = workspace["original"]
                if not isinstance(original, str) or len(original.encode("utf-8")) > MAX_DOCUMENT_BYTES:
                    raise StorageIntegrityError("A workspace original exceeds the protocol limit")
                try:
                    original_doc = parse_protocol_text(original)
                    created = _stamp(workspace["created"])
                except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                    raise StorageIntegrityError("A workspace original or timestamp is invalid") from exc
                rows = editions_by_workspace.get(workspace["id"], [])
                if not rows:
                    _finding(findings, "workspace_without_edition", subject)
                    continue
                expected_revision = 1
                previous_created = created
                for position, row in enumerate(rows):
                    is_first = position == 0
                    if row["revision"] != expected_revision:
                        _finding(findings, "revision_gap", subject,
                                 expected=expected_revision, observed=row["revision"])
                        expected_revision = row["revision"]
                    expected_revision += 1
                    if row["action"] not in ACTIONS:
                        _finding(findings, "unknown_edition_action", subject,
                                 revision=row["revision"])
                    if is_first and (row["revision"] != 1 or row["action"] != "import"):
                        _finding(findings, "first_edition_not_import", subject)
                    if not is_first and row["action"] == "import":
                        _finding(findings, "late_import_action", subject,
                                 revision=row["revision"])
                    text = row["document"]
                    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
                        raise StorageIntegrityError("A retained edition exceeds the protocol limit")
                    try:
                        document = parse_protocol_text(text)
                        observed_created = _stamp(row["created"])
                    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                        raise StorageIntegrityError("A retained edition or timestamp is invalid") from exc
                    actual_digest = document_sha256(document)
                    if not HEX64.fullmatch(row["digest"] or "") or row["digest"] != actual_digest:
                        raise StorageIntegrityError("A retained edition differs from its stored digest")
                    if observed_created < previous_created:
                        _finding(findings, "edition_time_regression", subject,
                                 revision=row["revision"])
                    previous_created = observed_created
                    if is_first:
                        if actual_digest != document_sha256(original_doc) or _json(document) != _json(original_doc):
                            _finding(findings, "original_first_edition_mismatch", subject)
                        if observed_created != created:
                            _finding(findings, "import_time_mismatch", subject)

            if workspace_count != counts["workspaces"]:
                raise StorageIntegrityError("Workspace count changed during the audit snapshot")

            for request in db.execute(
                    "SELECT id,fingerprint,result FROM requests ORDER BY id"):
                subject = _subject("request", scope, request["id"])
                if not HEX64.fullmatch(request["fingerprint"] or ""):
                    _finding(findings, "invalid_request_fingerprint", subject)
                raw = request["result"]
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
                    raise StorageIntegrityError("A retained request result exceeds the storage limit")
                try:
                    result = _decode(raw)
                except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                    raise StorageIntegrityError("A retained request result is not canonical JSON") from exc
                if not isinstance(result, dict):
                    _finding(findings, "invalid_request_result_shape", subject)
                    continue
                workspace = result.get("workspace")
                revision = result.get("revision")
                digest = result.get("digest")
                action = result.get("action")
                if (not isinstance(workspace, str) or type(revision) is not int or revision < 1 or
                        not isinstance(digest, str) or not HEX64.fullmatch(digest) or action not in ACTIONS):
                    _finding(findings, "invalid_request_result_shape", subject)
                    continue
                retained = edition_index.get((workspace, revision))
                if retained is None:
                    _finding(findings, "request_missing_edition", subject)
                    continue
                if retained["digest"] != digest:
                    _finding(findings, "request_edition_digest_mismatch", subject,
                             revision=revision)
                receipt = result.get("receipt")
                if action == "import":
                    if set(result) != {"workspace", "revision", "digest", "action"} or revision != 1:
                        _finding(findings, "invalid_import_result", subject)
                    if retained["action"] != "import":
                        _finding(findings, "import_result_action_mismatch", subject)
                    continue
                if set(result) != {"workspace", "revision", "digest", "receipt", "action"} or not isinstance(receipt, dict):
                    _finding(findings, "invalid_transition_result", subject)
                    continue
                expected_receipt = {"v", "action", "task_id", "worker_id", "lease_id", "attempt",
                                    "at", "before_sha256", "after_sha256", "changed", "replayed"}
                if set(receipt) != expected_receipt or receipt.get("v") != 1 or receipt.get("action") != action:
                    _finding(findings, "invalid_transition_receipt", subject)
                    continue
                before = receipt.get("before_sha256")
                after = receipt.get("after_sha256")
                changed = receipt.get("changed")
                replayed = receipt.get("replayed")
                if (not isinstance(before, str) or not HEX64.fullmatch(before) or
                        not isinstance(after, str) or not HEX64.fullmatch(after) or
                        type(changed) is not bool or type(replayed) is not bool or changed == replayed or
                        after != digest):
                    _finding(findings, "invalid_transition_receipt", subject)
                    continue
                try:
                    _stamp(receipt.get("at"))
                except (TypeError, ValueError):
                    _finding(findings, "invalid_transition_receipt", subject)
                    continue
                if changed:
                    prior = edition_index.get((workspace, revision - 1))
                    if (prior is None or prior["digest"] != before or
                            retained["action"] != action or retained["created"] != receipt["at"]):
                        _finding(findings, "changed_request_chronology_mismatch", subject,
                                 revision=revision)
                elif before != after:
                    _finding(findings, "replayed_request_digest_mismatch", subject,
                             revision=revision)

            findings.sort(key=lambda item: (item["kind"], item["subject"],
                                            _json({k: v for k, v in item.items()
                                                   if k not in {"kind", "subject"}})))
            report = {
                "schema": SCHEMA,
                "status": "clean" if not findings else "findings",
                "scope_sha256": hashlib.sha256(scope.encode()).hexdigest(),
                "schema_sha256": schema_sha256,
                "counts": counts,
                "findings": findings,
            }
            report["report_sha256"] = hashlib.sha256(_json(report).encode()).hexdigest()
            return report
    except (OSError, sqlite3.Error) as exc:
        raise StorageIntegrityError("Unable to inspect the operator database read-only") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="existing private operator SQLite database")
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    try:
        report = audit_database(args.db, max_rows=args.max_rows)
        print(_json(report))
        return 0 if report["status"] == "clean" else 2
    except (StorageIntegrityError, TypeError, ValueError, OverflowError) as exc:
        print(f"Storage integrity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
