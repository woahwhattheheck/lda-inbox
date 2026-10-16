from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from inbox_workspace_integrity import IntegrityError, audit_database, main
from task_protocol import (
    claim_task,
    complete_task,
    document_sha256,
)


NOW = "2026-10-16T04:00:00Z"


def task(task_id="task-1", command="prepare report"):
    return {
        "id": task_id,
        "created": "2026-10-16T03:00:00Z",
        "kind": "run_task",
        "command": command,
        "timeout_s": 3600,
        "done": False,
    }


def document(*tasks):
    return {"v": 1, "tasks": list(tasks)}


def leased(source, worker, lease):
    result, _ = claim_task(
        copy.deepcopy(source), task_id="task-1", worker_id=worker,
        lease_id=lease, now="2026-10-16T03:30:00Z", lease_seconds=3600,
        expected_document_sha256=document_sha256(source),
    )
    return result


def completed(source, worker, lease, completion, result):
    claimed = leased(source, worker, lease)
    value, _ = complete_task(
        claimed, task_id="task-1", worker_id=worker, lease_id=lease,
        expected_attempt=1, completion_id=completion, result=result,
        now="2026-10-16T03:45:00Z",
        expected_document_sha256=document_sha256(claimed),
    )
    return value


class WorkspaceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "operator.sqlite"
        with sqlite3.connect(self.database) as db:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE workspaces(id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    created TEXT NOT NULL, original TEXT NOT NULL);
                CREATE TABLE editions(workspace TEXT NOT NULL, revision INTEGER NOT NULL,
                    digest TEXT NOT NULL, document TEXT NOT NULL, action TEXT NOT NULL,
                    created TEXT NOT NULL, PRIMARY KEY(workspace,revision));
                CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL);
            """)
            db.execute("INSERT INTO meta VALUES('scope',?)", ("a" * 32,))

    def tearDown(self):
        self.temporary.cleanup()

    def add(self, workspace, value, *, digest=None):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)",
                       (workspace, workspace, "2026-10-16T03:00:00Z", text))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                workspace, 1, digest or document_sha256(value), text, "import",
                "2026-10-16T03:00:00Z"))

    def kinds(self, report):
        return [item["kind"] for item in report["findings"]]

    def test_unique_tasks_are_clean(self):
        self.add("one", document(task("task-1")))
        self.add("two", document(task("task-2")))
        report = audit_database(self.database, NOW)
        self.assertEqual("clean", report["status"])
        self.assertEqual([], report["findings"])

    def test_identical_pending_copies_are_not_additional_capacity(self):
        value = document(task())
        self.add("one", value)
        self.add("two", value)
        report = audit_database(self.database, NOW)
        self.assertEqual(["multiple_executable_copies"], self.kinds(report))

    def test_same_id_with_different_specification_is_reported(self):
        self.add("one", document(task(command="prepare report")))
        self.add("two", document(task(command="delete report")))
        report = audit_database(self.database, NOW)
        self.assertIn("spec_divergence", self.kinds(report))

    def test_two_live_leases_report_redacted_authority(self):
        source = document(task())
        self.add("one", leased(source, "worker-private-a", "lease-private-a"))
        self.add("two", leased(source, "worker-private-b", "lease-private-b"))
        report = audit_database(self.database, NOW)
        self.assertIn("concurrent_live_leases", self.kinds(report))
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("worker-private", rendered)
        self.assertNotIn("lease-private", rendered)

    def test_completed_and_pending_copy_reports_stale_executable(self):
        source = document(task())
        self.add("done", completed(source, "worker-a", "lease-a", "finish-a", "ok"))
        self.add("pending", source)
        report = audit_database(self.database, NOW)
        self.assertIn("stale_executable_after_completion", self.kinds(report))

    def test_different_terminal_results_report_completion_conflict(self):
        source = document(task())
        self.add("one", completed(source, "worker-a", "lease-a", "finish-a", "alpha secret"))
        self.add("two", completed(source, "worker-b", "lease-b", "finish-b", "beta secret"))
        report = audit_database(self.database, NOW)
        self.assertEqual(["completion_conflict"], self.kinds(report))
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("alpha secret", rendered)
        self.assertNotIn("beta secret", rendered)

    def test_digest_or_protocol_corruption_fails_closed(self):
        self.add("bad", document(task()), digest="0" * 64)
        with self.assertRaisesRegex(IntegrityError, "differs from its digest"):
            audit_database(self.database, NOW)

    def test_report_and_cli_are_deterministic(self):
        value = document(task(command="private customer command"))
        self.add("one", value)
        self.add("two", value)
        first = audit_database(self.database, NOW)
        second = audit_database(self.database, datetime(2026, 10, 16, 4, tzinfo=timezone.utc))
        self.assertEqual(first, second)
        self.assertNotIn("private customer command", json.dumps(first))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(2, main(["--db", str(self.database), "--as-of", NOW]))
        self.assertEqual(first, json.loads(output.getvalue()))
        expected = hashlib.sha256(json.dumps(
            {key: value for key, value in first.items() if key != "report_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        self.assertEqual(expected, first["report_sha256"])


if __name__ == "__main__":
    unittest.main()
