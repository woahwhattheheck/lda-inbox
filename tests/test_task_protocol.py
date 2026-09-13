from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_protocol import (
    MAX_LEASE_S,
    TaskProtocolError,
    canonical_document_text,
    claim_task,
    complete_task,
    document_sha256,
    parse_protocol_path,
    parse_protocol_text,
    release_task,
    renew_task,
    task_spec_sha256,
    validate_protocol_document,
)

BASE_NOW = "2026-09-13T10:00:00Z"


def pending_doc(*, timeout_s: int = 300, metadata: object | None = None):
    task = {
        "id": "task-1",
        "created": "2026-09-13T09:59:00Z",
        "kind": "run_task",
        "command": "reply with current time",
        "timeout_s": timeout_s,
        "done": False,
    }
    if metadata is not None:
        task["metadata"] = metadata
    return {"v": 1, "tasks": [task]}


def claim(doc, *, worker="phone-a", lease="lease-1", now=BASE_NOW, seconds=120):
    return claim_task(
        doc,
        task_id="task-1",
        worker_id=worker,
        lease_id=lease,
        now=now,
        lease_seconds=seconds,
        expected_document_sha256=document_sha256(doc),
    )


class ProtocolValidationTests(unittest.TestCase):
    def test_legacy_pending_and_completed_tasks_remain_valid(self):
        pending = pending_doc()
        self.assertIs(validate_protocol_document(pending), pending)
        completed = pending_doc()
        completed["tasks"][0].update(
            done=True,
            completed_at="2026-09-13T10:00:01Z",
            result="retired before pickup",
        )
        self.assertIs(validate_protocol_document(completed), completed)

    def test_semantic_digest_ignores_json_whitespace_and_key_order(self):
        first = (
            '{"v":1,"tasks":[{"id":"task-1","created":"2026-09-13T09:59:00Z",'
            '"kind":"run_task","command":"x","timeout_s":30,"done":false}]}'
        )
        second = '''{
          "tasks": [{
            "done": false,
            "timeout_s": 30,
            "command": "x",
            "kind": "run_task",
            "created": "2026-09-13T09:59:00Z",
            "id": "task-1"
          }],
          "v": 1
        }'''
        self.assertEqual(
            document_sha256(parse_protocol_text(first)),
            document_sha256(parse_protocol_text(second)),
        )

    def test_explicit_null_execution_is_rejected(self):
        doc = pending_doc()
        doc["tasks"][0]["execution"] = None
        with self.assertRaisesRegex(TaskProtocolError, "expected a JSON object"):
            validate_protocol_document(doc)

    def test_execution_unknown_fields_fail_closed(self):
        doc, _ = claim(pending_doc())
        doc["tasks"][0]["execution"]["surprise"] = True
        with self.assertRaisesRegex(TaskProtocolError, "unknown fields"):
            validate_protocol_document(doc)

    def test_task_spec_digest_binds_additive_metadata_but_not_execution_receipts(self):
        doc = pending_doc(metadata={"customer": "acme", "priority": 3})
        original = task_spec_sha256(doc["tasks"][0])
        claimed, _ = claim(doc)
        self.assertEqual(original, task_spec_sha256(claimed["tasks"][0]))
        claimed["tasks"][0]["metadata"]["priority"] = 4
        self.assertNotEqual(original, task_spec_sha256(claimed["tasks"][0]))
        with self.assertRaisesRegex(TaskProtocolError, "not bound"):
            validate_protocol_document(claimed)

    def test_protocol_path_reuses_base_bounded_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.json"
            path.write_bytes(b" " * (1_048_576 + 1))
            with self.assertRaisesRegex(TaskProtocolError, "exceeds 1048576 bytes on disk"):
                parse_protocol_path(path)


class ClaimTests(unittest.TestCase):
    def test_claim_binds_worker_lease_task_and_attempt(self):
        doc = pending_doc()
        before = document_sha256(doc)
        next_doc, receipt = claim(doc)
        execution = next_doc["tasks"][0]["execution"]
        self.assertEqual(execution["state"], "leased")
        self.assertEqual(execution["attempt"], 1)
        self.assertEqual(execution["worker_id"], "phone-a")
        self.assertEqual(execution["lease_id"], "lease-1")
        self.assertEqual(execution["task_sha256"], task_spec_sha256(next_doc["tasks"][0]))
        self.assertEqual(receipt["before_sha256"], before)
        self.assertEqual(receipt["after_sha256"], document_sha256(next_doc))
        self.assertTrue(receipt["changed"])
        self.assertFalse(receipt["replayed"])
        self.assertNotEqual(doc, next_doc)
        self.assertNotIn("execution", doc["tasks"][0])

    def test_same_live_claim_identity_is_idempotent(self):
        leased, _ = claim(pending_doc(), seconds=120)
        replay, receipt = claim(
            leased,
            now="2026-09-13T10:00:30Z",
            seconds=60,
        )
        self.assertEqual(replay, leased)
        self.assertFalse(receipt["changed"])
        self.assertTrue(receipt["replayed"])
        self.assertEqual(receipt["before_sha256"], receipt["after_sha256"])

    def test_other_worker_cannot_steal_live_lease(self):
        leased, _ = claim(pending_doc(), seconds=120)
        with self.assertRaisesRegex(TaskProtocolError, "actively leased"):
            claim(
                leased,
                worker="phone-b",
                lease="lease-2",
                now="2026-09-13T10:01:00Z",
                seconds=30,
            )

    def test_exact_expiry_allows_new_lease_and_increments_attempt(self):
        leased, _ = claim(pending_doc(), seconds=60)
        reassigned, _ = claim(
            leased,
            worker="phone-b",
            lease="lease-2",
            now="2026-09-13T10:01:00Z",
            seconds=60,
        )
        execution = reassigned["tasks"][0]["execution"]
        self.assertEqual(execution["attempt"], 2)
        self.assertEqual(execution["worker_id"], "phone-b")

    def test_expired_lease_identity_cannot_be_replayed(self):
        leased, _ = claim(pending_doc(), seconds=60)
        with self.assertRaisesRegex(TaskProtocolError, "expired lease identity"):
            claim(
                leased,
                worker="phone-a",
                lease="lease-1",
                now="2026-09-13T10:01:00Z",
                seconds=60,
            )

    def test_released_lease_id_cannot_be_reused(self):
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
        with self.assertRaisesRegex(TaskProtocolError, "released lease_id cannot be reused"):
            claim(
                released,
                worker="phone-a",
                lease="lease-1",
                now="2026-09-13T10:00:31Z",
                seconds=60,
            )
        reclaimed, _ = claim(
            released,
            worker="phone-a",
            lease="lease-2",
            now="2026-09-13T10:00:31Z",
            seconds=60,
        )
        self.assertEqual(reclaimed["tasks"][0]["execution"]["attempt"], 2)

    def test_claim_time_must_be_monotonic_with_creation_and_release(self):
        with self.assertRaisesRegex(TaskProtocolError, "precedes task creation"):
            claim(pending_doc(), now="2026-09-13T09:58:59Z", seconds=60)

        leased, _ = claim(pending_doc())
        released, _ = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:00:30Z",
            expected_document_sha256=document_sha256(leased),
        )
        with self.assertRaisesRegex(TaskProtocolError, "precedes the prior release"):
            claim(
                released,
                worker="phone-b",
                lease="lease-2",
                now="2026-09-13T10:00:29Z",
                seconds=60,
            )

    def test_claim_rejects_stale_document_digest(self):
        doc = pending_doc()
        stale = "0" * 64
        with self.assertRaisesRegex(TaskProtocolError, "document CAS mismatch"):
            claim_task(
                doc,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now=BASE_NOW,
                lease_seconds=60,
                expected_document_sha256=stale,
            )

    def test_claim_lease_cannot_exceed_task_timeout_or_protocol_ceiling(self):
        with self.assertRaisesRegex(TaskProtocolError, "exceeds task timeout_s 30"):
            claim(pending_doc(timeout_s=30), seconds=31)
        with self.assertRaisesRegex(TaskProtocolError, rf"\[1, {MAX_LEASE_S}\]"):
            claim(pending_doc(timeout_s=86_400), seconds=MAX_LEASE_S + 1)


class RenewReleaseTests(unittest.TestCase):
    def test_renew_extends_and_exact_retry_is_idempotent(self):
        leased, _ = claim(pending_doc(timeout_s=300), seconds=120)
        renewed, receipt = renew_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:01:00Z",
            lease_seconds=120,
            expected_document_sha256=document_sha256(leased),
        )
        self.assertEqual(
            renewed["tasks"][0]["execution"]["lease_expires_at"],
            "2026-09-13T10:03:00Z",
        )
        self.assertTrue(receipt["changed"])
        replay, replay_receipt = renew_task(
            renewed,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:01:00Z",
            lease_seconds=120,
            expected_document_sha256=document_sha256(renewed),
        )
        self.assertEqual(replay, renewed)
        self.assertTrue(replay_receipt["replayed"])
        self.assertFalse(replay_receipt["changed"])

    def test_renew_cannot_shorten_or_use_wrong_owner_or_expired_lease(self):
        leased, _ = claim(pending_doc(), seconds=120)
        with self.assertRaisesRegex(TaskProtocolError, "must not shorten"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:00:30Z",
                lease_seconds=30,
                expected_document_sha256=document_sha256(leased),
            )
        with self.assertRaisesRegex(TaskProtocolError, "do not own"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-b",
                lease_id="lease-1",
                now="2026-09-13T10:00:30Z",
                lease_seconds=120,
                expected_document_sha256=document_sha256(leased),
            )
        with self.assertRaisesRegex(TaskProtocolError, "expired"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:02:00Z",
                lease_seconds=120,
                expected_document_sha256=document_sha256(leased),
            )

    def test_renew_cannot_extend_total_authority_past_task_timeout(self):
        leased, _ = claim(pending_doc(timeout_s=300), seconds=240)
        with self.assertRaisesRegex(TaskProtocolError, "exceeds task timeout_s 300"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:02:00Z",
                lease_seconds=240,
                expected_document_sha256=document_sha256(leased),
            )

    def test_renew_time_cannot_precede_original_claim(self):
        leased, _ = claim(pending_doc(), seconds=120)
        with self.assertRaisesRegex(TaskProtocolError, "precedes the active claim"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T09:59:59Z",
                lease_seconds=180,
                expected_document_sha256=document_sha256(leased),
            )

    def test_release_is_idempotent_only_for_same_reason(self):
        leased, _ = claim(pending_doc())
        released, receipt = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:00:30Z",
            reason="battery low",
            expected_document_sha256=document_sha256(leased),
        )
        self.assertEqual(released["tasks"][0]["execution"]["state"], "available")
        self.assertTrue(receipt["changed"])
        replay, replay_receipt = release_task(
            released,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            now="2026-09-13T10:00:31Z",
            reason="battery low",
            expected_document_sha256=document_sha256(released),
        )
        self.assertEqual(replay, released)
        self.assertTrue(replay_receipt["replayed"])
        with self.assertRaisesRegex(TaskProtocolError, "changed the release reason"):
            release_task(
                released,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:00:31Z",
                reason="different",
                expected_document_sha256=document_sha256(released),
            )

    def test_release_rejects_wrong_owner_and_oversized_reason(self):
        leased, _ = claim(pending_doc())
        with self.assertRaisesRegex(TaskProtocolError, "do not own"):
            release_task(
                leased,
                task_id="task-1",
                worker_id="phone-b",
                lease_id="lease-1",
                now="2026-09-13T10:00:30Z",
                expected_document_sha256=document_sha256(leased),
            )
        with self.assertRaisesRegex(TaskProtocolError, "exceeds 2048"):
            release_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now="2026-09-13T10:00:30Z",
                reason="x" * 2049,
                expected_document_sha256=document_sha256(leased),
            )


class CompletionTests(unittest.TestCase):
    def test_complete_binds_result_and_completion_identity(self):
        leased, _ = claim(pending_doc())
        completed, receipt = complete_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            completion_id="completion-1",
            result="12:00; 82%",
            now="2026-09-13T10:01:00Z",
            expected_document_sha256=document_sha256(leased),
        )
        task = completed["tasks"][0]
        self.assertTrue(task["done"])
        self.assertEqual(task["result"], "12:00; 82%")
        self.assertEqual(task["execution"]["state"], "completed")
        self.assertEqual(task["execution"]["completion_id"], "completion-1")
        self.assertTrue(receipt["changed"])
        validate_protocol_document(completed)

    def test_exact_completion_replay_is_idempotent_but_conflicts_are_denied(self):
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
        replay, receipt = complete_task(
            completed,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            completion_id="completion-1",
            result="ok",
            now="2026-09-13T10:01:30Z",
            expected_document_sha256=document_sha256(completed),
        )
        self.assertEqual(replay, completed)
        self.assertTrue(receipt["replayed"])
        for completion_id, result in (("completion-2", "ok"), ("completion-1", "different")):
            with self.assertRaisesRegex(TaskProtocolError, "does not exactly match"):
                complete_task(
                    completed,
                    task_id="task-1",
                    worker_id="phone-a",
                    lease_id="lease-1",
                    completion_id=completion_id,
                    result=result,
                    now="2026-09-13T10:01:30Z",
                    expected_document_sha256=document_sha256(completed),
                )

    def test_completion_rejects_wrong_owner_and_expired_lease(self):
        leased, _ = claim(pending_doc(), seconds=60)
        with self.assertRaisesRegex(TaskProtocolError, "do not own"):
            complete_task(
                leased,
                task_id="task-1",
                worker_id="phone-b",
                lease_id="lease-1",
                completion_id="completion-1",
                result="ok",
                now="2026-09-13T10:00:30Z",
                expected_document_sha256=document_sha256(leased),
            )
        with self.assertRaisesRegex(TaskProtocolError, "expired"):
            complete_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                completion_id="completion-1",
                result="ok",
                now="2026-09-13T10:01:00Z",
                expected_document_sha256=document_sha256(leased),
            )

    def test_completed_result_tamper_fails_validation(self):
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
        completed["tasks"][0]["result"] = "tampered"
        with self.assertRaisesRegex(TaskProtocolError, "does not bind"):
            validate_protocol_document(completed)

    def test_completion_rejects_result_over_base_envelope_limit(self):
        leased, _ = claim(pending_doc())
        with self.assertRaisesRegex(TaskProtocolError, "exceeds 262144 UTF-8 bytes"):
            complete_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                completion_id="completion-1",
                result="x" * 262_145,
                now="2026-09-13T10:01:00Z",
                expected_document_sha256=document_sha256(leased),
            )


class CliTests(unittest.TestCase):
    def test_cli_transition_stages_output_without_overwriting_input(self):
        doc = pending_doc()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            original = canonical_document_text(doc)
            path.write_text(original, encoding="utf-8")
            digest = document_sha256(doc)
            run = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "task_protocol.py"),
                    "claim",
                    str(path),
                    "--task-id",
                    "task-1",
                    "--worker-id",
                    "phone-a",
                    "--lease-id",
                    "lease-1",
                    "--now",
                    BASE_NOW,
                    "--lease-seconds",
                    "60",
                    "--expected-document-sha256",
                    digest,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            staged = parse_protocol_text(run.stdout)
            self.assertEqual(staged["tasks"][0]["execution"]["state"], "leased")
            receipt = json.loads(run.stderr)
            self.assertEqual(receipt["action"], "claim")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_cli_stale_cas_fails_without_stdout_document(self):
        doc = pending_doc()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbox.json"
            path.write_text(canonical_document_text(doc), encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "task_protocol.py"),
                    "claim",
                    str(path),
                    "--task-id",
                    "task-1",
                    "--worker-id",
                    "phone-a",
                    "--lease-id",
                    "lease-1",
                    "--now",
                    BASE_NOW,
                    "--lease-seconds",
                    "60",
                    "--expected-document-sha256",
                    "0" * 64,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, "")
            self.assertIn("document CAS mismatch", run.stderr)


if __name__ == "__main__":
    unittest.main()
