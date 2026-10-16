from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from inbox_workspace_lineage import LineageError, compare_databases, main


class WorkspaceLineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "baseline.sqlite"
        self._create(self.baseline)
        self._workspace(self.baseline, "private-workspace", "private-request")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _create(path: Path, scope="a" * 32):
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE workspaces(id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    created TEXT NOT NULL, original TEXT NOT NULL);
                CREATE TABLE editions(workspace TEXT NOT NULL REFERENCES workspaces(id),
                    revision INTEGER NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL,
                    action TEXT NOT NULL, created TEXT NOT NULL,
                    PRIMARY KEY(workspace,revision));
                CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL);
            """)
            db.execute("INSERT INTO meta VALUES('scope',?)", (scope,))

    @staticmethod
    def _workspace(path: Path, workspace: str, request: str):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)", (
                workspace, "secret label", "2026-10-16T08:00:00Z", "secret original"))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                workspace, 1, "b" * 64, "secret document", "import",
                "2026-10-16T08:00:00Z"))
            db.execute("INSERT INTO requests VALUES(?,?,?)", (
                request, "c" * 64, "secret result"))

    def copy(self, name="candidate.sqlite"):
        candidate = self.root / name
        shutil.copyfile(self.baseline, candidate)
        return candidate

    def test_equal_copy_is_read_only_and_deterministic(self):
        candidate = self.copy()
        before = [hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (self.baseline, candidate)]
        first = compare_databases(self.baseline, candidate)
        second = compare_databases(self.baseline, candidate)
        self.assertEqual(first, second)
        self.assertEqual("equal", first["status"])
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (self.baseline, candidate)])

    def test_forward_continuation_tracks_all_additions(self):
        candidate = self.copy()
        with sqlite3.connect(candidate) as db:
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                "private-workspace", 2, "d" * 64, "later secret", "claim",
                "2026-10-16T08:10:00Z"))
            db.execute("INSERT INTO requests VALUES(?,?,?)", (
                "later-request", "e" * 64, "later result"))
        self._workspace(candidate, "new-private-workspace", "new-private-request")
        report = compare_databases(self.baseline, candidate)
        self.assertEqual("forward", report["status"])
        self.assertEqual({"workspaces": 1, "editions": 2, "requests": 2},
                         report["additions"])

    def test_workspace_and_request_removal_diverge(self):
        candidate = self.copy()
        with sqlite3.connect(candidate) as db:
            db.execute("DELETE FROM requests")
            db.execute("DELETE FROM editions")
            db.execute("DELETE FROM workspaces")
        report = compare_databases(self.baseline, candidate)
        self.assertEqual("diverged", report["status"])
        self.assertEqual({"request_removed", "workspace_removed"},
                         {item["kind"] for item in report["findings"]})

    def test_edition_rollback_and_rewrite_are_distinguished(self):
        candidate = self.copy()
        with sqlite3.connect(self.baseline) as db:
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                "private-workspace", 2, "d" * 64, "later", "claim",
                "2026-10-16T08:10:00Z"))
        rollback = compare_databases(self.baseline, candidate)
        self.assertEqual("edition_history_rollback", rollback["findings"][0]["kind"])
        with sqlite3.connect(candidate) as db:
            db.execute("UPDATE editions SET digest=? WHERE revision=1", ("f" * 64,))
        rewritten = compare_databases(self.baseline, candidate)
        self.assertEqual("edition_history_rewritten", rewritten["findings"][0]["kind"])
        self.assertEqual(1, rewritten["findings"][0]["revision"])

    def test_request_rewrite_diverges(self):
        candidate = self.copy()
        with sqlite3.connect(candidate) as db:
            db.execute("UPDATE requests SET result='replacement'")
        report = compare_databases(self.baseline, candidate)
        self.assertEqual("request_rewritten", report["findings"][0]["kind"])

    def test_scope_mismatch_is_unrelated(self):
        candidate = self.root / "unrelated.sqlite"
        self._create(candidate, scope="f" * 32)
        self._workspace(candidate, "private-workspace", "private-request")
        report = compare_databases(self.baseline, candidate)
        self.assertEqual("unrelated", report["status"])
        self.assertEqual([{"kind": "scope_mismatch"}], report["findings"])

    def test_output_is_redacted_and_cli_status_is_guarded(self):
        candidate = self.copy()
        with sqlite3.connect(candidate) as db:
            db.execute("UPDATE requests SET result='changed private payload'")
        report = compare_databases(self.baseline, candidate)
        rendered = json.dumps(report, sort_keys=True)
        for private in ("private-workspace", "private-request", "secret",
                        "changed private payload"):
            self.assertNotIn(private, rendered)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(2, main([
                "--baseline", str(self.baseline), "--candidate", str(candidate)]))
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_invalid_schema_revision_and_limits_fail_closed(self):
        candidate = self.copy()
        with self.assertRaisesRegex(LineageError, "max_rows"):
            compare_databases(self.baseline, candidate, max_rows=1)
        with sqlite3.connect(candidate) as db:
            db.execute("UPDATE editions SET revision=2")
        with self.assertRaisesRegex(LineageError, "not contiguous"):
            compare_databases(self.baseline, candidate)
        with sqlite3.connect(self.baseline) as db:
            db.execute("ALTER TABLE requests ADD COLUMN extra TEXT")
        with self.assertRaisesRegex(LineageError, "different operator schema"):
            compare_databases(self.baseline, candidate)


if __name__ == "__main__":
    unittest.main()
