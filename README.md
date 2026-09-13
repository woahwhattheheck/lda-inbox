# lda-inbox

Task inbox for the LocalDeviceAgent phone agent (polled by the app).

## Contract validation

`inbox.json` is validated without third-party dependencies:

```sh
python validate_inbox.py inbox.json
python -m unittest discover -s tests -v
```

The version-1 contract rejects duplicate JSON keys or task IDs, unsupported task kinds, malformed or offset-free timestamps, non-positive/boolean timeouts, and incoherent completion receipts. Unknown additive fields remain permitted so producers can attach metadata without weakening the required task envelope.
