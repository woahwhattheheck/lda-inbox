# Lease / CAS execution protocol

`task_protocol.py` is an additive coordination layer for the version-1
`inbox.json` envelope. It does **not** execute a task, contact a phone, publish a
Git commit, or grant device authority by itself. It gives producers and polling
workers a deterministic state machine for deciding which worker currently owns
a task and for staging a repository update that can be published with a real
Git/ref compare-and-swap.

The base `validate_inbox.py` contract remains unchanged. Legacy pending and
completed tasks without an `execution` object remain valid.

## Why this exists

Polling the same JSON document from multiple retries or devices is otherwise
ambiguous: two workers can read the same pending task, both execute it, and only
later race to write a completion receipt. The protocol narrows that race by
requiring a published lease before execution and by binding that lease to the
exact task specification.

It provides:

- one active `worker_id` + `lease_id` pair at a time;
- a canonical SHA-256 of immutable/additive task-specification fields;
- bounded leases (at most one hour and never beyond the task's `timeout_s`
  measured from the original claim);
- monotonic claim/renew/release timestamps supplied by a trusted coordinator;
- explicit release and expired-lease reassignment with an incrementing attempt;
- idempotent replay for a live claim, an exact renewal, an exact release, and an
  exact completion;
- result and completion identity binding;
- semantic document SHA-256 receipts for transition audit;
- fail-closed rejection of unknown execution-state fields and stale semantic
  snapshots.

This is **not an exactly-once side-effect guarantee**. If a device performs an
irreversible external effect and crashes before publishing completion, a later
lease may legitimately retry the task after expiry. Commands that can cause
external side effects still need their own idempotency key / provider receipt or
human policy appropriate to that effect.

## Trust and publication boundary

The CLI's `--expected-document-sha256` protects a transition against an
unexpected **semantic** input snapshot. It does not lock Git, and it is not a
replacement for repository compare-and-swap.

A safe writer should:

1. read `inbox.json` from an exact repository ref/blob;
2. run `task_protocol.py validate` and `task_protocol.py digest`;
3. stage the desired transition using that digest;
4. publish the staged bytes only if the repository ref/blob is still the exact
   version read in step 1;
5. re-read the published document and confirm the intended `worker_id`,
   `lease_id`, `task_sha256`, and state before executing the task.

Two workers may compute different valid claim candidates from the same semantic
input. The repository ref/blob CAS in step 4 is what ensures only one of those
candidates becomes authoritative.

`--now` is also an authority input. It must come from a trusted, offset-aware
coordinator clock. The protocol does not authenticate callers or prove clock
truth.

## State model

A task with no `execution` object is unleased. Once claimed, the execution
object has protocol version `1` and one of these states:

### `leased`

The worker owns the task until `lease_expires_at`.

Required fields:

- `v`
- `state`
- `attempt`
- `worker_id`
- `lease_id`
- `claimed_at`
- `lease_expires_at`
- `task_sha256`

At exact expiry, the lease is no longer active. Reassignment requires a new
`lease_id` and increments `attempt`.

### `available`

The prior owner explicitly released the task. The previous lease identity is
retained as audit history and cannot be reused. `released_at` is required;
`release_reason` is optional and bounded to 2 KiB UTF-8.

### `completed`

Completion requires an active, unexpired lease and adds `completion_id` plus
`result_sha256`. Top-level `done`, `completed_at`, and `result` remain the base
v1 completion receipt. Retrying the exact same completion is idempotent; a
different completion ID or result conflicts.

## Task-spec binding

`task_sha256` includes every task field except mutable protocol/completion
fields:

- `done`
- `completed_at`
- `result`
- `execution`

That means additive task metadata is part of the lease authority. Changing a
command, timeout, metadata field, customer identifier, or other additive spec
field invalidates an existing lease instead of silently changing work beneath a
worker.

## CLI

Validate the base envelope plus protocol state:

```sh
python task_protocol.py validate inbox.json
```

Get the canonical semantic digest for the current snapshot:

```sh
python task_protocol.py digest inbox.json
```

Stage a claim (stdout is the next JSON document; stderr is a compact transition
receipt):

```sh
python task_protocol.py claim inbox.json \
  --task-id t-123 \
  --worker-id phone-a \
  --lease-id lease-20260913-a \
  --now 2026-09-13T10:00:00Z \
  --lease-seconds 120 \
  --expected-document-sha256 "$SHA"
```

The same pattern is available for:

```text
renew   --lease-seconds N
release [--reason TEXT]
complete --completion-id ID --result TEXT
```

Mutating commands never overwrite the input path and never publish to Git. A
successful transition prints only a staged next document on stdout. The caller
owns the repository-level ref/blob CAS and must not execute until the published
lease is observed as authoritative.

## Validation

Run the complete repository suite and both validators:

```sh
python -m unittest discover -s tests -v
python validate_inbox.py inbox.json
python task_protocol.py validate inbox.json
```
