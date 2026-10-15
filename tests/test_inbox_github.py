"""Focused transport/recovery contract; provider and staging ports stay local."""
import base64
import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import inbox_github as client

HEAD = "1" * 40
NEXT = "2" * 40
OTHER = "3" * 40
NOW = datetime(2026, 10, 15, 20, 0, 30, tzinfo=timezone.utc)
BEFORE = {"v": 1, "tasks": [{"id": "transport-case", "kind": "run_task", "done": False}]}
AFTER = copy.deepcopy(BEFORE)
AFTER["tasks"][0]["execution"] = {
    "state": "leased", "worker_id": "worker-a", "lease_id": "lease-a", "attempt": 1,
    "claimed_at": "2026-10-15T20:00:00Z", "lease_expires_at": "2026-10-15T20:02:00Z",
}
ARGS = {"task_id": "transport-case", "worker_id": "worker-a", "lease_id": "lease-a",
        "now": "2026-10-15T20:00:00Z", "lease_seconds": 120}


def snapshot(value, head=HEAD):
    raw = client.json_bytes(value)
    return client.Snapshot(head, client.blob_oid(raw), raw)


def protocol_port():
    # The existing task_protocol owns domain validation and transition semantics.
    # This controlled port isolates the new publisher's exact I/O boundaries.
    port = Mock()
    port.parse_protocol_text.side_effect = json.loads
    port.document_sha256.side_effect = lambda value: hashlib.sha256(client.json_bytes(value)).hexdigest()
    port.canonical_document_text.side_effect = lambda value: client.json_bytes(value).decode()
    port.claim_task.side_effect = lambda document, **kwargs: (copy.deepcopy(AFTER), {
        "action": "claim", "task_id": kwargs["task_id"], "attempt": 1,
        "before_sha256": kwargs["expected_document_sha256"], "changed": True,
    })
    return port


class MemoryTransport:
    def __init__(self):
        self.target = client.Target("example/inbox")
        self.current = snapshot(BEFORE)
        self.commits = 0
        self.reads = 0
        self.failure = None
        self.lose_reply = False

    def read(self):
        self.reads += 1
        return self.current

    def commit(self, expected_head, raw, request_id):
        self.commits += 1
        if self.failure:
            raise self.failure
        if expected_head != self.current.head:
            raise client.ProviderError("expected-head conflict")
        self.current = client.Snapshot(NEXT, client.blob_oid(raw), raw)
        if self.lose_reply:
            raise client.ProviderError("lost mutation reply")
        return NEXT


class InboxGitHubTests(unittest.TestCase):
    def operation(self):
        transport = MemoryTransport()
        coordinator = client.Coordinator(transport, protocol_port(), lambda: NOW)
        request = coordinator.prepare(transport.current, "claim", ARGS)
        return transport, coordinator, request

    def test_target_rejects_noncanonical_branch_components(self):
        for branch in ("@", ".hidden", "team/.hidden", "release.",
                       "team/release./next", "main\x7f"):
            with self.subTest(branch=repr(branch)), self.assertRaisesRegex(
                    client.InboxGitHubError, "invalid branch"):
                client.Target("example/inbox", branch)
        self.assertEqual(client.Target("example/inbox", "release/next").branch,
                         "release/next")
        self.assertEqual(client.Target("example/inbox", "team/@").branch, "team/@")

    def test_immutable_tree_blob_read_and_cache(self):
        raw = client.json_bytes(BEFORE)
        blob = client.blob_oid(raw)
        transport = client.GitHubInbox(client.Target("example/inbox"), "test-only-token")
        transport.request = Mock(side_effect=[
            {"object": {"type": "commit", "sha": HEAD}},
            {"truncated": False, "tree": [{"path": "inbox.json", "type": "blob",
                "mode": "100644", "sha": blob}]},
            {"sha": blob, "encoding": "base64", "size": len(raw),
                "content": base64.b64encode(raw).decode()},
            {"object": {"type": "commit", "sha": HEAD}},
        ])
        first = transport.read()
        self.assertIs(first, transport.read())
        self.assertEqual(transport.request.call_count, 4)
        self.assertEqual(transport.request.call_args_list[1].args,
                         ("GET", f"/repos/example/inbox/git/trees/{HEAD}"))
        self.assertEqual(transport.request.call_args_list[2].args,
                         ("GET", f"/repos/example/inbox/git/blobs/{blob}"))
        transport._cached = None
        transport.request = Mock(side_effect=[
            {"object": {"type": "commit", "sha": HEAD}},
            {"truncated": False, "tree": [{"path": "inbox.json", "type": "blob",
                "mode": "120000", "sha": blob}]},
        ])
        with self.assertRaisesRegex(client.InboxGitHubError, "regular repository file"):
            transport.read()
        with self.assertRaises(client.InboxGitHubError):
            client.Snapshot(HEAD, blob, raw + b" ")

    def test_mutation_is_one_exact_expected_head_addition(self):
        transport = client.GitHubInbox(client.Target("example/inbox", "work/queue"), "test-only-token")
        transport.request = Mock(return_value={"data": {"createCommitOnBranch": {"commit": {"oid": NEXT}}}})
        raw = client.json_bytes(AFTER)
        self.assertEqual(transport.commit(HEAD, raw, "a" * 32), NEXT)
        method, path, payload = transport.request.call_args.args
        self.assertEqual((method, path), ("POST", "/graphql"))
        change = payload["variables"]["input"]
        self.assertEqual(change["expectedHeadOid"], HEAD)
        self.assertEqual(change["branch"]["branchName"], "work/queue")
        self.assertEqual(len(change["fileChanges"]["additions"]), 1)
        self.assertEqual(base64.b64decode(change["fileChanges"]["additions"][0]["contents"]), raw)
        self.assertNotIn("test-only-token", json.dumps(payload))
        transport.request.return_value = {"errors": [{"type": "UNPROCESSABLE", "message": "private input"}]}
        with self.assertRaisesRegex(client.ProviderError, "UNPROCESSABLE") as error:
            transport.commit(HEAD, raw, "b" * 32)
        self.assertNotIn("private input", str(error.exception))

    def test_lost_reply_and_repeated_resume_commit_once(self):
        transport, coordinator, request = self.operation()
        transport.lose_reply = True
        first = coordinator.apply(request)
        self.assertEqual(first["status"], "observed")
        self.assertTrue(first["authority_active"])
        second = coordinator.apply(request)
        self.assertEqual(second["status"], "observed")
        self.assertEqual(transport.commits, 1)
        self.assertEqual(first["execution"]["attempt"], second["execution"]["attempt"])
        # Expired recovery reports the same original generation, never a new claim.
        coordinator.clock = lambda: datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)
        expired = coordinator.apply(request)
        self.assertFalse(expired["authority_active"])
        self.assertEqual(transport.commits, 1)

    def test_conflicts_expired_writes_and_refusals(self):
        transport, coordinator, request = self.operation()
        transport.current = snapshot({"v": 1, "tasks": []}, OTHER)
        result = coordinator.apply(request)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["task_present"])
        self.assertEqual(transport.commits, 0)
        transport.current = snapshot(BEFORE)
        coordinator.clock = lambda: datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(client.InboxGitHubError, "expired"):
            coordinator.apply(request)
        self.assertEqual(transport.commits, 0)
        for status in (401, 403, 429):
            transport, coordinator, request = self.operation()
            transport.failure = client.ProviderError("provider refused", status=status, retry_after="60")
            result = coordinator.apply(request)
            self.assertEqual(result["status"], "provider_refused")
            self.assertEqual((transport.reads, transport.commits), (1, 1))
            self.assertFalse(result["authority_active"])

    def test_durable_request_identity_and_cli_opening(self):
        transport, coordinator, request = self.operation()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "request.json"
            raw = client.json_bytes(request)
            client.create_private_file(path, raw)
            self.assertEqual(client.load_private_file(path), request)
            with self.assertRaises(FileExistsError):
                client.create_private_file(path, b"replacement")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)
        tampered = copy.deepcopy(request)
        tampered["proposed"] += " "
        with self.assertRaises(client.InboxGitHubError):
            coordinator.apply(tampered)
        self.assertEqual(transport.commits, 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_result:
            client.main(["--help"])
        self.assertEqual(exit_result.exception.code, 0)
        self.assertIn("claim,renew,release,complete", output.getvalue())


if __name__ == "__main__":
    unittest.main()
