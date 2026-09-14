from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from validate_inbox import (
    MAX_COMMAND_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_RESULT_BYTES,
    MAX_TASKS,
    MAX_TASK_ID_BYTES,
    MAX_TIMEOUT_S,
    InboxValidationError,
    validate_path,
    validate_text,
)


def task(index: int = 1, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": f"task-{index}",
        "created": "2026-09-13T05:00:00+00:00",
        "kind": "run_task",
        "command": "report status",
        "timeout_s": 120,
        "done": False,
    }
    value.update(overrides)
    return value


def encode(*tasks: dict[str, object], **extra: object) -> str:
    value: dict[str, object] = {"v": 1, "tasks": list(tasks)}
    value.update(extra)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class InboxResourceEnvelopeTests(unittest.TestCase):
    def assert_invalid(self, text: str, pattern: str) -> None:
        with self.assertRaisesRegex(InboxValidationError, pattern):
            validate_text(text)

    def test_task_ids_are_canonical_ascii_transport_identifiers(self) -> None:
        for bad_id in (
            ".starts-with-dot",
            "contains/slash",
            "contains:colon",
            "contains space",
            "contains\nnewline",
            "tásk-unicode",
        ):
            with self.subTest(task_id=bad_id):
                self.assert_invalid(encode(task(id=bad_id)), "canonical ASCII identifier")

        for good_id in ("a", "task-1", "T_20260913.0042", "9-end"):
            with self.subTest(task_id=good_id):
                parsed = validate_text(encode(task(id=good_id)))
                self.assertEqual(good_id, parsed["tasks"][0]["id"])

    def test_task_id_byte_ceiling_is_enforced(self) -> None:
        valid_id = "a" + "x" * (MAX_TASK_ID_BYTES - 1)
        validate_text(encode(task(id=valid_id)))
        self.assert_invalid(
            encode(task(id=valid_id + "x")),
            rf"exceeds {MAX_TASK_ID_BYTES} UTF-8 bytes",
        )

    def test_task_count_has_a_hard_ceiling(self) -> None:
        validate_text(encode(*(task(index) for index in range(MAX_TASKS))))
        self.assert_invalid(
            encode(*(task(index) for index in range(MAX_TASKS + 1))),
            rf"exceeds maximum {MAX_TASKS} tasks",
        )

    def test_timeout_has_a_hard_ceiling(self) -> None:
        validate_text(encode(task(timeout_s=MAX_TIMEOUT_S)))
        self.assert_invalid(
            encode(task(timeout_s=MAX_TIMEOUT_S + 1)),
            rf"exceeds maximum {MAX_TIMEOUT_S} seconds",
        )

    def test_command_limit_counts_utf8_bytes_not_python_characters(self) -> None:
        validate_text(encode(task(command="x" * MAX_COMMAND_BYTES)))
        self.assert_invalid(
            encode(task(command="🚀" * (MAX_COMMAND_BYTES // 4 + 1))),
            rf"exceeds {MAX_COMMAND_BYTES} UTF-8 bytes",
        )

    def test_completed_result_has_a_bounded_receipt(self) -> None:
        common = {
            "done": True,
            "completed_at": "2026-09-13T05:01:00+00:00",
        }
        validate_text(encode(task(result="x" * MAX_RESULT_BYTES, **common)))
        self.assert_invalid(
            encode(task(result="x" * (MAX_RESULT_BYTES + 1), **common)),
            rf"exceeds {MAX_RESULT_BYTES} UTF-8 bytes",
        )

    def test_document_size_is_rejected_before_json_work(self) -> None:
        oversized = " " * (MAX_DOCUMENT_BYTES + 1)
        self.assert_invalid(
            oversized,
            rf"document exceeds {MAX_DOCUMENT_BYTES} UTF-8 bytes",
        )

    def test_path_size_is_rejected_before_reading_the_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "oversized.json"
            path.write_bytes(b" " * (MAX_DOCUMENT_BYTES + 1))
            with self.assertRaisesRegex(
                InboxValidationError,
                rf"document exceeds {MAX_DOCUMENT_BYTES} bytes on disk",
            ):
                validate_path(path)


    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_path_rejects_fifo_without_opening_or_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(InboxValidationError, "regular file"):
                validate_path(path)

    def test_path_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.json"
            link = Path(temp_dir) / "inbox.json"
            target.write_text(encode(task()), encoding="utf-8")
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(InboxValidationError, "regular file"):
                validate_path(link)

    def test_path_rejects_replacement_between_lstat_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.json"
            replacement = Path(temp_dir) / "replacement.json"
            path.write_text(encode(task(command="first")), encoding="utf-8")
            replacement.write_text(encode(task(command="replacement")), encoding="utf-8")
            if not path.stat().st_ino:
                self.skipTest("filesystem does not expose stable inode identity")

            real_open = os.open
            swapped = False

            def swap_then_open(candidate: object, flags: int) -> int:
                nonlocal swapped
                if not swapped:
                    os.replace(replacement, path)
                    swapped = True
                return real_open(candidate, flags)

            with mock.patch("validate_inbox.os.open", side_effect=swap_then_open):
                with self.assertRaisesRegex(InboxValidationError, "changed while opening"):
                    validate_path(path)

    def test_path_read_is_bounded_even_if_regular_file_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.json"
            path.write_text(encode(task()), encoding="utf-8")
            real_read = os.read
            grown = False

            def grow_then_read(descriptor: int, size: int) -> bytes:
                nonlocal grown
                if not grown:
                    with path.open("ab") as stream:
                        stream.write(b" " * (MAX_DOCUMENT_BYTES + 1))
                    grown = True
                return real_read(descriptor, size)

            with mock.patch("validate_inbox.os.read", side_effect=grow_then_read):
                with self.assertRaisesRegex(
                    InboxValidationError,
                    rf"document exceeds {MAX_DOCUMENT_BYTES} bytes while reading",
                ):
                    validate_path(path)

    def test_path_rejects_invalid_utf8_from_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(InboxValidationError, "UTF-8"):
                validate_path(path)


if __name__ == "__main__":
    unittest.main()
