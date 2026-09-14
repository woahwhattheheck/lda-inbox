from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Tuple

from effect_receipt_common import (EffectError, MAX_PAYLOAD_BYTES, MAX_TOKEN_FILE_BYTES, TOKEN_SCHEMA, load_json_bytes, validate_id)


def safe_read(path: Path, limit: int) -> bytes:
    flags=os.O_RDONLY | (getattr(os,"O_NOFOLLOW",0))
    try: fd=os.open(path,flags)
    except OSError as exc: raise EffectError("INPUT_OPEN_FAILED") from exc
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit: raise EffectError("INPUT_NOT_SAFE_REGULAR_FILE")
        chunks=[]; total=0
        while True:
            chunk=os.read(fd,min(65536,limit+1-total))
            if not chunk: break
            chunks.append(chunk); total += len(chunk)
            if total > limit: raise EffectError("INPUT_TOO_LARGE")
        after=os.fstat(fd)
        if (after.st_dev,after.st_ino,after.st_size)!=(info.st_dev,info.st_ino,info.st_size): raise EffectError("INPUT_CHANGED_DURING_READ")
        return b"".join(chunks)
    finally: os.close(fd)


def load_payload_file(path: Path):
    return load_json_bytes(safe_read(path, MAX_PAYLOAD_BYTES))


def write_token_file(path: Path, *, task_id: str, effect_id: str, token: str) -> None:
    obj={"schema":TOKEN_SCHEMA,"task_id":task_id,"effect_id":effect_id,"effect_generation":1,"token":token}
    raw=(json.dumps(obj,sort_keys=True,separators=(",",":"))+"\n").encode()
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
    fd=os.open(path,flags,0o600)
    try: os.write(fd,raw); os.fsync(fd)
    finally: os.close(fd)


def read_token_file(path: Path) -> Tuple[str,str,str]:
    obj=load_json_bytes(safe_read(path,MAX_TOKEN_FILE_BYTES))
    if not isinstance(obj,dict) or set(obj)!={"schema","task_id","effect_id","effect_generation","token"}: raise EffectError("INVALID_TOKEN_FILE")
    if obj["schema"]!=TOKEN_SCHEMA or obj["effect_generation"]!=1: raise EffectError("INVALID_TOKEN_FILE")
    return validate_id("task_id",obj["task_id"]), validate_id("effect_id",obj["effect_id"]), obj["token"]
