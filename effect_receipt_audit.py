"""Deterministic, read-only inbox/effect-ledger reconciliation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from effect_receipt_common import (EffectError,SCHEMA,validate_attempt,validate_id,validate_receipt_ref,validate_sha)
from effect_receipt_ledger import read_effects_snapshot
from task_protocol import document_sha256, parse_protocol_path, task_spec_sha256

AUDIT_SCHEMA = "lda.task-effect-audit/v1"
UNRESOLVED_STATES = frozenset(
    {"TOKEN_PENDING", "PREPARED", "DISPATCHED", "RECONCILIATION_REQUIRED"}
)
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED_FINAL"})


def _finding(row: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any]:
    state = row["state"]
    if task is None:
        code = "ORPHAN_EFFECT"
    elif row["task_sha256"] != task_spec_sha256(task):
        code = "TASK_BINDING_MISMATCH"
    elif task["done"] and state in UNRESOLVED_STATES:
        code = "COMPLETED_WITH_UNRESOLVED_EFFECT"
    elif state in UNRESOLVED_STATES:
        code = "UNRESOLVED_EFFECT"
    elif state in TERMINAL_STATES:
        code = "TERMINAL_EFFECT"
    else:
        raise EffectError("INVALID_EFFECT_STATE_IN_SNAPSHOT")
    return {
        "code": code,
        "effect_id": row["effect_id"],
        "effect_state": state,
        "task_id": row["task_id"],
    }


def _validate_public_row(row: dict[str, Any]) -> None:
    if not isinstance(row,dict) or row.get('schema')!=SCHEMA: raise EffectError('INVALID_PUBLIC_EFFECT_SCHEMA')
    for name in ('task_id','effect_id','operation'):
        validate_id(name,row.get(name))
    validate_sha(row.get('payload_sha256'),'payload_sha256');validate_sha(row.get('task_sha256'),'task_sha256')
    lease=row.get('lease')
    if not isinstance(lease,dict): raise EffectError('INVALID_PUBLIC_EFFECT_LEASE')
    validate_id('worker_id',lease.get('worker_id'));validate_id('lease_id',lease.get('lease_id'));validate_attempt(lease.get('attempt'))
    generation=row.get('effect_generation');dispatch=row.get('dispatch_count');state=row.get('state');outcome=row.get('outcome')
    if isinstance(generation,bool) or not isinstance(generation,int) or generation<1: raise EffectError('INVALID_EFFECT_GENERATION')
    if isinstance(dispatch,bool) or dispatch not in (0,1): raise EffectError('INVALID_DISPATCH_COUNT')
    if state in ('TOKEN_PENDING','PREPARED'):
        if dispatch!=0 or outcome is not None: raise EffectError('INVALID_UNDISPATCHED_EFFECT_ROW')
    elif state in ('DISPATCHED','RECONCILIATION_REQUIRED'):
        if dispatch!=1 or outcome is not None: raise EffectError('INVALID_UNRESOLVED_EFFECT_ROW')
    elif state in TERMINAL_STATES:
        if dispatch!=1 or not isinstance(outcome,dict) or outcome.get('kind')!=state: raise EffectError('INVALID_TERMINAL_EFFECT_ROW')
        validate_receipt_ref(outcome.get('receipt_ref'));validate_sha(outcome.get('receipt_sha256'),'receipt_sha256')
    else: raise EffectError('INVALID_EFFECT_STATE_IN_SNAPSHOT')


def audit_document(
    document: dict[str, Any], rows: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    tasks = {task["id"]: task for task in document["tasks"]}
    ordered = sorted(rows, key=lambda row: (row["task_id"], row["effect_id"]))
    seen: set[tuple[str, str]] = set()
    findings: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    for row in ordered:
        _validate_public_row(row)
        identity = (row.get("task_id"), row.get("effect_id"))
        if identity in seen:
            raise EffectError("DUPLICATE_EFFECT_IDENTITY_IN_SNAPSHOT")
        seen.add(identity)
        findings.append(_finding(row, tasks.get(row["task_id"])))
        effects.append(
            {
                "effect_id": row["effect_id"],
                "operation": row["operation"],
                "outcome": row["outcome"],
                "payload_sha256": row["payload_sha256"],
                "state": row["state"],
                "task_id": row["task_id"],
                "task_sha256": row["task_sha256"],
            }
        )
    counts = Counter(item["code"] for item in findings)
    return {
        "schema": AUDIT_SCHEMA,
        "authorizes_completion": False,
        "document_sha256": document_sha256(document),
        "effects": effects,
        "findings": findings,
        "finding_counts": {key: counts[key] for key in sorted(counts)},
        "limitations": {
            "external_effect_execution_proven": False,
            "payment_or_delivery_proven": False,
            "provider_authenticity_proven": False,
        },
        "task_count": len(tasks),
    }


def audit_paths(*, inbox_path: Path, db_path: Path) -> dict[str, Any]:
    document=parse_protocol_path(inbox_path)
    return audit_document(document,read_effects_snapshot(db_path))
