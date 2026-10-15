#!/usr/bin/env python3
"""Create and verify deterministic chains of portable LDA audit bundles.

The chain is an offline integrity envelope only.  It does not authenticate a
bundle's source, authorize completion, or prove that an external effect ran.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable

from effect_audit_bundle import BundleError, MAX_BUNDLE_BYTES, verify_bundle

CHAIN_SCHEMA = "lda.task-effect-audit-chain/v1"
MAX_CHAIN_BYTES = 16 * 1024 * 1024
MAX_CHAIN_ENTRIES = 64
ZERO_SHA256 = "0" * 64


class ChainError(ValueError):
    """The audit chain is malformed or internally inconsistent."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ChainError("value is not bounded canonical JSON") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_json_domain(value: Any) -> None:
    pending = [value]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 500_000:
            raise ChainError("JSON value exceeds structural limit")
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise ChainError("unpaired Unicode surrogate")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ChainError("non-finite JSON number")
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())


def _exact_keys(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ChainError(f"{where}: invalid fields")
    return value


def _entry_core(index: int, previous: str, bundle: dict[str, Any]) -> dict[str, Any]:
    report = verify_bundle(bundle)
    return {
        "index": index,
        "previous_entry_sha256": previous,
        "bundle_sha256": _sha(bundle),
        "audit_sha256": report["audit_sha256"],
        "document_sha256": bundle["audit"]["document_sha256"],
        "bundle": bundle,
    }


def _append(entries: list[dict[str, Any]], bundles: Iterable[dict[str, Any]]) -> None:
    seen = {entry["bundle_sha256"] for entry in entries}
    previous = entries[-1]["entry_sha256"] if entries else ZERO_SHA256
    for bundle in bundles:
        if len(entries) >= MAX_CHAIN_ENTRIES:
            raise ChainError("chain entry limit exceeded")
        core = _entry_core(len(entries), previous, bundle)
        if core["bundle_sha256"] in seen:
            raise ChainError("duplicate audit bundle")
        entry = {**core, "entry_sha256": _sha(core)}
        entries.append(entry)
        seen.add(core["bundle_sha256"])
        previous = entry["entry_sha256"]


def _finish(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        raise ChainError("chain must contain at least one bundle")
    chain = {
        "schema": CHAIN_SCHEMA,
        "authorizes_completion": False,
        "limitations": {
            "external_effect_execution_proven": False,
            "omission_proven_without_external_anchor": False,
            "provider_authenticity_proven": False,
            "source_authenticity_proven": False,
        },
        "entries": entries,
        "manifest": {
            "bundle_count": len(entries),
            "chain_sha256": _sha(entries),
            "head_entry_sha256": entries[-1]["entry_sha256"],
        },
    }
    if len(_canonical(chain)) + 1 > MAX_CHAIN_BYTES:
        raise ChainError("chain exceeds byte limit")
    return chain


def build_chain(bundles: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    _append(entries, bundles)
    return _finish(entries)


def verify_chain(chain: Any) -> dict[str, Any]:
    """Verify every bundle and every continuity/hash/count invariant."""
    _validate_json_domain(chain)
    root = _exact_keys(
        chain,
        {"schema", "authorizes_completion", "limitations", "entries", "manifest"},
        "chain",
    )
    if root["schema"] != CHAIN_SCHEMA or root["authorizes_completion"] is not False:
        raise ChainError("chain authority/schema mismatch")
    limitations = _exact_keys(
        root["limitations"],
        {
            "external_effect_execution_proven",
            "omission_proven_without_external_anchor",
            "provider_authenticity_proven",
            "source_authenticity_proven",
        },
        "chain limitations",
    )
    if any(value is not False for value in limitations.values()):
        raise ChainError("chain limitations must remain false")
    entries = root["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CHAIN_ENTRIES:
        raise ChainError("chain entries must be a bounded nonempty array")

    seen: set[str] = set()
    previous = ZERO_SHA256
    reports: list[dict[str, Any]] = []
    for index, value in enumerate(entries):
        entry = _exact_keys(
            value,
            {
                "index",
                "previous_entry_sha256",
                "bundle_sha256",
                "audit_sha256",
                "document_sha256",
                "bundle",
                "entry_sha256",
            },
            f"entry {index}",
        )
        if type(entry["index"]) is not int or entry["index"] != index:
            raise ChainError("entry index mismatch")
        if entry["previous_entry_sha256"] != previous:
            raise ChainError("entry continuity mismatch")
        try:
            report = verify_bundle(entry["bundle"])
        except BundleError as exc:
            raise ChainError(f"entry {index}: invalid bundle: {exc}") from exc
        expected = _entry_core(index, previous, entry["bundle"])
        if any(entry[key] != expected[key] for key in expected):
            raise ChainError("entry projection mismatch")
        if entry["entry_sha256"] != _sha(expected):
            raise ChainError("entry digest mismatch")
        if entry["bundle_sha256"] in seen:
            raise ChainError("duplicate audit bundle")
        seen.add(entry["bundle_sha256"])
        previous = entry["entry_sha256"]
        reports.append(report)

    manifest = _exact_keys(
        root["manifest"],
        {"bundle_count", "chain_sha256", "head_entry_sha256"},
        "manifest",
    )
    expected_manifest = {
        "bundle_count": len(entries),
        "chain_sha256": _sha(entries),
        "head_entry_sha256": previous,
    }
    if manifest != expected_manifest:
        raise ChainError("chain manifest mismatch")
    return {
        "schema": CHAIN_SCHEMA,
        "valid": True,
        "authorizes_completion": False,
        "bundle_count": len(entries),
        "chain_sha256": manifest["chain_sha256"],
        "head_entry_sha256": previous,
        "task_count": sum(report["task_count"] for report in reports),
        "effect_count": sum(report["effect_count"] for report in reports),
    }


def extend_chain(chain: dict[str, Any], bundles: Iterable[dict[str, Any]]) -> dict[str, Any]:
    verify_chain(chain)
    entries = json.loads(_canonical(chain["entries"]).decode("utf-8"))
    before = len(entries)
    _append(entries, bundles)
    if len(entries) == before:
        raise ChainError("extension must add at least one bundle")
    return _finish(entries)


def verify_extension(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_report = verify_chain(base)
    candidate_report = verify_chain(candidate)
    base_entries = base["entries"]
    candidate_entries = candidate["entries"]
    if len(candidate_entries) <= len(base_entries):
        raise ChainError("candidate is not a strict extension")
    if candidate_entries[: len(base_entries)] != base_entries:
        raise ChainError("candidate does not preserve the base chain")
    return {
        **candidate_report,
        "extends_chain_sha256": base_report["chain_sha256"],
        "added_bundle_count": len(candidate_entries) - len(base_entries),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChainError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_bytes(raw: bytes, limit: int, label: str) -> Any:
    if len(raw) > limit:
        raise ChainError(f"{label} exceeds byte limit")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ChainError(f"invalid JSON constant: {token}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise ChainError(f"{label} is not UTF-8") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ChainError(f"{label} is not valid JSON") from exc


def _load_path(path: Path, limit: int, label: str) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    return _load_bytes(raw, limit, label)


def _load_stdin() -> Any:
    return _load_bytes(sys.stdin.buffer.read(MAX_CHAIN_BYTES + 1), MAX_CHAIN_BYTES, "chain")


def _print(value: Any) -> None:
    raw = _canonical(value)
    if len(raw) + 1 > MAX_CHAIN_BYTES:
        raise ChainError("chain exceeds byte limit")
    sys.stdout.buffer.write(raw + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="portable LDA audit bundle chains")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--bundle", action="append", required=True, type=Path)
    extend = sub.add_parser("extend")
    extend.add_argument("--chain", required=True, type=Path)
    extend.add_argument("--bundle", action="append", required=True, type=Path)
    sub.add_parser("verify", help="verify one bounded chain from standard input")
    verify_ext = sub.add_parser("verify-extension")
    verify_ext.add_argument("--base", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            _print(build_chain(_load_path(path, MAX_BUNDLE_BYTES, "bundle") for path in args.bundle))
        elif args.command == "extend":
            base = _load_path(args.chain, MAX_CHAIN_BYTES, "chain")
            _print(extend_chain(base, (_load_path(path, MAX_BUNDLE_BYTES, "bundle") for path in args.bundle)))
        elif args.command == "verify-extension":
            _print(verify_extension(_load_path(args.base, MAX_CHAIN_BYTES, "chain"), _load_stdin()))
        else:
            _print(verify_chain(_load_stdin()))
        return 0
    except (BundleError, ChainError, OSError) as exc:
        print(_canonical({"error": str(exc), "ok": False}).decode("utf-8"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
