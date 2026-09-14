import hashlib
import json
import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from effect_receipts import EffectError, EffectLedger, read_token_file, write_token_file


class EffectReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "effects.sqlite"
        self.ledger = EffectLedger(self.db)
        self.kw = dict(
            task_id="task-1",
            effect_id="email-lead-abc",
            operation="send_email",
            payload={"lead":"lead:abc","template":"intro-v1","amount_cents":250000},
            worker_id="ZKR-F9T6",
            lease_id="lease-1",
            attempt=1,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self):
        return self.ledger.prepare(**self.kw)

    def test_prepare_dispatch_success_and_public_token_absence(self):
        public, token = self.prepare()
        self.assertEqual(public["state"], "PREPARED")
        self.assertTrue(token)
        self.assertNotIn(token, json.dumps(public))
        dispatched = self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        self.assertEqual(dispatched["state"], "DISPATCHED")
        receipt = hashlib.sha256(b"provider-message-1").hexdigest()
        done = self.ledger.record_outcome(
            task_id="task-1", effect_id="email-lead-abc", token=token,
            kind="SUCCEEDED", receipt_ref="provider:msg-1", receipt_sha256=receipt,
        )
        self.assertEqual(done["state"], "SUCCEEDED")
        self.assertFalse(done["retry_authorized"])
        self.assertNotIn(token, json.dumps(done))

    def test_repeated_dispatch_turns_unknown_into_reconciliation_hold(self):
        _, token = self.prepare()
        self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        with self.assertRaisesRegex(EffectError, "RECONCILIATION_REQUIRED"):
            self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        self.assertEqual(self.ledger.inspect(task_id="task-1", effect_id="email-lead-abc")["state"], "RECONCILIATION_REQUIRED")

    def test_later_lease_generation_after_dispatch_cannot_retry(self):
        _, token = self.prepare()
        self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        changed = dict(self.kw, worker_id="other-worker", lease_id="lease-2", attempt=2)
        with self.assertRaisesRegex(EffectError, "RECONCILIATION_REQUIRED"):
            self.ledger.prepare(**changed)
        self.assertEqual(self.ledger.inspect(task_id="task-1", effect_id="email-lead-abc")["state"], "RECONCILIATION_REQUIRED")

    def test_later_lease_generation_before_dispatch_also_fails_closed(self):
        self.prepare()
        changed = dict(self.kw, worker_id="other-worker", lease_id="lease-2", attempt=2)
        with self.assertRaisesRegex(EffectError, "IDENTITY_CONFLICT"):
            self.ledger.prepare(**changed)

    def test_changed_payload_under_same_effect_conflicts(self):
        self.prepare()
        changed = dict(self.kw, payload={"lead":"lead:abc","template":"intro-v2","amount_cents":250000})
        with self.assertRaisesRegex(EffectError, "IDENTITY_CONFLICT"):
            self.ledger.prepare(**changed)

    def test_exact_prepare_replay_is_idempotent_but_does_not_reissue_secret(self):
        first, token = self.prepare()
        second, replay_token = self.prepare()
        self.assertEqual(first, second)
        self.assertIsNone(replay_token)
        self.assertIsNotNone(token)

    def test_stale_token_cannot_dispatch_or_finish(self):
        _, token = self.prepare()
        with self.assertRaisesRegex(EffectError, "STALE_OR_INVALID"):
            self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token + "x")
        self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        with self.assertRaisesRegex(EffectError, "STALE_OR_INVALID"):
            self.ledger.record_outcome(task_id="task-1", effect_id="email-lead-abc", token=token + "x", kind="SUCCEEDED", receipt_ref="provider:x", receipt_sha256="0"*64)

    def test_outcome_before_dispatch_rejected(self):
        _, token = self.prepare()
        with self.assertRaisesRegex(EffectError, "OUTCOME_BEFORE_DISPATCH"):
            self.ledger.record_outcome(task_id="task-1", effect_id="email-lead-abc", token=token, kind="SUCCEEDED", receipt_ref="provider:x", receipt_sha256="0"*64)

    def test_terminal_replay_exact_only(self):
        _, token = self.prepare()
        self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        kwargs = dict(task_id="task-1", effect_id="email-lead-abc", token=token, kind="FAILED_FINAL", receipt_ref="provider:declined", receipt_sha256="1"*64)
        first = self.ledger.record_outcome(**kwargs)
        self.assertEqual(first, self.ledger.record_outcome(**kwargs))
        with self.assertRaisesRegex(EffectError, "CONFLICTING_TERMINAL_OUTCOME"):
            self.ledger.record_outcome(task_id="task-1", effect_id="email-lead-abc", token=token, kind="SUCCEEDED", receipt_ref="provider:late", receipt_sha256="2"*64)

    def test_restart_persists_unknown_hold(self):
        _, token = self.prepare()
        self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        with self.assertRaises(EffectError):
            self.ledger.mark_dispatched(task_id="task-1", effect_id="email-lead-abc", token=token)
        reopened = EffectLedger(self.db)
        self.assertEqual(reopened.inspect(task_id="task-1", effect_id="email-lead-abc")["state"], "RECONCILIATION_REQUIRED")

    def test_token_hash_not_public_or_plaintext_in_database(self):
        _, token = self.prepare()
        raw = self.db.read_bytes()
        self.assertNotIn(token.encode(), raw)
        public = self.ledger.inspect(task_id="task-1", effect_id="email-lead-abc")
        self.assertNotIn("token", json.dumps(public))

    def test_nonfinite_float_and_bool_attempt_rejected(self):
        with self.assertRaisesRegex(EffectError, "NONFINITE_NUMBER"):
            self.ledger.prepare(**dict(self.kw, payload={"x": float("nan")}))
        with self.assertRaisesRegex(EffectError, "FLOAT_NOT_ALLOWED"):
            self.ledger.prepare(**dict(self.kw, payload={"x": 1.5}))
        with self.assertRaisesRegex(EffectError, "INVALID_ATTEMPT"):
            self.ledger.prepare(**dict(self.kw, attempt=True))

    def test_private_token_file_is_mode_600_and_round_trips(self):
        _, token = self.prepare()
        path = self.root / "effect.token.json"
        write_token_file(path, task_id="task-1", effect_id="email-lead-abc", token=token)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_token_file(path), ("task-1", "email-lead-abc", token))
        with self.assertRaises(FileExistsError):
            write_token_file(path, task_id="task-1", effect_id="email-lead-abc", token=token)

    def test_cli_compile_dispatch_unknown_hold(self):
        payload = self.root / "payload.json"
        payload.write_text(json.dumps(self.kw["payload"]), encoding="utf-8")
        token_file = self.root / "token.json"
        cmd = [sys.executable, str(ROOT / "effect_receipts.py"), "--db", str(self.db)]
        prep = subprocess.run(cmd + ["prepare", "--task-id", "task-cli", "--effect-id", "effect-cli", "--operation", "send_email", "--payload-file", str(payload), "--worker-id", "worker-1", "--lease-id", "lease-1", "--attempt", "1", "--token-out", str(token_file)], text=True, capture_output=True)
        self.assertEqual(prep.returncode, 0, prep.stderr)
        first = subprocess.run(cmd + ["dispatch", "--token-file", str(token_file)], text=True, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run(cmd + ["dispatch", "--token-file", str(token_file)], text=True, capture_output=True)
        self.assertEqual(second.returncode, 2)
        self.assertIn("RECONCILIATION_REQUIRED", second.stderr)

    def test_concurrent_prepare_has_one_secret_winner(self):
        db = str(self.db)
        kw = self.kw
        def worker(worker_id, lease_id, attempt):
            try:
                ledger = EffectLedger(db)
                public, token = ledger.prepare(**dict(kw, worker_id=worker_id, lease_id=lease_id, attempt=attempt))
                return ("ok", public["lease"]["attempt"], bool(token))
            except Exception as exc:
                return ("err", str(exc), False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: worker(*args), [("worker-A", "lease-A", 1), ("worker-B", "lease-B", 2)]))
        self.assertEqual(sum(1 for row in results if row[0] == "ok" and row[2]), 1, results)
        self.assertEqual(sum(1 for row in results if row[0] == "err"), 1, results)


if __name__ == "__main__":
    unittest.main()
