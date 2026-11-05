"""Extraction of the marked part of a Claude Code interview (from its start marker to the seal).

Only user-visible text and AskUserQuestion inputs/results leave this adapter.
Thinking blocks, unrelated tool results, and earlier session history never do.
Claude's JSONL format is not a public stable schema and changes between
versions: what this adapter does not know is skipped and counted, never a
reason to stop. The human's words are cross-checked with the hook records
(without a transcript, they come from the hook records alone); it stops only
when the record is provably broken (corrupt or truncated JSONL, conflicting
records, a question hook that contradicts the transcript).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from ..common.counts import tally
from ..common.errors import CaptureError
from ..common.sessions import SCREEN_FILLED_QUESTION_FIELDS, answer_content as _answer_content, prefilled_question

_TAIL_BYTES = 4096
_IGNORED_RECORDS = {
    "progress", "file-history-snapshot", "queue-operation", "pr-link",
    "last-prompt", "custom-title", "agent-name", "agent-color",
    "agent-setting", "session-name", "session-end", "notification",
    "hook_progress",
    # Session settings, titles, bookkeeping and cached API request copies.  The
    # visible conversation itself is only in user/assistant records.
    "mode", "permission-mode", "atis-latch", "bridge-session", "ai-title",
    "tools-used", "file-history-delta", "cost-state", "relocated",
    "worktree-state", "api-request", "api-request-shape", "api-request-blob",
    "session-owner",
}
# Claude Code adds record, attachment and content-block types between versions.
# An unknown one is skipped and counted by type rather than stopping the
# capture. Human words cannot vanish this way: every raw UserPromptSubmit is
# matched with the transcript, and the hook records alone are used if the
# prompts cannot be placed.
# Every transcript item falls into one class:
# 1. the human's words: user records and queued_command from a human (kept);
# 2. Claude Code-generated: the record/attachment types and user-role texts
#    below, other queued messages (subagent reports, task notifications) and
#    compaction boundaries and summaries (skipped and counted);
# 3. content the human attached: kept as the reference in their text
#    ("[Image #1]", "@path"), the content itself only counted by type;
# 4. anything unknown: skipped and counted by type.
_IGNORED_ATTACHMENTS = {
    "hook_success", "hook_additional_context", "hook_blocking_error",
    "total_tokens_reminder", "silent_turn_reminder", "task_reminder", "task_status",
    "deferred_tools_record", "deferred_tools_delta", "agent_listing_delta",
    "mcp_instructions_delta", "skill_listing", "invoked_skills", "command_permissions",
    "environment", "prompt_snapshot", "date", "model", "credential_org",
    "session_context", "remote_session_change", "instructions", "nested_memory",
    "sandbox_instructions", "auto_mode", "auto_mode_exit", "ultrathink_effort",
    "plan_mode", "plan_mode_exit", "plan_mode_reentry", "thinking_drop",
    "read_truncation_notice", "edited_text_file",
}
# User-role text written by Claude Code, not the human: interrupt markers and
# the input/output of `!` shell commands (which may hold secrets).
_CLAUDE_CODE_TEXTS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})
_CLAUDE_CODE_TEXT_PREFIXES = ("<bash-input>", "<bash-stdout>", "<bash-stderr>")
_HUMAN_ATTACHMENTS = frozenset({"file", "directory", "pdf_reference", "at_mention_reference"})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _name(value: Any) -> str | None:
    """A type name or ID as this version reads it: a string, or None for any other form."""
    return value if isinstance(value, str) else None


def _read_answer(value: Any) -> str | None:
    """A question's answer, or None when there is none this version reads."""
    try:
        return _answer_content(value)
    except CaptureError:
        return None


def _invalid_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    """A JSON number that stays a JSON number (1e999 would become infinity)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def transcript_marker(path: str | None) -> dict:
    """Mark an append boundary without copying earlier conversation content.

    Claude may not have created its transcript when UserPromptSubmit fires.
    The tail fingerprint detects truncation/rewrite/rotation before sealing.
    Without a transcript path (or if the file is never written) the interview
    is built from the hook records alone.
    """
    if not isinstance(path, str) or not path.strip():
        return {"path": None, "offset": 0}
    p = Path(path).expanduser().resolve(strict=False)
    try:
        stat = p.stat()
    except FileNotFoundError:
        return {"path": str(p), "offset": 0, "existed": False,
                "tail_size": 0, "tail_sha256": _digest(b"")}
    except OSError as exc:
        raise CaptureError(f"Cannot inspect Claude transcript: {exc}") from exc
    if not p.is_file():
        raise CaptureError("transcript_path is not a regular file")
    tail_size = min(stat.st_size, _TAIL_BYTES)
    try:
        with p.open("rb") as stream:
            stream.seek(stat.st_size - tail_size)
            tail = stream.read(tail_size)
    except OSError as exc:
        raise CaptureError(f"Cannot mark Claude transcript: {exc}") from exc
    if tail and not tail.endswith(b"\n"):
        raise CaptureError("Transcript has an incomplete line; retry before starting the interview")
    return {"path": str(p), "offset": stat.st_size, "existed": True,
            "device": stat.st_dev, "inode": stat.st_ino,
            "tail_size": tail_size, "tail_sha256": _digest(tail)}


class _Segment:
    """The marked part of a transcript, read lazily one line at a time.

    Lines are split only at newline (``\\n``): JSON strings may hold U+2028,
    U+2029 or U+0085 unescaped, which ``str.splitlines`` would split. Each line
    is strict UTF-8. The size is never limited; ``bytes_read`` is known once the
    lines have been read.
    """

    def __init__(self, marker: dict):
        self.marker = marker
        self.bytes_read = 0

    def __iter__(self):
        marker = self.marker
        try:
            path = Path(marker["path"])
            offset = marker["offset"]
            tail_size = marker["tail_size"]
            if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
                    or not isinstance(tail_size, int) or not 0 <= tail_size <= min(offset, _TAIL_BYTES)):
                raise ValueError("invalid offsets")
            stat = path.stat()
            if not path.is_file():
                raise CaptureError("Transcript is no longer a regular file")
            if stat.st_size < offset:
                raise CaptureError("Transcript was truncated; restart the interview rather than guess its history")
            if marker.get("existed") and (stat.st_dev, stat.st_ino) != (
                    marker.get("device"), marker.get("inode")):
                raise CaptureError("Transcript was replaced or rotated during the interview")
            with path.open("rb") as stream:
                stream.seek(offset - tail_size)
                if _digest(stream.read(tail_size)) != marker["tail_sha256"]:
                    raise CaptureError("Transcript prefix changed during the interview")
                for raw in stream:
                    self.bytes_read += len(raw)
                    yield raw.decode("utf-8")
        except CaptureError:
            raise
        except (KeyError, ValueError, TypeError, OSError, UnicodeError) as exc:
            raise CaptureError(f"Cannot read the marked interview transcript: {exc}") from exc


def _scaffolding(text: str, record: dict) -> bool:
    """Known Claude-generated user-role content, never assumed human input."""
    return bool(record.get("isMeta")) or text.lstrip().startswith((
        "<command-message>", "<command-name>", "<local-command-caveat>",
        "<local-command-stdout>", "Base directory for this skill:",
    ))


def _claude_code_text(content: Any) -> bool:
    """User-role content consisting only of Claude Code-generated text."""
    if isinstance(content, list):
        if not content or any(
            not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str)
            for block in content
        ):
            return False
        content = "\n".join(block["text"] for block in content)
    if not isinstance(content, str):
        return False
    return content.strip() in _CLAUDE_CODE_TEXTS or content.lstrip().startswith(_CLAUDE_CODE_TEXT_PREFIXES)


def _queued_by_human(attachment: dict) -> bool:
    """A message the human typed while Claude worked: Claude Code marks its origin human."""
    origin = attachment.get("origin")
    return (attachment.get("commandMode") == "prompt" and not attachment.get("isMeta")
            and isinstance(origin, dict) and origin.get("kind") == "human")


_QUEUED_IN_ANOTHER_FORM = ("attachments", "queued_command (prompt in another form)")


def _prompt_matches(prompt: str, parts: list[str], record: dict) -> bool:
    text = "\n".join(parts)
    if prompt == text or (len(parts) == 1 and prompt == parts[0]):
        return True
    if not _scaffolding(text, record):
        return False
    if prompt in text:
        return True
    command = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    arguments = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    if command:
        reconstructed = command.group(1) + (" " + arguments.group(1) if arguments else "")
        return reconstructed == prompt
    return False


def _asked_input(question_input: dict) -> dict:
    """The part of an AskUserQuestion input written by the model, not the screen."""
    return {key: value for key, value in question_input.items() if key not in SCREEN_FILLED_QUESTION_FIELDS}


def _unanswered_content(value: Any) -> str:
    """Exact text of a cancelled or failed question result; never a human answer."""
    if isinstance(value, str):
        return value
    # Any other form is kept as JSON: it is never a human answer either way.
    return _json(value)


def capture_interview(marker: dict, events: list[dict]) -> dict:
    """Return visible messages with explicit provenance.

    ``events`` are the original hook input objects recorded since activation.
    UserPromptSubmit is authoritative for raw prompts (including the original
    slash command). PostToolUse can preserve structured AskUserQuestion answers
    more faithfully than Claude's human-readable tool-result rendering.
    """
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise CaptureError("Hook events must be a list of objects")
    # Each record is checked on its own; the records are never joined into one string.
    for index, event in enumerate(events):
        try:
            _json(event)
        except (TypeError, ValueError) as exc:
            raise CaptureError(f"Invalid hook event data at index {index}: {exc}") from exc
    prompts: list[dict] = []
    post_questions: list[tuple[int, dict]] = []
    unknown: dict[str, dict[str, int]] = {"records": {}, "attachments": {}, "blocks": {}, "hooks": {}}
    for index, event in enumerate(events):
        event_name = event.get("hook_event_name")
        if event_name == "UserPromptSubmit":
            if not isinstance(event.get("prompt"), str):
                raise CaptureError("UserPromptSubmit is missing its exact prompt")
            prompts.append({"event_index": index, "text": event["prompt"], "used": False,
                            "prompt_id": event.get("prompt_id")})
        elif event_name == "PostToolUse" and event.get("tool_name") == "AskUserQuestion" and not event.get("agent_id"):
            if not isinstance(event.get("tool_input"), dict) or "tool_response" not in event:
                # A hook input in a form this version does not read: counted, never a stop.
                tally(unknown["hooks"], "PostToolUse AskUserQuestion (in another form)", "(untyped)")
                continue
            if event.get("is_error"):
                continue  # An unsuccessful AskUserQuestion is not human evidence.
            post_questions.append((index, event))
    # No transcript (no path, or a file Claude Code never wrote): the hook records alone.
    missing = marker["path"] is None or not (marker.get("existed") or Path(marker["path"]).exists())
    segment = _Segment(marker)

    messages: list[dict] = []
    questions: dict[str, dict] = {}
    seen_uuids: dict[str, str] = {}
    visible_records = 0
    ignored_records = 0
    transcript_human_prompts = 0
    omitted_attachments: dict[str, int] = {}
    unhooked: list[dict] = []
    # Claude Code also sends its own messages (task notifications, subagent
    # reports) through UserPromptSubmit. The transcript marks their origin. They
    # are matched with hook prompts after the scan, once the human's words have
    # taken theirs, so a human prompt with the same text is never taken for one.
    notices: list[tuple[str, object]] = []

    def automated(text: str, prompt_id: object = None) -> None:
        if text:
            notices.append((text, prompt_id))

    def read_text(content: Any, other_form: tuple[str, str]) -> tuple[str, int]:
        """The text and image count of a message's content (a string or blocks); other forms counted."""
        if isinstance(content, str):
            return content, 0
        if not isinstance(content, list):
            skip_unknown(*other_form)
            return "", 0
        texts: list[str] = []
        images = 0
        for block in content:
            if not isinstance(block, dict):
                skip_unknown("blocks", "(block in another form)")
            elif block.get("type") == "image":
                images += 1
            elif block.get("type") != "text":
                skip_unknown("blocks", block.get("type"))
            elif isinstance(block.get("text"), str):
                texts.append(block["text"])
            else:
                skip_unknown("blocks", "text (in another form)")
        return "\n".join(texts), images

    def take(candidates: list[dict], prompt_id: object) -> dict | None:
        """The hook prompt with this prompt ID among the candidates, else the first."""
        return next((p for p in candidates if prompt_id and p["prompt_id"] == prompt_id), None) or next(iter(candidates), None)

    def add(role: str, content: str, source: dict) -> dict:
        result = {"role": role, "content": content, "source": source}
        messages.append(result)
        return result

    def omit(kind: str, count: int = 1) -> None:
        if count:
            omitted_attachments[kind] = omitted_attachments.get(kind, 0) + count

    def skip_unknown(group: str, name: object) -> None:
        tally(unknown[group], name, "(untyped)")

    def duplicate(record: dict, value: Any) -> bool:
        uuid = record.get("uuid")
        if not isinstance(uuid, str):
            return False  # No UUID, or one in a form this version does not read: nothing to compare.
        fingerprint = _digest(_json(value).encode("utf-8"))
        if uuid in seen_uuids:
            if seen_uuids[uuid] != fingerprint:
                raise CaptureError("Conflicting transcript records share one UUID")
            return True
        seen_uuids[uuid] = fingerprint
        return False

    for number, line in enumerate(() if missing else segment, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite)
        except ValueError as exc:
            raise CaptureError(f"Malformed or incomplete transcript JSONL at segment line {number}") from exc
        if not isinstance(record, dict):
            skip_unknown("records", "(line in another form)")
            ignored_records += 1
            continue
        kind = _name(record.get("type"))
        if (record.get("isSidechain") or kind == "summary" or record.get("isCompactSummary")
                or _name(record.get("subtype")) in {"compact_boundary", "compaction"}):
            # A subagent's own records, and compaction: the boundary and its
            # summary are Claude Code's; the original exchange stays earlier in the file.
            ignored_records += 1
            continue
        if kind == "attachment":
            attachment = record.get("attachment")
            attachment_type = _name(attachment.get("type")) if isinstance(attachment, dict) else None
            if attachment_type == "queued_command" and _queued_by_human(attachment):
                if duplicate(record, attachment):
                    continue
                text, images = read_text(attachment.get("prompt"), _QUEUED_IN_ANOTHER_FORM)
                omit("image", images)
                if text.strip():
                    # Claude Code marks the origin human; link a matching hook event if any.
                    matching = take([p for p in prompts if not p["used"] and p["text"] == text], record.get("promptId"))
                    source = {"origin": "claude_transcript", "segment_line": number, "kind": "prompt",
                              "delivery": "queued_while_claude_was_working", "origin_kind": "human"}
                    if matching:
                        matching["used"] = True
                        source.update(origin="UserPromptSubmit", hook_event_index=matching["event_index"])
                    else:
                        source["hook_record"] = False
                    message = add("user", text, source)
                    if not matching:
                        unhooked.append(message)
                    # Claude Code's own human mark makes it a response even without a hook record.
                    transcript_human_prompts += 1
                visible_records += 1
                continue
            if attachment_type in _HUMAN_ATTACHMENTS:
                if not duplicate(record, attachment):
                    omit(attachment_type)
            elif attachment_type == "queued_command":
                if not duplicate(record, attachment):
                    # Images in a notice belong to the skipped message, not to the human's attachments.
                    automated(read_text(attachment.get("prompt"), _QUEUED_IN_ANOTHER_FORM)[0],
                              record.get("promptId"))
            elif attachment_type not in _IGNORED_ATTACHMENTS:
                skip_unknown("attachments", attachment_type)
            # Other queued messages (subagent reports, task notifications) are tool output.
            ignored_records += 1
            continue
        if kind in _IGNORED_RECORDS or kind == "system":
            ignored_records += 1
            continue
        if kind not in {"user", "assistant"}:
            skip_unknown("records", kind)
            ignored_records += 1
            continue
        message = record.get("message")
        if not isinstance(message, dict) or "content" not in message or message.get("role", kind) != kind:
            # A form this version does not read: counted, never a stop. Human words it
            # may hold are still in the hook records, which are used if they cannot be placed.
            skip_unknown("records", f"{kind} (message in another form)")
            ignored_records += 1
            continue
        if duplicate(record, message):
            continue
        if kind == "user" and _claude_code_text(message["content"]):
            ignored_records += 1
            continue
        origin = record.get("origin")
        if kind == "user" and isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            # A message Claude Code delivered as a prompt (e.g. a task notification).
            automated(read_text(message["content"], ("records", "user (content in another form)"))[0],
                      record.get("promptId"))
            ignored_records += 1
            continue
        uuid = record.get("uuid")
        source = {"origin": "claude_transcript", "segment_line": number}
        if uuid:
            source["uuid"] = uuid
        content = message["content"]
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            skip_unknown("records", f"{kind} (content in another form)")
            ignored_records += 1
            continue
        text_parts: list[str] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                skip_unknown("blocks", "(block in another form)")
                continue
            block_type = _name(block.get("type"))
            block_source = {**source, "block_index": block_index}
            if block_type == "text":
                if not isinstance(block.get("text"), str):
                    skip_unknown("blocks", "text (in another form)")
                    continue
                if kind == "assistant":
                    add("assistant", block["text"], {**block_source, "kind": "text"})
                else:
                    text_parts.append(block["text"])
            elif block_type in {"thinking", "redacted_thinking"} and kind == "assistant":
                pass
            elif block_type == "image" and kind == "user":
                omit("image")
            elif block_type == "tool_use" and kind == "assistant":
                if block.get("name") != "AskUserQuestion":
                    continue
                tool_id, question_input = block.get("id"), block.get("input")
                items = question_input.get("questions") if isinstance(question_input, dict) else None
                if (not isinstance(tool_id, str) or not tool_id or not isinstance(items, list) or not items or any(
                        not isinstance(item, dict) or not isinstance(item.get("question"), str)
                        or not item["question"].strip() for item in items)):
                    # A question in a form this version does not read is counted; its
                    # hook record then cannot be placed, so the hook records are used.
                    skip_unknown("blocks", "AskUserQuestion (input in another form)")
                    continue
                if tool_id in questions:
                    raise CaptureError("Repeated AskUserQuestion ID is ambiguous")
                # The model may have filled in the answers itself; such a question is
                # kept as it was asked, but its answer never counts as the human's.
                prefilled = prefilled_question(question_input)
                question_message = add("assistant", _json(question_input), {
                    **block_source, "kind": "questionnaire_prefilled" if prefilled else "questionnaire",
                    "tool_use_id": tool_id, "encoding": "json-values",
                })
                questions[tool_id] = {"input": question_input, "question": question_message, "answer": None,
                                      "prefilled": prefilled}
            elif block_type == "tool_result" and kind == "user":
                tool_id = _name(block.get("tool_use_id"))
                if tool_id not in questions:
                    # Unrelated command output, files, Skill expansion, etc.
                    continue
                if questions[tool_id]["answer"] is not None:
                    raise CaptureError("Multiple answers for one AskUserQuestion call")
                if block.get("is_error"):
                    # The human cancelled the screen (e.g. to clarify in chat) or
                    # the call failed.  Keep the result verbatim but never as a
                    # human answer; any follow-up arrives as an ordinary prompt.
                    unanswered_source = {
                        **block_source, "kind": "questionnaire_unanswered", "tool_use_id": tool_id,
                        "encoding": "string" if isinstance(block.get("content"), str) else "json-values",
                    }
                    if isinstance(record.get("toolDenialKind"), str):
                        unanswered_source["denial_kind"] = record["toolDenialKind"]
                    questions[tool_id]["answer"] = add(
                        "user", _unanswered_content(block.get("content")), unanswered_source)
                    questions[tool_id]["unanswered"] = True
                    continue
                content = _read_answer(block.get("content"))
                if content is None:
                    # Claude Code's rendering of the answer, with none this version reads:
                    # counted; the PostToolUse record, if any, gives the answer.
                    skip_unknown("blocks", "tool_result (no answer this version reads)")
                    continue
                answer = add("user", content, {
                    **block_source, "kind": "questionnaire_answer", "tool_use_id": tool_id,
                    "encoding": "string" if isinstance(block.get("content"), str) else "json-values",
                })
                questions[tool_id]["answer"] = answer
            else:
                skip_unknown("blocks", block_type)
        if text_parts:
            matching = take([p for p in prompts if not p["used"] and _prompt_matches(p["text"], text_parts, record)],
                            record.get("promptId"))
            if matching:
                matching["used"] = True
                # The hook record is the evidence, unless the transcript gives the
                # origin in a form this version does not know (then it is not counted).
                origin = record.get("origin")
                known = origin is None or isinstance(origin, dict)
                matching["origin_unknown"] = not known
                transcript_human_prompts += known
                add("user", matching["text"], {
                    **source, "kind": "prompt", "origin": "UserPromptSubmit",
                    "hook_event_index": matching["event_index"], **({} if known else {"origin_kind": "unknown"}),
                })
            elif not _scaffolding("\n".join(text_parts), record):
                # Kept, but marked: no hook recorded it (the hook may have failed).
                # It counts as a response only when Claude Code marks its origin
                # human; otherwise its origin is unknown.
                origin = record.get("origin")
                marked = isinstance(origin, dict) and origin.get("kind") == "human"
                transcript_human_prompts += marked
                for part in text_parts:
                    unhooked.append(add("user", part, {**source, "kind": "prompt", "hook_record": False,
                                                       **({"origin_kind": "human"} if marked else {})}))
        visible_records += 1

    # The original slash command may not be serialized as literal text. Its raw
    # hook event is nevertheless authoritative and belongs at the start.
    # A question hook must agree with the transcript's question of the same ID
    # (checked before any fallback); a hook question the transcript does not
    # show at all means its form is not understood.
    linked: list[tuple[int, dict, str]] = []
    unplaced_question = False
    for event_index, event in post_questions:
        tool_id = _name(event.get("tool_use_id"))
        if not tool_id:
            matches = [key for key, q in questions.items()
                       if _asked_input(q["input"]) == _asked_input(event["tool_input"])]
            # Without an ID, a hook that fits several questions cannot be placed.
            tool_id = matches[0] if len(matches) == 1 else None
        if tool_id not in questions:
            unplaced_question = True
            continue
        if _asked_input(questions[tool_id]["input"]) != _asked_input(event["tool_input"]):
            raise CaptureError("AskUserQuestion hook and transcript disagree about the question")
        if questions[tool_id].get("unanswered"):
            raise CaptureError("AskUserQuestion hook answers a question the transcript records as cancelled or failed")
        if any(tool_id == seen for _, _, seen in linked):
            raise CaptureError("Duplicate AskUserQuestion answer hooks are ambiguous")
        linked.append((event_index, event, tool_id))
    # Notices take, from the hook prompts left, the one with their prompt ID (the
    # transcript may word a notice differently from its hook prompt), or else the
    # only one with the same text. With several and no ID to tell them apart, any
    # of them may be the notice: each is kept, with an unknown origin, and none
    # counts as the human's.
    automated_events: set[int] = set()
    for text, prompt_id in notices:
        left = [p for p in prompts if not p["used"]]
        matching = next((p for p in left if prompt_id and p["prompt_id"] == prompt_id), None)
        same_text = [p for p in left if p["text"] == text]
        if matching is None and len(same_text) == 1:
            matching = same_text[0]
        if matching:
            matching["used"] = True
            automated_events.add(matching["event_index"])
        else:
            for prompt in same_text:
                prompt["origin_unknown"] = True
    unused = [p for p in prompts if not p["used"]]
    scan = {"visible_records": visible_records, "ignored_records": ignored_records,
            "omitted_attachments": omitted_attachments, "unknown_types": unknown}
    if missing or unplaced_question or (unused and (len(unused) != 1 or unused[0] is not prompts[0])):
        # No transcript, or one whose form is not understood well enough to place
        # the human's words: use the hook records, in their order.
        if missing:
            # A notice cannot be told from the human's prompt: those after the request are not counted.
            for prompt in prompts[1:]:
                prompt["origin_unknown"] = True
        return _hooks_only(marker, events, [p for p in prompts if p["event_index"] not in automated_events],
                           post_questions, questions, segment.bytes_read, scan, unhooked,
                           _NO_TRANSCRIPT if missing else _UNPLACED)
    if unused:
        first = unused[0]
        messages.insert(0, {"role": "user", "content": first["text"], "source": {
            "origin": "UserPromptSubmit", "hook_event_index": first["event_index"], "kind": "prompt",
        }})
        transcript_human_prompts += 1

    for event_index, event, tool_id in linked:
        item = questions[tool_id]
        content = _read_answer(event["tool_response"])
        if content is None:
            # No answer this version reads: counted; the transcript's answer, if any, stays.
            skip_unknown("hooks", "PostToolUse AskUserQuestion (no answer this version reads)")
            continue
        hook_answer = {
            "role": "user", "content": content,
            "source": {"origin": "PostToolUse", "hook_event_index": event_index,
                       "kind": "questionnaire_prefilled_answer" if item["prefilled"] else "questionnaire_answer",
                       "tool_use_id": tool_id,
                       "encoding": "string" if isinstance(event["tool_response"], str) else "json-values"},
        }
        if item["answer"] is None:
            at = messages.index(item["question"]) + 1
            messages.insert(at, hook_answer)
        else:
            at = messages.index(item["answer"])
            # Keep the transcript rendering as provenance, not as a second
            # human answer. The structured response is the authoritative value.
            hook_answer["source"]["transcript_rendering"] = item["answer"]["content"]
            messages[at] = hook_answer
        item["answer"] = hook_answer

    if not any(m["role"] == "user" for m in messages):
        raise CaptureError("No human input was captured")
    # Cancelled, failed or never-answered questions, and answers the model
    # filled in itself, are kept as they are but are not human answers.
    # Claude's messages may all be in unknown forms; the coverage shows that
    # (``assistant_messages``) instead of stopping.
    shown = any(m["role"] == "assistant" for m in messages)
    return {"schema_version": 1, "messages": messages, "coverage": _coverage(
        "bounded-transcript-with-hook-provenance", marker, segment.bytes_read, scan, messages,
        reason=None if shown else ("No message of Claude's was in a form this version reads; the human's words "
                                   "are complete, Claude's are not included."),
        answered=sum(1 for item in questions.values()
                     if item["answer"] is not None and not item.get("unanswered") and not item["prefilled"]),
        unanswered=sum(1 for item in questions.values() if item.get("unanswered") or item["answer"] is None),
        prefilled=sum(1 for item in questions.values() if item["prefilled"]),
        user_responses=max(0, transcript_human_prompts - 1), unhooked=len(unhooked))}


EXCLUDED = [
    "earlier-session-history", "thinking", "unrelated-tool-output", "known-skill-expansion",
    "interrupt-markers-and-shell-commands", "attached-image-and-file-content",
    "subagent-reports-and-task-notifications", "compaction-summaries", "unknown-record-types",
]


def _coverage(mode: str, marker: dict, captured_bytes: int, scan: dict, messages: list[dict], *, answered: int,
              unanswered: int, prefilled: int, user_responses: int, unhooked: int, unverified: int = 0,
              reason: str | None = None, excluded: tuple[str, ...] = ()) -> dict:
    """The same coverage fields for every way the interview is built."""
    value = {"mode": mode}
    if reason:
        value["reason"] = reason
    value.update({
        "start_offset": marker["offset"],
        "captured_bytes": captured_bytes,
        "visible_records": scan["visible_records"],
        "ignored_records": scan["ignored_records"],
        "assistant_messages": sum(1 for m in messages if m["role"] == "assistant"),
        "answered_questions": answered,
        "unanswered_questions": unanswered,
        "prefilled_questions": prefilled,
        "unverified_questions": unverified,
        "user_responses": user_responses,
        "human_response_count": answered + user_responses,
        "user_texts_without_hook_record": unhooked,
        "omitted_attachments": dict(sorted(scan["omitted_attachments"].items())),
        "unknown_types": {group: dict(sorted(counts.items())) for group, counts in scan["unknown_types"].items()},
        "structured_values_encoding": "JSON; string values unchanged, not original JSON whitespace",
        "excluded": EXCLUDED + list(excluded),
    })
    return value


_UNPLACED = "The transcript's form did not let every human prompt be placed; "
_NO_TRANSCRIPT = ("There was no Claude Code transcript; messages Claude Code sent as prompts (such as task "
                  "notifications) cannot be told from the human's, so the prompts after the request are kept "
                  "with an unknown origin; ")


def _hooks_only(marker: dict, events: list[dict], prompts: list[dict], post_questions: list[tuple[int, dict]],
                questions: dict[str, dict], captured_bytes: int, scan: dict, unhooked: list[dict], why: str) -> dict:
    """The interview from the hook records, in their order.

    Used when the transcript's form does not let every human prompt be placed,
    or when there is no transcript (``why`` says which). The human's prompts and
    the AskUserQuestion questions and answers are complete; Claude's other
    messages are not included. An answer the hook gives in a form this version
    does not read is taken from the transcript (``questions``) when it has one.
    Visible human texts and questions the hooks did not record are kept at the
    end, in transcript order (their place is unknown). The coverage says all of this.
    """
    asked = {}
    for event in events:
        if (event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") == "AskUserQuestion"
                and not event.get("agent_id")):
            key = _name(event.get("tool_use_id"))
            asked[key] = asked.get(key, False) or prefilled_question(event.get("tool_input"))
    counts = {"answered": 0, "prefilled": 0, "unverified": 0, "unanswered": 0}

    def question(tool_id: str | None, tool_input: dict, source: dict, answer: dict | None) -> list[dict]:
        """A question and its answer (``content`` and ``source``), named by its PreToolUse record.

        Only that record shows whether the model filled in the answers; without
        it the question is kept as unverified and is not counted.
        """
        prefilled = asked.get(tool_id)
        counts["prefilled"] += prefilled is True
        counts["unverified"] += prefilled is None
        name = {True: "questionnaire_prefilled", False: "questionnaire", None: "questionnaire_unverified"}[prefilled]
        result = [{"role": "assistant", "content": _json(_asked_input(tool_input)), "source": {
            **source, "kind": name, "encoding": "json-values"}}]
        if answer is None:
            counts["unanswered"] += 1
        else:
            counts["answered"] += prefilled is False
            result.append({"role": "user", "content": answer["content"],
                           "source": {**answer["source"], "kind": name + "_answer"}})
        return result

    def transcript_answer(tool_id: str | None) -> dict | None:
        item = questions.get(tool_id) if tool_id else None
        return item["answer"] if item and item["answer"] is not None and not item.get("unanswered") else None

    items = [(p["event_index"], "prompt", p) for p in prompts] + [(i, "question", e) for i, e in post_questions]
    messages: list[dict] = []
    hooked: set = set()
    for index, kind, item in sorted(items, key=lambda entry: entry[0]):
        if kind == "prompt":
            # A prompt whose origin is unknown keeps that mark.
            messages.append({"role": "user", "content": item["text"], "source": {
                "origin": "UserPromptSubmit", "hook_event_index": index, "kind": "prompt",
                **({"origin_kind": "unknown"} if item.get("origin_unknown") else {})}})
            continue
        tool_id = _name(item.get("tool_use_id"))
        hooked.add(tool_id)
        source = {"origin": "PostToolUse", "hook_event_index": index, "tool_use_id": tool_id}
        content = _read_answer(item["tool_response"])
        if content is not None:
            answer = {"content": content, "source": {
                **source, "encoding": "string" if isinstance(item["tool_response"], str) else "json-values"}}
        else:
            # No answer this version reads: counted; the transcript's answer, if any, is used.
            tally(scan["unknown_types"]["hooks"], "PostToolUse AskUserQuestion (no answer this version reads)", "(untyped)")
            answer = transcript_answer(tool_id)
        messages.extend(question(tool_id, item["tool_input"], source, answer))
    # What the hooks did not record, in transcript order: visible human texts and questions.
    tail = [(message["source"]["segment_line"], message.get("source", {}).get("block_index", -1), [message])
            for message in unhooked]
    for tool_id, item in questions.items():
        if tool_id in hooked:
            continue
        source = {**item["question"]["source"], "hook_record": False}
        answer = transcript_answer(tool_id)
        if answer is not None:
            answer = {**answer, "source": {**answer["source"], "hook_record": False}}
        kept = question(tool_id, item["input"], source, answer)
        if item.get("unanswered"):
            kept.append({**item["answer"], "source": {**item["answer"]["source"], "hook_record": False}})
        tail.append((source["segment_line"], source["block_index"], kept))
    for _, _, kept in sorted(tail, key=lambda entry: entry[:2]):
        messages.extend(kept)
    if not prompts and not unhooked:
        raise CaptureError("No human input was captured")
    # As on the transcript path, a text without a hook record is a response only
    # when Claude Code marked its origin human; one message counts once, however
    # many text blocks it has.
    marked = len({message["source"]["segment_line"] for message in unhooked
                  if message["source"].get("origin_kind") == "human"})
    # The first prompt is the request; a prompt whose origin is unknown is not counted.
    responses = sum(1 for prompt in prompts[1:] if not prompt.get("origin_unknown"))
    return {"schema_version": 1, "messages": messages, "coverage": _coverage(
        "hook-records-only", marker, captured_bytes, scan, messages, answered=counts["answered"],
        unanswered=counts["unanswered"], prefilled=counts["prefilled"], user_responses=responses + marked,
        unhooked=len(unhooked), unverified=counts["unverified"],
        reason=why + "Claude's messages other than its AskUserQuestion questions are not included; human "
               "texts and questions without a hook record are kept at the end, in transcript order; a text counts "
               "as a response only when Claude Code marked it human, a prompt whose origin is unknown is not "
               "counted, and a question without its PreToolUse record is kept as unverified and not counted; an "
               "answer the hook gives in a form this version does not read is taken from the transcript.",
        excluded=("assistant-messages",))}
