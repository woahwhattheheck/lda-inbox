from __future__ import annotations

import os
import stat
from pathlib import Path

from effect_receipt_common import EffectError


def publish_private_file(path: Path, raw: bytes) -> None:
    path = Path(path)
    parent = path.parent if str(path.parent) else Path('.')
    name = path.name
    if not name or name in ('.', '..'):
        raise EffectError('INVALID_PRIVATE_PATH')
    dflags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        parent_fd = os.open(parent, dflags)
    except OSError as exc:
        raise EffectError('PRIVATE_PARENT_OPEN_FAILED') from exc
    fd = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise EffectError('PRIVATE_PATH_OCCUPIED') from exc
        except OSError as exc:
            raise EffectError('PRIVATE_CREATE_FAILED') from exc
        os.fchmod(fd, 0o600)
        created = os.fstat(fd)
        if not stat.S_ISREG(created.st_mode):
            raise EffectError('PRIVATE_FILE_NOT_REGULAR')
        offset = 0
        while offset < len(raw):
            try:
                written = os.write(fd, raw[offset:])
            except OSError as exc:
                raise EffectError('PRIVATE_WRITE_FAILED') from exc
            if written <= 0:
                raise EffectError('PRIVATE_SHORT_WRITE')
            offset += written
        try:
            os.fsync(fd)
        except OSError as exc:
            raise EffectError('PRIVATE_FSYNC_FAILED') from exc
        os.lseek(fd, 0, os.SEEK_SET)
        readback = bytearray()
        while len(readback) <= len(raw):
            chunk = os.read(fd, len(raw) + 1 - len(readback))
            if not chunk:
                break
            readback.extend(chunk)
        if bytes(readback) != raw:
            raise EffectError('PRIVATE_READBACK_MISMATCH')
        final_fd = os.fstat(fd)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (final_fd.st_dev, final_fd.st_ino) != (created.st_dev, created.st_ino)
            or (visible.st_dev, visible.st_ino) != (created.st_dev, created.st_ino)
            or final_fd.st_size != len(raw)
            or (final_fd.st_mode & 0o077) != 0
        ):
            raise EffectError('PRIVATE_FINAL_METADATA_INVALID')
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise EffectError('PRIVATE_DIRECTORY_FSYNC_FAILED') from exc
    finally:
        # Failure after O_EXCL creation intentionally leaves the visible pathname
        # untouched. TOKEN_PENDING is non-authoritative; a residue is reconciled
        # explicitly. Never perform check-then-unlink on a contested public name.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def read_private_file(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise EffectError('PRIVATE_FILE_MISSING') from exc
    except OSError as exc:
        raise EffectError('PRIVATE_OPEN_FAILED') from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > limit
            or (before.st_mode & 0o077) != 0
        ):
            raise EffectError('PRIVATE_FILE_NOT_SAFE')
        if hasattr(os, 'geteuid') and before.st_uid != os.geteuid():
            raise EffectError('PRIVATE_FILE_NOT_OWNED')
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise EffectError('PRIVATE_FILE_TOO_LARGE')
        after = os.fstat(fd)
        visible = os.stat(path, follow_symlinks=False)
        generation = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
        if any(getattr(after, field) != getattr(before, field) for field in generation):
            raise EffectError('PRIVATE_FILE_CHANGED_DURING_READ')
        if (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino):
            raise EffectError('PRIVATE_VISIBLE_PATH_REBOUND')
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (after.st_mode & 0o077) != 0:
            raise EffectError('PRIVATE_FILE_NOT_SAFE')
        if hasattr(os, 'geteuid') and after.st_uid != os.geteuid():
            raise EffectError('PRIVATE_FILE_NOT_OWNED')
        return bytes(data)
    finally:
        os.close(fd)
