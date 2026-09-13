#!/usr/bin/env python3
"""Validate the repository-backed LocalDeviceAgent task inbox."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SUPPORTED_VERSION = 1
SUPPORTED_KINDS = frozenset({"run_task"})


class InboxValidationError(ValueError):
    """Raised when the task inbox is malformed or internally inconsistent."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InboxValidationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(token: str) -> Any:
    raise InboxValidationError(f"invalid JSON constant: {token}")


def _validate_json_domain(value: Any) -> None:
    stack: list[tuple[str, Any]] = [("root", value)]
    while stack:
        where, current = stack.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise InboxValidationError(
                    f"{where}: unpaired Unicode surrogate is not allowed"
                )
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise InboxValidationError(
                    f"{where}: non-finite JSON number is not allowed"
                )
            continue
        if isinstance(current, list):
            stack.extend(
                (f"{where}[{index}]", item)
                for index, item in reversed(list(enumerate(current)))
            )
            continue
        if isinstance(current, dict):
            for key, item in reversed(list(current.items())):
                stack.append((f"{where}.{key}", item))
                stack.append((f"{where} key", key))


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise InboxValidationError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InboxValidationError(f"{field}: expected a non-empty ISO-8601 string")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise InboxValidationError(f"{field}: invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InboxValidationError(f"{field}: timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _validate_task(task: Any, index: int, seen_ids: set[str]) -> None:
    where = f"tasks[{index}]"
    if not isinstance(task, dict):
        raise InboxValidationError(f"{where}: expected a JSON object")

    task_id = _require(task, "id", where)
    if not isinstance(task_id, str) or not task_id.strip():
        raise InboxValidationError(f"{where}.id: expected a non-empty string")
    if task_id != task_id.strip():
        raise InboxValidationError(f"{where}.id: surrounding whitespace is not allowed")
    if task_id in seen_ids:
        raise InboxValidationError(f"{where}.id: duplicate task id {task_id!r}")
    seen_ids.add(task_id)

    created = _parse_timestamp(_require(task, "created", where), f"{where}.created")

    kind = _require(task, "kind", where)
    if not isinstance(kind, str) or kind not in SUPPORTED_KINDS:
        supported = ", ".join(sorted(SUPPORTED_KINDS))
        raise InboxValidationError(
            f"{where}.kind: unsupported task kind {kind!r}; expected one of {supported}"
        )

    command = _require(task, "command", where)
    if not isinstance(command, str) or not command.strip():
        raise InboxValidationError(f"{where}.command: expected a non-empty string")

    timeout_s = _require(task, "timeout_s", where)
    if type(timeout_s) is not int or timeout_s <= 0:
        raise InboxValidationError(f"{where}.timeout_s: expected a positive integer")

    done = _require(task, "done", where)
    if type(done) is not bool:
        raise InboxValidationError(f"{where}.done: expected a JSON boolean")

    if done:
        completed = _parse_timestamp(
            _require(task, "completed_at", where), f"{where}.completed_at"
        )
        if completed < created:
            raise InboxValidationError(
                f"{where}.completed_at: completion precedes task creation"
            )
        result = _require(task, "result", where)
        if not isinstance(result, str):
            raise InboxValidationError(f"{where}.result: expected a string")
    else:
        for field in ("completed_at", "result"):
            if field in task:
                raise InboxValidationError(
                    f"{where}.{field}: pending tasks must not carry completion fields"
                )


def validate_text(text: str) -> dict[str, Any]:
    """Parse and validate one inbox JSON document."""
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except InboxValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise InboxValidationError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except ValueError as exc:
        raise InboxValidationError(f"invalid JSON number: {exc}") from exc
    except RecursionError as exc:
        raise InboxValidationError("JSON nesting exceeds the supported depth") from exc

    _validate_json_domain(document)
    if not isinstance(document, dict):
        raise InboxValidationError("root: expected a JSON object")

    version = _require(document, "v", "root")
    if type(version) is not int or version != SUPPORTED_VERSION:
        raise InboxValidationError(
            f"root.v: expected integer version {SUPPORTED_VERSION}"
        )

    tasks = _require(document, "tasks", "root")
    if not isinstance(tasks, list):
        raise InboxValidationError("root.tasks: expected a JSON array")

    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        _validate_task(task, index, seen_ids)
    return document


def validate_path(path: Path) -> dict[str, Any]:
    """Read one UTF-8 file and validate its inbox contract."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InboxValidationError(f"unable to read UTF-8 JSON: {exc}") from exc
    return validate_text(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("inbox.json"),
        help="inbox JSON to validate (default: inbox.json)",
    )
    args = parser.parse_args(argv)
    try:
        document = validate_path(args.path)
    except InboxValidationError as exc:
        print(f"{args.path}: INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"{args.path}: valid ({len(document['tasks'])} tasks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
