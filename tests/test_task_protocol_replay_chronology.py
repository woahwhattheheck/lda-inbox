from __future__ import annotations

import unittest

from task_protocol import (
    TaskProtocolError,
    claim_task,
    complete_task,
    document_sha256,
    release_task,
    renew_task,
)


def pending_doc():
    return {
        "v": 1,
        "tasks": [{
            "id": "task-1",
            "created": "2026-09-13T09:59:00Z",
            "kind": "run_task",
            "command": "noop",
            "timeout_s": 300,
            "done": False,
        }],
    }


def claim(doc, *, worker="phone-a", lease="lease-a", now="2026-09-13T10:00:00Z", seconds=60):
    return claim_task(
        doc,
        task_id="task-1",
        worker_id=worker,
        lease_id=lease,
        now=now,
        lease_seconds=seconds,
        expected_document_sha256=document_sha256(doc),
    )


class ReplayChronologyTests(unittest.TestCase):
    def test_claim_replay_time_cannot_precede_original_claim(self):
        leased, _ = claim(pending_doc(), seconds=120)
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original claim"):
            claim(
                leased,
                now="2026-09-13T09:59:59Z",
                seconds=120,
            )

    def test_release_replay_time_cannot_precede_original_release(self):
        leased, _ = claim(pending_doc(), seconds=120)
        released, _ = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-a",
            expected_attempt=1,
            now="2026-09-13T10:00:20Z",
            expected_document_sha256=document_sha256(leased),
        )
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original release"):
            release_task(
                released,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-a",
                expected_attempt=1,
                now="2026-09-13T10:00:19Z",
                expected_document_sha256=document_sha256(released),
            )

    def test_completion_replay_time_cannot_precede_original_completion(self):
        leased, _ = claim(pending_doc(), seconds=120)
        completed, _ = complete_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-a",
            expected_attempt=1,
            completion_id="completion-a",
            result="ok",
            now="2026-09-13T10:00:20Z",
            expected_document_sha256=document_sha256(leased),
        )
        with self.assertRaisesRegex(TaskProtocolError, "precedes the original completion"):
            complete_task(
                completed,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-a",
                expected_attempt=1,
                completion_id="completion-a",
                result="ok",
                now="2026-09-13T10:00:19Z",
                expected_document_sha256=document_sha256(completed),
            )

    def test_expired_generation_cannot_be_renewed_even_if_raw_ids_repeat(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first,
            worker="phone-a",
            lease="lease-a",
            now="2026-09-13T10:01:00Z",
            seconds=120,
        )
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            renew_task(
                second,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-a",
                expected_attempt=1,
                now="2026-09-13T10:01:10Z",
                lease_seconds=120,
                expected_document_sha256=document_sha256(second),
            )

    def test_document_digest_and_generation_are_independent_cas_fences(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first,
            worker="phone-a",
            lease="lease-a",
            now="2026-09-13T10:01:00Z",
            seconds=120,
        )
        stale_doc_sha = document_sha256(first)
        with self.assertRaisesRegex(TaskProtocolError, "document CAS mismatch"):
            release_task(
                second,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-a",
                expected_attempt=2,
                now="2026-09-13T10:01:10Z",
                expected_document_sha256=stale_doc_sha,
            )


if __name__ == "__main__":
    unittest.main()
