# Lease / CAS execution protocol

`task_protocol.py` coordinates ownership of tasks in `inbox.json`. It never
executes a task, contacts a phone, authenticates a caller, or publishes a Git
update. Mutating commands stage the next JSON document on stdout only.

## Trust boundary

A safe publisher must:

1. read `inbox.json` from an exact Git ref/blob;
2. run the base validator and `task_protocol.py validate`;
3. compute `task_protocol.py digest`;
4. stage one transition against that semantic document SHA;
5. publish the staged bytes only if the exact Git ref/blob is unchanged;
6. re-read the published document and confirm the intended lease authority
   before executing the task.

`--expected-document-sha256` is a semantic snapshot fence, not a Git lock.
`--now` is trusted coordinator input; the protocol does not prove clock truth.

## Authority model

The lease capability is:

```text
(worker_id, lease_id, attempt)
```

- `worker_id` names the worker.
- `lease_id` is a correlation identifier.
- `attempt` is the immutable lease generation and is the ABA fence.

A raw `lease_id` is **not globally unique**. After exact expiry or explicit
release, any valid next claim increments `attempt`; the claimant may reuse a
prior raw `lease_id`. That reuse is safe because every authority-bearing
mutation (`renew`, `release`, `complete`) requires the exact published attempt.

Example:

```text
attempt 1: phone-a / lease-a
attempt 2: phone-b / lease-b
attempt 3: phone-a / lease-a
```

A stale request carrying `(phone-a, lease-a, attempt=1)` cannot mutate attempt
3. Matching worker and raw lease strings are insufficient.

The claim receipt includes `attempt`; consumers must carry that exact value
forward. If a task is reassigned, the previous generation becomes permanently
stale even if its raw IDs later recur.

## States

### No `execution`

The task has not been leased.

### `leased`

Required execution fields:

- `v`
- `state`
- `attempt`
- `worker_id`
- `lease_id`
- `claimed_at`
- `lease_expires_at`
- `task_sha256`

A live identical claim is idempotent within the same generation. At exact
expiry, the lease is inactive; a new claim increments `attempt`.

### `available`

An active generation was explicitly released. It retains its worker, lease,
attempt, claim/expiry timestamps, task digest, and `released_at` so an exact
release retry is idempotent. A new claim increments `attempt`.

An expired generation cannot be released after the fact.

### `completed`

Completion requires a live, unexpired lease and the exact current attempt.
The execution receipt additionally binds `completion_id` and `result_sha256`;
the base task holds `done=true`, `completed_at`, and the result. Only an exact
same-generation completion replay is idempotent.

## Task binding and time bounds

`task_sha256` covers every task field except mutable completion/protocol fields:

- `done`
- `completed_at`
- `result`
- `execution`

Changing command, timeout, metadata, customer identifiers, or other task
specification invalidates an existing lease.

A lease expires at most 3,600 seconds after its **original generation claim**
and never after that task's `timeout_s`. Renewals cannot reset the original
claim clock.

## CLI

Validate and digest:

```sh
python task_protocol.py validate inbox.json
SHA="$(python task_protocol.py digest inbox.json)"
```

Stage a claim:

```sh
python task_protocol.py claim inbox.json \
  --task-id task-123 \
  --worker-id phone-a \
  --lease-id lease-a \
  --now 2026-09-13T10:00:00Z \
  --lease-seconds 120 \
  --expected-document-sha256 "$SHA"
```

The stderr receipt includes the published candidate's `attempt`. After
repository CAS and re-read, bind that value on every mutation:

```sh
python task_protocol.py renew inbox.json \
  --task-id task-123 \
  --worker-id phone-a \
  --lease-id lease-a \
  --expected-attempt 1 \
  --now 2026-09-13T10:01:00Z \
  --lease-seconds 120 \
  --expected-document-sha256 "$SHA"

python task_protocol.py release inbox.json \
  --task-id task-123 \
  --worker-id phone-a \
  --lease-id lease-a \
  --expected-attempt 1 \
  --now 2026-09-13T10:01:10Z \
  --reason "battery low" \
  --expected-document-sha256 "$SHA"

python task_protocol.py complete inbox.json \
  --task-id task-123 \
  --worker-id phone-a \
  --lease-id lease-a \
  --expected-attempt 1 \
  --completion-id completion-a \
  --result "ok" \
  --now 2026-09-13T10:01:20Z \
  --expected-document-sha256 "$SHA"
```

Each mutating command prints only the staged next document to stdout and a
compact transition receipt to stderr. It never overwrites `inbox.json`.

## Security / execution boundary

The protocol narrows repository coordination races. It does not make external
effects exactly once. A worker can perform an irreversible action and crash
before publishing completion; after expiry another generation may legitimately
retry. Any command with external side effects requires a downstream
idempotency/provider receipt or human policy that survives that crash window.

## Regression requirements

The repository suite must cover, at minimum:

- exact-expiry reassignment using the same raw `lease_id`;
- same-worker/same-lease exact-expiry creating a new generation, not a replay;
- A→B→A raw-identity reuse through both expiry and explicit release;
- stale generation rejection for renew/release/complete;
- independent semantic-document CAS rejection;
- chronology checks for idempotent retries;
- expired lease release rejection;
- normal and `python -O` execution.
