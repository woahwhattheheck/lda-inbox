from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from typing import Any, Dict, Iterable, Optional, Tuple

SCHEMA = "lda.external-effect-ledger/v1"
TOKEN_SCHEMA = "lda.external-effect-token/v1"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_TOKEN_FILE_BYTES = 4096
MAX_RECEIPT_REF = 256


class EffectError(ValueError):
    """Stable fail-closed contract error."""


def _reject_constant(value: str) -> None:
    raise EffectError(f"NONFINITE_JSON:{value}")


def _pairs_no_duplicates(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise EffectError(f"DUPLICATE_JSON_KEY:{key}")
        out[key] = value
    return out


def load_json_bytes(data: bytes) -> Any:
    if len(data) > MAX_PAYLOAD_BYTES:
        raise EffectError("PAYLOAD_TOO_LARGE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EffectError("PAYLOAD_NOT_UTF8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise EffectError("INVALID_JSON") from exc
    _validate_json_graph(value)
    return value


def _validate_json_graph(value: Any, *, depth: int = 0, nodes: Optional[list[int]] = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 4096:
        raise EffectError("JSON_NODE_LIMIT")
    if depth > 32:
        raise EffectError("JSON_DEPTH_LIMIT")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if isinstance(value, bool):
            raise EffectError("BOOL_INTEGER_ALIAS")
        if abs(value) > 9_007_199_254_740_991:
            raise EffectError("INTEGER_OUT_OF_RANGE")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EffectError("NONFINITE_NUMBER")
        raise EffectError("FLOAT_NOT_ALLOWED")
    if isinstance(value, list):
        for item in value:
            _validate_json_graph(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EffectError("NON_STRING_KEY")
            if any(ord(ch) < 0x20 for ch in key):
                raise EffectError("CONTROL_IN_KEY")
            _validate_json_graph(item, depth=depth + 1, nodes=nodes)
        return
    raise EffectError("UNSUPPORTED_JSON_TYPE")


def canonical_payload(value: Any) -> bytes:
    _validate_json_graph(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise EffectError("PAYLOAD_TOO_LARGE")
    return raw


def validate_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise EffectError(f"INVALID_{name.upper()}")
    return value


def validate_attempt(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2_147_483_647:
        raise EffectError("INVALID_ATTEMPT")
    return value


def validate_sha(value: str, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise EffectError(f"INVALID_{name.upper()}")
    return value


def validate_receipt_ref(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_RECEIPT_REF:
        raise EffectError("INVALID_RECEIPT_REF")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise EffectError("INVALID_RECEIPT_REF")
    return value


def token_hash(token: str) -> str:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
        raise EffectError("INVALID_EFFECT_TOKEN")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "schema": SCHEMA, "task_id": row["task_id"], "effect_id": row["effect_id"],
        "operation": row["operation"], "payload_sha256": row["payload_sha256"],
        "lease": {"worker_id": row["worker_id"], "lease_id": row["lease_id"], "attempt": row["attempt"]},
        "effect_generation": row["effect_generation"], "state": row["state"],
        "dispatch_count": row["dispatch_count"],
        "outcome": None if row["outcome_kind"] is None else {
            "kind": row["outcome_kind"], "receipt_ref": row["outcome_receipt_ref"],
            "receipt_sha256": row["outcome_receipt_sha256"],
        },
        "retry_authorized": False,
        "authority": {
            "executes_external_effect": False, "provider_authenticity_proven": False,
            "payment_or_delivery_proven": False, "retry_after_unknown_outcome": False,
        },
    }
