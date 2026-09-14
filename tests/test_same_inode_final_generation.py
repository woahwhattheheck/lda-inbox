from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from validate_inbox import InboxValidationError, validate_path


ORIGINAL = b'{"v":1,"tasks":[]}'
MUTATED = b'{"v":1,"tasks":{}}'


class SameInodeFinalGenerationTests(unittest.TestCase):
    def _exercise(self, *, via_alias: bool) -> None:
        self.assertEqual(len(ORIGINAL), len(MUTATED))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "inbox.json"
            path.write_bytes(ORIGINAL)
            target = path
            alias = root / "alias.json"
            if via_alias:
                try:
                    os.link(path, alias)
                except OSError as exc:
                    self.skipTest(f"hard links unavailable: {exc}")
                target = alias

            original_inode = path.stat().st_ino
            if not original_inode:
                self.skipTest("filesystem does not expose stable inode identity")

            real_lstat = Path.lstat
            path_lstats = 0
            mutated = False

            def mutate_same_inode_before_final_visible_stat(candidate: Path):
                nonlocal path_lstats, mutated
                if candidate == path:
                    path_lstats += 1
                    if path_lstats == 2:
                        target.write_bytes(MUTATED)
                        mutated = True
                return real_lstat(candidate)

            with mock.patch.object(
                Path,
                "lstat",
                mutate_same_inode_before_final_visible_stat,
            ):
                with self.assertRaisesRegex(
                    InboxValidationError,
                    "file changed while reading|path changed while reading",
                ):
                    validate_path(path)

            self.assertTrue(mutated)
            self.assertEqual(path.stat().st_ino, original_inode)
            self.assertEqual(path.read_bytes(), MUTATED)
            if via_alias:
                self.assertEqual(alias.stat().st_ino, original_inode)
                self.assertEqual(alias.read_bytes(), MUTATED)

    def test_same_inode_same_size_mutation_after_final_read_snapshot_is_rejected(self) -> None:
        self._exercise(via_alias=False)

    def test_hard_link_alias_write_after_final_read_snapshot_is_rejected(self) -> None:
        self._exercise(via_alias=True)


if __name__ == "__main__":
    unittest.main()
