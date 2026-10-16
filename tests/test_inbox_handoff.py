from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_protocol
from inbox_handoff import InboxHandoffError, compose_handoff

NOW = "2026-10-16T01:00:00Z"


def pending() -> dict:
    return {
        "v": 1,
        "tasks": [
            {
                "id": "task-1",
                "created": NOW,
                "kind": "run_task",
                "command": "private customer command",
                "timeout_s": 300,
                "done": False,
                "metadata": {"private": "private customer metadata"},
            }
        ],
    }


class InboxHandoffTests(unittest.TestCase):
    def test_composes_deterministic_redacted_history_and_latest_plan(self):
        first = pending()
        second = task_protocol.claim_task(
            first,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-a",
            now=NOW,
            lease_seconds=120,
            expected_document_sha256=task_protocol.document_sha256(first),
        )[0]

        result = compose_handoff(
            [first, second],
            now="2026-10-16T01:01:00Z",
            worker_id="phone-a",
            lease_id="lease-a",
        )
        repeated = compose_handoff(
            [copy.deepcopy(first), copy.deepcopy(second)],
            now="2026-10-16T01:01:00Z",
            worker_id="phone-a",
            lease_id="lease-a",
        )

        self.assertEqual(result, repeated)
        self.assertTrue(result["valid"])
        self.assertEqual(result["snapshot_count"], 2)
        self.assertEqual(result["history"]["summary"]["claimed"], 1)
        self.assertEqual(result["current_plan"]["tasks"][0]["decision"], "owned_live")
        self.assertEqual(
            result["latest_document_sha256"],
            result["current_plan"]["document_sha256"],
        )
        unsigned = {key: value for key, value in result.items() if key != "handoff_sha256"}
        expected_digest = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(result["handoff_sha256"], expected_digest)

        rendered = json.dumps(result, sort_keys=True)
        for private in (
            "private customer command",
            "private customer metadata",
            "phone-a",
            "lease-a",
        ):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            result["authority"],
            {
                "provider_contacted": False,
                "publication_attempted": False,
                "inbox_mutated": False,
                "executes_task": False,
                "grants_lease_authority": False,
            },
        )

    def test_rejects_component_digest_disagreement(self):
        first = pending()
        second = copy.deepcopy(first)
        with patch(
            "inbox_handoff.plan_document",
            return_value={"document_sha256": "0" * 64},
        ):
            with self.assertRaisesRegex(InboxHandoffError, "disagree"):
                compose_handoff(
                    [first, second],
                    now=NOW,
                    worker_id="phone-a",
                    lease_id="lease-a",
                )

    def test_requires_retained_sequence(self):
        with self.assertRaisesRegex(InboxHandoffError, "at least two"):
            compose_handoff(
                [pending()],
                now=NOW,
                worker_id="phone-a",
                lease_id="lease-a",
            )

    def test_cli_protocol_failure_redacts_path_and_private_value(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "customer-first.json"
            second = Path(directory) / "customer-secret-second.json"
            first.write_text(json.dumps(pending()), encoding="utf-8")
            invalid = pending()
            invalid["tasks"][0]["kind"] = "customer-secret-kind"
            second.write_text(json.dumps(invalid), encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    "inbox_handoff.py",
                    str(first),
                    str(second),
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

        self.assertEqual(run.returncode, 2)
        result = json.loads(run.stdout)
        self.assertFalse(result["valid"])
        self.assertEqual(result["error"]["code"], "HANDOFF_INVALID")
        self.assertIn("snapshot[1] failed protocol validation", run.stdout)
        self.assertNotIn("customer-secret-kind", run.stdout)
        self.assertNotIn("customer-secret-second", run.stdout)
        self.assertNotIn(str(second), run.stdout)


if __name__ == "__main__":
    unittest.main()
