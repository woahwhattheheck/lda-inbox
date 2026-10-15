#!/usr/bin/env python3
"""Deterministic offline lease-to-effect reconciliation walkthrough.

The demo uses a private temporary inbox, token file, and SQLite ledger. It
records only a dispatch marker: it never invokes a phone, provider, mailbox,
payment processor, repository host, or any other external effect.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import effect_receipt_authority
from effect_receipt_common import EffectError
from effect_receipt_io import normalized_token_path, read_token_file, write_token_file
from effect_receipt_ledger import EffectLedger
from task_protocol import claim_task, document_sha256


TASK_ID = "offline-effect-demo"
EFFECT_ID = "offline-marker"
PAYLOAD = {"action": "demonstrate-reconciliation", "external": False}


def _pending_document() -> dict[str, Any]:
    return {
        "v": 1,
        "tasks": [
            {
                "id": TASK_ID,
                "created": "2026-10-15T09:59:00Z",
                "kind": "run_task",
                "command": "run the offline effect-reconciliation demo",
                "timeout_s": 600,
                "done": False,
            }
        ],
    }


def _write_document(path: Path, document: dict[str, Any]) -> str:
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return document_sha256(document)


def _step(name: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": name,
        "state": row["state"],
        "attempt": row["lease"]["attempt"],
        "effect_generation": row["effect_generation"],
        "dispatch_count": row["dispatch_count"],
        "retry_authorized": row["retry_authorized"],
    }


def run_demo() -> dict[str, Any]:
    """Run the complete offline safety course and return a redacted transcript."""

    steps: list[dict[str, Any]] = []
    original_inbox = effect_receipt_authority.CANONICAL_INBOX_PATH
    original_clock = effect_receipt_authority._utc_now

    with tempfile.TemporaryDirectory(prefix="lda-effect-demo-") as directory:
        root = Path(directory)
        inbox_path = root / "inbox.json"
        token_path = root / "attempt-1.token.json"
        database_path = root / "effects.sqlite"

        try:
            effect_receipt_authority.CANONICAL_INBOX_PATH = inbox_path
            effect_receipt_authority._utc_now = lambda: datetime(
                2026, 10, 15, 10, 0, 30, tzinfo=timezone.utc
            )

            pending = _pending_document()
            claimed, claim_receipt = claim_task(
                pending,
                task_id=TASK_ID,
                worker_id="worker-a",
                lease_id="lease-a",
                now="2026-10-15T10:00:00Z",
                lease_seconds=60,
                expected_document_sha256=document_sha256(pending),
            )
            document_sha = _write_document(inbox_path, claimed)
            steps.append(
                {
                    "step": "lease-attempt-1",
                    "attempt": claim_receipt["attempt"],
                    "document_sha256": document_sha,
                }
            )

            ledger = EffectLedger(database_path)
            prepared, token = ledger.prepare(
                task_id=TASK_ID,
                effect_id=EFFECT_ID,
                operation="offline_marker",
                payload=PAYLOAD,
                worker_id="worker-a",
                lease_id="lease-a",
                attempt=1,
                token_path=normalized_token_path(token_path),
                expected_document_sha256=document_sha,
                now="2026-10-15T10:00:10Z",
            )
            if not token:
                raise RuntimeError("first preparation did not create a private token")
            steps.append(_step("token-pending", prepared))

            write_token_file(
                token_path,
                task_id=prepared["task_id"],
                effect_id=prepared["effect_id"],
                effect_generation=prepared["effect_generation"],
                token=token,
                worker_id=prepared["lease"]["worker_id"],
                lease_id=prepared["lease"]["lease_id"],
                attempt=prepared["lease"]["attempt"],
                task_sha256=prepared["task_sha256"],
            )
            token_record = read_token_file(token_path)
            prepared = ledger.finalize_prepare(
                task_id=TASK_ID,
                effect_id=EFFECT_ID,
                token_record=token_record,
            )
            steps.append(_step("prepared", prepared))

            dispatched = ledger.mark_dispatched(
                task_id=TASK_ID,
                effect_id=EFFECT_ID,
                token_record=token_record,
                expected_document_sha256=document_sha,
            )
            steps.append(_step("dispatch-marker-no-external-action", dispatched))

            try:
                ledger.mark_dispatched(
                    task_id=TASK_ID,
                    effect_id=EFFECT_ID,
                    token_record=token_record,
                    expected_document_sha256=document_sha,
                )
            except EffectError as error:
                if str(error) != "RECONCILIATION_REQUIRED_AFTER_DISPATCH":
                    raise
                steps.append(
                    {
                        "step": "duplicate-dispatch-denied",
                        "error": str(error),
                        "state": ledger.inspect(
                            task_id=TASK_ID, effect_id=EFFECT_ID
                        )["state"],
                    }
                )
            else:
                raise RuntimeError("duplicate dispatch unexpectedly succeeded")

            reopened = EffectLedger(database_path)
            reopened_row = reopened.inspect(task_id=TASK_ID, effect_id=EFFECT_ID)
            if reopened_row["state"] != "RECONCILIATION_REQUIRED":
                raise RuntimeError("reopened ledger did not preserve the hold")
            steps.append(_step("restart-preserves-hold", reopened_row))

            later_claim, later_receipt = claim_task(
                claimed,
                task_id=TASK_ID,
                worker_id="worker-b",
                lease_id="lease-b",
                now="2026-10-15T10:01:00Z",
                lease_seconds=60,
                expected_document_sha256=document_sha256(claimed),
            )
            later_sha = _write_document(inbox_path, later_claim)
            try:
                reopened.prepare(
                    task_id=TASK_ID,
                    effect_id=EFFECT_ID,
                    operation="offline_marker",
                    payload=PAYLOAD,
                    worker_id="worker-b",
                    lease_id="lease-b",
                    attempt=later_receipt["attempt"],
                    token_path=normalized_token_path(root / "attempt-2.token.json"),
                    expected_document_sha256=later_sha,
                    now="2026-10-15T10:01:10Z",
                )
            except EffectError as error:
                if str(error) != "RECONCILIATION_REQUIRED_AFTER_DISPATCH":
                    raise
                steps.append(
                    {
                        "step": "later-attempt-denied",
                        "attempt": later_receipt["attempt"],
                        "error": str(error),
                    }
                )
            else:
                raise RuntimeError("later attempt unexpectedly bypassed reconciliation")

            receipt_bytes = b"synthetic offline receipt: no provider effect occurred"
            terminal = reopened.record_outcome(
                task_id=TASK_ID,
                effect_id=EFFECT_ID,
                token_record=token_record,
                kind="FAILED_FINAL",
                receipt_ref="offline:synthetic-no-effect",
                receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            )
            steps.append(_step("synthetic-receipt-resolves-terminal", terminal))

            transcript = {
                "demo": "lda-offline-effect-reconciliation",
                "offline": True,
                "external_actions_performed": 0,
                "provider_receipts_verified": 0,
                "verified_revenue": False,
                "private_token_redacted": True,
                "final_state": terminal["state"],
                "synthetic_receipt": terminal["outcome"],
                "steps": steps,
            }
            if token in json.dumps(transcript, sort_keys=True):
                raise RuntimeError("private token leaked into the transcript")
            return transcript
        finally:
            effect_receipt_authority.CANONICAL_INBOX_PATH = original_inbox
            effect_receipt_authority._utc_now = original_clock


def main() -> int:
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
