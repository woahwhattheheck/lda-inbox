from __future__ import annotations

import copy
import unittest

from task_protocol import (
    TaskProtocolError,
    claim_task,
    document_sha256,
    release_task,
    validate_protocol_document,
)


def pending_doc() -> dict:
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


def leased_doc() -> dict:
    doc = pending_doc()
    leased, _ = claim_task(
        doc,
        task_id="task-1",
        worker_id="phone-a",
        lease_id="lease-1",
        now="2026-09-13T10:00:00Z",
        lease_seconds=120,
        expected_document_sha256=document_sha256(doc),
    )
    return leased


class ImportedExecutionChronologyTests(unittest.TestCase):
    def test_import_rejects_claim_before_task_creation(self):
        doc = leased_doc()
        doc["tasks"][0]["execution"]["claimed_at"] = "2026-09-13T09:58:59Z"
        with self.assertRaisesRegex(TaskProtocolError, "precedes task creation"):
            validate_protocol_document(doc)

    def test_import_rejects_release_at_exact_expiry(self):
        doc = leased_doc()
        execution = doc["tasks"][0]["execution"]
        execution["state"] = "available"
        execution["released_at"] = execution["lease_expires_at"]
        with self.assertRaisesRegex(TaskProtocolError, "release occurred at/after lease expiry"):
            validate_protocol_document(doc)

    def test_import_rejects_release_after_expiry(self):
        doc = leased_doc()
        execution = doc["tasks"][0]["execution"]
        execution["state"] = "available"
        execution["released_at"] = "2026-09-13T10:02:01Z"
        with self.assertRaisesRegex(TaskProtocolError, "release occurred at/after lease expiry"):
            validate_protocol_document(doc)

    def test_valid_in_window_release_remains_accepted(self):
        leased = leased_doc()
        released, _ = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            expected_attempt=1,
            now="2026-09-13T10:01:00Z",
            expected_document_sha256=document_sha256(leased),
        )
        self.assertIs(validate_protocol_document(released), released)


if __name__ == "__main__":
    unittest.main()
