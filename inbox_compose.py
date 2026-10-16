#!/usr/bin/env python3
"""Author a new pending-task inbox and open it in the retained operator desk."""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
import sys
from urllib.parse import urlsplit

from inbox_operator import (
    Desk,
    Handler,
    MAX_REQUEST_BYTES,
    decode,
    demo_document,
    encode,
)
from task_protocol import canonical_document_text, document_sha256, parse_protocol_text
from validate_inbox import MAX_TASKS

COMPOSE_STATIC = {
    "/compose": ("compose.html", "text/html; charset=utf-8"),
    "/compose.html": ("compose.html", "text/html; charset=utf-8"),
    "/compose.js": ("compose.js", "text/javascript; charset=utf-8"),
    "/compose.css": ("compose.css", "text/css; charset=utf-8"),
}
ROOT_FIELDS = frozenset({"v", "label", "tasks"})
TASK_FIELDS = frozenset({"id", "created", "kind", "command", "timeout_s", "done"})
MAX_LABEL_CHARS = 160


def validate_composition(value: object) -> dict[str, object]:
    """Validate one authoring draft and return its canonical pending inbox."""
    if not isinstance(value, dict):
        raise ValueError("Draft must be a JSON object")
    unknown_root = sorted(set(value) - ROOT_FIELDS)
    missing_root = sorted(ROOT_FIELDS - set(value))
    if missing_root or unknown_root:
        raise ValueError(
            f"Draft fields must be exactly v, label and tasks; missing={missing_root}, unknown={unknown_root}"
        )
    if value.get("v") != 1 or type(value.get("v")) is not int:
        raise ValueError("Draft version must be integer 1")
    label = value.get("label")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL_CHARS:
        raise ValueError(f"Inbox label must contain 1 to {MAX_LABEL_CHARS} characters")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Add at least one task before validation")
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"An inbox may contain at most {MAX_TASKS} tasks")

    normalized_tasks: list[dict[str, object]] = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Task {index + 1} must be a JSON object")
        unknown = sorted(set(task) - TASK_FIELDS)
        missing = sorted(TASK_FIELDS - set(task))
        if missing or unknown:
            raise ValueError(
                f"Task {index + 1} fields must be exactly id, created, kind, command, timeout_s and done; "
                f"missing={missing}, unknown={unknown}"
            )
        if task.get("kind") != "run_task":
            raise ValueError(f"Task {index + 1} kind must be run_task")
        if task.get("done") is not False:
            raise ValueError(f"Task {index + 1} must be pending with done=false")
        normalized_tasks.append(dict(task))

    document: dict[str, object] = {
        "v": 1,
        "label": label.strip(),
        "tasks": normalized_tasks,
    }
    text = canonical_document_text(document)
    parsed = parse_protocol_text(text)
    return {
        "document": parsed,
        "canonical_text": text,
        "digest": document_sha256(parsed),
        "task_count": len(normalized_tasks),
        "bytes": len(text.encode("utf-8")),
    }


class ComposeHandler(Handler):
    """Existing operator API plus a narrow native authoring-validation route."""

    def do_GET(self):
        target = urlsplit(self.path)
        if target.path in COMPOSE_STATIC:
            if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
                return self.respond(403, {"error": "Open the local address printed by the launcher"})
            filename, mime = COMPOSE_STATIC[target.path]
            return self.respond(200, (self.server.assets / filename).read_bytes(), mime)
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/api/compose/validate":
            return super().do_POST()
        if not self.authorized():
            return self.respond(403, {"error": "Open the current launcher URL to reconnect"})
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Expected application/json")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("A fixed request length is required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError(Request is empty or exceeds the authoring limit")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Request ended before all bytes arrived")
            request = decode(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"document"}:
                raise ValueError("Validation requires exactly one document field")
            return self.respond(200, validate_composition(request["document"]))
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            return self.respond(400, {"error": str(exc)})


class ComposeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, desk: Desk):
        super().__init__(("127.0.0.1", port), ComposeHandler)
        self.desk = desk
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.assets = Path(__file__).with_name("operator_ui")


def _ensure_demo(desk: Desk) -> None:
    with desk.connection() as db:
        exists = db.execute("SELECT 1 FROM requests WHERE id='demo-v1'").fetchone()
    if not exists:
        desk.import_document({
            "request_id": "demo-v1",
            "label": "Local demonstration",
            "source_text": encode(demo_document()),
        })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="private local operator database")
    parser.add_argument("--port", type=int, default=8765, help="loopback port retained across restarts")
    parser.add_argument("--demo", action="store_true", help="add the maintained operator demonstration once")
    args = parser.parse_args(argv)
    try:
        desk = Desk(args.db)
        if args.demo:
            _ensure_demo(desk)
        server = ComposeServer(args.port, desk)
        print(f"{server.origin}/compose#token={server.token}", flush=True)
        print("Authoring and operator desk share the selected retained database.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Unable to open inbox authoring desk: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
