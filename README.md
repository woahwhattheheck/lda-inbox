# lda-inbox

Task inbox for the LocalDeviceAgent phone agent (polled by the app).

## Contract validation

`inbox.json` is validated without third-party dependencies:

```sh
python validate_inbox.py inbox.json
python -m unittest discover -s tests -v
```

The version-1 contract rejects duplicate JSON keys or task IDs, unsupported task kinds, malformed or offset-free timestamps, non-positive/boolean timeouts, and incoherent completion receipts. It also treats the repository file as a bounded execution envelope rather than an unbounded work queue:

- the JSON document is capped at 1 MiB and at 256 tasks;
- task IDs are canonical ASCII transport identifiers (`[A-Za-z0-9][A-Za-z0-9._-]*`) capped at 128 UTF-8 bytes;
- commands are capped at 64 KiB and completion results at 256 KiB, measured as UTF-8 bytes;
- per-task timeouts are capped at 86,400 seconds (24 hours).

These ceilings make publication and phone polling fail closed on accidental or hostile resource amplification while leaving normal task metadata extensible. Unknown additive fields remain permitted so producers can attach metadata without weakening the required task envelope; the 1 MiB whole-document ceiling bounds that metadata as well.
