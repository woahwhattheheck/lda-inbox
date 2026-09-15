from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from effect_receipt_common import EffectError
from effect_receipt_ledger import EffectLedger


class EffectLedgerPathCustodyTests(unittest.TestCase):
    def _victim(self, root: Path) -> Path:
        path = root / "victim.sqlite"
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            con.execute("INSERT INTO sentinel(value) VALUES('keep')")
            con.commit()
        finally:
            con.close()
        os.chmod(path, 0o644)
        return path

    def _snapshot(self, path: Path):
        return path.read_bytes(), stat.S_IMODE(os.stat(path).st_mode)

    def _assert_victim_unchanged(self, path: Path, snapshot) -> None:
        self.assertEqual(self._snapshot(path), snapshot)
        con = sqlite3.connect(path)
        try:
            self.assertEqual(
                con.execute("SELECT value FROM sentinel").fetchone()[0],
                "keep",
            )
            self.assertIsNone(
                con.execute(
                    "SELECT name FROM sqlite_master WHERE name='effects'"
                ).fetchone()
            )
        finally:
            con.close()

    def test_fresh_database_is_created_private_and_usable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.sqlite"
            EffectLedger(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            con = sqlite3.connect(path)
            try:
                self.assertIsNotNone(
                    con.execute(
                        "SELECT name FROM sqlite_master WHERE name='effects'"
                    ).fetchone()
                )
            finally:
                con.close()

    def test_existing_single_link_database_is_hardened_and_reused(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.sqlite"
            EffectLedger(path)
            os.chmod(path, 0o644)
            EffectLedger(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_final_symlink_is_rejected_without_touching_foreign_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            victim = self._victim(root)
            before = self._snapshot(victim)
            alias = root / "ledger.sqlite"
            os.symlink(victim.name, alias)
            with self.assertRaisesRegex(EffectError, "DB_PATH_UNSAFE"):
                EffectLedger(alias)
            self._assert_victim_unchanged(victim, before)

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_hardlink_alias_is_rejected_without_touching_foreign_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            victim = self._victim(root)
            before = self._snapshot(victim)
            alias = root / "ledger.sqlite"
            os.link(victim, alias)
            try:
                with self.assertRaisesRegex(EffectError, "DB_PATH_UNSAFE"):
                    EffectLedger(alias)
            finally:
                alias.unlink()
            self._assert_victim_unchanged(victim, before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_final_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real"
            real.mkdir(mode=0o700)
            alias = root / "link"
            os.symlink(real.name, alias)
            with self.assertRaisesRegex(EffectError, "DB_PARENT_UNSAFE"):
                EffectLedger(alias / "ledger.sqlite")
            self.assertFalse((real / "ledger.sqlite").exists())

    def test_group_writable_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "unsafe"
            parent.mkdir()
            os.chmod(parent, 0o770)
            with self.assertRaisesRegex(EffectError, "DB_PARENT_UNSAFE"):
                EffectLedger(parent / "ledger.sqlite")
            self.assertFalse((parent / "ledger.sqlite").exists())

    def test_non_directory_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "not-a-directory"
            parent.write_text("sentinel", encoding="utf-8")
            with self.assertRaisesRegex(EffectError, "DB_PARENT_PREPARE_FAILED"):
                EffectLedger(parent / "ledger.sqlite")
            self.assertEqual(parent.read_text(encoding="utf-8"), "sentinel")

    def test_descriptor_path_unavailable_fails_closed_without_path_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.sqlite"
            with mock.patch(
                "effect_receipt_ledger._descriptor_db_uri",
                side_effect=EffectError("DB_DESCRIPTOR_PATH_UNAVAILABLE"),
            ), mock.patch("effect_receipt_ledger.sqlite3.connect") as connect:
                with self.assertRaisesRegex(
                    EffectError, "DB_DESCRIPTOR_PATH_UNAVAILABLE"
                ):
                    EffectLedger(path)
            connect.assert_not_called()
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_rebind_during_sqlite_open_fails_before_foreign_schema_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = root / "ledger.sqlite"
            victim = self._victim(root)
            before = self._snapshot(victim)
            reserved = root / "reserved-ledger.sqlite"
            real_connect = sqlite3.connect
            swapped = False

            def swap_then_connect(filename, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    ledger.rename(reserved)
                    os.symlink(victim.name, ledger)
                return real_connect(filename, *args, **kwargs)

            with mock.patch(
                "effect_receipt_ledger.sqlite3.connect",
                side_effect=swap_then_connect,
            ):
                with self.assertRaisesRegex(EffectError, "DB_PATH_REBOUND"):
                    EffectLedger(ledger)
            self._assert_victim_unchanged(victim, before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_aba_rebind_opens_retained_descriptor_not_foreign_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = root / "ledger.sqlite"
            victim = self._victim(root)
            before = self._snapshot(victim)
            reserved = root / "reserved-ledger.sqlite"
            real_connect = sqlite3.connect
            opened_names = []
            swapped = False

            def swap_open_restore(filename, *args, **kwargs):
                nonlocal swapped
                opened_names.append(os.fspath(filename))
                if not swapped:
                    swapped = True
                    ledger.rename(reserved)
                    os.symlink(victim.name, ledger)
                    try:
                        return real_connect(filename, *args, **kwargs)
                    finally:
                        ledger.unlink()
                        reserved.rename(ledger)
                return real_connect(filename, *args, **kwargs)

            with mock.patch(
                "effect_receipt_ledger.sqlite3.connect",
                side_effect=swap_open_restore,
            ):
                try:
                    EffectLedger(ledger)
                except (EffectError, sqlite3.Error):
                    # Renaming the anchored database during SQLite open may make
                    # WAL setup fail closed. The security invariant is that the
                    # returned connection can never target the foreign database.
                    pass

            self.assertTrue(swapped)
            self.assertTrue(opened_names)
            self.assertRegex(
                opened_names[0],
                r"^file:(?://)?/(?:proc/self/fd|dev/fd)/[0-9]+[?]mode=rw$",
            )
            self._assert_victim_unchanged(victim, before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_dangling_rebind_cannot_create_foreign_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = root / "ledger.sqlite"
            reserved = root / "reserved-ledger.sqlite"
            foreign = root / "must-not-exist.sqlite"
            real_connect = sqlite3.connect
            swapped = False

            def swap_then_connect(filename, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    ledger.rename(reserved)
                    os.symlink(foreign.name, ledger)
                return real_connect(filename, *args, **kwargs)

            with mock.patch(
                "effect_receipt_ledger.sqlite3.connect",
                side_effect=swap_then_connect,
            ):
                with self.assertRaisesRegex(EffectError, "DB_PATH_REBOUND"):
                    EffectLedger(ledger)
            self.assertFalse(foreign.exists())


if __name__ == "__main__":
    unittest.main()
