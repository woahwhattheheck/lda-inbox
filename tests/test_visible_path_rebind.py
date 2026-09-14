from __future__ import annotations

import os
import tempfile
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from validate_inbox import InboxValidationError, _visible_path_matches, validate_path


class VisiblePathRebindTests(unittest.TestCase):
    def test_final_path_authority_requires_stable_inode_identity(self) -> None:
        mode = stat.S_IFREG | 0o600
        opened = SimpleNamespace(
            st_dev=1, st_ino=0, st_mode=mode, st_nlink=1, st_size=10
        )
        visible = SimpleNamespace(
            st_dev=1, st_ino=0, st_mode=mode, st_nlink=1, st_size=10
        )
        self.assertFalse(_visible_path_matches(opened, visible))

    def test_post_read_foreign_replacement_is_rejected_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "inbox.json"
            retained_name = root / "opened-generation.json"
            staged_replacement = root / "replacement.json"
            payload = b'{"v":1,"tasks":[]}'
            path.write_bytes(payload)
            staged_replacement.write_bytes(payload)
            original_inode = path.stat().st_ino
            replacement_inode = staged_replacement.stat().st_ino
            if not original_inode or not replacement_inode:
                self.skipTest("filesystem does not expose stable inode identity")
            self.assertNotEqual(original_inode, replacement_inode)

            real_lstat = Path.lstat
            path_lstats = 0
            swapped = False

            def lstat_then_rebind(candidate: Path):
                nonlocal path_lstats, swapped
                if candidate == path:
                    path_lstats += 1
                    if path_lstats == 2:
                        os.replace(path, retained_name)
                        os.replace(staged_replacement, path)
                        swapped = True
                return real_lstat(candidate)

            with mock.patch.object(Path, "lstat", lstat_then_rebind):
                with self.assertRaisesRegex(
                    InboxValidationError,
                    "path changed while reading",
                ):
                    validate_path(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(retained_name.read_bytes(), payload)
            self.assertEqual(path.stat().st_ino, replacement_inode)
            self.assertEqual(retained_name.stat().st_ino, original_inode)


if __name__ == "__main__":
    unittest.main()
