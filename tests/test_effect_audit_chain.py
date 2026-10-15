from __future__ import annotations

import copy
import hashlib
import json
import unittest

from effect_audit_chain import (
    CHAIN_SCHEMA,
    ChainError,
    build_chain,
    extend_chain,
    verify_chain,
    verify_extension,
)


def _sha(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _bundle(marker: str):
    bindings = []
    effects = []
    audit = {
        "schema": "lda.task-effect-audit-portable/v1",
        "authorizes_completion": False,
        "document_sha256": hashlib.sha256(marker.encode()).hexdigest(),
        "effects": effects,
        "findings": [],
        "finding_counts": {},
        "limitations": {
            "external_effect_execution_proven": False,
            "payment_or_delivery_proven": False,
            "provider_authenticity_proven": False,
        },
        "task_count": 0,
    }
    return {
        "schema": "lda.task-effect-audit-bundle/v1",
        "authorizes_completion": False,
        "audit": audit,
        "effect_snapshot": effects,
        "limitations": {
            "external_effect_execution_proven": False,
            "payment_or_delivery_proven": False,
            "provider_authenticity_proven": False,
            "source_authenticity_proven": False,
        },
        "manifest": {
            "audit_sha256": _sha(audit),
            "effect_snapshot_sha256": _sha(effects),
            "task_bindings_sha256": _sha(bindings),
        },
        "task_bindings": bindings,
    }


class AuditChainTests(unittest.TestCase):
    def test_build_and_verify_deterministically(self):
        one = build_chain([_bundle("one"), _bundle("two")])
        two = build_chain([_bundle("one"), _bundle("two")])
        self.assertEqual(one, two)
        report = verify_chain(one)
        self.assertEqual(report["schema"], CHAIN_SCHEMA)
        self.assertEqual(report["bundle_count"], 2)
        self.assertIs(report["authorizes_completion"], False)
        self.assertEqual(report["effect_count"], 0)
        self.assertEqual(report["task_count"], 0)

    def test_entry_links_to_predecessor(self):
        chain = build_chain([_bundle("one"), _bundle("two")])
        self.assertEqual(
            chain["entries"][1]["previous_entry_sha256"],
            chain["entries"][0]["entry_sha256"],
        )
        self.assertEqual(
            chain["manifest"]["head_entry_sha256"],
            chain["entries"][1]["entry_sha256"],
        )

    def test_root_authority_fails_closed(self):
        chain = build_chain([_bundle("one")])
        chain["authorizes_completion"] = True
        with self.assertRaisesRegex(ChainError, "authority/schema"):
            verify_chain(chain)

    def test_root_schema_fails_closed(self):
        chain = build_chain([_bundle("one")])
        chain["schema"] = "other"
        with self.assertRaisesRegex(ChainError, "authority/schema"):
            verify_chain(chain)

    def test_limitation_cannot_claim_external_proof(self):
        chain = build_chain([_bundle("one")])
        chain["limitations"]["provider_authenticity_proven"] = True
        with self.assertRaisesRegex(ChainError, "limitations"):
            verify_chain(chain)

    def test_bundle_tamper_is_rejected_even_if_outer_manifest_is_stale(self):
        chain = build_chain([_bundle("one")])
        chain["entries"][0]["bundle"]["audit"]["document_sha256"] = "f" * 64
        with self.assertRaisesRegex(ChainError, "invalid bundle"):
            verify_chain(chain)

    def test_entry_projection_tamper_is_rejected(self):
        chain = build_chain([_bundle("one")])
        chain["entries"][0]["document_sha256"] = "f" * 64
        with self.assertRaisesRegex(ChainError, "projection"):
            verify_chain(chain)

    def test_entry_digest_tamper_is_rejected(self):
        chain = build_chain([_bundle("one")])
        chain["entries"][0]["entry_sha256"] = "f" * 64
        with self.assertRaisesRegex(ChainError, "digest"):
            verify_chain(chain)

    def test_removal_without_manifest_rewrite_is_rejected(self):
        chain = build_chain([_bundle("one"), _bundle("two")])
        chain["entries"].pop()
        with self.assertRaisesRegex(ChainError, "manifest"):
            verify_chain(chain)

    def test_reorder_is_rejected(self):
        chain = build_chain([_bundle("one"), _bundle("two")])
        chain["entries"].reverse()
        with self.assertRaisesRegex(ChainError, "index|continuity"):
            verify_chain(chain)

    def test_duplicate_bundle_is_rejected_at_build_time(self):
        bundle = _bundle("one")
        with self.assertRaisesRegex(ChainError, "duplicate"):
            build_chain([bundle, copy.deepcopy(bundle)])

    def test_extend_preserves_base_entries_exactly(self):
        base = build_chain([_bundle("one")])
        extended = extend_chain(base, [_bundle("two")])
        self.assertEqual(extended["entries"][:1], base["entries"])
        report = verify_extension(base, extended)
        self.assertEqual(report["added_bundle_count"], 1)
        self.assertEqual(
            report["extends_chain_sha256"], base["manifest"]["chain_sha256"]
        )

    def test_empty_extension_is_rejected(self):
        base = build_chain([_bundle("one")])
        with self.assertRaisesRegex(ChainError, "at least one"):
            extend_chain(base, [])

    def test_candidate_must_be_strict_extension(self):
        base = build_chain([_bundle("one")])
        with self.assertRaisesRegex(ChainError, "strict extension"):
            verify_extension(base, copy.deepcopy(base))

    def test_candidate_cannot_rewrite_base_history(self):
        base = build_chain([_bundle("one")])
        unrelated = build_chain([_bundle("other"), _bundle("two")])
        with self.assertRaisesRegex(ChainError, "preserve"):
            verify_extension(base, unrelated)

    def test_empty_chain_is_rejected(self):
        with self.assertRaisesRegex(ChainError, "at least one"):
            build_chain([])


if __name__ == "__main__":
    unittest.main()
