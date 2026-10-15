import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from effect_audit_bundle import BundleError, build_bundle, bundle_bytes, verify_bundle
from effect_receipt_common import SCHEMA
from task_protocol import task_spec_sha256


def task(task_id="task-1", done=False):
    value = {
        "id": task_id,
        "created": "2026-10-15T12:00:00Z",
        "kind": "run_task",
        "command": "do it",
        "timeout_s": 60,
        "done": done,
    }
    if done:
        value.update(completed_at="2026-10-15T12:00:10Z", result="done")
    return value


def row(item, effect_id="effect-1", state="PREPARED"):
    terminal = state in ("SUCCEEDED", "FAILED_FINAL")
    dispatched = state in ("DISPATCHED", "RECONCILIATION_REQUIRED") or terminal
    return {
        "schema": SCHEMA,
        "task_id": item["id"],
        "effect_id": effect_id,
        "operation": "send_email",
        "payload_sha256": "1" * 64,
        "task_sha256": task_spec_sha256(item),
        "lease": {"worker_id": "worker", "lease_id": "lease", "attempt": 1},
        "effect_generation": 1,
        "state": state,
        "dispatch_count": 1 if dispatched else 0,
        "outcome": {"kind": state, "receipt_ref": "synthetic:r1", "receipt_sha256": "2" * 64} if terminal else None,
        "retry_authorized": False,
        "authority": {},
    }


class BundleTests(unittest.TestCase):
    def bundle(self):
        first, second = task("task-a"), task("task-b", done=True)
        return build_bundle(
            {"v": 1, "tasks": [second, first]},
            [row(second, "effect-z", "DISPATCHED"), row(first, "effect-a", "SUCCEEDED")],
        )

    def test_bundle_is_deterministic_redacted_and_verifiable(self):
        first = self.bundle()
        second = self.bundle()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        encoded = json.dumps(first, sort_keys=True)
        for forbidden in ("token_hash", "token_path", "worker_id", "lease_id", "do it", "synthetic:r1"):
            self.assertNotIn(forbidden, encoded)
        result = verify_bundle(first)
        self.assertTrue(result["valid"])
        self.assertFalse(result["authorizes_completion"])
        self.assertEqual(result["task_count"], 2)
        self.assertEqual(result["effect_count"], 2)

    def test_order_is_canonical(self):
        bundle = self.bundle()
        self.assertEqual([item["task_id"] for item in bundle["task_bindings"]], ["task-a", "task-b"])
        self.assertEqual([item["task_id"] for item in bundle["effect_snapshot"]], ["task-a", "task-b"])
        bundle["task_bindings"].reverse()
        with self.assertRaisesRegex(BundleError, "manifest digest|not canonical"):
            verify_bundle(bundle)

    def test_each_manifest_section_is_bound(self):
        for section in ("audit", "effect_snapshot", "task_bindings"):
            with self.subTest(section=section):
                bundle = self.bundle()
                if section == "audit":
                    bundle[section]["task_count"] += 1
                elif section == "effect_snapshot":
                    bundle[section][0]["operation"] = "changed"
                else:
                    bundle[section][0]["done"] = not bundle[section][0]["done"]
                with self.assertRaisesRegex(BundleError, "manifest digest mismatch"):
                    verify_bundle(bundle)

    def test_finding_and_count_revalidation(self):
        for field in ("findings", "finding_counts"):
            with self.subTest(field=field):
                bundle = self.bundle()
                if field == "findings":
                    bundle["audit"][field][0]["code"] = "UNRESOLVED_EFFECT"
                else:
                    bundle["audit"][field] = {}
                bundle["manifest"]["audit_sha256"] = __import__("hashlib").sha256(
                    json.dumps(bundle["audit"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                with self.assertRaisesRegex(BundleError, "finding"):
                    verify_bundle(bundle)

    def test_authority_or_extra_fields_fail_closed(self):
        for mutation in ("authority", "extra"):
            with self.subTest(mutation=mutation):
                bundle = self.bundle()
                if mutation == "authority":
                    bundle["authorizes_completion"] = True
                else:
                    bundle["unexpected"] = True
                with self.assertRaises(BundleError):
                    verify_bundle(bundle)

    def test_boolean_and_float_count_aliases_fail(self):
        for field, alias in (("task_count", True), ("task_count", 2.0), ("finding_counts", True), ("finding_counts", 1.0)):
            with self.subTest(field=field, alias=alias):
                bundle = self.bundle()
                if field == "task_count":
                    bundle["audit"][field] = alias
                else:
                    key = next(iter(bundle["audit"][field]))
                    bundle["audit"][field][key] = alias
                bundle["manifest"]["audit_sha256"] = __import__("hashlib").sha256(
                    json.dumps(bundle["audit"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                with self.assertRaises(BundleError):
                    verify_bundle(bundle)

    def test_create_serialization_uses_verifier_byte_ceiling(self):
        bundle = self.bundle()
        raw = bundle_bytes(bundle)
        with mock.patch("effect_audit_bundle.MAX_BUNDLE_BYTES", len(raw) + 1):
            self.assertEqual(bundle_bytes(bundle), raw)
        with mock.patch("effect_audit_bundle.MAX_BUNDLE_BYTES", len(raw)):
            with self.assertRaisesRegex(BundleError, "byte limit"):
                bundle_bytes(bundle)

    def test_duplicate_effect_identity_fails(self):
        bundle = self.bundle()
        bundle["effect_snapshot"].append(copy.deepcopy(bundle["effect_snapshot"][0]))
        with self.assertRaises(BundleError):
            verify_bundle(bundle)

    def test_cli_verify_round_trip(self):
        raw = json.dumps(self.bundle(), sort_keys=True, separators=(",", ":"))
        result = subprocess.run(
            [sys.executable, str(ROOT / "effect_audit_bundle.py"), "verify"],
            input=raw,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_cli_rejects_duplicate_keys_and_oversize_input(self):
        duplicate = subprocess.run(
            [sys.executable, str(ROOT / "effect_audit_bundle.py"), "verify"],
            input='{"schema":"a","schema":"b"}',
            text=True,
            capture_output=True,
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("duplicate JSON key", duplicate.stderr)
        oversized = subprocess.run(
            [sys.executable, str(ROOT / "effect_audit_bundle.py"), "verify"],
            input=b" " * (4 * 1024 * 1024 + 1),
            capture_output=True,
        )
        self.assertEqual(oversized.returncode, 2)
        self.assertIn(b"byte limit", oversized.stderr)

    def test_cli_rejects_excessively_deep_json_without_traceback(self):
        deep = "[" * 2000 + "0" + "]" * 2000
        result = subprocess.run(
            [sys.executable, str(ROOT / "effect_audit_bundle.py"), "verify"],
            input=deep,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
