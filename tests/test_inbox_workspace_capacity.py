from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from inbox_workspace_capacity import CapacityError, audit_database, main


class WorkspaceCapacityTests(unittest.TestCase):
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
            db.execute("INSERT INTO meta VALUES('scope',?)", ("private-scope",))
            source = '{"tasks":[],"v":1}'
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)",
                       ("secret-workspace", "private customer", "2026-10-16T00:00:00Z", source))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)",
                       ("secret-workspace", 1, "a" * 64, source, "import", "2026-10-16T00:00:00Z"))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)",
                       ("secret-workspace", 2, "b" * 64, source, "complete", "2026-10-16T00:01:00Z"))
            db.execute("INSERT INTO requests VALUES(?,?,?)",
                       ("secret-request", "c" * 64, '{"result":"private result"}'))

    def tearDown(self):
        self.temporary.cleanup()

    def test_reports_aggregate_capacity_without_private_values(self):
        report = audit_database(self.database)
        self.assertEqual("ok", report["status"])
        self.assertEqual(1, report["rows"]["workspaces"])
        self.assertEqual(2, report["rows"]["editions"])
        self.assertEqual(2, report["max_editions_per_workspace"])
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("secret-workspace", rendered)
        self.assertNotIn("secret-request", rendered)
        self.assertNotIn("private customer", rendered)
        self.assertNotIn("private result", rendered)
        self.assertNotIn("private-scope", rendered)

    def test_pressure_uses_logical_size_and_cli_exit_two(self):
        report = audit_database(self.database, max_bytes=1)
        self.assertEqual("pressure", report["status"])
        self.assertIn("logical_size_at_or_above_limit", report["warnings"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(2, main(["--db", str(self.database), "--max-bytes", "1"]))
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_report_is_deterministic_and_digest_bound(self):
        first = audit_database(self.database)
        second = audit_database(self.database)
        self.assertEqual(first, second)
        unsigned = {key: value for key, value in first.items() if key != "report_sha256"}
        expected = hashlib.sha256(json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        self.assertEqual(expected, first["report_sha256"])

    def test_wrong_schema_and_row_ceiling_fail_closed(self):
        with self.assertRaisesRegex(CapacityError, "exceeds"):
            audit_database(self.database, max_rows=1)
        with sqlite3.connect(self.database) as db:
            db.execute("ALTER TABLE requests ADD COLUMN surprise TEXT")
        with self.assertRaisesRegex(CapacityError, "unsupported schema"):
            audit_database(self.database)

    def test_rejects_links_and_invalid_limits(self):
        linked = Path(self.temporary.name) / "linked.sqlite"
        linked.symlink_to(self.database)
        with self.assertRaisesRegex(CapacityError, "regular file"):
            audit_database(linked)
        with self.assertRaises(CapacityError):
            audit_database(self.database, warn_at=0)
        with self.assertRaises(CapacityError):
            audit_database(self.database, max_bytes=0)


if __name__ == "__main__":
    unittest.main()
