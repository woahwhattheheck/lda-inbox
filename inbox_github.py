#!/usr/bin/env python3
"""Publish existing task-protocol transitions with GitHub expected-head CAS.

One invocation performs one requested transition. An immutable local request
survives a lost reply; ``resume`` reconciles that request before any repeat CAS.
This module does not execute inbox commands or communicate with a device.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

API = "https://api.github.com"
MAX_BYTES = 8 * 1024 * 1024
OID = re.compile(r"[0-9a-f]{40}\Z")
ACTIONS = ("claim", "renew", "release", "complete")
MUTATION = """mutation InboxCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}"""


class InboxGitHubError(ValueError):
    """Invalid input or a failed publication boundary."""


class ProviderError(InboxGitHubError):
    """A provider refusal or uncertain network result, without request secrets."""

    def __init__(self, message: str, *, status: int | None = None,
                 request_id: str | None = None, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after

    def public(self) -> dict[str, Any]:
        return {"message": str(self), "http_status": self.status,
                "request_id": self.request_id, "retry_after": self.retry_after}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InboxGitHubError("duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise InboxGitHubError("non-finite JSON value")


def decode_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise InboxGitHubError("JSON exceeds the transport byte limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InboxGitHubError("invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise InboxGitHubError("expected a JSON object")
    return value


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def oid(value: Any) -> str:
    if not isinstance(value, str) or OID.fullmatch(value) is None:
        raise InboxGitHubError("expected a complete Git SHA-1 object ID")
    return value


def blob_oid(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise InboxGitHubError("invalid request timestamp") from exc
    if result.tzinfo is None:
        raise InboxGitHubError("request timestamp needs a UTC offset")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Target:
    repository: str
    branch: str = "main"
    path: str = "inbox.json"

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise InboxGitHubError("repository must be owner/name")
        # Only canonical branch and repository-relative file names are accepted.
        for name, value in (("branch", self.branch), ("path", self.path)):
            if (not isinstance(value, str) or not value or value != value.strip()
                    or value.startswith("/") or value.endswith("/")
                    or any(part in ("", ".", "..") for part in value.split("/"))
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    or "\\" in value):
                raise InboxGitHubError(f"invalid {name}")
        if (self.branch == "@" or ".." in self.branch or "@{" in self.branch
                or any(c in self.branch for c in " ~^:?*[")
                or any(p.startswith(".") or p.endswith(".") or p.endswith(".lock")
                       for p in self.branch.split("/"))):
            raise InboxGitHubError("invalid branch name")

    def as_dict(self) -> dict[str, str]:
        return {"repository": self.repository, "branch": self.branch, "path": self.path}


@dataclass(frozen=True)
class Snapshot:
    head: str
    blob: str
    raw: bytes

    def __post_init__(self) -> None:
        oid(self.head)
        oid(self.blob)
        if len(self.raw) > MAX_BYTES or blob_oid(self.raw) != self.blob:
            raise InboxGitHubError("inbox bytes do not match the pinned Git blob")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A repository rename or redirect needs explicit target resolution.
        return None


class GitHubInbox:
    """Small stdlib transport. No retry loops, account switching or force pushes."""

    def __init__(self, target: Target, token: str, *, timeout: float = 30,
                 opener: Any = None):
        if not token or "\n" in token or "\r" in token:
            raise InboxGitHubError("a valid token environment variable is required")
        self.target = target
        self._token = token
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirect())
        self._cached: Snapshot | None = None

    def request(self, method: str, path: str,
                payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json_bytes(payload)
        if body is not None and len(body) > MAX_BYTES:
            raise InboxGitHubError("request exceeds the transport byte limit")
        request = Request(API + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "lda-inbox-publisher/1",
            "X-GitHub-Api-Version": "2026-03-10",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            # Do not echo an HTTP body that might contain submitted task data.
            details = {"status": exc.code,
                       "request_id": exc.headers.get("X-GitHub-Request-Id"),
                       "retry_after": exc.headers.get("Retry-After")}
            exc.close()
            raise ProviderError(f"GitHub HTTP {exc.code}", **details) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"GitHub transport failure ({type(exc).__name__})") from None
        try:
            return decode_json(raw)
        except InboxGitHubError as exc:
            raise ProviderError("GitHub returned an invalid or oversized JSON response") from exc

    def read(self) -> Snapshot:
        repo = self.target.repository
        branch = quote(self.target.branch, safe="")
        ref = self.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        target = ref.get("object", {})
        if not isinstance(target, dict) or target.get("type") != "commit":
            raise InboxGitHubError("branch does not name a commit")
        head = oid(target.get("sha"))
        if self._cached is not None and self._cached.head == head:
            return self._cached
        # Walk immutable trees so symlinks/submodules cannot masquerade as a file.
        tree_id = head
        parts = self.target.path.split("/")
        entry = None
        for index, name in enumerate(parts):
            tree = self.request("GET", f"/repos/{repo}/git/trees/{tree_id}")
            if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
                raise InboxGitHubError("incomplete repository tree")
            entries = [item for item in tree["tree"]
                       if isinstance(item, dict) and item.get("path") == name]
            if len(entries) != 1:
                raise InboxGitHubError("inbox path is absent or ambiguous")
            entry = entries[0]
            tree_id = oid(entry.get("sha"))
            if index < len(parts) - 1:
                if entry.get("type") != "tree" or entry.get("mode") != "040000":
                    raise InboxGitHubError("inbox parent is not a repository directory")
            elif entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                raise InboxGitHubError("inbox must be a regular repository file")
        if self._cached is not None and self._cached.blob == tree_id:
            self._cached = Snapshot(head, tree_id, self._cached.raw)
            return self._cached
        item = self.request("GET", f"/repos/{repo}/git/blobs/{tree_id}")
        if item.get("encoding") != "base64":
            raise InboxGitHubError("inbox blob must use base64 encoding")
        try:
            encoded = item["content"]
            if not isinstance(encoded, str):
                raise ValueError("content is not text")
            raw = base64.b64decode(encoded.replace("\n", ""), validate=True)
        except (KeyError, ValueError, binascii.Error) as exc:
            raise InboxGitHubError("invalid inbox blob encoding") from exc
        if (type(item.get("size")) is not int or item["size"] != len(raw)
                or oid(item.get("sha")) != tree_id):
            raise InboxGitHubError("inbox blob size/identity mismatch")
        self._cached = Snapshot(head, tree_id, raw)
        return self._cached

    def commit(self, expected_head: str, raw: bytes, request_id: str) -> str:
        payload = {"query": MUTATION, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": self.target.repository,
                       "branchName": self.target.branch},
            "expectedHeadOid": oid(expected_head),
            "message": {"headline": "Update task inbox coordination"},
            "clientMutationId": request_id,
            "fileChanges": {"additions": [{"path": self.target.path,
                "contents": base64.b64encode(raw).decode("ascii")} ]},
        }}}
        response = self.request("POST", "/graphql", payload)
        try:
            node = (response.get("data") or {}).get("createCommitOnBranch") or {}
            commit = (node.get("commit") or {}).get("oid")
            if commit is not None:
                return oid(commit)
        except (AttributeError, InboxGitHubError) as exc:
            raise ProviderError("GitHub returned an invalid commit response") from exc
        # clientMutationId correlates; only expectedHeadOid is the write fence.
        errors = response.get("errors") or []
        if not isinstance(errors, list):
            raise ProviderError("GitHub returned invalid GraphQL errors")
        types = sorted({str(item.get("type", "GRAPHQL_ERROR")) for item in errors
                        if isinstance(item, dict)})
        raise ProviderError("GitHub commit refused or unconfirmed: "
                            + ", ".join(types or ["MISSING_COMMIT"]))


def create_private_file(path: Path, raw: bytes) -> None:
    """Publish a complete local request exactly once, without overwriting one."""
    if len(raw) > MAX_BYTES:
        raise InboxGitHubError("request file exceeds the byte limit")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic create-only publication, including two processes sharing a name.
        os.link(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        os.unlink(temporary)


def load_private_file(path: Path) -> dict[str, Any]:
    """Decode only one observed, unchanged generation of the retained request."""
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
              "st_mtime_ns", "st_ctime_ns")

    def generation(info: os.stat_result) -> tuple:
        if (not stat.S_ISREG(info.st_mode) or not info.st_ino
                or info.st_size > MAX_BYTES):
            raise InboxGitHubError("request must be a bounded regular file")
        return tuple(getattr(info, field) for field in fields)

    try:
        expected = generation(path.lstat())
        flags = os.O_RDONLY
        for flag in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
            flags |= getattr(os, flag, 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if generation(opened) != expected:
                raise InboxGitHubError("request file changed while opening")
            chunks = []
            remaining = MAX_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_BYTES or len(raw) != opened.st_size:
                raise InboxGitHubError("request byte count changed while reading")
            # Compare the descriptor on both sides of the final pathname read.
            # This catches same-inode edits during that pathname observation too.
            if (generation(os.fstat(fd)) != expected
                    or generation(path.lstat()) != expected
                    or generation(os.fstat(fd)) != expected):
                raise InboxGitHubError("request file changed while reading")
        finally:
            os.close(fd)
    except OSError as exc:
        raise InboxGitHubError("unable to read retained request file") from exc
    return decode_json(raw)


def task(document: dict[str, Any], task_id: str) -> dict[str, Any]:
    matches = [row for row in document["tasks"] if row["id"] == task_id]
    if len(matches) != 1:
        raise InboxGitHubError("requested task is absent or ambiguous")
    return matches[0]


class Coordinator:
    """Compose the unchanged protocol with durable intent and one CAS write."""

    def __init__(self, transport: GitHubInbox, protocol: Any,
                 clock: Callable[[], datetime] = utc_now):
        self.transport = transport
        self.protocol = protocol
        self.clock = clock

    def prepare(self, snapshot: Snapshot, action: str,
                arguments: dict[str, Any]) -> dict[str, Any]:
        if action not in ACTIONS:
            raise InboxGitHubError("unsupported transition")
        document = self.protocol.parse_protocol_text(snapshot.raw.decode("utf-8"))
        args = dict(arguments)
        args["expected_document_sha256"] = self.protocol.document_sha256(document)
        proposed, receipt = getattr(self.protocol, action + "_task")(document, **args)
        return {
            "v": 1, "request_id": uuid.uuid4().hex,
            "target": self.transport.target.as_dict(), "action": action,
            "arguments": args, "head": snapshot.head, "blob": snapshot.blob,
            "original_base64": base64.b64encode(snapshot.raw).decode("ascii"),
            "proposed": self.protocol.canonical_document_text(proposed),
            "transition": receipt,
        }

    def validate_request(self, request: dict[str, Any]) -> tuple[dict, dict]:
        expected = {"v", "request_id", "target", "action", "arguments", "head", "blob",
                    "original_base64", "proposed", "transition"}
        if (set(request) != expected or type(request["v"]) is not int or request["v"] != 1
                or request["target"] != self.transport.target.as_dict()
                or not isinstance(request["request_id"], str)
                or re.fullmatch(r"[0-9a-f]{32}", request["request_id"]) is None):
            raise InboxGitHubError("request identity/target/schema mismatch")
        try:
            original = base64.b64decode(request["original_base64"], validate=True)
            snapshot = Snapshot(request["head"], request["blob"], original)
            if not isinstance(request["arguments"], dict):
                raise InboxGitHubError("request arguments must be an object")
            recreated = self.prepare(snapshot, request["action"], request["arguments"])
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise InboxGitHubError("invalid retained request") from exc
        for field in ("arguments", "proposed", "transition"):
            if recreated[field] != request[field]:
                raise InboxGitHubError("retained request does not match its protocol transition")
        return (self.protocol.parse_protocol_text(original.decode("utf-8")),
                self.protocol.parse_protocol_text(request["proposed"]))

    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate a retained request and return a redacted offline summary."""
        _, proposed = self.validate_request(request)
        selected = request["arguments"]["task_id"]
        execution = task(proposed, selected).get("execution") or {}
        if not isinstance(execution, dict):
            raise InboxGitHubError("proposed execution must be an object")
        public_execution = {
            key: execution[key]
            for key in ("state", "attempt", "lease_expires_at")
            if key in execution
        }
        return {
            "v": 1,
            "status": "valid",
            "request_id": request["request_id"],
            "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
            "target": request["target"],
            "action": request["action"],
            "task_id": selected,
            "head": request["head"],
            "blob": request["blob"],
            "expected_document_sha256": request["arguments"]["expected_document_sha256"],
            "proposed_document_sha256": self.protocol.document_sha256(proposed),
            "transition_sha256": hashlib.sha256(
                json_bytes(request["transition"])).hexdigest(),
            "requested_at": request["arguments"]["now"],
            "proposed_execution": public_execution,
            "provider_contacted": False,
            "publication_attempted": False,
            "executes_task": False,
        }

    def reconcile_local(self, request: dict[str, Any],
                        current: dict[str, Any]) -> dict[str, Any]:
        """Classify one stable local inbox against a retained request.

        This deliberately does not infer a provider head, publication readiness,
        or live lease authority from local bytes.
        """
        before, proposed = self.validate_request(request)
        self.protocol.validate_protocol_document(current)
        selected = request["arguments"]["task_id"]
        wanted = task(proposed, selected)
        current_row = next(
            (row for row in current["tasks"] if row["id"] == selected), None)
        current_digest = self.protocol.document_sha256(current)
        expected_digest = self.protocol.document_sha256(before)
        if current_row == wanted:
            status = "already_applied"
        elif current_digest == expected_digest:
            status = "base_equivalent"
        else:
            status = "diverged"
        return {
            "v": 1,
            "status": status,
            "request_id": request["request_id"],
            "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
            "target": request["target"],
            "action": request["action"],
            "task_id": selected,
            "expected_document_sha256": expected_digest,
            "proposed_document_sha256": self.protocol.document_sha256(proposed),
            "current_document_sha256": current_digest,
            "task_present": current_row is not None,
            "matches_expected_base": current_digest == expected_digest,
            "matches_proposed_task": current_row == wanted,
            "source": "stable_local_file",
            "provider_contacted": False,
            "publication_attempted": False,
            "publication_ready": False,
            "authority_active": False,
            "executes_task": False,
        }

    def _live_before_write(self, request: dict, before: dict, after: dict) -> None:
        now = self.clock()
        if now.tzinfo is None:
            raise InboxGitHubError("coordinator clock needs a UTC offset")
        if instant(request["arguments"]["now"]) > now:
            raise InboxGitHubError("request time is in the future")
        selected = request["arguments"]["task_id"]
        candidates = []
        if request["action"] != "claim":
            candidates.append(task(before, selected).get("execution"))
        if request["action"] in ("claim", "renew"):
            candidates.append(task(after, selected).get("execution"))
        for execution in candidates:
            if (not isinstance(execution, dict)
                    or instant(execution["lease_expires_at"]) <= now):
                raise InboxGitHubError("request lease expired before publication; retain this request")

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        before, proposed = self.validate_request(request)
        selected = request["arguments"]["task_id"]
        wanted = task(proposed, selected)
        current = self.transport.read()
        document = self.protocol.parse_protocol_text(current.raw.decode("utf-8"))
        if next((row for row in document["tasks"] if row["id"] == selected), None) == wanted:
            return self._receipt(request, current, document, "observed", None, None)
        if (current.head != request["head"] or current.blob != request["blob"]
                or self.protocol.document_sha256(document) != request["arguments"]["expected_document_sha256"]):
            return self._receipt(request, current, document, "conflict", None, None)
        self._live_before_write(request, before, proposed)
        committed = None
        error = None
        try:
            committed = self.transport.commit(current.head, request["proposed"].encode("utf-8"),
                                              request["request_id"])
        except ProviderError as exc:
            error = exc.public()
            if exc.status in (401, 403, 429):
                # Respect auth and rate-limit refusals without further requests.
                return self._receipt(request, current, document, "provider_refused", None, error)
        # Always observe the current branch, not only the commit returned by GraphQL.
        try:
            current = self.transport.read()
            document = self.protocol.parse_protocol_text(current.raw.decode("utf-8"))
        except (ProviderError, InboxGitHubError, ValueError, UnicodeError) as exc:
            return {"v": 1, "status": "outcome_unknown", "request_id": request["request_id"],
                    "mutation_commit": committed, "authority_active": False,
                    "provider_error": error, "readback_error": type(exc).__name__}
        matched = next((row for row in document["tasks"] if row["id"] == selected), None) == wanted
        if matched:
            status = "published" if committed else "observed"
        else:
            status = "state_changed" if committed else "outcome_unknown"
        return self._receipt(request, current, document, status, committed, error)

    def _receipt(self, request: dict, snapshot: Snapshot, document: dict,
                 status: str, committed: str | None, error: dict | None) -> dict:
        selected = request["arguments"]["task_id"]
        row = next((item for item in document["tasks"] if item["id"] == selected), None)
        expected_row = task(self.protocol.parse_protocol_text(request["proposed"]), selected)
        execution = (row or {}).get("execution") or {}
        observed_at = self.clock()
        active = (row == expected_row and execution.get("state") == "leased"
                  and instant(execution["claimed_at"]) <= observed_at
                  < instant(execution["lease_expires_at"]))
        return {"v": 1, "status": status, "request_id": request["request_id"],
                "target": request["target"], "head": snapshot.head, "blob": snapshot.blob,
                "document_sha256": self.protocol.document_sha256(document),
                "task_id": selected, "task_present": row is not None,
                "execution": execution, "authority_active": active,
                "observed_at": observed_at.isoformat(), "mutation_commit": committed,
                "transition": request["transition"], "provider_error": error}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--repo", required=True, help="explicit owner/repository")
    root.add_argument("--branch", default="main")
    root.add_argument("--path", default="inbox.json")
    root.add_argument("--token-env", default="GH_TOKEN", help="token environment variable name")
    sub = root.add_subparsers(dest="action", required=True)
    sub.add_parser("read", help="read the current pinned document and lease summary")
    inspect = sub.add_parser(
        "inspect", help="validate and summarize a retained request without network access")
    inspect.add_argument("--request", type=Path, required=True)
    reconcile = sub.add_parser(
        "reconcile-local",
        help="compare a retained request with one stable local inbox without network access")
    reconcile.add_argument("--request", type=Path, required=True)
    reconcile.add_argument("--inbox", type=Path, required=True)
    resume = sub.add_parser("resume", help="reconcile/retry the exact retained request")
    resume.add_argument("--request", type=Path, required=True)
    for action in ACTIONS:
        command = sub.add_parser(action)
        command.add_argument("--request", type=Path, required=True,
                             help="new private request file; use resume if it already exists")
        command.add_argument("--task-id", required=True)
        command.add_argument("--worker-id", required=True)
        command.add_argument("--lease-id", required=True)
        command.add_argument("--now", help="trusted ISO-8601 time; default current UTC")
        if action != "claim":
            command.add_argument("--expected-attempt", type=int, required=True)
        if action in ("claim", "renew"):
            command.add_argument("--lease-seconds", type=int, required=True)
        if action == "release":
            command.add_argument("--reason")
        if action == "complete":
            command.add_argument("--completion-id", required=True)
            command.add_argument("--result", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        import task_protocol as protocol
        target = Target(args.repo, args.branch, args.path)
        if args.action in ("inspect", "reconcile-local"):
            # Offline commands bind to the exact CLI target but deliberately do
            # not load a token or construct a provider transport.
            target_only = type("TargetOnly", (), {"target": target})()
            coordinator = Coordinator(target_only, protocol)
            request = load_private_file(args.request)
            if args.action == "inspect":
                result = coordinator.inspect(request)
            else:
                result = coordinator.reconcile_local(
                    request, protocol.parse_protocol_path(args.inbox))
            sys.stdout.write(json_bytes(result).decode("utf-8"))
            return 0
        token = os.environ.get(args.token_env, "")
        if not token and args.token_env == "GH_TOKEN":
            token = os.environ.get("GITHUB_TOKEN", "")
        transport = GitHubInbox(target, token)
        coordinator = Coordinator(transport, protocol)
        if args.action == "read":
            snapshot = transport.read()
            document = protocol.parse_protocol_text(snapshot.raw.decode("utf-8"))
            result = {"v": 1, "target": transport.target.as_dict(),
                      "head": snapshot.head, "blob": snapshot.blob,
                      "document_sha256": protocol.document_sha256(document),
                      "tasks": [{"id": row["id"], "kind": row["kind"], "done": row["done"],
                                 "execution": row.get("execution")}
                                for row in document["tasks"]]}
        else:
            if args.action == "resume":
                request = load_private_file(args.request)
            else:
                arguments = {"task_id": args.task_id, "worker_id": args.worker_id,
                             "lease_id": args.lease_id,
                             "now": args.now or utc_now().isoformat()}
                for field in ("expected_attempt", "lease_seconds", "reason", "completion_id", "result"):
                    if hasattr(args, field):
                        arguments[field] = getattr(args, field)
                request = coordinator.prepare(transport.read(), args.action, arguments)
                create_private_file(args.request, json_bytes(request))
            result = coordinator.apply(request)
        sys.stdout.write(json_bytes(result).decode("utf-8"))
        if args.action == "read":
            return 0
        if result["status"] not in ("published", "observed"):
            return 3
        # An observed old claim is not permission to execute after its expiry.
        return 0 if request["action"] not in ("claim", "renew") or result["authority_active"] else 4
    except (InboxGitHubError, ValueError, OSError, ImportError) as exc:
        details = exc.public() if isinstance(exc, ProviderError) else {"message": str(exc)}
        sys.stderr.write(json.dumps({"status": "error", **details}, ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
