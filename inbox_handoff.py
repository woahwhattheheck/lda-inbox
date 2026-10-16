#!/usr/bin/env python3
"""Compose a redacted retained-history audit and current worker plan.

The command is offline and read-only. It validates an ordered sequence of
retained LDA inbox snapshots through the maintained protocol, audits their
continuity, and plans the latest snapshot for one exact worker/lease identity.
It does not contact a provider or device, publish Git, mutate an inbox, execute
a task, or grant lease authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from inbox_history_audit import MAX_SNAPSHOTS, HistoryAuditError, audit_history
from inbox_worker_plan import WorkerPlanError, plan_document
from task_protocol import TaskProtocolError, parse_protocol_path

HANDOFF_VERSION = 1
AUTHORITY_BOUNDARY = {
    "provider_contacted": False,
    "publication_attempted": False,
    "inbox_mutated": False,
    "executes_task": False,
    "grants_lease_authority": False,
}


class InboxHandoffError(ValueError):
    """Raised when a retained-history handoff cannot be composed safely."""


def _canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise InboxHandoffError("handoff output is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def compose_handoff(
    documents: Sequence[dict[str, Any]],
    *,
    now: str,
    worker_id: str,
    lease_id: str,
) -> dict[str, Any]:
    """Return one deterministic audit + plan envelope for retained snapshots."""
    if len(documents) < 2:
        raise InboxHandoffError("at least two ordered snapshots are required")
    if len(documents) > MAX_SNAPSHOTS:
        raise InboxHandoffError(f"snapshot count exceeds {MAX_SNAPSHOTS}")

    history = audit_history(documents)
    plan = plan_document(
        documents[-1],
        now=now,
        worker_id=worker_id,
        lease_id=lease_id,
    )

    try:
        history_digest = history["snapshots"][-1]["document_sha256"]
        plan_digest = plan["document_sha256"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InboxHandoffError("component output is missing its latest document digest") from exc
    if history_digest != plan_digest:
        raise InboxHandoffError("history and plan disagree on the latest document digest")

    payload: dict[str, Any] = {
        "v": HANDOFF_VERSION,
        "valid": True,
        "snapshot_count": len(documents),
        "latest_document_sha256": history_digest,
        "history": history,
        "current_plan": plan,
        "authority": dict(AUTHORITY_BOUNDARY),
    }
    return {**payload, "handoff_sha256": _canonical_sha256(payload)}


def _render_error(message: str) -> str:
    return json.dumps(
        {
            "v": HANDOFF_VERSION,
            "valid": False,
            "authority": dict(AUTHORITY_BOUNDARY),
            "error": {"code": "HANDOFF_INVALID", "message": message},
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("snapshots", nargs="+", type=Path)
    root.add_argument("--now", required=True, help="trusted offset-aware ISO-8601 planning time")
    root.add_argument("--worker-id", required=True)
    root.add_argument("--lease-id", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if len(args.snapshots) < 2:
            raise InboxHandoffError("at least two ordered snapshots are required")
        if len(args.snapshots) > MAX_SNAPSHOTS:
            raise InboxHandoffError(f"snapshot count exceeds {MAX_SNAPSHOTS}")

        documents: list[dict[str, Any]] = []
        for index, path in enumerate(args.snapshots):
            try:
                documents.append(parse_protocol_path(path))
            except (TaskProtocolError, OSError):
                raise InboxHandoffError(
                    f"snapshot[{index}] failed protocol validation"
                ) from None

        result = compose_handoff(
            documents,
            now=args.now,
            worker_id=args.worker_id,
            lease_id=args.lease_id,
        )
    except (InboxHandoffError, HistoryAuditError, WorkerPlanError) as exc:
        sys.stdout.write(_render_error(str(exc)))
        return 2
    except TaskProtocolError:
        sys.stdout.write(_render_error("protocol validation failed"))
        return 2
    except (OSError, TypeError, ValueError, RecursionError):
        sys.stdout.write(_render_error("handoff composition failed"))
        return 2

    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
