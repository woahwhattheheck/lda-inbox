#!/usr/bin/env python3
"""Deterministic, offline walkthrough of the generation-bound lease protocol.

The demo stages documents in memory only.  It performs no Git write, phone
operation, provider call, or external side effect.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from task_protocol import (
    TaskProtocolError,
    claim_task,
    complete_task,
    document_sha256,
    release_task,
    renew_task,
)


TASK_ID = "offline-demo-task"


def _pending_document() -> dict[str, Any]:
    return {
        "v": 1,
        "tasks": [
            {
                "id": TASK_ID,
                "created": "2026-10-15T09:59:00Z",
                "kind": "run_task",
                "command": "return the deterministic offline demo result",
                "timeout_s": 600,
                "done": False,
            }
        ],
    }


def _transition(
    steps: list[dict[str, Any]],
    name: str,
    document: dict[str, Any],
    operation: Callable[..., tuple[dict[str, Any], dict[str, Any]]],
    **arguments: Any,
) -> dict[str, Any]:
    before = document_sha256(document)
    next_document, receipt = operation(
        document, expected_document_sha256=before, **arguments
    )
    if receipt["before_sha256"] != before:
        raise RuntimeError(f"{name}: receipt did not bind the input document")
    if receipt["after_sha256"] != document_sha256(next_document):
        raise RuntimeError(f"{name}: receipt did not bind the staged document")
    steps.append(
        {
            "step": name,
            "attempt": receipt["attempt"],
            "changed": receipt["changed"],
            "replayed": receipt["replayed"],
            "before_sha256": receipt["before_sha256"],
            "after_sha256": receipt["after_sha256"],
        }
    )
    return next_document


def run_demo() -> dict[str, Any]:
    """Run the complete in-memory handoff and return its evidence transcript."""

    steps: list[dict[str, Any]] = []
    document = _pending_document()
    initial_sha256 = document_sha256(document)

    document = _transition(
        steps,
        "claim",
        document,
        claim_task,
        task_id=TASK_ID,
        worker_id="worker-a",
        lease_id="lease-a",
        now="2026-10-15T10:00:00Z",
        lease_seconds=180,
    )
    first_claim_sha256 = document_sha256(document)

    document = _transition(
        steps,
        "idempotent-claim-replay",
        document,
        claim_task,
        task_id=TASK_ID,
        worker_id="worker-a",
        lease_id="lease-a",
        now="2026-10-15T10:00:10Z",
        lease_seconds=60,
    )
    if document_sha256(document) != first_claim_sha256:
        raise RuntimeError("claim replay changed the semantic document")

    document = _transition(
        steps,
        "renew",
        document,
        renew_task,
        task_id=TASK_ID,
        worker_id="worker-a",
        lease_id="lease-a",
        expected_attempt=1,
        now="2026-10-15T10:00:30Z",
        lease_seconds=180,
    )
    document = _transition(
        steps,
        "release",
        document,
        release_task,
        task_id=TASK_ID,
        worker_id="worker-a",
        lease_id="lease-a",
        expected_attempt=1,
        now="2026-10-15T10:00:45Z",
        reason="handoff to worker-b",
    )
    document = _transition(
        steps,
        "handoff-claim",
        document,
        claim_task,
        task_id=TASK_ID,
        worker_id="worker-b",
        lease_id="lease-b",
        now="2026-10-15T10:00:46Z",
        lease_seconds=180,
    )

    stale_rejection: dict[str, Any]
    try:
        complete_task(
            document,
            task_id=TASK_ID,
            worker_id="worker-a",
            lease_id="lease-a",
            expected_attempt=1,
            completion_id="stale-completion",
            result="must not publish",
            now="2026-10-15T10:00:50Z",
            expected_document_sha256=document_sha256(document),
        )
    except TaskProtocolError as error:
        if "generation mismatch" not in str(error):
            raise RuntimeError("stale generation failed for an unexpected reason") from error
        stale_rejection = {
            "step": "stale-generation-rejection",
            "expected_attempt": 1,
            "current_attempt": 2,
            "error": str(error),
            "document_sha256": document_sha256(document),
        }
        steps.append(stale_rejection)
    else:
        raise RuntimeError("stale generation unexpectedly completed the task")

    document = _transition(
        steps,
        "complete",
        document,
        complete_task,
        task_id=TASK_ID,
        worker_id="worker-b",
        lease_id="lease-b",
        expected_attempt=2,
        completion_id="completion-b",
        result="offline lease/CAS demo complete",
        now="2026-10-15T10:01:00Z",
    )

    task = document["tasks"][0]
    if task["execution"]["state"] != "completed" or not task["done"]:
        raise RuntimeError("final task state is not completed")
    return {
        "demo": "lda-offline-lease-cas",
        "offline": True,
        "initial_document_sha256": initial_sha256,
        "final_document_sha256": document_sha256(document),
        "final_authority": {
            "worker_id": task["execution"]["worker_id"],
            "lease_id": task["execution"]["lease_id"],
            "attempt": task["execution"]["attempt"],
        },
        "final_state": task["execution"]["state"],
        "steps": steps,
    }


def main() -> int:
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
