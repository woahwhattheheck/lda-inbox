#!/usr/bin/env python3
"""Create and verify deterministic, public-only task/effect audit bundles."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable

from effect_receipt_audit import TERMINAL_STATES, UNRESOLVED_STATES, audit_document
from effect_receipt_common import EffectError, validate_id, validate_sha
from effect_receipt_ledger import read_effects_snapshot
from task_protocol import TaskProtocolError, parse_protocol_path, task_spec_sha256

BUNDLE_SCHEMA = "lda.task-effect-audit-bundle/v1"
PORTABLE_AUDIT_SCHEMA = "lda.task-effect-audit-portable/v1"
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


class BundleError(ValueError):
    """The portable bundle is malformed or internally inconsistent."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def bundle_bytes(bundle: Any) -> bytes:
    """Serialize one bundle under the same ceiling enforced by the verifier."""
    raw = _canonical(bundle)
    # Creation appends one canonical trailing newline; account for the exact
    # bytes the verifier will receive rather than only the JSON payload.
    if len(raw) + 1 > MAX_BUNDLE_BYTES:
        raise BundleError("bundle exceeds byte limit")
    return raw


def _exact_keys(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BundleError(f"{where}: invalid fields")
    return value


def _validate_json_domain(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise BundleError("unpaired Unicode surrogate")
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise BundleError("non-finite JSON number")
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())


def _binding(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "done": task["done"],
        "task_id": task["id"],
        "task_sha256": task_spec_sha256(task),
    }


def _portable_effect(effect: dict[str, Any]) -> dict[str, Any]:
    outcome = effect["outcome"]
    return {
        "effect_id": effect["effect_id"],
        "operation": effect["operation"],
        "outcome": None
        if outcome is None
        else {"kind": outcome["kind"], "receipt_sha256": outcome["receipt_sha256"]},
        "payload_sha256": effect["payload_sha256"],
        "state": effect["state"],
        "task_id": effect["task_id"],
        "task_sha256": effect["task_sha256"],
    }


def build_bundle(document: dict[str, Any], rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build a portable bundle without private ledger fields or task contents."""
    materialized = list(rows)
    source_audit = audit_document(document, materialized)
    bindings = sorted((_binding(task) for task in document["tasks"]), key=lambda item: item["task_id"])
    effects = [_portable_effect(effect) for effect in source_audit["effects"]]
    audit = {**source_audit, "schema": PORTABLE_AUDIT_SCHEMA, "effects": effects}
    manifest = {
        "audit_sha256": _sha(audit),
        "effect_snapshot_sha256": _sha(effects),
        "task_bindings_sha256": _sha(bindings),
    }
    return {
        "schema": BUNDLE_SCHEMA,
        "authorizes_completion": False,
        "audit": audit,
        "effect_snapshot": effects,
        "limitations": {
            "external_effect_execution_proven": False,
            "payment_or_delivery_proven": False,
            "provider_authenticity_proven": False,
            "source_authenticity_proven": False,
        },
        "manifest": manifest,
        "task_bindings": bindings,
    }


def build_bundle_paths(*, inbox_path: Path, db_path: Path) -> dict[str, Any]:
    document = parse_protocol_path(inbox_path)
    return build_bundle(document, read_effects_snapshot(db_path))


def _validate_binding(value: Any) -> dict[str, Any]:
    item = _exact_keys(value, {"done", "task_id", "task_sha256"}, "task binding")
    try:
        validate_id("task_id", item["task_id"])
        validate_sha(item["task_sha256"], "task_sha256")
    except EffectError as exc:
        raise BundleError(str(exc)) from exc
    if not isinstance(item["done"], bool):
        raise BundleError("task binding: done must be boolean")
    return item


def _validate_effect(value: Any) -> dict[str, Any]:
    item = _exact_keys(
        value,
        {"effect_id", "operation", "outcome", "payload_sha256", "state", "task_id", "task_sha256"},
        "effect snapshot row",
    )
    try:
        for name in ("task_id", "effect_id", "operation"):
            validate_id(name, item[name])
        validate_sha(item["payload_sha256"], "payload_sha256")
        validate_sha(item["task_sha256"], "task_sha256")
    except EffectError as exc:
        raise BundleError(str(exc)) from exc
    state = item["state"]
    if state in UNRESOLVED_STATES:
        if item["outcome"] is not None:
            raise BundleError("unresolved effect has an outcome")
    elif state in TERMINAL_STATES:
        outcome = _exact_keys(item["outcome"], {"kind", "receipt_sha256"}, "effect outcome")
        if outcome["kind"] != state:
            raise BundleError("effect outcome kind mismatch")
        try:
            validate_sha(outcome["receipt_sha256"], "receipt_sha256")
        except EffectError as exc:
            raise BundleError(str(exc)) from exc
    else:
        raise BundleError("invalid effect state")
    return item


def _expected_finding(effect: dict[str, Any], bindings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    task = bindings.get(effect["task_id"])
    if task is None:
        code = "ORPHAN_EFFECT"
    elif effect["task_sha256"] != task["task_sha256"]:
        code = "TASK_BINDING_MISMATCH"
    elif task["done"] and effect["state"] in UNRESOLVED_STATES:
        code = "COMPLETED_WITH_UNRESOLVED_EFFECT"
    elif effect["state"] in UNRESOLVED_STATES:
        code = "UNRESOLVED_EFFECT"
    else:
        code = "TERMINAL_EFFECT"
    return {
        "code": code,
        "effect_id": effect["effect_id"],
        "effect_state": effect["state"],
        "task_id": effect["task_id"],
    }


def verify_bundle(bundle: Any) -> dict[str, Any]:
    """Strictly verify bundle structure, hashes, ordering, counts, and findings."""
    _validate_json_domain(bundle)
    root = _exact_keys(
        bundle,
        {"schema", "authorizes_completion", "audit", "effect_snapshot", "limitations", "manifest", "task_bindings"},
        "bundle",
    )
    if root["schema"] != BUNDLE_SCHEMA or root["authorizes_completion"] is not False:
        raise BundleError("bundle authority/schema mismatch")
    limitations = _exact_keys(
        root["limitations"],
        {"external_effect_execution_proven", "payment_or_delivery_proven", "provider_authenticity_proven", "source_authenticity_proven"},
        "bundle limitations",
    )
    if any(value is not False for value in limitations.values()):
        raise BundleError("bundle limitations must remain false")

    bindings_list = root["task_bindings"]
    effects_list = root["effect_snapshot"]
    if not isinstance(bindings_list, list) or not isinstance(effects_list, list):
        raise BundleError("bundle snapshots must be arrays")
    bindings = [_validate_binding(item) for item in bindings_list]
    effects = [_validate_effect(item) for item in effects_list]
    if bindings != sorted(bindings, key=lambda item: item["task_id"]):
        raise BundleError("task bindings are not canonical")
    if effects != sorted(effects, key=lambda item: (item["task_id"], item["effect_id"])):
        raise BundleError("effect snapshot is not canonical")
    if len({item["task_id"] for item in bindings}) != len(bindings):
        raise BundleError("duplicate task binding")
    if len({(item["task_id"], item["effect_id"]) for item in effects}) != len(effects):
        raise BundleError("duplicate effect identity")

    manifest = _exact_keys(
        root["manifest"],
        {"audit_sha256", "effect_snapshot_sha256", "task_bindings_sha256"},
        "manifest",
    )
    for name, value in manifest.items():
        try:
            validate_sha(value, name)
        except EffectError as exc:
            raise BundleError(str(exc)) from exc
    expected_hashes = {
        "audit_sha256": _sha(root["audit"]),
        "effect_snapshot_sha256": _sha(effects),
        "task_bindings_sha256": _sha(bindings),
    }
    if manifest != expected_hashes:
        raise BundleError("manifest digest mismatch")

    audit = _exact_keys(
        root["audit"],
        {"schema", "authorizes_completion", "document_sha256", "effects", "findings", "finding_counts", "limitations", "task_count"},
        "audit",
    )
    if audit["schema"] != PORTABLE_AUDIT_SCHEMA or audit["authorizes_completion"] is not False:
        raise BundleError("audit authority/schema mismatch")
    try:
        validate_sha(audit["document_sha256"], "document_sha256")
    except EffectError as exc:
        raise BundleError(str(exc)) from exc
    if type(audit["task_count"]) is not int or not 0 <= audit["task_count"] <= 256:
        raise BundleError("audit task_count must be a bounded integer")
    if audit["effects"] != effects or audit["task_count"] != len(bindings):
        raise BundleError("audit snapshot/count mismatch")
    audit_limitations = _exact_keys(
        audit["limitations"],
        {"external_effect_execution_proven", "payment_or_delivery_proven", "provider_authenticity_proven"},
        "audit limitations",
    )
    if any(value is not False for value in audit_limitations.values()):
        raise BundleError("audit limitations must remain false")
    by_id = {item["task_id"]: item for item in bindings}
    findings = [_expected_finding(effect, by_id) for effect in effects]
    if audit["findings"] != findings:
        raise BundleError("audit finding mismatch")
    counts = Counter(item["code"] for item in findings)
    expected_counts = {key: counts[key] for key in sorted(counts)}
    if not isinstance(audit["finding_counts"], dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in audit["finding_counts"].items()
    ):
        raise BundleError("audit finding counts must be nonnegative integers")
    if audit["finding_counts"] != expected_counts:
        raise BundleError("audit finding-count mismatch")
    return {
        "schema": BUNDLE_SCHEMA,
        "valid": True,
        "authorizes_completion": False,
        "audit_sha256": manifest["audit_sha256"],
        "effect_count": len(effects),
        "task_count": len(bindings),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_stdin() -> Any:
    raw = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise BundleError("bundle exceeds byte limit")
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(BundleError(f"invalid JSON constant: {token}")),
        )
    except UnicodeDecodeError as exc:
        raise BundleError("bundle is not UTF-8") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise BundleError("bundle is not valid JSON") from exc


def _print(value: Any) -> None:
    sys.stdout.buffer.write(_canonical(value) + b"\n")


def _print_bundle(value: Any) -> None:
    sys.stdout.buffer.write(bundle_bytes(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="portable lda-inbox audit bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--inbox", required=True, type=Path)
    create.add_argument("--db", required=True, type=Path)
    sub.add_parser("verify", help="verify one bounded JSON bundle from standard input")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            _print_bundle(build_bundle_paths(inbox_path=args.inbox, db_path=args.db))
        else:
            _print(verify_bundle(_load_stdin()))
        return 0
    except (BundleError, EffectError, TaskProtocolError, OSError) as exc:
        print(_canonical({"error": str(exc), "ok": False}).decode("utf-8"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
