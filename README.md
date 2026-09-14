# lda-inbox

Repository-backed task inbox for the LocalDeviceAgent phone agent.

## Validate

```sh
python validate_inbox.py inbox.json
python task_protocol.py validate inbox.json
python -m unittest discover -s tests -v
```

The base version-1 inbox validator fail-closes malformed JSON, duplicate IDs,
unsafe identifiers, unsupported task shapes, incoherent completion receipts,
non-finite values, unsafe path/file inputs, and bounded document/task resources.

Path validation opens one stable regular-file generation through a bounded
file descriptor. Symlinks, FIFOs, devices, pathname replacement, in-read file
mutation, and growth beyond the byte ceiling fail closed before JSON authority
is granted.

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
