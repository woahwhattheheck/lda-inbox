from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Union

from effect_receipt_common import EffectError


SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _owned(info: os.stat_result) -> bool:
    return not hasattr(os, "geteuid") or info.st_uid == os.geteuid()


def _sidecar_path(db_path: Union[os.PathLike[str], str], suffix: str) -> Path:
    return Path(os.fspath(db_path) + suffix)


def assert_sidecar_namespace_safe(db_path: Union[os.PathLike[str], str]) -> None:
    """Reject pre-existing SQLite auxiliary names that can alias foreign storage.

    This is a namespace precondition, not a custom SQLite VFS.  It deliberately
    fails closed on symlinks, non-regular files, foreign ownership, extra hard
    links, and group/world-writable sidecars before Python asks SQLite to use
    the database.
    """
    for suffix in SIDECAR_SUFFIXES:
        path = _sidecar_path(db_path, suffix)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EffectError("DB_SIDECAR_STAT_FAILED") from exc
        if not stat.S_ISREG(info.st_mode):
            raise EffectError("DB_SIDECAR_UNSAFE")
        if not _owned(info):
            raise EffectError("DB_SIDECAR_UNSAFE")
        if info.st_nlink != 1:
            raise EffectError("DB_SIDECAR_UNSAFE")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise EffectError("DB_SIDECAR_UNSAFE")


class SidecarGuardMixin:
    """Re-check sidecar namespace safety at Python-visible SQLite boundaries.

    These checks close unsafe aliases present before connect and aliases swapped
    between Python-visible operations.  They do not claim to bind SQLite's
    internal auxiliary-file descriptors against an adversarial same-UID rename
    in the sub-operation interval after a successful check; that requires a
    custom VFS or OS isolation and is an explicit threat-boundary exclusion.
    """

    def _bind_sidecar_guard(self, db_path: Union[os.PathLike[str], str]) -> None:
        self._sidecar_guard_db_path = os.fspath(db_path)
        self._guard_sidecars()

    def _guard_sidecars(self) -> None:
        db_path = getattr(self, "_sidecar_guard_db_path", None)
        if db_path is not None:
            assert_sidecar_namespace_safe(db_path)

    def execute(self, *args, **kwargs):
        self._guard_sidecars()
        result = super().execute(*args, **kwargs)
        self._guard_sidecars()
        return result

    def executemany(self, *args, **kwargs):
        self._guard_sidecars()
        result = super().executemany(*args, **kwargs)
        self._guard_sidecars()
        return result

    def executescript(self, *args, **kwargs):
        self._guard_sidecars()
        result = super().executescript(*args, **kwargs)
        self._guard_sidecars()
        return result

    def commit(self):
        self._guard_sidecars()
        result = super().commit()
        self._guard_sidecars()
        return result

    def rollback(self):
        self._guard_sidecars()
        result = super().rollback()
        self._guard_sidecars()
        return result
