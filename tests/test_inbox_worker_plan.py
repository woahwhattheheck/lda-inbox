from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import task_protocol
from inbox_worker_plan import WorkerPlanError, plan_document

NOW = "2026-10-16T01:00:00Z"


def pending(task_id: str, *, created: str = "2026-10-16T00:00:00Z") -> dict:
    return {
        "id": task_id,
        "created": created,
        "kind": "run_task",
        "command": f"private command for {task_id}",
        "timeout_s": 300,
        "done": False,
        "metadata": {"private": "not for the plan"},
    }


def claim(document: dict, task_id: str, *, worker="phone-a", lease="lease-a", now=NOW):
    return task_protocol.claim_task(
        document,
        task_id=task_id,
        worker_id=worker,
        lease_id=lease,
        now=now,
        lease_seconds=120,
        expected_document_sha256=task_protocol.document_sha256(document),
    )[0]


class WorkerPlanTests(unittest.TestCase):
    def test_complete_lifecycle_plan_is_redacted_and_deterministic(self):
        document = {
            "v": 1,
            "tasks": [
                pending("new"),
                pending("mine"),
                pending("other"),
                pending("expired"),
                pending("released"),
                pending("done"),
                pending("future", created="2026-10-16T02:00:00Z"),
            ],
        }
        document = claim(document, "mine", worker="phone-a", lease="lease-a")
        document = claim(document, "other", worker="phone-b", lease="lease-b")
        document = claim(
            document,
            "expired",
            worker="phone-c",
            lease="lease-c",
            now="2026-10-16T00:57:00Z",
        )
        document = claim(
            document,
            "released",
            worker="phone-d",
            lease="lease-d",
            now="2026-10-16T00:59:00Z",
        )
        document = task_protocol.release_task(
            document,
            task_id="released",
            worker_id="phone-d",
            lease_id="lease-d",
            expected_attempt=1,
            now="2026-10-16T00:59:30Z",
            expected_document_sha256=task_protocol.document_sha256(document),
        )[0]
        document = claim(document, "done", worker="phone-e", lease="lease-e")
        document = task_protocol.complete_task(
            document,
            task_id="done",
            worker_id="phone-e",
            lease_id="lease-e",
            expected_attempt=1,
            completion_id="complete-e",
            result="ok",
            now="2026-10-16T01:00:30Z",
            expected_document_sha256=task_protocol.document_sha256(document),
        )[0]

        first = plan_document(
            document, now="2026-10-16T01:01:00Z", worker_id="phone-a", lease_id="lease-a"
        )
        second = plan_document(
            copy.deepcopy(document),
            now="2026-10-16T01:01:00Z",
            worker_id="phone-a",
            lease_id="lease-a",
        )
        self.assertEqual(first, second)
        by_id = {item["task_id"]: item for item in first["tasks"]}
        self.assertEqual(by_id["new"]["decision"], "claimable")
        self.assertEqual(by_id["mine"]["decision"], "owned_live")
        self.assertEqual(by_id["mine"]["expected_attempt"], 1)
        self.assertEqual(by_id["other"]["decision"], "wait_for_other_lease")
        self.assertEqual(by_id["expired"]["decision"], "claimable")
        self.assertEqual(by_id["expired"]["next_attempt"], 2)
        self.assertEqual(by_id["released"]["decision"], "claimable")
        self.assertEqual(by_id["released"]["next_attempt"], 2)
        self.assertEqual(by_id["done"]["decision"], "completed")
        self.assertEqual(by_id["future"]["decision"], "wait_for_creation")
        rendered = json.dumps(first, sort_keys=True)
        for private in (
            "private command",
            "not for the plan",
            "phone-b",
            "lease-b",
            "phone-a",
            "lease-a",
            "complete-e",
            "ok",
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            first["authority"],
            {
                "provider_contacted": False,
                "publication_attempted": False,
                "inbox_mutated": False,
                "executes_task": False,
                "grants_lease_authority": False,
            },
        )

    def test_exact_identity_requires_worker_and_lease(self):
        document = claim(
            {"v": 1, "tasks": [pending("task-1")]},
            "task-1",
            worker="shared-worker",
            lease="current-lease",
        )
        wrong_lease = plan_document(
            document,
            now="2026-10-16T01:01:00Z",
            worker_id="shared-worker",
            lease_id="stale-lease",
        )
        self.assertEqual(
            wrong_lease["tasks"][0]["decision"], "wait_for_other_lease"
        )

    def test_exact_expiry_and_attempt_ceiling(self):
        document = claim(
            {"v": 1, "tasks": [pending("task-1")]},
            "task-1",
            now="2026-10-16T00:58:00Z",
        )
        document["tasks"][0]["execution"]["attempt"] = task_protocol.MAX_ATTEMPT
        task_protocol.validate_protocol_document(document)
        result = plan_document(
            document, now=NOW, worker_id="phone-z", lease_id="lease-z"
        )
        self.assertEqual(result["tasks"][0]["decision"], "attempt_ceiling")
        self.assertIsNone(result["tasks"][0]["next_transition"])

    def test_invalid_identity_and_naive_time_fail_closed(self):
        document = {"v": 1, "tasks": [pending("task-1")]}
        with self.assertRaisesRegex(WorkerPlanError, "canonical ASCII"):
            plan_document(document, now=NOW, worker_id="bad id", lease_id="lease-a")
        with self.assertRaisesRegex(WorkerPlanError, "UTC offset"):
            plan_document(
                document,
                now="2026-10-16T01:00:00",
                worker_id="phone-a",
                lease_id="lease-a",
            )

    def test_cli_reads_bounded_validated_path(self):
        document = {"v": 1, "tasks": [pending("task-1")]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    "inbox_worker_plan.py",
                    str(path),
                    "--now",
                    NOW,
                    "--worker-id",
                    "phone-a",
                    "--lease-id",
                    "lease-a",
                ],
                cwd=Path(__file__).parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["tasks"][0]["decision"], "claimable")
        self.assertEqual(result["document_sha256"], task_protocol.document_sha256(document))


if __name__ == "__main__":
    unittest.main()
