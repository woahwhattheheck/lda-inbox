#!/usr/bin/env python3
"""Build a deterministic, redacted worker plan from one immutable LDA inbox.

The planner is deliberately offline and read-only.  It does not claim, renew,
release, complete, publish, or execute a task.  Its output is scheduling advice
for a caller that still has to use the generation-bound protocol and repository
compare-and-swap publisher.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import task_protocol

PLAN_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_ID_BYTES = 128


class WorkerPlanError(ValueError):
    """Raised when planner input is invalid."""


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkerPlanError(f"{field}: expected a trimmed ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkerPlanError(f"{field}: invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkerPlanError(f"{field}: timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    rendered = value.astimezone(timezone.utc).isoformat(
        timespec="microseconds" if value.microsecond else "seconds"
    )
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _require_identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkerPlanError(f"{field}: expected a non-empty trimmed identifier")
    if len(value.encode("utf-8", errors="surrogatepass")) > MAX_ID_BYTES:
        raise WorkerPlanError(f"{field}: exceeds {MAX_ID_BYTES} UTF-8 bytes")
    if ID_RE.fullmatch(value) is None:
        raise WorkerPlanError(f"{field}: expected canonical ASCII identifier syntax")
    return value


def _new_generation(task: dict[str, Any], execution: dict[str, Any] | None) -> dict[str, Any]:
    next_attempt = 1 if execution is None else execution["attempt"] + 1
    if next_attempt > task_protocol.MAX_ATTEMPT:
        return {
            "decision": "attempt_ceiling",
            "next_transition": None,
            "next_attempt": None,
        }
    return {
        "decision": "claimable",
        "next_transition": "claim",
        "next_attempt": next_attempt,
    }


def _task_plan(
    task: dict[str, Any],
    *,
    now: datetime,
    worker_id: str,
    lease_id: str,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "task_id": task["id"],
        "kind": task["kind"],
        "task_sha256": task_protocol.task_spec_sha256(task),
    }
    execution = task.get("execution")

    if task["done"]:
        item.update(
            decision="completed",
            next_transition=None,
            current_attempt=execution.get("attempt") if execution else None,
        )
        return item

    created = _parse_time(task["created"], f"task {task['id']!r} created")
    if now < created:
        item.update(
            decision="wait_for_creation",
            next_transition=None,
            retry_at=_format_time(created),
            current_attempt=execution.get("attempt") if execution else None,
        )
        return item

    if execution is None:
        item.update(current_attempt=None, **_new_generation(task, None))
        return item

    state = execution["state"]
    item["current_attempt"] = execution["attempt"]
    if state == "completed":
        item.update(decision="completed", next_transition=None)
        return item

    if state == "available":
        released = _parse_time(
            execution["released_at"], f"task {task['id']!r} released_at"
        )
        if now < released:
            item.update(
                decision="wait_for_release",
                next_transition=None,
                retry_at=_format_time(released),
            )
            return item
        item.update(**_new_generation(task, execution))
        return item

    claimed = _parse_time(
        execution["claimed_at"], f"task {task['id']!r} claimed_at"
    )
    expires = _parse_time(
        execution["lease_expires_at"], f"task {task['id']!r} lease_expires_at"
    )
    if now >= expires:
        item.update(**_new_generation(task, execution))
        return item

    exact_owner = (
        execution["worker_id"] == worker_id and execution["lease_id"] == lease_id
    )
    if now < claimed:
        item.update(
            decision="wait_for_claim_start",
            next_transition=None,
            retry_at=_format_time(claimed),
            exact_identity_match=exact_owner,
        )
        return item

    if exact_owner:
        item.update(
            decision="owned_live",
            next_transition="renew_release_or_complete",
            expected_attempt=execution["attempt"],
            lease_expires_at=_format_time(expires),
            exact_identity_match=True,
        )
        return item

    item.update(
        decision="wait_for_other_lease",
        next_transition=None,
        retry_at=_format_time(expires),
        exact_identity_match=False,
    )
    return item


def plan_document(
    document: dict[str, Any],
    *,
    now: str,
    worker_id: str,
    lease_id: str,
) -> dict[str, Any]:
    """Return a redacted local lifecycle plan for one validated snapshot."""
    task_protocol.validate_protocol_document(document)
    instant = _parse_time(now, "now")
    worker = _require_identity(worker_id, "worker_id")
    lease = _require_identity(lease_id, "lease_id")
    tasks = [
        _task_plan(task, now=instant, worker_id=worker, lease_id=lease)
        for task in document["tasks"]
    ]
    counts = {
        decision: sum(item["decision"] == decision for item in tasks)
        for decision in sorted({item["decision"] for item in tasks})
    }
    return {
        "v": PLAN_VERSION,
        "planned_at": _format_time(instant),
        "document_sha256": task_protocol.document_sha256(document),
        "task_count": len(tasks),
        "counts": counts,
        "tasks": tasks,
        "authority": {
            "provider_contacted": False,
            "publication_attempted": False,
            "inbox_mutated": False,
            "executes_task": False,
            "grants_lease_authority": False,
        },
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("inbox", type=Path)
    root.add_argument("--now", required=True, help="trusted ISO-8601 planning time")
    root.add_argument("--worker-id", required=True)
    root.add_argument("--lease-id", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        document = task_protocol.parse_protocol_path(args.inbox)
        result = plan_document(
            document,
            now=args.now,
            worker_id=args.worker_id,
            lease_id=args.lease_id,
        )
        sys.stdout.write(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        return 0
    except (WorkerPlanError, task_protocol.TaskProtocolError, OSError) as exc:
        sys.stderr.write(
            json.dumps(
                {"status": "error", "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
