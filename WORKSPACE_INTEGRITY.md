# Cross-workspace task integrity audit

`inbox_workspace_integrity.py` examines the latest retained edition of every
workspace in one local operator database. It answers a question that an ordinary
workspace or workload view cannot answer safely: does the same task identity
remain independently executable or disagree across imported workspace copies?

The audit is read-only. It does not select a winning workspace, claim or release
a task, change a lease, contact a phone or provider, or grant execution authority.
Use the original task in the maintained operator for every action after resolving
the finding through the task's actual source owner.

## Run

Choose the exact instant at which live leases should be classified:

```sh
python inbox_workspace_integrity.py \
  --db /private/operator.sqlite \
  --as-of 2026-10-16T12:00:00Z \
  > /private/Workspace_Integrity.json
```

The command opens the database read-only, starts one SQLite read transaction,
checks every latest edition against its retained digest, and validates each inbox
through the maintained task protocol. Exit status is `0` for a clean report, `2`
when findings are present, and `1` when the selected database cannot be audited
safely. A finding is an operator decision input, not permission to execute,
overwrite or delete either copy.

## Findings

- `spec_divergence`: one task ID names different immutable task specifications.
- `multiple_executable_copies`: two or more nonterminal copies could be claimed or
  continued independently. Identical imported copies are not additional fleet
  capacity.
- `concurrent_live_leases`: more than one copy has unexpired lease authority at
  the supplied `--as-of` instant.
- `stale_executable_after_completion`: a terminal copy coexists with a copy that
  can still execute.
- `completion_conflict`: terminal copies retain different result digests.

The deterministic JSON includes workspace and task IDs, edition/specification
digests, states, attempts and hashed authority/result bindings. It excludes task
commands, results, workspace labels, worker IDs, lease IDs and completion IDs.
Keep the report private when workspace/task identifiers themselves are private.
The final `report_sha256` binds every other report field, including the explicit
audit instant.

## Resolution boundary

Resolve a repeated identity in its original coordination record. Preserve both
source editions until the source owner identifies the authoritative workspace.
Do not infer that an expired lease or an old imported copy is abandoned. The
maintained per-workspace document digest, optimistic concurrency and lease
generation rules still govern any later transition.
