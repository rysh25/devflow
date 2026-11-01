"""Fail-closed extraction of a *bounded* Claude Code interview.

Only user-visible text and AskUserQuestion inputs/results leave this adapter.
Thinking blocks, unrelated tool results, and earlier session history never do.
Claude's JSONL format is not a public stable schema: unknown visible formats
raise CaptureError instead of silently manufacturing a complete transcript.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

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
}
# Harness-generated attachments: hook results, reminders and session context.
# Attachments carrying human text (queued_command) or files the human attached
# (file, directory, pdf_reference, ...) are deliberately absent and fail closed.
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
# Claude Code's question screen writes the human's choices and notes into these
# AskUserQuestion input fields.  PostToolUse sees them; the transcript keeps the
# model's original call, which must not contain them.
_SCREEN_FILLED_QUESTION_FIELDS = ("answers", "annotations")


class CaptureError(Exception):
    """The adapter cannot establish an unambiguous, bounded transcript."""


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
    return {key: value for key, value in question_input.items() if key not in _SCREEN_FILLED_QUESTION_FIELDS}


def _answer_content(value: Any) -> str:
    # Preserve string values exactly. Structured input/result values are encoded
    # losslessly as JSON, not paraphrased into an alleged verbatim quotation.
    if isinstance(value, str):
        if not value.strip():
            raise CaptureError("AskUserQuestion has no human answer")
        return value
    if isinstance(value, list):
        if not value:
            raise CaptureError("AskUserQuestion has no human answer")
        for block in value:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise CaptureError("Unsupported non-text AskUserQuestion answer")
        if not any(block["text"].strip() for block in value):
            raise CaptureError("AskUserQuestion has no human answer")
        return _json(value)
    if isinstance(value, dict):
        if any(value.get(key) for key in ("is_error", "error", "cancelled", "canceled", "aborted")):
            raise CaptureError("AskUserQuestion failed or was cancelled")
        answers = value.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise CaptureError("AskUserQuestion has no human answer")
        for answer in answers.values():
            if isinstance(answer, str) and answer.strip():
                continue
            if isinstance(answer, list) and answer and all(isinstance(x, str) and x.strip() for x in answer):
                continue
            raise CaptureError("AskUserQuestion has an empty or unsupported human answer")
        return _json(value)
    raise CaptureError("Unsupported AskUserQuestion answer type")


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


def human_answer_text(value: Any) -> str:
    """Validated exact answer representation used by the escalation ledger."""
    _answer_content(value)
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def capture_interview(marker: dict, events: list[dict]) -> dict:
    """Return visible messages with explicit provenance, or fail closed.

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
            prompts.append({"event_index": index, "text": event["prompt"], "used": False})
        elif event_name == "PostToolUse" and event.get("tool_name") == "AskUserQuestion":
            if not isinstance(event.get("tool_input"), dict) or "tool_response" not in event:
                raise CaptureError("AskUserQuestion hook is missing question input or answer output")
            if event.get("is_error"):
                raise CaptureError("An unsuccessful AskUserQuestion is not human evidence")
            post_questions.append((index, event))

    messages: list[dict] = []
    questions: dict[str, dict] = {}
    seen_uuids: dict[str, str] = {}
    visible_records = 0
    ignored_records = 0
    transcript_human_prompts = 0

    def add(role: str, content: str, source: dict) -> dict:
        result = {"role": role, "content": content, "source": source}
        messages.append(result)
        return result

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
        if kind == "summary" or record.get("subtype") in {"compact_boundary", "compaction"}:
            raise CaptureError("Compaction occurred during the interview; do not substitute a summary for the original exchange")
        if record.get("isSidechain"):
            ignored_records += 1
            continue
        if kind == "attachment":
            attachment = record.get("attachment")
            attachment_type = attachment.get("type") if isinstance(attachment, dict) else None
            if attachment_type not in _IGNORED_ATTACHMENTS:
                raise CaptureError(f"Unsupported transcript attachment at line {number}: {attachment_type!r}")
            ignored_records += 1
            continue
        if kind in _IGNORED_RECORDS or kind == "system":
            ignored_records += 1
            continue
        if kind not in {"user", "assistant"}:
            raise CaptureError(f"Unsupported transcript record type at line {number}: {kind!r}")
        message = record.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise CaptureError(f"Transcript message missing content at line {number}")
        if message.get("role", kind) != kind:
            raise CaptureError(f"Mismatched transcript role at line {number}")
        uuid = record.get("uuid")
        if uuid is not None:
            if not isinstance(uuid, str):
                raise CaptureError("Unsupported transcript UUID")
            fingerprint = _digest(_json(message).encode("utf-8"))
            if uuid in seen_uuids:
                if seen_uuids[uuid] != fingerprint:
                    raise CaptureError("Conflicting transcript records share one UUID")
                continue
            seen_uuids[uuid] = fingerprint
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
            elif block_type == "tool_use" and kind == "assistant":
                if block.get("name") != "AskUserQuestion":
                    continue
                tool_id, question_input = block.get("id"), block.get("input")
                if not isinstance(tool_id, str) or not tool_id or not isinstance(question_input, dict):
                    raise CaptureError("AskUserQuestion tool use lacks an ID or structured input")
                items = question_input.get("questions")
                if any(question_input.get(field) for field in _SCREEN_FILLED_QUESTION_FIELDS):
                    raise CaptureError("Programmatically prefilled AskUserQuestion answers are not human evidence")
                if not isinstance(items, list) or not items or any(
                    not isinstance(item, dict) or not isinstance(item.get("question"), str)
                    or not item["question"].strip() for item in items
                ):
                    raise CaptureError("AskUserQuestion has no supported nonempty questions")
                if tool_id in questions:
                    raise CaptureError("Repeated AskUserQuestion ID is ambiguous")
                question_message = add("assistant", _json(question_input), {
                    **block_source, "kind": "questionnaire", "tool_use_id": tool_id,
                    "encoding": "json-values",
                })
                questions[tool_id] = {"input": question_input, "question": question_message, "answer": None}
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
                raise CaptureError(f"Unsupported visible content block at line {number}: {block_type!r}")
        if text_parts:
            matching = next((p for p in prompts if not p["used"] and _prompt_matches(p["text"], text_parts, record)), None)
            if matching:
                matching["used"] = True
                transcript_human_prompts += 1
                add("user", matching["text"], {
                    **source, "kind": "prompt", "origin": "UserPromptSubmit",
                    "hook_event_index": matching["event_index"],
                })
            elif not _scaffolding("\n".join(text_parts), record):
                if prompts:
                    raise CaptureError("Visible user text has no matching raw UserPromptSubmit event")
                transcript_human_prompts += 1
                for part in text_parts:
                    add("user", part, {**source, "kind": "prompt"})
        visible_records += 1

    # The original slash command may not be serialized as literal text. Its raw
    # hook event is nevertheless authoritative and belongs at the start.
    unused = [p for p in prompts if not p["used"]]
    if unused:
        if len(unused) != 1 or unused[0] is not prompts[0]:
            raise CaptureError("Cannot place every raw human prompt unambiguously in the transcript")
        first = unused[0]
        messages.insert(0, {"role": "user", "content": first["text"], "source": {
            "origin": "UserPromptSubmit", "hook_event_index": first["event_index"], "kind": "prompt",
        }})
        transcript_human_prompts += 1

    for event_index, event in post_questions:
        tool_id = event.get("tool_use_id")
        if not tool_id:
            matches = [key for key, q in questions.items()
                       if _asked_input(q["input"]) == _asked_input(event["tool_input"])]
            if len(matches) != 1:
                raise CaptureError("Cannot link an AskUserQuestion hook to one transcript question")
            tool_id = matches[0]
        if tool_id not in questions or _asked_input(questions[tool_id]["input"]) != _asked_input(event["tool_input"]):
            raise CaptureError("AskUserQuestion hook and transcript disagree about the question")
        item = questions[tool_id]
        if item.get("unanswered"):
            raise CaptureError("AskUserQuestion hook answers a question the transcript records as cancelled or failed")
        if item.get("hook_answer_seen"):
            raise CaptureError("Duplicate AskUserQuestion answer hooks are ambiguous")
        item["hook_answer_seen"] = True
        hook_answer = {
            "role": "user", "content": _answer_content(event["tool_response"]),
            "source": {"origin": "PostToolUse", "hook_event_index": event_index,
                       "kind": "questionnaire_answer", "tool_use_id": tool_id,
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

    unanswered = [tool_id for tool_id, item in questions.items() if item["answer"] is None]
    if unanswered:
        raise CaptureError("Interview contains unanswered AskUserQuestion calls: " + ", ".join(unanswered))
    if not visible_records or not any(m["role"] == "assistant" for m in messages):
        raise CaptureError("No assistant interview exchange was found after activation")
    if not any(m["role"] == "user" for m in messages):
        raise CaptureError("No human input was captured")
    unanswered_questions = sum(1 for item in questions.values() if item.get("unanswered"))
    answered = len(questions) - unanswered_questions
    user_responses = max(0, transcript_human_prompts - 1)
    return {
        "schema_version": 1,
        "messages": messages,
        "coverage": {
            "mode": "bounded-transcript-with-hook-provenance",
            "start_offset": marker["offset"],
            "captured_bytes": len(segment.encode("utf-8")),
            "visible_records": visible_records,
            "ignored_records": ignored_records,
            "answered_questions": answered,
            "unanswered_questions": unanswered_questions,
            "user_responses": user_responses,
            "human_response_count": answered + user_responses,
            "structured_values_encoding": "JSON; string values unchanged, not original JSON whitespace",
            "excluded": ["earlier-session-history", "thinking", "unrelated-tool-output", "known-skill-expansion"],
        },
    }
