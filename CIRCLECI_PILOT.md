# Public CI redundancy pilot

This repository is an intentionally small, already-public, non-secret pilot for a second CI execution rail while GitHub-hosted Actions capacity or billing is unavailable.

## Scope

The CircleCI job in `.circleci/config.yml` mirrors the existing GitHub Actions `inbox-contract` payload on Python 3.12:

```text
python -m unittest discover -s tests -v
python validate_inbox.py inbox.json
python task_protocol.py validate inbox.json
```

It does **not** replace, relax, or reinterpret the native GitHub check. A CircleCI pass is supplemental execution evidence only; it is never represented as a GitHub Actions success.

## Cost and blast-radius fence

- public repository only; no private source is made public
- no secrets, contexts, deploy keys, provider credentials, or write tokens are required by the job
- one `small` Docker executor, `parallelism: 1`
- branch filter is limited to `main` and the pilot branch
- no deployment, publication, billing, or external mutation steps
- command-level no-output timeouts are bounded to 2–5 minutes

Connecting the repository to CircleCI, accepting provider terms, changing a billing plan, or granting an app new permissions is deliberately **not** performed by this carrier. Those remain explicit owner/provider actions.

## Activation receipt

After an authorized owner connects this public repository, record the first CircleCI pipeline URL, exact Git commit SHA, Python version, and the three command conclusions. Until that receipt exists, this carrier means **CONFIG_READY / NOT_EXECUTED_ON_CIRCLECI**.
