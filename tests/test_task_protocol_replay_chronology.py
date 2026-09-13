from __future__ import annotations

import unittest

from task_protocol import (
    TaskProtocolError,
    claim_task,
    complete_task,
    document_sha256,
    release_task,
)


def pending_doc():
    return {
        "v": 1,
        "tasks": [
            {
                "id": "task-1",
                "created": "2026-09-13T09:59:00Z",
                "kind": "run_task",
                "command": "reply with current time",
                "timeout_s": 300,
                "done": False,
            }
        ],
    }


def claim(doc):
    return claim_task(
        doc,
        task_id="task-1",
        worker_id="phone-a",
        lease_id="lease-1",
        now="2026-09-13T10:00:00Z",
        lease_seconds=120,
        expected_document_sha256=document_sha256(doc),
    )


class ReplayChronologyTests(unittest.TestCase):
    def test_claim_replay_time_cannot_precede_original_claim(self):
        leased, _ = claim(pending_doc())
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original claim"):
            claim_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T09:59:59Z",
                lease_seconds=60,
                expected_document_sha256=document_sha256(leased),
            )

    def test_release_replay_time_cannot_precede_original_release(self):
        leased, _ = claim(pending_doc())
        released, _ = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:00:30Z",
            reason="battery low",
            expected_document_sha256=document_sha256(leased),
        )
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original release"):
            release_task(
                released,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:00:29Z",
                reason="battery low",
                expected_document_sha256=document_sha256(released),
            )

    def test_completion_replay_time_cannot_precede_original_completion(self):
        leased, _ = claim(pending_doc())
        completed, _ = complete_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            completion_id="completion-1",
            result="ok",
            now="2026-09-13T10:01:00Z",
            expected_document_sha256=document_sha256(leased),
        )
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original completion"):
            complete_task(
                completed,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                completion_id="completion-1",
                result="ok",
                now="2026-09-13T10:00:59Z",
                expected_document_sha256=document_sha256(completed),
            )


if __name__ == "__main__":
    unittest.main()
