# GitHub inbox publisher

`inbox_github.py` connects the existing `task_protocol.py` state machine to an
explicit GitHub repository and branch. It uses Python 3.10+ and the standard
library. Run it from this checkout, alongside the existing protocol and base
validator. Authentication comes from `GH_TOKEN`, falling back to `GITHUB_TOKEN`;
`--token-env` selects an existing environment variable without changing accounts.

## Read and publish

Read the current inbox, its immutable commit/blob, semantic digest and lease
summary. Task command text and result text are omitted from this summary.

```sh
python inbox_github.py --repo OWNER/REPOSITORY read
```

Each new transition uses a new private request filename. The complete request
is written and synchronized before the provider mutation. Use a private local
directory that already exists; keep request files with the worker's operational
state, outside the repository.

Inspect an existing request locally before resuming it. This command loads no
token and makes no provider request. It reconstructs the protocol transition and
prints a redacted identity/fingerprint summary; it omits the original and
proposed documents, task result, worker ID and lease ID.

```sh
python inbox_github.py --repo OWNER/REPOSITORY inspect \
  --request /private/worker/claim-001.json
```

The CLI repository, branch and path must match the request's retained target.
`request_sha256` identifies the canonical request object, while the document and
transition digests identify its reconstructed state without displaying private
source or result text. `provider_contacted`, `publication_attempted`, and
`executes_task` are always false in an inspection result. Inspection does not
establish a live lease or publish the transition.

Reconcile that validated request against one stable local inbox snapshot when
the provider route is unavailable or intentionally not in use:

```sh
python inbox_github.py --repo OWNER/REPOSITORY reconcile-local \
  --request /private/worker/claim-001.json \
  --inbox /offline/current-inbox.json
```

The command classifies the local snapshot as `base_equivalent`,
`already_applied`, or `diverged`. It prints only document/request fingerprints
and booleans; task command/result text, worker ID, lease ID, and retained source
documents remain omitted. The inbox path is read through the protocol's stable
regular-file reader, including final visible-path and generation checks.

Local equivalence does not prove the current provider branch head, make a
request publication-ready, or establish live task authority. Accordingly,
`provider_contacted`, `publication_attempted`, `publication_ready`,
`authority_active`, and `executes_task` are always false. Use `resume` for the
fresh provider read and exact expected-head publication/reconciliation step.

```sh
python inbox_github.py --repo OWNER/REPOSITORY claim \
  --request /private/worker/claim-001.json \
  --task-id TASK_ID --worker-id WORKER_ID --lease-id LEASE_ID \
  --lease-seconds 120

python inbox_github.py --repo OWNER/REPOSITORY renew \
  --request /private/worker/renew-001.json \
  --task-id TASK_ID --worker-id WORKER_ID --lease-id LEASE_ID \
  --expected-attempt 1 --lease-seconds 120

python inbox_github.py --repo OWNER/REPOSITORY release \
  --request /private/worker/release-001.json \
  --task-id TASK_ID --worker-id WORKER_ID --lease-id LEASE_ID \
  --expected-attempt 1 --reason "worker handoff"

python inbox_github.py --repo OWNER/REPOSITORY complete \
  --request /private/worker/complete-001.json \
  --task-id TASK_ID --worker-id WORKER_ID --lease-id LEASE_ID \
  --expected-attempt 1 --completion-id COMPLETION_ID --result "actual result"
```

Use the attempt returned by the actual claim, not the illustrative `1` above.
`--branch` and `--path` go before the subcommand. The default path is
`inbox.json`; the default branch is `main`. The default transition time is the
worker's current UTC clock. `--now` supplies an explicit trusted timestamp, but
publication still checks that the retained request is not future-dated and its
required lease remains live at the actual publication clock.

## Recovery without another generation

After a lost response, resume the same retained request:

```sh
python inbox_github.py --repo OWNER/REPOSITORY resume \
  --request /private/worker/claim-001.json
```

The client reconstructs the transition with the existing protocol, reads the
current branch, and compares the entire intended task. If that exact task is
already present, it returns `observed` without a write. Unrelated task changes
do not prevent recognition of an already-published request. An expired original
claim remains the original attempt and returns `authority_active: false`; a
resume cannot silently turn it into a new generation.

If the original branch head still matches, the identical prepared bytes can be
published against that same expected head. If the branch changed first, the
client returns `conflict`; retain the old request, inspect the current snapshot,
and prepare a distinct operation when appropriate. It never overwrites a
concurrent edit, force-pushes, rotates credentials, or repeatedly retries an
ambiguous provider response.

## Publication contract

The reader resolves the current branch to a commit, traverses only immutable
Git trees, rejects symlinks and submodules, and verifies the complete file bytes
against the Git blob ID. An unchanged head reuses the in-memory snapshot after
one fresh ref read. An unchanged blob at a new head reuses the original bytes.

The writer calls GitHub GraphQL `createCommitOnBranch` with `expectedHeadOid` and
one exact file addition. `clientMutationId` is a correlation ID, not a provider
idempotency guarantee. The expected-head condition is the atomic write fence.
Every publication is followed by a current-branch read, rather than treating a
successful HTTP response as current task authority.

A retained request includes task source and result data, but never the token.
The create-only file operation does not overwrite existing worker state.
Redirects are not followed with credentials. HTTP 401, 403 and 429 end the
operation without retries; request ID and Retry-After are included when returned.
The client does not modify repository permissions or workflow settings.

## Results

| Status | Meaning |
| --- | --- |
| `published` | A commit was returned and the intended task was read back. |
| `observed` | The exact intended task is present; this call need not have created it. |
| `conflict` | The prepared base changed before this call could publish. |
| `provider_refused` | Authorization or rate limiting stopped the request. |
| `state_changed` | A commit was returned, but current task state differs. |
| `outcome_unknown` | The retained request needs further readback/reconciliation. |

Exit 0 means read success or an observed/published transition; claim/renew also
require `authority_active: true`. Exit 2 is an input, storage or provider error;
exit 3 is a nonterminal publication result; exit 4 is an observed expired claim.
A successful receipt records a point-in-time observation, not a perpetual lease.
Workers must continue to honor STOP, current task ownership and lease expiry.
The existing effect-receipt ledger and provider-specific idempotency remain the
boundary for irreversible effects; this publisher never executes a command.

## Focused source check

```sh
python -m unittest discover -s tests -p test_inbox_github.py -v
```

This checks only the new transport and recovery boundaries with explicit local
provider/staging ports. It does not rerun the accepted protocol or ledger suites.

Primary API references: GitHub's
[commit mutation contract](https://docs.github.com/en/graphql/reference/commits)
and [REST API versioning](https://docs.github.com/rest/overview/api-versions).
