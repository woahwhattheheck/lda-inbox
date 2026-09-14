#!/usr/bin/env python3
"""Validate the repository-backed LocalDeviceAgent task inbox.

The mature semantic validator is preserved byte-for-byte in
``_validate_inbox_base.py``.  This public facade strengthens only the local
regular-file custody boundary and delegates parsed bytes to that validator.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

import _validate_inbox_base as _base
from _validate_inbox_base import *  # noqa: F401,F403


def _same_opened_generation(before: os.stat_result, after: os.stat_result) -> bool:
    """Require the held regular-file generation to remain unchanged."""
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(before, field, None) == getattr(after, field, None) for field in fields)


def _visible_path_matches(opened: os.stat_result, visible: os.stat_result) -> bool:
    """Require the final visible path to name the retained regular file."""
    if not stat.S_ISREG(visible.st_mode):
        return False

    opened_inode = getattr(opened, "st_ino", 0)
    visible_inode = getattr(visible, "st_ino", 0)
    if opened_inode and visible_inode:
        if (opened.st_dev, opened_inode) != (visible.st_dev, visible_inode):
            return False

    fields = ("st_dev", "st_mode", "st_nlink", "st_size")
    return all(getattr(opened, field, None) == getattr(visible, field, None) for field in fields)


def validate_path(path: Path) -> dict[str, Any]:
    """Read one bounded regular-file generation and validate its inbox contract."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise InboxValidationError(f"unable to stat inbox JSON: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise InboxValidationError("inbox JSON path must name a regular file")
    if before.st_size > MAX_DOCUMENT_BYTES:
        raise InboxValidationError(
            f"document exceeds {MAX_DOCUMENT_BYTES} bytes on disk ({before.st_size} bytes)"
        )

    flags = os.O_RDONLY
    for flag_name in ("O_BINARY", "O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, flag_name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InboxValidationError(f"unable to open inbox JSON: {exc}") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise InboxValidationError("inbox JSON path must name a regular file")
        if not _base._stable_file_identity(before, opened):
            raise InboxValidationError("inbox JSON path changed while opening")
        if opened.st_size > MAX_DOCUMENT_BYTES:
            raise InboxValidationError(
                f"document exceeds {MAX_DOCUMENT_BYTES} bytes on disk ({opened.st_size} bytes)"
            )

        chunks: list[bytes] = []
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise InboxValidationError(
                f"document exceeds {MAX_DOCUMENT_BYTES} bytes while reading"
            )
        if len(raw) != opened.st_size:
            raise InboxValidationError("inbox JSON byte count changed while reading")

        after = os.fstat(descriptor)
        if not _same_opened_generation(opened, after):
            raise InboxValidationError("inbox JSON file changed while reading")
        try:
            visible_after = path.lstat()
        except OSError as exc:
            raise InboxValidationError("inbox JSON path changed while reading") from exc
        if not _visible_path_matches(after, visible_after):
            raise InboxValidationError("inbox JSON path changed while reading")
    except InboxValidationError:
        raise
    except OSError as exc:
        raise InboxValidationError(f"unable to read inbox JSON: {exc}") from exc
    finally:
        os.close(descriptor)

    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise InboxValidationError(f"unable to read UTF-8 JSON: {exc}") from exc
    return validate_text(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_base.__doc__)
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
