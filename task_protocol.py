#!/usr/bin/env python3
"""Generation-bound lease/CAS coordination for the repository-backed phone inbox.

The protocol never executes a task or publishes a repository update. Mutating
commands only stage the next JSON document. The publisher must still compare
and swap the exact repository ref/blob, re-read the published state, and only
then execute work.

Lease authority is the tuple (worker_id, lease_id, attempt). ``lease_id`` is a
correlation identifier, not a globally unique capability: it may recur in a
later generation, but every renew/release/complete request must bind the exact
attempt number of the currently published lease.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from validate_inbox import InboxValidationError, validate_path, validate_text

PROTOCOL_VERSION = 1
MAX_ID_BYTES = 128
MAX_RELEASE_REASON_BYTES = 2_048
MAX_LEASE_S = 3_600
MAX_ATTEMPT = 1_000_000
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MUTABLE_TASK_FIELDS = frozenset({"done", "completed_at", "result", "execution"})
EXECUTION_STATES = frozenset({"leased", "available", "completed"})


class TaskProtocolError(ValueError):
    """Raised when protocol state or a requested transition is invalid."""


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TaskProtocolError(f"{field}: expected a trimmed ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TaskProtocolError(f"{field}: invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TaskProtocolError(f"{field}: timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    rendered = utc.isoformat(timespec="microseconds" if utc.microsecond else "seconds")
    return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8", errors="surrogatepass"))


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TaskProtocolError(f"{field}: expected a non-empty trimmed identifier")
    if _utf8_bytes(value) > MAX_ID_BYTES or ID_RE.fullmatch(value) is None:
        raise TaskProtocolError(
            f"{field}: expected <= {MAX_ID_BYTES} UTF-8 bytes using canonical ASCII identifier syntax"
        )
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise TaskProtocolError(f"{field}: expected a lowercase SHA-256 hex digest")
    return value


def _require_exact_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise TaskProtocolError(f"{field}: expected integer in [{minimum}, {maximum}]")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskProtocolError(f"value is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def canonical_document_text(document: dict[str, Any]) -> str:
    validate_protocol_document(document)
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    )


def document_sha256(document: dict[str, Any]) -> str:
    validate_protocol_document(document)
    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def task_spec_sha256(task: dict[str, Any]) -> str:
    spec = {key: value for key, value in task.items() if key not in MUTABLE_TASK_FIELDS}
    return hashlib.sha256(_canonical_json_bytes(spec)).hexdigest()


def _result_sha256(result: str) -> str:
    return hashlib.sha256(result.encode("utf-8")).hexdigest()


def _validate_execution(task: dict[str, Any], index: int) -> None:
    where = f"tasks[{index}].execution"
    if "execution" not in task:
        return
    execution = task["execution"]
    if not isinstance(execution, dict):
        raise TaskProtocolError(f"{where}: expected a JSON object")

    _require_exact_int(
        execution.get("v"), f"{where}.v",
        minimum=PROTOCOL_VERSION, maximum=PROTOCOL_VERSION,
    )
    state = execution.get("state")
    if state not in EXECUTION_STATES:
        raise TaskProtocolError(
            f"{where}.state: expected one of {', '.join(sorted(EXECUTION_STATES))}"
        )
    _require_exact_int(
        execution.get("attempt"), f"{where}.attempt", minimum=1, maximum=MAX_ATTEMPT
    )
    _require_id(execution.get("worker_id"), f"{where}.worker_id")
    _require_id(execution.get("lease_id"), f"{where}.lease_id")

    expected_task_digest = task_spec_sha256(task)
    actual_task_digest = _require_sha256(
        execution.get("task_sha256"), f"{where}.task_sha256"
    )
    if actual_task_digest != expected_task_digest:
        raise TaskProtocolError(
            f"{where}.task_sha256: lease is not bound to the current task specification"
        )

    claimed = _parse_timestamp(execution.get("claimed_at"), f"{where}.claimed_at")
    created = _parse_timestamp(task.get("created"), f"tasks[{index}].created")
    if claimed < created:
        raise TaskProtocolError(f"{where}.claimed_at: precedes task creation")
    expires = _parse_timestamp(
        execution.get("lease_expires_at"), f"{where}.lease_expires_at"
    )
    if expires <= claimed:
        raise TaskProtocolError(f"{where}.lease_expires_at: must be after claimed_at")
    lease_span_s = (expires - claimed).total_seconds()
    if lease_span_s > MAX_LEASE_S:
        raise TaskProtocolError(
            f"{where}.lease_expires_at: total authority exceeds {MAX_LEASE_S} seconds"
        )
    if lease_span_s > task["timeout_s"]:
        raise TaskProtocolError(
            f"{where}.lease_expires_at: total authority exceeds task timeout_s {task['timeout_s']}"
        )

    common_fields = {
        "v", "state", "attempt", "worker_id", "lease_id",
        "task_sha256", "claimed_at", "lease_expires_at",
    }
    completion_fields = {"completion_id", "result_sha256"}
    allowed_fields = set(common_fields)
    if state == "available":
        allowed_fields.add("released_at")
        if "release_reason" in execution:
            allowed_fields.add("release_reason")
    elif state == "completed":
        allowed_fields.update(completion_fields)
    unknown = sorted(set(execution) - allowed_fields)
    if unknown:
        raise TaskProtocolError(f"{where}: unknown fields are not allowed: {unknown}")

    done = task.get("done")
    if state == "leased":
        if done is not False:
            raise TaskProtocolError(f"{where}: leased task must have done=false")
        forbidden = completion_fields | {"released_at", "release_reason"}
        present = sorted(forbidden.intersection(execution))
        if present:
            raise TaskProtocolError(f"{where}: leased state forbids fields {present}")
        return

    if state == "available":
        if done is not False:
            raise TaskProtocolError(f"{where}: available task must have done=false")
        released = _parse_timestamp(execution.get("released_at"), f"{where}.released_at")
        if released < claimed:
            raise TaskProtocolError(f"{where}.released_at: precedes claimed_at")
        if released >= expires:
            raise TaskProtocolError(
                f"{where}.released_at: release occurred at/after lease expiry"
            )
        reason = execution.get("release_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise TaskProtocolError(f"{where}.release_reason: expected a string")
            if _utf8_bytes(reason) > MAX_RELEASE_REASON_BYTES:
                raise TaskProtocolError(
                    f"{where}.release_reason: exceeds {MAX_RELEASE_REASON_BYTES} UTF-8 bytes"
                )
        present = sorted(completion_fields.intersection(execution))
        if present:
            raise TaskProtocolError(f"{where}: available state forbids fields {present}")
        return

    if done is not True:
        raise TaskProtocolError(f"{where}: completed execution requires done=true")
    if "released_at" in execution or "release_reason" in execution:
        raise TaskProtocolError(f"{where}: completed state forbids release fields")
    _require_id(execution.get("completion_id"), f"{where}.completion_id")
    result = task.get("result")
    if not isinstance(result, str):
        raise TaskProtocolError(f"{where}: completed task requires string result")
    if _require_sha256(execution.get("result_sha256"), f"{where}.result_sha256") != _result_sha256(result):
        raise TaskProtocolError(f"{where}.result_sha256: does not bind the task result")
    completed = _parse_timestamp(task.get("completed_at"), f"tasks[{index}].completed_at")
    if completed < claimed:
        raise TaskProtocolError(f"tasks[{index}].completed_at: precedes claim")
    if completed >= expires:
        raise TaskProtocolError(
            f"tasks[{index}].completed_at: completion occurred at/after lease expiry"
        )


def validate_protocol_document(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise TaskProtocolError("root: expected a JSON object")
    try:
        base_text = json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskProtocolError(f"root: not valid JSON: {exc}") from exc
    try:
        validated = validate_text(base_text)
    except InboxValidationError as exc:
        raise TaskProtocolError(str(exc)) from exc
    for index, task in enumerate(validated["tasks"]):
        _validate_execution(task, index)
    return document


def parse_protocol_text(text: str) -> dict[str, Any]:
    try:
        document = validate_text(text)
    except InboxValidationError as exc:
        raise TaskProtocolError(str(exc)) from exc
    for index, task in enumerate(document["tasks"]):
        _validate_execution(task, index)
    return document


def parse_protocol_path(path: Path) -> dict[str, Any]:
    try:
        document = validate_path(path)
    except InboxValidationError as exc:
        raise TaskProtocolError(str(exc)) from exc
    for index, task in enumerate(document["tasks"]):
        _validate_execution(task, index)
    return document


def _copy_validated(document: dict[str, Any]) -> dict[str, Any]:
    validate_protocol_document(document)
    return copy.deepcopy(document)


def _find_task(document: dict[str, Any], task_id: str) -> dict[str, Any]:
    wanted = _require_id(task_id, "task_id")
    for task in document["tasks"]:
        if task["id"] == wanted:
            return task
    raise TaskProtocolError(f"task {wanted!r} not found")


def _check_expected(document: dict[str, Any], expected_document_sha256: str) -> str:
    expected = _require_sha256(expected_document_sha256, "expected_document_sha256")
    actual = document_sha256(document)
    if actual != expected:
        raise TaskProtocolError(
            f"document CAS mismatch: expected {expected}, current {actual}"
        )
    return actual


def _require_lease_seconds(task: dict[str, Any], lease_seconds: Any) -> int:
    seconds = _require_exact_int(
        lease_seconds, "lease_seconds", minimum=1, maximum=MAX_LEASE_S
    )
    if seconds > task["timeout_s"]:
        raise TaskProtocolError(
            f"lease_seconds: {seconds} exceeds task timeout_s {task['timeout_s']}"
        )
    return seconds


def _require_generation(execution: dict[str, Any], expected_attempt: Any) -> int:
    expected = _require_exact_int(
        expected_attempt, "expected_attempt", minimum=1, maximum=MAX_ATTEMPT
    )
    current = execution["attempt"]
    if current != expected:
        raise TaskProtocolError(
            f"lease generation mismatch: expected attempt {expected}, current {current}"
        )
    return current


def _require_owner(
    execution: dict[str, Any],
    *,
    worker_id: str,
    lease_id: str,
    expected_attempt: Any,
) -> int:
    attempt = _require_generation(execution, expected_attempt)
    if execution["worker_id"] != worker_id or execution["lease_id"] != lease_id:
        raise TaskProtocolError(
            "worker_id/lease_id do not own the expected lease generation"
        )
    return attempt


def _receipt(
    *,
    action: str,
    task_id: str,
    worker_id: str,
    lease_id: str,
    attempt: int,
    at: str,
    before_sha256: str,
    after_sha256: str,
    changed: bool,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "v": 1,
        "action": action,
        "task_id": task_id,
        "worker_id": worker_id,
        "lease_id": lease_id,
        "attempt": attempt,
        "at": at,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "changed": changed,
        "replayed": replayed,
    }


def claim_task(
    document: dict[str, Any],
    *,
    task_id: str,
    worker_id: str,
    lease_id: str,
    now: str,
    lease_seconds: int,
    expected_document_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    next_document = _copy_validated(document)
    before = _check_expected(next_document, expected_document_sha256)
    task = _find_task(next_document, task_id)
    worker = _require_id(worker_id, "worker_id")
    lease = _require_id(lease_id, "lease_id")
    instant = _parse_timestamp(now, "now")
    instant_text = _format_timestamp(instant)
    seconds = _require_lease_seconds(task, lease_seconds)
    created = _parse_timestamp(task["created"], "task.created")
    if instant < created:
        raise TaskProtocolError("claim time precedes task creation")
    if task["done"]:
        raise TaskProtocolError(f"task {task_id!r} is already completed")

    execution = task.get("execution")
    attempt = 1
    if execution is not None:
        attempt = execution["attempt"]
        state = execution["state"]
        if state == "leased":
            claimed = _parse_timestamp(execution["claimed_at"], "claimed_at")
            expires = _parse_timestamp(execution["lease_expires_at"], "lease_expires_at")
            if (
                execution["worker_id"] == worker
                and execution["lease_id"] == lease
                and instant < expires
            ):
                if instant < claimed:
                    raise TaskProtocolError(
                        "claim replay time precedes the original claim"
                    )
                return next_document, _receipt(
                    action="claim",
                    task_id=task_id,
                    worker_id=worker,
                    lease_id=lease,
                    attempt=attempt,
                    at=instant_text,
                    before_sha256=before,
                    after_sha256=before,
                    changed=False,
                    replayed=True,
                )
            if instant < expires:
                raise TaskProtocolError(
                    f"task {task_id!r} is actively leased to another authority"
                )
            attempt += 1
        elif state == "available":
            released = _parse_timestamp(execution["released_at"], "released_at")
            if instant < released:
                raise TaskProtocolError("claim time precedes the prior release")
            attempt += 1
        else:
            raise TaskProtocolError(f"task {task_id!r} is already completed")
    if attempt > MAX_ATTEMPT:
        raise TaskProtocolError("task attempt ceiling exceeded")

    expires = instant + timedelta(seconds=seconds)
    task["execution"] = {
        "v": PROTOCOL_VERSION,
        "state": "leased",
        "attempt": attempt,
        "worker_id": worker,
        "lease_id": lease,
        "claimed_at": instant_text,
        "lease_expires_at": _format_timestamp(expires),
        "task_sha256": task_spec_sha256(task),
    }
    validate_protocol_document(next_document)
    after = document_sha256(next_document)
    return next_document, _receipt(
        action="claim",
        task_id=task_id,
        worker_id=worker,
        lease_id=lease,
        attempt=attempt,
        at=instant_text,
        before_sha256=before,
        after_sha256=after,
        changed=True,
        replayed=False,
    )


def renew_task(
    document: dict[str, Any],
    *,
    task_id: str,
    worker_id: str,
    lease_id: str,
    expected_attempt: int,
    now: str,
    lease_seconds: int,
    expected_document_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    next_document = _copy_validated(document)
    before = _check_expected(next_document, expected_document_sha256)
    task = _find_task(next_document, task_id)
    worker = _require_id(worker_id, "worker_id")
    lease = _require_id(lease_id, "lease_id")
    instant = _parse_timestamp(now, "now")
    instant_text = _format_timestamp(instant)
    seconds = _require_lease_seconds(task, lease_seconds)
    execution = task.get("execution")
    if not isinstance(execution, dict) or execution.get("state") != "leased":
        raise TaskProtocolError(f"task {task_id!r} has no active lease")
    attempt = _require_owner(
        execution, worker_id=worker, lease_id=lease, expected_attempt=expected_attempt
    )
    claimed = _parse_timestamp(execution["claimed_at"], "claimed_at")
    current_expiry = _parse_timestamp(execution["lease_expires_at"], "lease_expires_at")
    if instant < claimed:
        raise TaskProtocolError("renewal time precedes the active claim")
    if instant >= current_expiry:
        raise TaskProtocolError("active lease has expired and cannot be renewed")

    new_expiry = instant + timedelta(seconds=seconds)
    if new_expiry == current_expiry:
        return next_document, _receipt(
            action="renew", task_id=task_id, worker_id=worker, lease_id=lease,
            attempt=attempt, at=instant_text, before_sha256=before,
            after_sha256=before, changed=False, replayed=True,
        )
    if new_expiry < current_expiry:
        raise TaskProtocolError("renewal must not shorten the current lease expiry")
    execution["lease_expires_at"] = _format_timestamp(new_expiry)
    validate_protocol_document(next_document)
    after = document_sha256(next_document)
    return next_document, _receipt(
        action="renew", task_id=task_id, worker_id=worker, lease_id=lease,
        attempt=attempt, at=instant_text, before_sha256=before,
        after_sha256=after, changed=True, replayed=False,
    )


def release_task(
    document: dict[str, Any],
    *,
    task_id: str,
    worker_id: str,
    lease_id: str,
    expected_attempt: int,
    now: str,
    expected_document_sha256: str,
    reason: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    next_document = _copy_validated(document)
    before = _check_expected(next_document, expected_document_sha256)
    task = _find_task(next_document, task_id)
    worker = _require_id(worker_id, "worker_id")
    lease = _require_id(lease_id, "lease_id")
    instant = _parse_timestamp(now, "now")
    instant_text = _format_timestamp(instant)
    if reason is not None:
        if not isinstance(reason, str):
            raise TaskProtocolError("release reason must be a string")
        if _utf8_bytes(reason) > MAX_RELEASE_REASON_BYTES:
            raise TaskProtocolError(
                f"release reason exceeds {MAX_RELEASE_REASON_BYTES} UTF-8 bytes"
            )

    execution = task.get("execution")
    if not isinstance(execution, dict):
        raise TaskProtocolError(f"task {task_id!r} has no lease to release")
    attempt = _require_owner(
        execution, worker_id=worker, lease_id=lease, expected_attempt=expected_attempt
    )

    if execution["state"] == "available":
        released = _parse_timestamp(execution["released_at"], "released_at")
        if instant < released:
            raise TaskProtocolError(
                "release replay time precedes the original release"
            )
        if execution.get("release_reason") == reason:
            return next_document, _receipt(
                action="release", task_id=task_id, worker_id=worker, lease_id=lease,
                attempt=attempt, at=instant_text, before_sha256=before,
                after_sha256=before, changed=False, replayed=True,
            )
        raise TaskProtocolError("released lease replay changed the release reason")

    if execution["state"] != "leased":
        raise TaskProtocolError(f"task {task_id!r} is already completed")
    claimed = _parse_timestamp(execution["claimed_at"], "claimed_at")
    expiry = _parse_timestamp(execution["lease_expires_at"], "lease_expires_at")
    if instant < claimed:
        raise TaskProtocolError("release time precedes the active claim")
    if instant >= expiry:
        raise TaskProtocolError("lease expired before release")

    execution["state"] = "available"
    execution["released_at"] = instant_text
    if reason is not None:
        execution["release_reason"] = reason
    validate_protocol_document(next_document)
    after = document_sha256(next_document)
    return next_document, _receipt(
        action="release", task_id=task_id, worker_id=worker, lease_id=lease,
        attempt=attempt, at=instant_text, before_sha256=before,
        after_sha256=after, changed=True, replayed=False,
    )


def complete_task(
    document: dict[str, Any],
    *,
    task_id: str,
    worker_id: str,
    lease_id: str,
    expected_attempt: int,
    completion_id: str,
    result: str,
    now: str,
    expected_document_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    next_document = _copy_validated(document)
    before = _check_expected(next_document, expected_document_sha256)
    task = _find_task(next_document, task_id)
    worker = _require_id(worker_id, "worker_id")
    lease = _require_id(lease_id, "lease_id")
    completion = _require_id(completion_id, "completion_id")
    if not isinstance(result, str):
        raise TaskProtocolError("result must be a string")
    instant = _parse_timestamp(now, "now")
    instant_text = _format_timestamp(instant)

    execution = task.get("execution")
    if task["done"]:
        if not isinstance(execution, dict):
            raise TaskProtocolError("completed task has no execution receipt")
        attempt = _require_owner(
            execution, worker_id=worker, lease_id=lease, expected_attempt=expected_attempt
        )
        if (
            execution.get("state") == "completed"
            and execution.get("completion_id") == completion
            and task.get("result") == result
        ):
            completed = _parse_timestamp(task["completed_at"], "completed_at")
            if instant < completed:
                raise TaskProtocolError(
                    "completion replay time precedes the original completion"
                )
            return next_document, _receipt(
                action="complete", task_id=task_id, worker_id=worker, lease_id=lease,
                attempt=attempt, at=instant_text, before_sha256=before,
                after_sha256=before, changed=False, replayed=True,
            )
        raise TaskProtocolError(
            "completed task replay does not exactly match prior completion"
        )

    if not isinstance(execution, dict) or execution.get("state") != "leased":
        raise TaskProtocolError(f"task {task_id!r} has no active lease")
    attempt = _require_owner(
        execution, worker_id=worker, lease_id=lease, expected_attempt=expected_attempt
    )
    claimed = _parse_timestamp(execution["claimed_at"], "claimed_at")
    expiry = _parse_timestamp(execution["lease_expires_at"], "lease_expires_at")
    if instant < claimed:
        raise TaskProtocolError("completion time precedes the active claim")
    if instant >= expiry:
        raise TaskProtocolError("lease expired before completion")

    task["done"] = True
    task["completed_at"] = instant_text
    task["result"] = result
    execution["state"] = "completed"
    execution["completion_id"] = completion
    execution["result_sha256"] = _result_sha256(result)
    validate_protocol_document(next_document)
    after = document_sha256(next_document)
    return next_document, _receipt(
        action="complete", task_id=task_id, worker_id=worker, lease_id=lease,
        attempt=attempt, at=instant_text, before_sha256=before,
        after_sha256=after, changed=True, replayed=False,
    )


def _emit_transition(document: dict[str, Any], receipt: dict[str, Any]) -> None:
    sys.stdout.write(canonical_document_text(document))
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")), file=sys.stderr)


def _add_common_transition_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--now", required=True, help="trusted offset-aware ISO-8601 instant")
    parser.add_argument(
        "--expected-document-sha256",
        required=True,
        help="semantic digest printed by digest for the exact input snapshot",
    )


def _add_generation_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-attempt",
        required=True,
        type=int,
        help="exact published lease generation/attempt being authorized",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate_parser = sub.add_parser("validate", help="validate base + execution protocol")
    validate_parser.add_argument("path", type=Path)

    digest_parser = sub.add_parser("digest", help="print semantic inbox SHA-256")
    digest_parser.add_argument("path", type=Path)

    claim_parser = sub.add_parser("claim", help="claim or reassign a pending task")
    _add_common_transition_args(claim_parser)
    claim_parser.add_argument("--lease-seconds", required=True, type=int)

    renew_parser = sub.add_parser("renew", help="extend an active lease generation")
    _add_common_transition_args(renew_parser)
    _add_generation_arg(renew_parser)
    renew_parser.add_argument("--lease-seconds", required=True, type=int)

    release_parser = sub.add_parser("release", help="release an owned lease generation")
    _add_common_transition_args(release_parser)
    _add_generation_arg(release_parser)
    release_parser.add_argument("--reason")

    complete_parser = sub.add_parser("complete", help="complete an owned active lease generation")
    _add_common_transition_args(complete_parser)
    _add_generation_arg(complete_parser)
    complete_parser.add_argument("--completion-id", required=True)
    complete_parser.add_argument("--result", required=True)

    args = parser.parse_args(argv)
    try:
        document = parse_protocol_path(args.path)
        if args.command == "validate":
            print(f"{args.path}: protocol-valid ({len(document['tasks'])} tasks)")
            return 0
        if args.command == "digest":
            print(document_sha256(document))
            return 0

        common = {
            "task_id": args.task_id,
            "worker_id": args.worker_id,
            "lease_id": args.lease_id,
            "now": args.now,
            "expected_document_sha256": args.expected_document_sha256,
        }
        if args.command == "claim":
            next_document, receipt = claim_task(
                document, lease_seconds=args.lease_seconds, **common
            )
        elif args.command == "renew":
            next_document, receipt = renew_task(
                document,
                expected_attempt=args.expected_attempt,
                lease_seconds=args.lease_seconds,
                **common,
            )
        elif args.command == "release":
            next_document, receipt = release_task(
                document,
                expected_attempt=args.expected_attempt,
                reason=args.reason,
                **common,
            )
        elif args.command == "complete":
            next_document, receipt = complete_task(
                document,
                expected_attempt=args.expected_attempt,
                completion_id=args.completion_id,
                result=args.result,
                **common,
            )
        else:  # pragma: no cover
            raise AssertionError(args.command)
        _emit_transition(next_document, receipt)
        return 0
    except TaskProtocolError as exc:
        print(f"{getattr(args, 'path', 'inbox')}: PROTOCOL ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
