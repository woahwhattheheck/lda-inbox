#!/usr/bin/env python3
"""Fail-closed, read-only continuity audit for retained inbox snapshots.

The audit validates each snapshot with ``task_protocol`` and then verifies that
the ordered sequence could have been produced by the documented state machine.
It never stages a transition, publishes Git, loads a token, or contacts a
device/provider.  Output deliberately omits source paths, commands, results,
and raw worker/lease identities.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from task_protocol import (
    TaskProtocolError,
    document_sha256,
    parse_protocol_path,
    task_spec_sha256,
)

MAX_SNAPSHOTS = 256


class HistoryAuditError(ValueError):
    """Raised when individually valid snapshots violate history continuity."""


def _timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _task_map(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["id"]: task for task in document["tasks"]}


def _stable_execution_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    fields = (
        "attempt",
        "worker_id",
        "lease_id",
        "task_sha256",
        "claimed_at",
    )
    return all(before.get(field) == after.get(field) for field in fields)


def _fail(index: int, task_digest: str, detail: str) -> None:
    raise HistoryAuditError(
        f"transition[{index}] task_sha256={task_digest}: {detail}"
    )


def _execution_transition(
    before_task: dict[str, Any],
    after_task: dict[str, Any],
    *,
    transition_index: int,
) -> str:
    digest = task_spec_sha256(before_task)
    before = before_task.get("execution")
    after = after_task.get("execution")

    if before_task == after_task:
        return "unchanged"

    if before_task["done"]:
        _fail(transition_index, digest, "completed task changed or regressed")

    if before is None:
        if after is None:
            _fail(transition_index, digest, "task changed without a protocol transition")
        if after["state"] != "leased" or after["attempt"] != 1:
            _fail(transition_index, digest, "initial authority must be leased attempt 1")
        return "claimed"

    if after is None:
        _fail(transition_index, digest, "execution state was removed")

    before_attempt = before["attempt"]
    after_attempt = after["attempt"]
    before_state = before["state"]
    after_state = after["state"]

    if before_state == "completed":
        _fail(transition_index, digest, "completed execution changed or regressed")

    if after_attempt < before_attempt:
        _fail(transition_index, digest, "lease generation rolled back")
    if after_attempt > before_attempt + 1:
        _fail(transition_index, digest, "lease generation skipped")

    if after_attempt == before_attempt + 1:
        if after_state != "leased":
            _fail(transition_index, digest, "new generation must begin leased")
        if before_state == "leased":
            earliest = _timestamp(before["lease_expires_at"])
        elif before_state == "available":
            earliest = _timestamp(before["released_at"])
        else:  # pragma: no cover - per-snapshot validation owns this invariant.
            _fail(transition_index, digest, "unsupported predecessor state")
        if _timestamp(after["claimed_at"]) < earliest:
            _fail(transition_index, digest, "new generation predates prior release/expiry")
        return "reclaimed"

    if not _stable_execution_equal(before, after):
        _fail(transition_index, digest, "authority changed within one generation")

    if before_state == "leased" and after_state == "leased":
        if _timestamp(after["lease_expires_at"]) <= _timestamp(before["lease_expires_at"]):
            _fail(transition_index, digest, "renewal did not extend lease expiry")
        return "renewed"

    if before_state == "leased" and after_state == "available":
        if after["lease_expires_at"] != before["lease_expires_at"]:
            _fail(transition_index, digest, "release rewrote lease expiry")
        return "released"

    if before_state == "leased" and after_state == "completed":
        if after["lease_expires_at"] != before["lease_expires_at"]:
            _fail(transition_index, digest, "completion rewrote lease expiry")
        return "completed"

    if before_state == "available" and after_state == "available":
        _fail(transition_index, digest, "released generation was rewritten")

    _fail(
        transition_index,
        digest,
        f"invalid state transition {before_state!r} -> {after_state!r}",
    )
    raise AssertionError("unreachable")


def audit_history(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if len(documents) < 2:
        raise HistoryAuditError("at least two ordered snapshots are required")
    if len(documents) > MAX_SNAPSHOTS:
        raise HistoryAuditError(f"snapshot count exceeds {MAX_SNAPSHOTS}")

    # document_sha256 performs the full protocol validation before comparison.
    digests = [document_sha256(document) for document in documents]
    snapshots = [
        {"index": index, "document_sha256": digest, "task_count": len(document["tasks"])}
        for index, (document, digest) in enumerate(zip(documents, digests))
    ]
    transitions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for index, (before_doc, after_doc) in enumerate(zip(documents, documents[1:]), start=1):
        before_root = {key: value for key, value in before_doc.items() if key != "tasks"}
        after_root = {key: value for key, value in after_doc.items() if key != "tasks"}
        if before_root != after_root:
            raise HistoryAuditError(
                f"transition[{index}]: root metadata changed outside the task protocol"
            )
        before_ids = [task["id"] for task in before_doc["tasks"]]
        after_ids = [task["id"] for task in after_doc["tasks"]]
        if after_ids[: len(before_ids)] != before_ids:
            raise HistoryAuditError(
                f"transition[{index}]: existing tasks were removed, reordered, or replaced"
            )

        before_map = _task_map(before_doc)
        after_map = _task_map(after_doc)
        events: list[dict[str, Any]] = []
        for task_id in before_ids:
            before_task = before_map[task_id]
            after_task = after_map[task_id]
            before_spec = task_spec_sha256(before_task)
            after_spec = task_spec_sha256(after_task)
            if before_spec != after_spec:
                raise HistoryAuditError(
                    f"transition[{index}] task_sha256={before_spec}: immutable task specification changed"
                )
            kind = _execution_transition(
                before_task, after_task, transition_index=index
            )
            counts[kind] += 1
            if kind != "unchanged":
                event: dict[str, Any] = {"kind": kind, "task_sha256": before_spec}
                execution = after_task.get("execution")
                if execution is not None:
                    event["attempt"] = execution["attempt"]
                events.append(event)

        for task_id in after_ids[len(before_ids) :]:
            task = after_map[task_id]
            digest = task_spec_sha256(task)
            if task["done"] or "execution" in task:
                _fail(index, digest, "new task must enter history pending and unleased")
            counts["added"] += 1
            events.append({"kind": "added", "task_sha256": digest})

        transitions.append(
            {
                "index": index,
                "before_sha256": digests[index - 1],
                "after_sha256": digests[index],
                "events": events,
            }
        )

    return {
        "v": 1,
        "valid": True,
        "authority": "none",
        "snapshot_count": len(documents),
        "snapshots": snapshots,
        "transitions": transitions,
        "summary": dict(sorted(counts.items())),
    }


def _render_error(exc: Exception) -> str:
    return json.dumps(
        {
            "v": 1,
            "valid": False,
            "authority": "none",
            "error": {"code": "HISTORY_INVALID", "message": str(exc)},
        },
        sort_keys=True,
        indent=2,
    ) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit ordered LDA inbox snapshots without creating authority"
    )
    parser.add_argument("snapshots", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        if len(args.snapshots) < 2:
            raise HistoryAuditError("at least two ordered snapshots are required")
        if len(args.snapshots) > MAX_SNAPSHOTS:
            raise HistoryAuditError(f"snapshot count exceeds {MAX_SNAPSHOTS}")
        documents = []
        for index, path in enumerate(args.snapshots):
            try:
                documents.append(parse_protocol_path(path))
            except TaskProtocolError as exc:
                raise HistoryAuditError(
                    f"snapshot[{index}] failed protocol validation"
                ) from exc
        report = audit_history(documents)
    except HistoryAuditError as exc:
        sys.stdout.write(_render_error(exc))
        return 2
    except (TaskProtocolError, OSError):
        sys.stdout.write(_render_error(HistoryAuditError("protocol validation failed")))
        return 2
    sys.stdout.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
