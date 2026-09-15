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


class EffectLedgerSidecarCustodyTests(unittest.TestCase):
    SUFFIXES = ("-wal", "-shm", "-journal")

    def _victim(self, root: Path) -> Path:
        path = root / "foreign-sidecar-victim.bin"
        path.write_bytes((b"FOREIGN-SIDECAR-SENTINEL\n" * 200)[:4096])
        os.chmod(path, 0o600)
        return path

    def _snapshot(self, path: Path):
        info = os.stat(path)
        return (
            path.read_bytes(),
            stat.S_IMODE(info.st_mode),
            info.st_dev,
            info.st_ino,
            info.st_nlink,
        )

    def _assert_snapshot(self, path: Path, expected) -> None:
        self.assertEqual(self._snapshot(path), expected)

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_preexisting_hardlink_sidecars_fail_before_sqlite_open(self):
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ledger = root / "ledger.sqlite"
                victim = self._victim(root)
                alias = Path(str(ledger) + suffix)
                os.link(victim, alias)
                before = self._snapshot(victim)
                alias_info = os.stat(alias)

                with mock.patch("effect_receipt_ledger.sqlite3.connect") as connect:
                    with self.assertRaisesRegex(EffectError, "DB_AUXILIARY_PATH_UNSAFE"):
                        EffectLedger(ledger)
                connect.assert_not_called()

                self._assert_snapshot(victim, before)
                self.assertTrue(alias.exists())
                after_alias = os.stat(alias)
                self.assertEqual((after_alias.st_dev, after_alias.st_ino), (alias_info.st_dev, alias_info.st_ino))
                self.assertEqual(after_alias.st_nlink, alias_info.st_nlink)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_preexisting_symlink_sidecars_fail_before_sqlite_open(self):
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ledger = root / "ledger.sqlite"
                victim = self._victim(root)
                alias = Path(str(ledger) + suffix)
                os.symlink(victim.name, alias)
                before = self._snapshot(victim)

                with mock.patch("effect_receipt_ledger.sqlite3.connect") as connect:
                    with self.assertRaisesRegex(EffectError, "DB_AUXILIARY_PATH_UNSAFE"):
                        EffectLedger(ledger)
                connect.assert_not_called()

                self._assert_snapshot(victim, before)
                self.assertTrue(alias.is_symlink())
                self.assertEqual(os.readlink(alias), victim.name)

    def test_preexisting_broad_mode_sidecars_fail_closed(self):
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ledger = root / "ledger.sqlite"
                alias = Path(str(ledger) + suffix)
                alias.write_bytes(b"not-sqlite-sidecar")
                os.chmod(alias, 0o640)
                before = self._snapshot(alias)

                with mock.patch("effect_receipt_ledger.sqlite3.connect") as connect:
                    with self.assertRaisesRegex(EffectError, "DB_AUXILIARY_PATH_UNSAFE"):
                        EffectLedger(ledger)
                connect.assert_not_called()
                self._assert_snapshot(alias, before)

    @unittest.skipUnless(hasattr(os, "link"), "hard links unavailable")
    def test_alias_installed_between_connections_is_rejected_at_next_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "ledger.sqlite"
            ledger = EffectLedger(path)
            victim = self._victim(root)
            alias = Path(str(path) + "-wal")
            # A closed WAL connection normally removes its transient sidecars. If a
            # platform retains one here, remove only this test-owned generated path
            # before simulating the foreign generation.
            if alias.exists() or alias.is_symlink():
                alias.unlink()
            os.link(victim, alias)
            before = self._snapshot(victim)

            with mock.patch("effect_receipt_ledger.sqlite3.connect", wraps=sqlite3.connect) as connect:
                with self.assertRaisesRegex(EffectError, "DB_AUXILIARY_PATH_UNSAFE"):
                    ledger.inspect(task_id="missing-task", effect_id="missing-effect")
            connect.assert_not_called()
            self._assert_snapshot(victim, before)
            self.assertTrue(alias.exists())

    def test_wal_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.sqlite"
            EffectLedger(path)
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                con.close()

    def test_non_wal_effective_mode_fails_before_schema_initialization(self):
        class _ModeCursor:
            @staticmethod
            def fetchone():
                return ("delete",)

        class _NonWalConnection:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, statement):
                self.statements.append(statement)
                if statement == "PRAGMA journal_mode=WAL":
                    return _ModeCursor()
                raise AssertionError("schema initialization must not run without WAL")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.sqlite"
            fake = _NonWalConnection()
            with mock.patch.object(EffectLedger, "_connect", return_value=fake):
                with self.assertRaisesRegex(EffectError, "DB_WAL_REQUIRED"):
                    EffectLedger(path)

            self.assertTrue(fake.closed)
            self.assertEqual(fake.statements, ["PRAGMA journal_mode=WAL"])


if __name__ == "__main__":
    unittest.main()
