from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict

from effect_receipt_common import (
    EffectError,
    MAX_PAYLOAD_BYTES,
    MAX_TOKEN_FILE_BYTES,
    TOKEN_SCHEMA,
    load_json_bytes,
    token_hash,
    validate_attempt,
    validate_id,
    validate_sha,
)
from effect_receipt_private_file import publish_private_file, read_private_file


def safe_read(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EffectError('INPUT_OPEN_FAILED') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise EffectError('INPUT_NOT_SAFE_REGULAR_FILE')
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise EffectError('INPUT_TOO_LARGE')
        after = os.fstat(fd)
        generation = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(after, field) != getattr(info, field) for field in generation):
            raise EffectError('INPUT_CHANGED_DURING_READ')
        return b''.join(chunks)
    finally:
        os.close(fd)


def load_payload_file(path: Path):
    return load_json_bytes(safe_read(path, MAX_PAYLOAD_BYTES))


def normalized_token_path(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def token_path_missing(path: Path) -> bool:
    try:
        os.lstat(path)
        return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise EffectError('TOKEN_PATH_STAT_FAILED') from exc


def _token_raw(*, task_id: str, effect_id: str, effect_generation: int, token: str,
               worker_id: str, lease_id: str, attempt: int, task_sha256: str) -> bytes:
    if not isinstance(effect_generation, int) or isinstance(effect_generation, bool) or effect_generation < 1:
        raise EffectError('INVALID_EFFECT_GENERATION')
    token_hash(token)
    obj = {
        'schema': TOKEN_SCHEMA,
        'task_id': validate_id('task_id', task_id),
        'effect_id': validate_id('effect_id', effect_id),
        'effect_generation': effect_generation,
        'token': token,
        'worker_id': validate_id('worker_id', worker_id),
        'lease_id': validate_id('lease_id', lease_id),
        'attempt': validate_attempt(attempt),
        'task_sha256': validate_sha(task_sha256, 'task_sha256'),
    }
    return (json.dumps(obj, sort_keys=True, separators=(',', ':')) + '\n').encode()


def write_token_file(path: Path, **binding) -> None:
    try:
        publish_private_file(Path(path), _token_raw(**binding))
    except EffectError as exc:
        mapping = {
            'PRIVATE_PATH_OCCUPIED': 'TOKEN_PATH_OCCUPIED',
            'PRIVATE_WRITE_FAILED': 'TOKEN_WRITE_FAILED',
            'PRIVATE_SHORT_WRITE': 'TOKEN_SHORT_WRITE',
            'PRIVATE_FSYNC_FAILED': 'TOKEN_FSYNC_FAILED',
        }
        raise EffectError(mapping.get(str(exc), str(exc).replace('PRIVATE_', 'TOKEN_'))) from exc


def read_token_file(path: Path) -> Dict[str, object]:
    try:
        raw = read_private_file(Path(path), MAX_TOKEN_FILE_BYTES)
    except EffectError as exc:
        raise EffectError(str(exc).replace('PRIVATE_', 'TOKEN_')) from exc
    obj = load_json_bytes(raw)
    required = {'schema', 'task_id', 'effect_id', 'effect_generation', 'token', 'worker_id', 'lease_id', 'attempt', 'task_sha256'}
    if not isinstance(obj, dict) or set(obj) != required or obj['schema'] != TOKEN_SCHEMA:
        raise EffectError('INVALID_TOKEN_FILE')
    if not isinstance(obj['effect_generation'], int) or isinstance(obj['effect_generation'], bool) or obj['effect_generation'] < 1:
        raise EffectError('INVALID_TOKEN_FILE')
    token_hash(obj['token'])
    validate_id('task_id', obj['task_id'])
    validate_id('effect_id', obj['effect_id'])
    validate_id('worker_id', obj['worker_id'])
    validate_id('lease_id', obj['lease_id'])
    validate_attempt(obj['attempt'])
    validate_sha(obj['task_sha256'], 'task_sha256')
    return obj
