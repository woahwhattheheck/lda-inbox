from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from validate_inbox import InboxValidationError, main, validate_path, validate_text

ROOT = Path(__file__).resolve().parents[1]


def task(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "task-1",
        "created": "2026-09-13T05:00:00+00:00",
        "kind": "run_task",
        "command": "report status",
        "timeout_s": 120,
        "done": False,
    }
    value.update(overrides)
    return value


def document(*tasks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"v": 1, "tasks": list(tasks)}
    value.update(extra)
    return value


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class InboxValidationTests(unittest.TestCase):
    def assert_invalid(self, value: Any, pattern: str) -> None:
        with self.assertRaisesRegex(InboxValidationError, pattern):
            validate_text(value if isinstance(value, str) else encode(value))

    def test_repository_inbox_is_valid(self) -> None:
        parsed = validate_path(ROOT / "inbox.json")
        self.assertEqual(1, parsed["v"])
        self.assertEqual(2, len(parsed["tasks"]))

    def test_duplicate_json_keys_fail_closed_at_any_depth(self) -> None:
        for payload in (
            '{"v":1,"v":1,"tasks":[]}',
            '{"v":1,"tasks":[{"id":"a","id":"b"}]}',
        ):
            with self.subTest(payload=payload):
                self.assert_invalid(payload, "duplicate JSON key")

    def test_root_contract_rejects_wrong_shapes_and_versions(self) -> None:
        cases = (
            ([], "root: expected"),
            ({"tasks": []}, "missing required field 'v'"),
            ({"v": True, "tasks": []}, "expected integer version 1"),
            ({"v": 2, "tasks": []}, "expected integer version 1"),
            ({"v": 1, "tasks": {}}, "expected a JSON array"),
        )
        for value, pattern in cases:
            with self.subTest(value=value):
                self.assert_invalid(value, pattern)

    def test_tasks_must_be_objects_with_unique_clean_ids(self) -> None:
        self.assert_invalid(document("not-an-object"), "expected a JSON object")
        self.assert_invalid(document(task(id="")), "expected a non-empty string")
        self.assert_invalid(document(task(id=" padded ")), "surrounding whitespace")
        self.assert_invalid(
            document(task(id="same"), task(id="same")), "duplicate task id"
        )

    def test_run_task_fields_are_strict_but_additive_fields_are_allowed(self) -> None:
        self.assert_invalid(document(task(kind="future_kind")), "unsupported task kind")
        self.assert_invalid(document(task(command="  ")), "expected a non-empty string")
        parsed = validate_text(
            encode(document(task(metadata={"trace": "abc"}), producer="test"))
        )
        self.assertEqual("abc", parsed["tasks"][0]["metadata"]["trace"])
        self.assertEqual("test", parsed["producer"])

    def test_timeout_is_a_positive_integer_not_python_truthiness(self) -> None:
        for value in (True, False, 0, -1, 1.5, "120", None):
            with self.subTest(value=value):
                self.assert_invalid(
                    document(task(timeout_s=value)), "expected a positive integer"
                )
        validate_text(encode(document(task(timeout_s=1))))

    def test_timestamps_require_offsets(self) -> None:
        for value in ("", "not-a-time", "2026-09-13T05:00:00"):
            with self.subTest(value=value):
                self.assert_invalid(document(task(created=value)), "timestamp|ISO-8601")

    def test_completed_tasks_require_coherent_receipts(self) -> None:
        base = task(
            done=True,
            completed_at="2026-09-13T05:01:00Z",
            result="ok",
        )
        validate_text(encode(document(base)))

        for field in ("completed_at", "result"):
            broken = copy.deepcopy(base)
            del broken[field]
            with self.subTest(missing=field):
                self.assert_invalid(document(broken), f"missing required field '{field}'")

        self.assert_invalid(
            document(task(done=True, completed_at="2026-09-13T05:01:00Z", result={})),
            "result: expected a string",
        )
        self.assert_invalid(
            document(
                task(
                    done=True,
                    completed_at="2026-09-13T04:59:59+00:00",
                    result="impossible",
                )
            ),
            "completion precedes task creation",
        )

    def test_pending_tasks_cannot_carry_terminal_fields(self) -> None:
        for field, value in (
            ("completed_at", "2026-09-13T05:01:00+00:00"),
            ("result", "premature"),
        ):
            with self.subTest(field=field):
                self.assert_invalid(
                    document(task(**{field: value})),
                    "pending tasks must not carry completion fields",
                )

    def test_cli_reports_valid_and_invalid_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            valid_path = Path(temp_dir) / "valid.json"
            invalid_path = Path(temp_dir) / "invalid.json"
            valid_path.write_text(encode(document(task())), encoding="utf-8")
            invalid_path.write_text('{"v":1,"tasks":{}}', encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(0, main([str(valid_path)]))
            self.assertIn("valid (1 tasks)", stdout.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(2, main([str(invalid_path)]))
            self.assertIn("INVALID", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
