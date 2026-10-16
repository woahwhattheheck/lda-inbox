from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from inbox_storage_integrity import StorageIntegrityError, audit_database, main
from task_protocol import claim_task, complete_task, document_sha256


NOW = "2026-10-16T05:30:00Z"


def document():
    return {"v": 1, "tasks": [{
        "id": "private-task", "created": "2026-10-16T05:00:00Z",
        "kind": "run_task", "command": "private customer command",
        "timeout_s": 3600, "done": False,
    }]}


class StorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "operator.sqlite"
        with sqlite3.connect(self.database) as db:
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
            db.execute("INSERT INTO meta VALUES('scope',?)", ("a" * 32,))

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def text(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def import_workspace(self, *, original=None, first=None):
        source = original or document()
        retained = first or source
        source_text, retained_text = self.text(source), self.text(retained)
        digest = document_sha256(retained)
        result = {"workspace": "private-workspace", "revision": 1,
                  "digest": digest, "action": "import"}
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)", (
                "private-workspace", "private label", "2026-10-16T05:00:00Z", source_text))
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                "private-workspace", 1, digest, retained_text, "import",
                "2026-10-16T05:00:00Z"))
            db.execute("INSERT INTO requests VALUES(?,?,?)", (
                "private-request", "b" * 64, self.text(result)))
        return source

    def append_claim(self, source):
        claimed, receipt = claim_task(
            copy.deepcopy(source), task_id="private-task", worker_id="private-worker",
            lease_id="private-lease", now="2026-10-16T05:10:00Z", lease_seconds=3600,
            expected_document_sha256=document_sha256(source))
        digest = document_sha256(claimed)
        result = {"workspace": "private-workspace", "revision": 2,
                  "digest": digest, "receipt": receipt, "action": "claim"}
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO editions VALUES(?,?,?,?,?,?)", (
                "private-workspace", 2, digest, self.text(claimed), "claim", receipt["at"]))
            db.execute("INSERT INTO requests VALUES(?,?,?)", (
                "private-claim", "c" * 64, self.text(result)))
        return claimed

    def test_clean_import_and_transition(self):
        source = self.import_workspace()
        self.append_claim(source)
        report = audit_database(self.database)
        self.assertEqual("clean", report["status"])
        self.assertEqual({"workspaces": 1, "editions": 2, "requests": 2}, report["counts"])

    def test_digest_corruption_fails_closed(self):
        self.import_workspace()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE editions SET digest=?", ("0" * 64,))
        with self.assertRaisesRegex(StorageIntegrityError, "stored digest"):
            audit_database(self.database)

    def test_revision_gap_and_original_mismatch_are_findings(self):
        source = self.import_workspace(first={**document(), "label": "different"})
        with sqlite3.connect(self.database) as db:
            row = db.execute("SELECT * FROM editions").fetchone()
            db.execute("UPDATE editions SET revision=3 WHERE workspace=?", (row[0],))
            db.execute("UPDATE requests SET result=?", (self.text({
                "workspace": "private-workspace", "revision": 3,
                "digest": row[2], "action": "import"}),))
        kinds = {item["kind"] for item in audit_database(self.database)["findings"]}
        self.assertIn("revision_gap", kinds)
        self.assertIn("original_first_edition_mismatch", kinds)

    def test_workspace_without_edition_is_reported(self):
        source = self.text(document())
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO workspaces VALUES(?,?,?,?)", (
                "orphan", "secret label", "2026-10-16T05:00:00Z", source))
        report = audit_database(self.database)
        self.assertEqual("workspace_without_edition", report["findings"][0]["kind"])

    def test_request_missing_edition_is_reported(self):
        self.import_workspace()
        with sqlite3.connect(self.database) as db:
            result = self.text({"workspace": "private-workspace", "revision": 9,
                                "digest": "d" * 64, "action": "import"})
            db.execute("UPDATE requests SET result=?", (result,))
        kinds = {item["kind"] for item in audit_database(self.database)["findings"]}
        self.assertIn("request_missing_edition", kinds)

    def test_changed_receipt_must_link_prior_digest(self):
        source = self.import_workspace()
        self.append_claim(source)
        with sqlite3.connect(self.database) as db:
            result = json.loads(db.execute(
                "SELECT result FROM requests WHERE id='private-claim'").fetchone()[0])
            result["receipt"]["before_sha256"] = "e" * 64
            db.execute("UPDATE requests SET result=? WHERE id='private-claim'", (self.text(result),))
        kinds = {item["kind"] for item in audit_database(self.database)["findings"]}
        self.assertIn("changed_request_chronology_mismatch", kinds)

    def test_report_is_deterministic_redacted_and_read_only(self):
        source = self.import_workspace()
        self.append_claim(source)
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        first = audit_database(self.database)
        second = audit_database(self.database)
        self.assertEqual(first, second)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        rendered = self.text(first)
        for private in ("private-task", "private customer command", "private-worker",
                        "private-lease", "private-workspace", "private-request", "private label"):
            self.assertNotIn(private, rendered)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["--db", str(self.database)]))
        self.assertEqual(first, json.loads(output.getvalue()))

    def test_schema_divergence_and_row_limit_fail_closed(self):
        self.import_workspace()
        with self.assertRaisesRegex(StorageIntegrityError, "max_rows"):
            audit_database(self.database, max_rows=0)
        with sqlite3.connect(self.database) as db:
            db.execute("ALTER TABLE requests ADD COLUMN secret TEXT")
        with self.assertRaisesRegex(StorageIntegrityError, "different operator schema"):
            audit_database(self.database)


if __name__ == "__main__":
    unittest.main()
