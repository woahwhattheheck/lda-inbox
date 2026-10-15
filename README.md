# lda-inbox

Repository-backed task inbox for the LocalDeviceAgent phone agent.

## Validate

```sh
python validate_inbox.py inbox.json
python task_protocol.py validate inbox.json
```

The base version-1 inbox validator fail-closes malformed JSON, duplicate IDs,
unsafe identifiers, unsupported task shapes, incoherent completion receipts,
non-finite values, unsafe path/file inputs, and bounded document/task resources.

Path validation opens one bounded regular-file generation through a retained
file descriptor and compares the final visible pathname to that exact inode
before returning. Symlinks, FIFOs, devices, observed pathname replacement,
in-read file mutation, growth beyond the byte ceiling, and premature EOF fail
closed before JSON authority is granted. These are finite validation points;
the validator does not claim the host namespace remains immutable after return.

## Lease / CAS execution coordination

`task_protocol.py` adds an optional coordination state for multiple pollers and
retries. It does **not** execute commands, contact a device, or publish Git.

A worker must:

1. read `inbox.json` from an exact repository ref/blob;
2. validate it and compute its semantic document SHA-256;
3. stage a claim;
4. publish only with an exact ref/blob compare-and-swap;
5. re-read the published lease before executing anything.

Lease authority is the exact tuple:

```text
(worker_id, lease_id, attempt)
```

`attempt` is the immutable generation. A raw `lease_id` may be reused after an
expiry or explicit release, but that creates a new attempt. Every
`renew`, `release`, and `complete` request must provide `--expected-attempt`;
a stale A→B→A lease cannot mutate the later generation even when worker and
lease strings repeat.

Claims and all transition receipts expose the authoritative `attempt`. The
semantic document SHA and repository ref/blob CAS are separate fences:
generation binding prevents lease-identity ABA; repository CAS prevents stale
documents from being published.

This is still **not an exactly-once external-side-effect guarantee**. A crash
after an irreversible device/provider effect but before completion publication
can cause a later generation to retry. Side-effecting commands need their own
idempotency key/provider receipt or a human approval boundary.

See [TASK_PROTOCOL.md](TASK_PROTOCOL.md) for the state machine and CLI examples.

### Deterministic offline walkthrough

Run the maintained end-to-end example without a phone, provider, repository
write, or external side effect:

```sh
python lease_cas_demo.py
```

The JSON transcript binds every staged transition to the semantic document
SHA-256. It demonstrates an initial claim, an unchanged idempotent replay, a
renewal, an explicit release and handoff to a second worker, rejection of the
first worker's stale generation, and completion by the new authority. The demo
uses fixed timestamps and identifiers, so repeated runs produce byte-identical
output.

### Offline external-effect reconciliation walkthrough

Run the maintained lease-to-effect safety course without a phone, provider,
mailbox, payment processor, repository write, or external side effect:

```sh
python effect_receipt_demo.py
```

The redacted JSON transcript exercises the persistent receipt ledger from
`TOKEN_PENDING` through `PREPARED` and a `DISPATCHED` marker. It then proves
that an exact duplicate and a later lease generation cannot retry while the
outcome is unknown, that reopening SQLite preserves
`RECONCILIATION_REQUIRED`, and that only an exact bound receipt resolves the
hold. The final receipt is explicitly synthetic and records `FAILED_FINAL`; it
does not claim provider delivery, payment, revenue, or any real external
effect. Private token material and temporary filesystem paths never appear in
the transcript. Fixed timestamps and identifiers make repeated runs
byte-identical.

### Audit inbox completion against effect state

Run `python effect_receipts.py --db effects.sqlite audit --inbox inbox.json`
to obtain a deterministic public-only cross-store report. The audit identifies
orphan, binding-mismatch, unresolved, terminal, and completed-with-unresolved
conditions without authorizing completion or exposing private token material.

### Portable offline audit bundle

Create a deterministic, public-only handoff envelope from the same read-only
snapshot, then verify its structure, findings, counts, and SHA-256 manifest
without reopening either source store:

```sh
python effect_audit_bundle.py create --inbox inbox.json --db effects.sqlite > audit-bundle.json
python effect_audit_bundle.py verify < audit-bundle.json
```

The bundle retains only canonical task IDs/digests/completion flags, a redacted
effect projection, and the portable audit report. It excludes task commands,
lease and worker identifiers, receipt references, token hashes, token paths,
and other private ledger fields.
Its hashes provide internal tamper detection, not a signature or proof of source
authenticity. A valid bundle does not authorize task completion, effect retry,
provider action, payment, delivery, or revenue recognition.

`effect_audit_chain.py` retains a deterministic, hash-linked sequence of those
verified portable bundles. It detects changed, duplicated, reordered, or
silently removed entries relative to a retained chain and verifies strict
append-only extensions. See [AUDIT_CHAIN.md](AUDIT_CHAIN.md). The chain remains
an offline self-integrity artifact and does not expand the bundle's authority.

## Publish task transitions to GitHub

`inbox_github.py` completes the existing protocol's repository-publication arm:
read an immutable inbox snapshot, retain a private operation request, publish
with GitHub `expectedHeadOid`, and reread the current task authority. It supports
claim, renew, release, completion and recovery of the exact original request.
An expired recovered claim remains the original generation. Unchanged snapshots
reuse their verified content after a fresh branch-ref read.

See [INBOX_GITHUB.md](INBOX_GITHUB.md) for the complete command-line workflow,
recovery outcomes and token-environment configuration.

Broad hosted regression jobs are disabled in the retained GitHub and CircleCI
configuration under the owner's focused-validation policy. Source and historical
courses remain available. Run only the specific relevant check for changed work;
the publisher's focused command is documented in its guide.
