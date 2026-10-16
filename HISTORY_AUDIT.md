# Offline inbox history audit

`inbox_history_audit.py` verifies that an ordered sequence of retained,
individually valid inbox snapshots remains consistent with the version-1 task
and lease protocol.

```sh
python inbox_history_audit.py snapshot-001.json snapshot-002.json snapshot-003.json
```

The audit fails closed if root metadata drifts, history removes or reorders a
task, changes an immutable task specification, regresses or rewrites a completion, removes
execution state, rolls back or skips a generation, changes raw authority within
one generation, shortens a renewal, rewrites a release/completion expiry, or
starts a later generation before the prior release/expiry. New tasks may only
be appended in pending, unleased form.

Successful output is deterministic JSON containing document SHA-256 values,
task-specification SHA-256 values, attempts, transition kinds, and counts. It
omits input paths, commands, arguments, metadata, results, completion IDs,
release reasons, and raw worker/lease identities. Task and document digests
remain within the inbox privacy boundary.

This is a read-only continuity check over supplied files. It does **not** prove
snapshot origin or completeness, sign the report, inspect repository refs,
publish Git, load credentials, grant a lease, authorize completion, execute a
task, contact a device/provider, or recognize an external effect. A valid
report cannot replace the exact repository CAS and post-publication authority
read required by `TASK_PROTOCOL.md`.

Focused validation:

```sh
python -m unittest tests.test_inbox_history_audit
python -m py_compile inbox_history_audit.py tests/test_inbox_history_audit.py
```
