# Portable audit-bundle continuity chain

`effect_audit_chain.py` combines independently verified, redacted
`effect_audit_bundle.py` outputs into one deterministic offline continuity
record. Each entry contains the complete portable bundle, its canonical digest,
its audit/document projection, and a digest link to the preceding entry.

The verifier revalidates every embedded bundle and rejects changed bundle
content, changed projections, duplicate bundles, broken links, reordering,
removal without a matching manifest, invalid counts, and altered authority or
limitation fields. `verify-extension` additionally proves that a candidate
retains every byte-equivalent base entry before adding new entries.

```sh
# Create a chain from already-redacted audit bundles.
python effect_audit_chain.py create \
  --bundle audit-001.json --bundle audit-002.json > audit-chain.json

# Strict offline verification.
python effect_audit_chain.py verify < audit-chain.json

# Append without rewriting retained entries.
python effect_audit_chain.py extend \
  --chain audit-chain.json --bundle audit-003.json > audit-chain-next.json

# Verify that the next chain is a strict extension of the retained base.
python effect_audit_chain.py verify-extension \
  --base audit-chain.json < audit-chain-next.json
```

Inputs are bounded canonical JSON: each bundle remains subject to the 4 MiB
bundle ceiling, a chain contains at most 64 entries, and the complete chain is
limited to 16 MiB. Duplicate JSON keys, non-UTF-8 input, non-finite numbers,
unpaired surrogates, unknown fields, duplicate bundles, and invalid embedded
bundles fail closed.

## Authority ceiling

This chain is a self-integrity and continuity artifact, not a signature or
trusted timestamp. Without an independently retained earlier chain digest, a
newly recomputed shorter chain cannot prove that history was omitted. A valid
chain does not authenticate source/provider receipts, prove an external action,
authorize task completion or retry, recognize payment/delivery/revenue, contact
a device or provider, or mutate task/effect state.
