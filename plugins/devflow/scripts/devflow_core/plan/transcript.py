"""Extraction of a *bounded* Claude Code interview.

Only user-visible text and AskUserQuestion inputs/results leave this adapter.
Thinking blocks, unrelated tool results, and earlier session history never do.
Claude's JSONL format is not a public stable schema and changes between
versions: what this adapter does not know is skipped and counted, never a
reason to stop. The human's words are cross-checked with the hook records; it
stops only when the record is provably broken (corrupt or truncated JSONL,
conflicting records, a question hook that contradicts the transcript).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from ..common.errors import CaptureError
from ..common.sessions import SCREEN_FILLED_QUESTION_FIELDS, answer_content as _answer_content, prefilled_question

MAX_CAPTURE_BYTES = 16 * 1024 * 1024
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
MAX_UNKNOWN_TYPES = 32
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


def transcript_marker(path: str | None) -> dict:
    """Mark an append boundary without copying earlier conversation content.

    Claude may not have created its transcript when UserPromptSubmit fires.
    The tail fingerprint detects truncation/rewrite/rotation before sealing.
    """
    if path is None or not isinstance(path, str) or not path.strip():
        raise CaptureError("Claude did not supply transcript_path; full capture is unavailable")
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


def _read_segment(marker: dict) -> str:
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
        if stat.st_size - offset > MAX_CAPTURE_BYTES:
            raise CaptureError("Interview transcript exceeds the 16 MiB capture limit")
        with path.open("rb") as stream:
            stream.seek(offset - tail_size)
            if _digest(stream.read(tail_size)) != marker["tail_sha256"]:
                raise CaptureError("Transcript prefix changed during the interview")
            data = stream.read(MAX_CAPTURE_BYTES + 1)
        if len(data) > MAX_CAPTURE_BYTES:
            raise CaptureError("Interview transcript exceeds the 16 MiB capture limit")
        return data.decode("utf-8")
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


def _queued_human_prompt(attachment: dict) -> tuple[str, int, list] | None:
    """Text, image count and unknown block types of a message the human typed while Claude worked."""
    origin = attachment.get("origin")
    if (attachment.get("commandMode") != "prompt" or attachment.get("isMeta")
            or not isinstance(origin, dict) or origin.get("kind") != "human"):
        return None
    return _queued_text(attachment.get("prompt"))


def _queued_text(prompt: Any) -> tuple[str, int, list]:
    """Text, image count and unknown block types of a queued message's prompt."""
    if isinstance(prompt, str):
        return prompt, 0, []
    if not isinstance(prompt, list):
        raise CaptureError("Unsupported queued human message")
    texts: list[str] = []
    images = 0
    unknown: list = []
    for block in prompt:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif isinstance(block, dict) and block.get("type") == "image":
            images += 1
        else:
            unknown.append(block.get("type") if isinstance(block, dict) else None)
    return "\n".join(texts), images, unknown


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
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        for block in value
    ):
        return _json(value)
    raise CaptureError("Unsupported content in a cancelled or failed AskUserQuestion result")


def capture_interview(marker: dict, events: list[dict]) -> dict:
    """Return visible messages with explicit provenance.

    ``events`` are the original hook input objects recorded since activation.
    UserPromptSubmit is authoritative for raw prompts (including the original
    slash command). PostToolUse can preserve structured AskUserQuestion answers
    more faithfully than Claude's human-readable tool-result rendering.
    """
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise CaptureError("Hook events must be a list of objects")
    try:
        if len(_json(events).encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise CaptureError("Interview hook log exceeds the 16 MiB capture limit")
    except (TypeError, ValueError) as exc:
        raise CaptureError(f"Invalid hook event data: {exc}") from exc
    segment = _read_segment(marker)
    prompts: list[dict] = []
    post_questions: list[tuple[int, dict]] = []
    for index, event in enumerate(events):
        event_name = event.get("hook_event_name")
        if event_name == "UserPromptSubmit":
            if not isinstance(event.get("prompt"), str):
                raise CaptureError("UserPromptSubmit is missing its exact prompt")
            prompts.append({"event_index": index, "text": event["prompt"], "used": False,
                            "prompt_id": event.get("prompt_id")})
        elif event_name == "PostToolUse" and event.get("tool_name") == "AskUserQuestion" and not event.get("agent_id"):
            if not isinstance(event.get("tool_input"), dict) or "tool_response" not in event:
                raise CaptureError("AskUserQuestion hook is missing question input or answer output")
            if event.get("is_error"):
                continue  # An unsuccessful AskUserQuestion is not human evidence.
            post_questions.append((index, event))

    messages: list[dict] = []
    questions: dict[str, dict] = {}
    seen_uuids: dict[str, str] = {}
    visible_records = 0
    ignored_records = 0
    transcript_human_prompts = 0
    omitted_attachments: dict[str, int] = {}
    unknown: dict[str, dict[str, int]] = {"records": {}, "attachments": {}, "blocks": {}}
    unhooked: list[dict] = []
    # Claude Code also sends its own messages (task notifications, subagent
    # reports) through UserPromptSubmit. The transcript marks their origin. They
    # are matched with hook prompts after the scan, once the human's words have
    # taken theirs, so a human prompt with the same text is never taken for one.
    notices: list[tuple[str, object]] = []

    def automated(text: object, prompt_id: object = None) -> None:
        if isinstance(text, str):
            notices.append((text, prompt_id))

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
        counts = unknown[group]
        label = name[:64] if isinstance(name, str) and name else "(untyped)"
        if label not in counts and len(counts) >= MAX_UNKNOWN_TYPES:
            label = "(other)"
        counts[label] = counts.get(label, 0) + 1

    def duplicate(record: dict, value: Any) -> bool:
        uuid = record.get("uuid")
        if uuid is None:
            return False
        if not isinstance(uuid, str):
            raise CaptureError("Unsupported transcript UUID")
        fingerprint = _digest(_json(value).encode("utf-8"))
        if uuid in seen_uuids:
            if seen_uuids[uuid] != fingerprint:
                raise CaptureError("Conflicting transcript records share one UUID")
            return True
        seen_uuids[uuid] = fingerprint
        return False

    for number, line in enumerate(segment.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaptureError(f"Malformed or incomplete transcript JSONL at segment line {number}") from exc
        if not isinstance(record, dict):
            raise CaptureError(f"Transcript line {number} is not an object")
        kind = record.get("type")
        if (record.get("isSidechain") or kind == "summary" or record.get("isCompactSummary")
                or record.get("subtype") in {"compact_boundary", "compaction"}):
            # A subagent's own records, and compaction: the boundary and its
            # summary are Claude Code's; the original exchange stays earlier in the file.
            ignored_records += 1
            continue
        if kind == "attachment":
            attachment = record.get("attachment")
            attachment_type = attachment.get("type") if isinstance(attachment, dict) else None
            queued = _queued_human_prompt(attachment) if attachment_type == "queued_command" else None
            if queued is not None:
                if duplicate(record, attachment):
                    continue
                text, images, unknown_blocks = queued
                omit("image", images)
                for block_type in unknown_blocks:
                    skip_unknown("blocks", block_type)
                if text.strip():
                    # Claude Code marks the origin human; link a matching hook event if any.
                    matching = take([p for p in prompts if not p["used"] and p["text"] == text], record.get("promptId"))
                    source = {"origin": "claude_transcript", "segment_line": number, "kind": "prompt",
                              "delivery": "queued_while_claude_was_working", "origin_kind": "human"}
                    if matching:
                        matching["used"] = True
                        source.update(origin="UserPromptSubmit", hook_event_index=matching["event_index"])
                    elif prompts:
                        source["hook_record"] = False
                    message = add("user", text, source)
                    if not matching and prompts:
                        unhooked.append(message)
                    # Claude Code's own human mark makes it a response even without a hook record.
                    transcript_human_prompts += 1
                visible_records += 1
                continue
            if attachment_type in _HUMAN_ATTACHMENTS:
                if not duplicate(record, attachment):
                    omit(attachment_type)
            elif attachment_type == "queued_command":
                prompt = attachment.get("prompt")
                automated(_queued_text(prompt)[0] if isinstance(prompt, (str, list)) else None)
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
        if not isinstance(message, dict) or "content" not in message:
            raise CaptureError(f"Transcript message missing content at line {number}")
        if message.get("role", kind) != kind:
            raise CaptureError(f"Mismatched transcript role at line {number}")
        if duplicate(record, message):
            continue
        if kind == "user" and _claude_code_text(message["content"]):
            ignored_records += 1
            continue
        origin = record.get("origin")
        if kind == "user" and isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            # A message Claude Code delivered as a prompt (e.g. a task notification).
            content = message["content"]
            automated(content if isinstance(content, str) else "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
                if isinstance(content, list) else None, record.get("promptId"))
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
            raise CaptureError(f"Unsupported transcript content at line {number}")
        text_parts: list[str] = []
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise CaptureError(f"Unsupported content block at line {number}")
            block_type = block.get("type")
            block_source = {**source, "block_index": block_index}
            if block_type == "text":
                if not isinstance(block.get("text"), str):
                    raise CaptureError("Text block is missing a string")
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
                if not isinstance(tool_id, str) or not tool_id or not isinstance(question_input, dict):
                    raise CaptureError("AskUserQuestion tool use lacks an ID or structured input")
                items = question_input.get("questions")
                if not isinstance(items, list) or not items or any(
                    not isinstance(item, dict) or not isinstance(item.get("question"), str)
                    or not item["question"].strip() for item in items
                ):
                    raise CaptureError("AskUserQuestion has no supported nonempty questions")
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
                tool_id = block.get("tool_use_id")
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
                answer = add("user", _answer_content(block.get("content")), {
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
                transcript_human_prompts += 1
                add("user", matching["text"], {
                    **source, "kind": "prompt", "origin": "UserPromptSubmit",
                    "hook_event_index": matching["event_index"],
                })
            elif not _scaffolding("\n".join(text_parts), record):
                # Kept, but marked: no hook recorded it (the hook may have failed).
                # It counts as a response only when Claude Code marks its origin
                # human (or no prompt was hooked at all); otherwise its origin is unknown.
                marked = (record.get("origin") or {}).get("kind") == "human"
                transcript_human_prompts += marked or not prompts
                for part in text_parts:
                    message = add("user", part, {**source, "kind": "prompt", "hook_record": False if prompts else None,
                                                 **({"origin_kind": "human"} if marked else {})})
                    if prompts:
                        unhooked.append(message)
        visible_records += 1

    # The original slash command may not be serialized as literal text. Its raw
    # hook event is nevertheless authoritative and belongs at the start.
    # A question hook must agree with the transcript's question of the same ID
    # (checked before any fallback); a hook question the transcript does not
    # show at all means its form is not understood.
    linked: list[tuple[int, dict, str]] = []
    unplaced_question = False
    for event_index, event in post_questions:
        tool_id = event.get("tool_use_id")
        if not tool_id:
            matches = [key for key, q in questions.items()
                       if _asked_input(q["input"]) == _asked_input(event["tool_input"])]
            if len(matches) > 1:
                raise CaptureError("Cannot link an AskUserQuestion hook to one transcript question")
            tool_id = matches[0] if matches else None
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
    # Notices take, from the hook prompts left, the one with their prompt ID,
    # or else the only one with the same text (several: none, a human's may be among them).
    automated_events: set[int] = set()
    for text, prompt_id in notices:
        left = [p for p in prompts if not p["used"] and p["text"] == text]
        matching = next((p for p in left if prompt_id and p["prompt_id"] == prompt_id), None)
        if matching is None and len(left) == 1:
            matching = left[0]
        if matching:
            matching["used"] = True
            automated_events.add(matching["event_index"])
    unused = [p for p in prompts if not p["used"]]
    scan = {"visible_records": visible_records, "ignored_records": ignored_records,
            "omitted_attachments": omitted_attachments, "unknown_types": unknown}
    if unplaced_question or (unused and (len(unused) != 1 or unused[0] is not prompts[0])):
        # The transcript's form is not understood well enough to place the
        # human's words: use the hook records, in their order.
        return _hooks_only(marker, events, [p for p in prompts if p["event_index"] not in automated_events],
                           post_questions, len(segment.encode("utf-8")), scan, unhooked)
    if unused:
        first = unused[0]
        messages.insert(0, {"role": "user", "content": first["text"], "source": {
            "origin": "UserPromptSubmit", "hook_event_index": first["event_index"], "kind": "prompt",
        }})
        transcript_human_prompts += 1

    for event_index, event, tool_id in linked:
        item = questions[tool_id]
        hook_answer = {
            "role": "user", "content": _answer_content(event["tool_response"]),
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
        "bounded-transcript-with-hook-provenance", marker, len(segment.encode("utf-8")), scan, messages,
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
              unanswered: int, prefilled: int, user_responses: int, unhooked: int, reason: str | None = None,
              excluded: tuple[str, ...] = ()) -> dict:
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
        "user_responses": user_responses,
        "human_response_count": answered + user_responses,
        "user_texts_without_hook_record": unhooked,
        "omitted_attachments": dict(sorted(scan["omitted_attachments"].items())),
        "unknown_types": {group: dict(sorted(counts.items())) for group, counts in scan["unknown_types"].items()},
        "structured_values_encoding": "JSON; string values unchanged, not original JSON whitespace",
        "excluded": EXCLUDED + list(excluded),
    })
    return value


def _hooks_only(marker: dict, events: list[dict], prompts: list[dict], post_questions: list[tuple[int, dict]],
                captured_bytes: int, scan: dict, unhooked: list[dict]) -> dict:
    """The interview from the hook records, in their order.

    Used when the transcript's form does not let every human prompt be placed.
    The human's prompts and the AskUserQuestion questions and answers are
    complete; Claude's other messages are not included. Visible human texts
    the hooks did not record are kept at the end (their place is unknown).
    The coverage says all of this.
    """
    asked = {}
    for event in events:
        if (event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") == "AskUserQuestion"
                and not event.get("agent_id")):
            key = event.get("tool_use_id")
            asked[key] = asked.get(key, False) or prefilled_question(event.get("tool_input"))
    items = [(p["event_index"], "prompt", p) for p in prompts] + [(i, "question", e) for i, e in post_questions]
    messages: list[dict] = []
    answered = prefilled_questions = 0
    for index, kind, item in sorted(items, key=lambda entry: entry[0]):
        if kind == "prompt":
            messages.append({"role": "user", "content": item["text"], "source": {
                "origin": "UserPromptSubmit", "hook_event_index": index, "kind": "prompt"}})
            continue
        tool_id = item.get("tool_use_id")
        prefilled = asked.get(tool_id, True)  # Without its PreToolUse record, never a human answer.
        prefilled_questions += prefilled
        answered += not prefilled
        source = {"origin": "PostToolUse", "hook_event_index": index, "tool_use_id": tool_id}
        messages.append({"role": "assistant", "content": _json(_asked_input(item["tool_input"])), "source": {
            **source, "kind": "questionnaire_prefilled" if prefilled else "questionnaire", "encoding": "json-values"}})
        messages.append({"role": "user", "content": _answer_content(item["tool_response"]), "source": {
            **source, "kind": "questionnaire_prefilled_answer" if prefilled else "questionnaire_answer",
            "encoding": "string" if isinstance(item["tool_response"], str) else "json-values"}})
    messages.extend(unhooked)
    if not prompts and not unhooked:
        raise CaptureError("No human input was captured")
    # As on the transcript path, a text without a hook record is a response only
    # when Claude Code marked its origin human.
    marked = sum(1 for message in unhooked if message["source"].get("origin_kind") == "human")
    return {"schema_version": 1, "messages": messages, "coverage": _coverage(
        "hook-records-only", marker, captured_bytes, scan, messages, answered=answered, unanswered=0,
        prefilled=prefilled_questions, user_responses=max(0, len(prompts) - 1) + marked, unhooked=len(unhooked),
        reason="The transcript's form did not let every human prompt be placed; Claude's messages other than "
               "its AskUserQuestion questions are not included, and human texts without a hook record are "
               "kept at the end and counted as responses only when Claude Code marked them human.",
        excluded=("assistant-messages",))}
