# lda-inbox

Task inbox for the LocalDeviceAgent phone agent (polled by the app).

## Contract validation

`inbox.json` is validated without third-party dependencies:

```sh
python validate_inbox.py inbox.json
python task_protocol.py validate inbox.json
python -m unittest discover -s tests -v
```

The version-1 contract rejects duplicate JSON keys or task IDs, unsupported task kinds, malformed or offset-free timestamps, non-positive/boolean timeouts, and incoherent completion receipts. It also treats the repository file as a bounded execution envelope rather than an unbounded work queue:

- the JSON document is capped at 1 MiB and at 256 tasks;
- task IDs are canonical ASCII transport identifiers (`[A-Za-z0-9][A-Za-z0-9._-]*`) capped at 128 UTF-8 bytes;
- commands are capped at 64 KiB and completion results at 256 KiB, measured as UTF-8 bytes;
- per-task timeouts are capped at 86,400 seconds (24 hours).

These ceilings make publication and phone polling fail closed on accidental or hostile resource amplification while leaving normal task metadata extensible. Unknown additive fields remain permitted so producers can attach metadata without weakening the required task envelope; the 1 MiB whole-document ceiling bounds that metadata as well.

## Lease / CAS coordination

`task_protocol.py` adds an optional, backwards-compatible execution state for
multi-poller/retry coordination. A worker must first publish and then re-read an
exact task lease before it executes the command. The lease binds worker + lease
identity to the task specification, is time-bounded, supports explicit release
and expired reassignment, and produces deterministic transition receipts.

The CLI never overwrites `inbox.json` and never publishes a Git update; it only
stages a next JSON snapshot after checking the caller's semantic document
SHA-256. The publisher must still use an exact Git/ref or blob compare-and-swap
so two workers cannot both make their independently staged claims authoritative.

This coordination layer does not promise exactly-once external side effects and
does not authenticate callers or clocks. Side-effecting commands need their own
idempotency/receipt boundary. See [TASK_PROTOCOL.md](TASK_PROTOCOL.md) for the
state machine, trust model, publication sequence, and CLI examples.
