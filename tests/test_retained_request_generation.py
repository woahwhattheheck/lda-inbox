"""Focused ordinary file-generation recovery checks; no provider or task calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inbox_github as publisher


class RetainedRequestGeneration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "request.json"
        self.path.write_bytes(b'{"request":"original"}')

    def replace(self):
        successor = self.path.with_suffix(".new")
        successor.write_bytes(b'{"request":"replaced"}')
        successor.replace(self.path)

    def mutate(self):
        before = self.path.stat()
        self.path.write_bytes(b'{"request":"modified"}')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    def during_read(self, change):
        original_read = os.read
        changed = False

        def read(fd, count):
            nonlocal changed
            result = original_read(fd, count)
            if not changed:
                changed = True
                change()
            return result

        with patch.object(publisher.os, "read", side_effect=read):
            with self.assertRaises(publisher.InboxGitHubError):
                publisher.load_private_file(self.path)

    def test_stable_regular_request(self):
        self.assertEqual(publisher.load_private_file(self.path), {"request": "original"})

    def test_symlink_is_rejected(self):
        link = self.path.with_suffix(".link")
        link.symlink_to(self.path)
        with self.assertRaises(publisher.InboxGitHubError):
            publisher.load_private_file(link)

    def test_replacement_between_lstat_and_open(self):
        original_open = os.open
        def open_file(path, flags):
            self.replace()
            return original_open(path, flags)
        with patch.object(publisher.os, "open", side_effect=open_file):
            with self.assertRaises(publisher.InboxGitHubError):
                publisher.load_private_file(self.path)

    def test_visible_path_replacement_during_read(self):
        self.during_read(self.replace)

    def test_same_size_in_place_edit_during_read(self):
        self.during_read(self.mutate)

    def test_truncation_during_read(self):
        self.during_read(lambda: self.path.write_bytes(b'{}'))

    def test_edit_during_final_path_observation(self):
        original_lstat = Path.lstat
        calls = 0
        def lstat(path):
            nonlocal calls
            observation = original_lstat(path)
            calls += 1
            if calls == 2:
                self.mutate()
            return observation
        with patch.object(Path, "lstat", lstat):
            with self.assertRaises(publisher.InboxGitHubError):
                publisher.load_private_file(self.path)


if __name__ == "__main__":
    unittest.main()
