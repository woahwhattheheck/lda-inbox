from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_protocol import (
    MAX_ATTEMPT,
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


def pending_doc(*, timeout_s: int = 300, metadata=None):
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


def attempt(doc):
    return doc["tasks"][0]["execution"]["attempt"]


class ValidationTests(unittest.TestCase):
    def test_legacy_pending_and_completed_tasks_remain_valid(self):
        pending = pending_doc()
        self.assertIs(validate_protocol_document(pending), pending)
        completed = pending_doc()
        completed["tasks"][0].update(
            done=True, completed_at="2026-09-13T10:00:01Z", result="retired"
        )
        self.assertIs(validate_protocol_document(completed), completed)

    def test_semantic_digest_ignores_whitespace_and_key_order(self):
        first = '{"v":1,"tasks":[{"id":"task-1","created":"2026-09-13T09:59:00Z","kind":"run_task","command":"x","timeout_s":30,"done":false}]}'
        second = '{"tasks":[{"done":false,"timeout_s":30,"command":"x","kind":"run_task","created":"2026-09-13T09:59:00Z","id":"task-1"}],"v":1}'
        self.assertEqual(
            document_sha256(parse_protocol_text(first)),
            document_sha256(parse_protocol_text(second)),
        )

    def test_explicit_null_execution_rejected(self):
        doc = pending_doc()
        doc["tasks"][0]["execution"] = None
        with self.assertRaisesRegex(TaskProtocolError, "expected a JSON object"):
            validate_protocol_document(doc)

    def test_unknown_execution_fields_fail_closed(self):
        doc, _ = claim(pending_doc())
        doc["tasks"][0]["execution"]["surprise"] = True
        with self.assertRaisesRegex(TaskProtocolError, "unknown fields"):
            validate_protocol_document(doc)

    def test_task_digest_binds_metadata_not_execution(self):
        doc = pending_doc(metadata={"customer": "acme", "priority": 3})
        original = task_spec_sha256(doc["tasks"][0])
        leased, _ = claim(doc)
        self.assertEqual(original, task_spec_sha256(leased["tasks"][0]))
        leased["tasks"][0]["metadata"]["priority"] = 4
        self.assertNotEqual(original, task_spec_sha256(leased["tasks"][0]))
        with self.assertRaisesRegex(TaskProtocolError, "not bound"):
            validate_protocol_document(leased)

    def test_path_reuses_bounded_base_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "huge.json"
            p.write_bytes(b" " * (1_048_576 + 1))
            with self.assertRaisesRegex(TaskProtocolError, "1048576"):
                parse_protocol_path(p)

    def test_total_authority_bound_is_measured_from_original_claim(self):
        leased, _ = claim(pending_doc(timeout_s=300), seconds=240)
        with self.assertRaisesRegex(TaskProtocolError, "total authority exceeds"):
            renew_task(
                leased,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                expected_attempt=1,
                now="2026-09-13T10:02:00Z",
                lease_seconds=240,
                expected_document_sha256=document_sha256(leased),
            )


class ClaimTests(unittest.TestCase):
    def test_claim_receipt_exposes_authority_generation(self):
        doc = pending_doc()
        leased, receipt = claim(doc)
        ex = leased["tasks"][0]["execution"]
        self.assertEqual(ex["attempt"], 1)
        self.assertEqual(receipt["attempt"], 1)
        self.assertEqual(receipt["worker_id"], "phone-a")
        self.assertEqual(receipt["lease_id"], "lease-1")
        self.assertTrue(receipt["changed"])
        self.assertFalse(receipt["replayed"])
        self.assertNotIn("execution", doc["tasks"][0])

    def test_same_live_claim_is_idempotent_same_generation(self):
        leased, _ = claim(pending_doc(), seconds=120)
        replay, receipt = claim(leased, now="2026-09-13T10:00:30Z", seconds=60)
        self.assertEqual(replay, leased)
        self.assertEqual(receipt["attempt"], 1)
        self.assertFalse(receipt["changed"])
        self.assertTrue(receipt["replayed"])

    def test_other_authority_cannot_steal_live_lease(self):
        leased, _ = claim(pending_doc(), seconds=120)
        with self.assertRaisesRegex(TaskProtocolError, "actively leased"):
            claim(
                leased, worker="phone-b", lease="lease-2",
                now="2026-09-13T10:01:00Z", seconds=30,
            )

    def test_exact_expiry_allows_same_raw_id_as_new_generation(self):
        leased, _ = claim(pending_doc(), worker="phone-a", lease="lease-1", seconds=60)
        reassigned, receipt = claim(
            leased, worker="phone-b", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=60,
        )
        self.assertEqual(attempt(reassigned), 2)
        self.assertEqual(receipt["attempt"], 2)
        self.assertEqual(reassigned["tasks"][0]["execution"]["worker_id"], "phone-b")

    def test_exact_expiry_same_worker_same_id_is_new_generation_not_replay(self):
        leased, _ = claim(pending_doc(), seconds=60)
        reassigned, receipt = claim(
            leased, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=60,
        )
        self.assertEqual(attempt(reassigned), 2)
        self.assertTrue(receipt["changed"])
        self.assertFalse(receipt["replayed"])

    def test_release_then_same_raw_id_is_new_generation(self):
        leased, _ = claim(pending_doc())
        released, _ = release_task(
            leased,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-1",
            expected_attempt=1,
            now="2026-09-13T10:00:30Z",
            expected_document_sha256=document_sha256(leased),
        )
        reclaimed, receipt = claim(
            released, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:00:31Z", seconds=60,
        )
        self.assertEqual(attempt(reclaimed), 2)
        self.assertEqual(receipt["attempt"], 2)

    def test_claim_rejects_stale_document_digest(self):
        with self.assertRaisesRegex(TaskProtocolError, "document CAS mismatch"):
            claim_task(
                pending_doc(),
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                now=BASE_NOW,
                lease_seconds=60,
                expected_document_sha256="0" * 64,
            )

    def test_claim_bounds_lease_seconds(self):
        with self.assertRaisesRegex(TaskProtocolError, "exceeds task timeout"):
            claim(pending_doc(timeout_s=30), seconds=31)
        with self.assertRaisesRegex(TaskProtocolError, rf"\[1, {MAX_LEASE_S}\]"):
            claim(pending_doc(timeout_s=86_400), seconds=MAX_LEASE_S + 1)

    def test_attempt_ceiling_fails_closed(self):
        leased, _ = claim(pending_doc(), seconds=60)
        leased["tasks"][0]["execution"]["attempt"] = MAX_ATTEMPT
        self.assertIs(validate_protocol_document(leased), leased)
        with self.assertRaisesRegex(TaskProtocolError, "attempt ceiling"):
            claim(
                leased, worker="phone-b", lease="lease-2",
                now="2026-09-13T10:01:00Z", seconds=60,
            )


class GenerationAuthorityTests(unittest.TestCase):
    def test_stale_generation_cannot_renew_same_raw_identity(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=120,
        )
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            renew_task(
                second,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                expected_attempt=1,
                now="2026-09-13T10:01:30Z",
                lease_seconds=120,
                expected_document_sha256=document_sha256(second),
            )

    def test_stale_generation_cannot_release_same_raw_identity(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=120,
        )
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            release_task(
                second,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                expected_attempt=1,
                now="2026-09-13T10:01:30Z",
                expected_document_sha256=document_sha256(second),
            )

    def test_stale_generation_cannot_complete_same_raw_identity(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=120,
        )
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            complete_task(
                second,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-1",
                expected_attempt=1,
                completion_id="completion-old",
                result="stale",
                now="2026-09-13T10:01:30Z",
                expected_document_sha256=document_sha256(second),
            )

    def test_aba_expiry_raw_identity_reuse_requires_attempt_three(self):
        a1, _ = claim(pending_doc(), worker="phone-a", lease="lease-a", seconds=60)
        b2, _ = claim(
            a1, worker="phone-b", lease="lease-b",
            now="2026-09-13T10:01:00Z", seconds=60,
        )
        a3, _ = claim(
            b2, worker="phone-a", lease="lease-a",
            now="2026-09-13T10:02:00Z", seconds=120,
        )
        self.assertEqual(attempt(a3), 3)
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            release_task(
                a3,
                task_id="task-1",
                worker_id="phone-a",
                lease_id="lease-a",
                expected_attempt=1,
                now="2026-09-13T10:02:10Z",
                expected_document_sha256=document_sha256(a3),
            )
        released, receipt = release_task(
            a3,
            task_id="task-1",
            worker_id="phone-a",
            lease_id="lease-a",
            expected_attempt=3,
            now="2026-09-13T10:02:10Z",
            expected_document_sha256=document_sha256(a3),
        )
        self.assertEqual(receipt["attempt"], 3)
        self.assertEqual(released["tasks"][0]["execution"]["state"], "available")

    def test_aba_release_raw_identity_reuse_requires_attempt_three(self):
        a1, _ = claim(pending_doc(), worker="phone-a", lease="lease-a", seconds=240)
        av1, _ = release_task(
            a1,
            task_id="task-1", worker_id="phone-a", lease_id="lease-a",
            expected_attempt=1, now="2026-09-13T10:00:10Z",
            expected_document_sha256=document_sha256(a1),
        )
        b2, _ = claim(
            av1, worker="phone-b", lease="lease-b",
            now="2026-09-13T10:00:11Z", seconds=120,
        )
        av2, _ = release_task(
            b2,
            task_id="task-1", worker_id="phone-b", lease_id="lease-b",
            expected_attempt=2, now="2026-09-13T10:00:20Z",
            expected_document_sha256=document_sha256(b2),
        )
        a3, _ = claim(
            av2, worker="phone-a", lease="lease-a",
            now="2026-09-13T10:00:21Z", seconds=120,
        )
        with self.assertRaisesRegex(TaskProtocolError, "generation mismatch"):
            complete_task(
                a3,
                task_id="task-1", worker_id="phone-a", lease_id="lease-a",
                expected_attempt=1, completion_id="c1", result="old",
                now="2026-09-13T10:00:30Z",
                expected_document_sha256=document_sha256(a3),
            )
        done, receipt = complete_task(
            a3,
            task_id="task-1", worker_id="phone-a", lease_id="lease-a",
            expected_attempt=3, completion_id="c3", result="new",
            now="2026-09-13T10:00:30Z",
            expected_document_sha256=document_sha256(a3),
        )
        self.assertTrue(done["tasks"][0]["done"])
        self.assertEqual(receipt["attempt"], 3)


class RenewReleaseCompleteTests(unittest.TestCase):
    def test_renew_extends_and_exact_retry_is_idempotent(self):
        leased, _ = claim(pending_doc(timeout_s=300), seconds=120)
        renewed, receipt = renew_task(
            leased,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, now="2026-09-13T10:01:00Z", lease_seconds=120,
            expected_document_sha256=document_sha256(leased),
        )
        self.assertEqual(
            renewed["tasks"][0]["execution"]["lease_expires_at"],
            "2026-09-13T10:03:00Z",
        )
        replay, replay_receipt = renew_task(
            renewed,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, now="2026-09-13T10:01:00Z", lease_seconds=120,
            expected_document_sha256=document_sha256(renewed),
        )
        self.assertEqual(replay, renewed)
        self.assertTrue(replay_receipt["replayed"])
        self.assertEqual(receipt["attempt"], 1)

    def test_release_requires_active_unexpired_generation(self):
        leased, _ = claim(pending_doc(), seconds=60)
        with self.assertRaisesRegex(TaskProtocolError, "expired before release"):
            release_task(
                leased,
                task_id="task-1", worker_id="phone-a", lease_id="lease-1",
                expected_attempt=1, now="2026-09-13T10:01:00Z",
                expected_document_sha256=document_sha256(leased),
            )

    def test_release_replay_requires_same_reason_and_generation(self):
        leased, _ = claim(pending_doc())
        released, _ = release_task(
            leased,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, now="2026-09-13T10:00:30Z", reason="battery",
            expected_document_sha256=document_sha256(leased),
        )
        replay, receipt = release_task(
            released,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, now="2026-09-13T10:00:31Z", reason="battery",
            expected_document_sha256=document_sha256(released),
        )
        self.assertEqual(replay, released)
        self.assertTrue(receipt["replayed"])
        with self.assertRaisesRegex(TaskProtocolError, "changed the release reason"):
            release_task(
                released,
                task_id="task-1", worker_id="phone-a", lease_id="lease-1",
                expected_attempt=1, now="2026-09-13T10:00:31Z", reason="other",
                expected_document_sha256=document_sha256(released),
            )

    def test_complete_binds_result_and_is_exactly_idempotent(self):
        leased, _ = claim(pending_doc(), seconds=120)
        completed, receipt = complete_task(
            leased,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, completion_id="completion-1", result="ok",
            now="2026-09-13T10:01:00Z",
            expected_document_sha256=document_sha256(leased),
        )
        self.assertTrue(completed["tasks"][0]["done"])
        self.assertEqual(receipt["attempt"], 1)
        replay, replay_receipt = complete_task(
            completed,
            task_id="task-1", worker_id="phone-a", lease_id="lease-1",
            expected_attempt=1, completion_id="completion-1", result="ok",
            now="2026-09-13T10:01:01Z",
            expected_document_sha256=document_sha256(completed),
        )
        self.assertEqual(replay, completed)
        self.assertTrue(replay_receipt["replayed"])
        with self.assertRaisesRegex(TaskProtocolError, "does not exactly match"):
            complete_task(
                completed,
                task_id="task-1", worker_id="phone-a", lease_id="lease-1",
                expected_attempt=1, completion_id="completion-2", result="ok",
                now="2026-09-13T10:01:01Z",
                expected_document_sha256=document_sha256(completed),
            )

    def test_wrong_owner_is_rejected_even_with_current_generation(self):
        leased, _ = claim(pending_doc())
        with self.assertRaisesRegex(TaskProtocolError, "do not own"):
            renew_task(
                leased,
                task_id="task-1", worker_id="phone-b", lease_id="lease-1",
                expected_attempt=1, now="2026-09-13T10:00:10Z", lease_seconds=150,
                expected_document_sha256=document_sha256(leased),
            )


class CliTests(unittest.TestCase):
    def _write(self, doc):
        tmp = tempfile.TemporaryDirectory()
        p = Path(tmp.name) / "inbox.json"
        p.write_text(canonical_document_text(doc), encoding="utf-8")
        return tmp, p

    def test_mutating_cli_never_overwrites_input_and_receipt_has_attempt(self):
        doc = pending_doc()
        tmp, p = self._write(doc)
        self.addCleanup(tmp.cleanup)
        before_bytes = p.read_bytes()
        proc = subprocess.run(
            [
                sys.executable, "task_protocol.py", "claim", str(p),
                "--task-id", "task-1", "--worker-id", "phone-a",
                "--lease-id", "lease-1", "--now", BASE_NOW,
                "--lease-seconds", "120",
                "--expected-document-sha256", document_sha256(doc),
            ],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(p.read_bytes(), before_bytes)
        staged = json.loads(proc.stdout)
        receipt = json.loads(proc.stderr)
        self.assertEqual(staged["tasks"][0]["execution"]["attempt"], 1)
        self.assertEqual(receipt["attempt"], 1)

    def test_generation_mutators_require_expected_attempt_cli_arg(self):
        leased, _ = claim(pending_doc())
        tmp, p = self._write(leased)
        self.addCleanup(tmp.cleanup)
        proc = subprocess.run(
            [
                sys.executable, "task_protocol.py", "renew", str(p),
                "--task-id", "task-1", "--worker-id", "phone-a",
                "--lease-id", "lease-1", "--now", "2026-09-13T10:00:10Z",
                "--lease-seconds", "120",
                "--expected-document-sha256", document_sha256(leased),
            ],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--expected-attempt", proc.stderr)

    def test_cli_rejects_stale_expected_attempt(self):
        first, _ = claim(pending_doc(), seconds=60)
        second, _ = claim(
            first, worker="phone-a", lease="lease-1",
            now="2026-09-13T10:01:00Z", seconds=120,
        )
        tmp, p = self._write(second)
        self.addCleanup(tmp.cleanup)
        proc = subprocess.run(
            [
                sys.executable, "task_protocol.py", "complete", str(p),
                "--task-id", "task-1", "--worker-id", "phone-a",
                "--lease-id", "lease-1", "--expected-attempt", "1",
                "--now", "2026-09-13T10:01:10Z",
                "--completion-id", "c", "--result", "x",
                "--expected-document-sha256", document_sha256(second),
            ],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("generation mismatch", proc.stderr)


if __name__ == "__main__":
    unittest.main()
