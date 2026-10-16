# Offline worker lifecycle plan

`inbox_worker_plan.py` turns one fully validated inbox snapshot into a
deterministic, redacted scheduling view. It is intended for a phone/host worker
that needs to decide whether to claim, continue its exact current lease, wait
for another generation, or leave completed work alone.

```sh
python inbox_worker_plan.py inbox.json \
  --now 2026-10-16T01:00:00Z \
  --worker-id phone-a \
  --lease-id lease-a
```

`--now` is explicit trusted coordinator input so the same snapshot and inputs
produce byte-identical JSON. Both worker and lease identifiers are required:
matching only one is never reported as current ownership. The identifiers are
used for the comparison but are not copied into the output.

Each task is classified as one of:

- `claimable`: the task has no generation, was released, or its prior lease is
  expired. `next_attempt` is the generation a successful claim would create.
- `owned_live`: the supplied worker **and** lease match a currently live
  generation. The plan reports the required `expected_attempt` and expiry.
- `wait_for_other_lease`: another exact identity holds the live generation.
- `wait_for_creation`, `wait_for_claim_start`, or `wait_for_release`: the
  trusted planning time precedes the relevant validated timestamp.
- `completed`: no further lifecycle transition is proposed.
- `attempt_ceiling`: another generation would exceed the protocol ceiling.

The output retains only task ID/kind, semantic document/task digests, timing,
attempts, decisions, and counts. It omits commands, arguments, metadata,
results, completion IDs, worker IDs, and lease IDs.

Keep the plan in the same privacy boundary as its source inbox. Task IDs and
semantic digests remain correlation identifiers; the planner does not claim
they are anonymous or safe for public publication.

## Authority ceiling

This tool opens the inbox through the existing bounded stable-file validator.
It performs no network request, token loading, repository write, inbox mutation,
phone/provider action, or task execution. A plan is not a lease, a CAS receipt,
or permission to act. Use `task_protocol.py` to stage the chosen transition and
`inbox_github.py` to publish it with an exact head/blob guard and provider
readback. Only the exact published `(worker_id, lease_id, attempt)` generation
can authorize lifecycle mutations.
