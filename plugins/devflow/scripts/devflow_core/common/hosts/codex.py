"""Read native conversation records through a short-lived public stdio API."""
from dataclasses import dataclass
from contextlib import contextmanager
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from ... import __version__
from ..errors import CaptureError, DevFlowError
from ..counts import tally
from ..process import stop_tree
from ..storage import parse_json

API_TIMEOUT_SECONDS = 30
MAX_PAGES = 10000
PAGE_SIZE = 100


@dataclass(frozen=True)
class ConversationRecord:
    session_id: str
    turn_id: str
    item_id: str
    role: str
    text: str
    started_at_ms: int


@dataclass(frozen=True)
class Conversation:
    records: list[ConversationRecord]
    unknown_types: dict[str, int]
    ignored_types: dict[str, int]


@contextmanager
def _api(project):
    executable = shutil.which("codex")
    if not executable:
        raise CaptureError("Codex CLI is unavailable for the native conversation API")
    try:
        process = subprocess.Popen([executable, "--no-daemon", "app-server", "--stdio"], cwd=project,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   shell=False, **({"start_new_session": True} if os.name != "nt" else
                                                   {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}))
    except OSError as exc:
        raise CaptureError("Cannot start Codex conversation API: " + str(exc)) from exc
    responses = queue.Queue()
    def read():
        try:
            for raw in process.stdout:
                responses.put(raw)
        finally:
            responses.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def call(method, params, number):
        deadline = time.monotonic() + API_TIMEOUT_SECONDS
        process.stdin.write((json.dumps({"method": method, "params": params, "id": number}) + "\n").encode())
        process.stdin.flush()
        while True:
            if time.monotonic() >= deadline:
                raise CaptureError(f"Codex conversation API {method} timed out after {API_TIMEOUT_SECONDS}s")
            try:
                raw = responses.get(timeout=max(.001, deadline-time.monotonic()))
            except queue.Empty as exc:
                raise CaptureError(f"Codex conversation API {method} timed out after {API_TIMEOUT_SECONDS}s") from exc
            if raw is None:
                raise CaptureError("Codex conversation API closed before replying")
            try:
                value = parse_json(raw, "Codex conversation API")
            except DevFlowError as exc:
                raise CaptureError("Codex conversation API returned invalid JSON") from exc
            if not isinstance(value, dict) or value.get("id") != number:
                continue
            if "error" in value or not isinstance(value.get("result"), dict):
                raise CaptureError("Codex conversation API could not read this session")
            return value["result"]
    try:
        call("initialize", {"clientInfo": {"name": "devflow", "version": __version__},
                            "capabilities": {"experimentalApi": True}}, 1)
        process.stdin.write(b'{"method":"initialized"}\n'); process.stdin.flush()
        yield call
    except (OSError, ValueError) as exc:
        raise CaptureError("Cannot read Codex conversation API: " + str(exc)) from exc
    finally:
        # Cleanup is best effort. A dead pipe or a delayed reap must not hide
        # the protocol/identity/timeout error that made capture fail.
        for cleanup in (lambda: stop_tree(process), lambda: process.wait(timeout=5), process.stdin.close,
                        lambda: reader.join(timeout=2), process.stdout.close):
            try:
                cleanup()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass


def _thread(call, session_id):
    thread = call("thread/read", {"threadId": session_id, "includeTurns": False}, 2).get("thread")
    if (not isinstance(thread, dict) or thread.get("id") != session_id or thread.get("parentThreadId")
            or thread.get("sessionId", session_id) != session_id):
        raise CaptureError("Codex API identity does not match the actual root session; do not guess")


def _page(call, session_id, number, cursor=None, limit=PAGE_SIZE):
    params = {"threadId": session_id, "limit": limit, "sortDirection": "asc"}
    if cursor is not None:
        params["cursor"] = cursor
    page = call("thread/items/list", params, number)
    if not isinstance(page.get("data"), list):
        raise CaptureError("Codex conversation API items have an unsupported shape")
    return page


def diagnose_conversation(project, session_id=None) -> dict:
    """Static limits, plus an explicit-session probe; no model or project writes."""
    report = {"timeout_seconds_per_call": API_TIMEOUT_SECONDS, "max_pages": MAX_PAGES,
              "page_size": PAGE_SIZE, "checked": False, "ready": None, "all_pages_verified": False,
              "scope": "Pass the actual --session to probe initialization, root identity and the first items page"}
    if session_id is not None:
        try:
            with _api(project) as call:
                _thread(call, session_id)
                _page(call, session_id, 3, limit=1)
            report.update(checked=True, ready=True, scope="Initialization, root identity and first items page")
        except CaptureError as exc:
            report.update(checked=True, ready=False, error=str(exc))
    return report


def read_conversation(session_id: str, project) -> Conversation:
    with _api(project) as call:
        _thread(call, session_id)
        records, cursor, seen = [], None, set()
        unknown, ignored = {}, {}
        for number in range(3, 3 + MAX_PAGES):
            page = _page(call, session_id, number, cursor)
            for row in page["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("item"), dict):
                    raise CaptureError("Codex conversation API record has an unsupported shape")
                item, turn = row["item"], row.get("turnId")
                kind = item.get("type")
                if kind not in {"userMessage", "agentMessage"}:
                    counts = ignored if kind in {"reasoning", "commandExecution"} else unknown
                    tally(counts, "item:" + kind if isinstance(kind, str) else None, "item:(unnamed)")
                    continue  # No reasoning, command output or child reports in an interview.
                if kind == "agentMessage" and item.get("delivery") == "async":
                    tally(ignored, "item:asyncAgentMessage", "item:(unnamed)")
                    continue
                if kind == "agentMessage":
                    text = item.get("text")
                else:
                    content = item.get("content")
                    blocks = []
                    if isinstance(content, list):
                        for block in content:
                            block_type = block.get("type") if isinstance(block, dict) else None
                            if block_type == "text" and isinstance(block.get("text"), str):
                                blocks.append(block["text"])
                            else:
                                tally(unknown, "content:" + block_type if isinstance(block_type, str) else None,
                                      "content:(unnamed)")
                    text = "\n".join(blocks) if isinstance(content, list) else None
                if not isinstance(text, str) or not isinstance(turn, str) or not isinstance(item.get("id"), str):
                    raise CaptureError("Codex visible message lacks exact text or provenance")
                timestamp = row.get("startedAtMs")
                if type(timestamp) is not int:
                    raise CaptureError("Codex visible message lacks its native timestamp")
                records.append(ConversationRecord(session_id, turn, item["id"],
                               "assistant" if kind == "agentMessage" else "user", text, timestamp))
            cursor = page.get("nextCursor")
            if cursor is None:
                return Conversation(records, unknown, ignored)
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CaptureError("Codex conversation API pagination is inconsistent")
            seen.add(cursor)
        raise CaptureError("Codex conversation API exceeds the bounded page scan")
