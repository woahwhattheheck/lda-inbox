# Retained inbox handoff

`inbox_handoff.py` composes the two maintained read-only views needed when an
operator resumes work from retained inbox editions:

1. `inbox_history_audit.py` verifies that the ordered editions form a valid
   protocol history.
2. `inbox_worker_plan.py` evaluates the latest edition for one exact
   worker/lease identity at an explicit trusted time.

It is a source-only handoff. It does not claim, renew, release, complete, execute,
publish, contact a provider or device, or grant lease authority.

## Run

Provide two to 256 snapshots in oldest-to-newest order:

```sh
python inbox_handoff.py \
  snapshot-001.json snapshot-002.json snapshot-003.json \
  --now 2026-10-16T01:00:00Z \
  --worker-id phone-a \
  --lease-id lease-a \
  > inbox-handoff.json
```

Every snapshot is opened through the existing bounded protocol path reader. A
malformed snapshot, unsafe path generation, invalid transition, changed immutable
task specification, completion rewrite, lease-generation break, or invalid
planning identity/time fails the complete operation.

## Output

A successful envelope contains:

- `latest_document_sha256`: the digest shared by the final audited snapshot and
  the current plan;
- `history`: the unchanged redacted retained-history report;
- `current_plan`: the unchanged redacted worker plan for the latest snapshot;
- `handoff_sha256`: SHA-256 of the canonical envelope before that field is added;
- an explicit all-false authority boundary.

The output can contain task IDs, task kinds, task digests, attempts, transition
kinds, decision states and relevant timestamps. It does not copy task commands,
metadata, results, release reasons, source paths, worker IDs or lease IDs.
Repeated runs over the same editions with the same explicit arguments produce
the same JSON values.

`handoff_sha256` detects changes inside the emitted envelope. It is not a
signature, proof of repository provenance, lease, approval, provider receipt,
or authorization to execute anything.

## Failure and recovery

The CLI returns exit status 2 and a redacted `HANDOFF_INVALID` JSON object on
failure. Snapshot read and validation errors identify only their zero-based
position; private file names, paths and invalid source values are not copied to
the report.

After a valid handoff, a worker still must use the documented generation-bound
`task_protocol.py` transition, publish with the repository compare-and-swap
boundary, and re-read the published authority before any execution. The handoff
never substitutes for those steps.
