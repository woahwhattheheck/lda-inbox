import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from inbox_history_audit import HistoryAuditError, audit_history
from task_protocol import (
    claim_task,
    complete_task,
    document_sha256,
    release_task,
    renew_task,
)

NOW = "2026-10-16T01:00:00Z"


def pending(task_id="task-1", command="offline work"):
    return {
        "v": 1,
        "tasks": [{
            "id": task_id,
            "created": NOW,
            "kind": "run_task",
            "command": command,
            "timeout_s": 300,
            "done": False,
        }],
    }


def claim(doc, *, worker="phone-a", lease="lease-a", now=NOW, seconds=120):
    return claim_task(
        doc,
        task_id=doc["tasks"][0]["id"],
        worker_id=worker,
        lease_id=lease,
        now=now,
        lease_seconds=seconds,
        expected_document_sha256=document_sha256(doc),
    )[0]


class HistoryAuditTests(unittest.TestCase):
    def test_accepts_complete_lifecycle_and_redacts_authority_and_payload(self):
        base = pending(command="secret command")
        leased = claim(base)
        renewed = renew_task(
            leased, task_id="task-1", worker_id="phone-a", lease_id="lease-a",
            expected_attempt=1, now="2026-10-16T01:01:00Z", lease_seconds=120,
            expected_document_sha256=document_sha256(leased),
        )[0]
        released = release_task(
            renewed, task_id="task-1", worker_id="phone-a", lease_id="lease-a",
            expected_attempt=1, now="2026-10-16T01:01:30Z", reason="private reason",
            expected_document_sha256=document_sha256(renewed),
        )[0]
        reclaimed = claim(
            released, worker="phone-b", lease="lease-b",
            now="2026-10-16T01:01:31Z", seconds=120,
        )
        completed = complete_task(
            reclaimed, task_id="task-1", worker_id="phone-b", lease_id="lease-b",
            expected_attempt=2, completion_id="completion-private", result="secret result",
            now="2026-10-16T01:02:00Z",
            expected_document_sha256=document_sha256(reclaimed),
        )[0]
        report = audit_history([base, leased, renewed, released, reclaimed, completed])
        self.assertTrue(report["valid"])
        self.assertEqual(report["authority"], "none")
        self.assertEqual(report["summary"]["completed"], 1)
        rendered = json.dumps(report)
        for secret in (
            "secret command", "secret result", "private reason", "phone-a",
            "phone-b", "lease-a", "lease-b", "completion-private", "task-1",
        ):
            self.assertNotIn(secret, rendered)

    def test_accepts_pending_task_append(self):
        first = pending()
        second = json.loads(json.dumps(first))
        second["tasks"].append(pending("task-2")["tasks"][0])
        report = audit_history([first, second])
        self.assertEqual(report["summary"]["added"], 1)

    def test_rejects_root_metadata_drift(self):
        first = pending()
        first["retained_control"] = "stable"
        second = json.loads(json.dumps(first))
        second["retained_control"] = "changed"
        with self.assertRaisesRegex(HistoryAuditError, "root metadata changed"):
            audit_history([first, second])

    def test_rejects_removal_and_reorder(self):
        first = pending()
        first["tasks"].append(pending("task-2")["tasks"][0])
        with self.assertRaisesRegex(HistoryAuditError, "removed, reordered"):
            audit_history([first, {"v": 1, "tasks": list(reversed(first["tasks"]))}])

    def test_rejects_immutable_spec_drift(self):
        first = pending()
        second = json.loads(json.dumps(first))
        second["tasks"][0]["command"] = "changed"
        with self.assertRaisesRegex(HistoryAuditError, "immutable task specification"):
            audit_history([first, second])

    def test_rejects_completion_rollback_and_result_rewrite(self):
        leased = claim(pending())
        completed = complete_task(
            leased, task_id="task-1", worker_id="phone-a", lease_id="lease-a",
            expected_attempt=1, completion_id="c1", result="ok",
            now="2026-10-16T01:01:00Z",
            expected_document_sha256=document_sha256(leased),
        )[0]
        with self.assertRaisesRegex(HistoryAuditError, "completed task"):
            audit_history([completed, leased])
        rewritten = json.loads(json.dumps(completed))
        rewritten["tasks"][0]["result"] = "rewritten"
        import hashlib
        rewritten["tasks"][0]["execution"]["result_sha256"] = hashlib.sha256(
            b"rewritten"
        ).hexdigest()
        with self.assertRaisesRegex(HistoryAuditError, "completed task"):
            audit_history([completed, rewritten])

    def test_rejects_generation_skip(self):
        first = claim(pending(), seconds=60)
        later = claim(first, worker="phone-b", lease="lease-b", now="2026-10-16T01:01:00Z")
        later["tasks"][0]["execution"]["attempt"] = 3
        with self.assertRaisesRegex(HistoryAuditError, "generation skipped"):
            audit_history([first, later])

    def test_rejects_authority_change_within_generation(self):
        first = claim(pending())
        changed = json.loads(json.dumps(first))
        changed["tasks"][0]["execution"]["worker_id"] = "phone-b"
        with self.assertRaisesRegex(HistoryAuditError, "authority changed"):
            audit_history([first, changed])

    def test_rejects_renewal_shortening(self):
        first = claim(pending(), seconds=120)
        shortened = json.loads(json.dumps(first))
        shortened["tasks"][0]["execution"]["lease_expires_at"] = "2026-10-16T01:01:00Z"
        with self.assertRaisesRegex(HistoryAuditError, "did not extend"):
            audit_history([first, shortened])

    def test_rejects_equal_instant_expiry_text_rewrite(self):
        first = claim(pending(), seconds=120)
        rewritten = json.loads(json.dumps(first))
        rewritten["tasks"][0]["execution"]["lease_expires_at"] = (
            "2026-10-16T01:02:00+00:00"
        )
        with self.assertRaisesRegex(HistoryAuditError, "did not extend"):
            audit_history([first, rewritten])

    def test_rejects_reclaim_before_prior_expiry(self):
        first = claim(pending(), seconds=120)
        later = claim(first, worker="phone-b", lease="lease-b", now="2026-10-16T01:02:00Z")
        later["tasks"][0]["execution"]["claimed_at"] = "2026-10-16T01:01:59Z"
        later["tasks"][0]["execution"]["lease_expires_at"] = "2026-10-16T01:03:59Z"
        with self.assertRaisesRegex(HistoryAuditError, "predates prior"):
            audit_history([first, later])

    def test_cli_failure_is_redacted_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "private-a.json"
            second = Path(tmp) / "private-b.json"
            first.write_text(json.dumps(pending()), encoding="utf-8")
            changed = pending(command="private changed command")
            second.write_text(json.dumps(changed), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "inbox_history_audit.py", str(first), str(second)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 2)
            report = json.loads(proc.stdout)
            self.assertFalse(report["valid"])
            self.assertNotIn(str(first), proc.stdout)
            self.assertNotIn("private changed command", proc.stdout)

    def test_cli_read_failure_does_not_echo_private_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "customer-secret-name.json"
            other = Path(tmp) / "other-secret-name.json"
            proc = subprocess.run(
                [sys.executable, "inbox_history_audit.py", str(missing), str(other)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn(str(missing), proc.stdout)
            self.assertNotIn("customer-secret-name", proc.stdout)
            self.assertIn("failed protocol validation", proc.stdout)

    def test_cli_protocol_failure_does_not_echo_invalid_private_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            first.write_text(json.dumps(pending()), encoding="utf-8")
            invalid = pending()
            invalid["tasks"][0]["kind"] = "customer-secret-kind"
            second.write_text(json.dumps(invalid), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "inbox_history_audit.py", str(first), str(second)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("customer-secret-kind", proc.stdout)
            self.assertIn("snapshot[1] failed protocol validation", proc.stdout)


if __name__ == "__main__":
    unittest.main()
