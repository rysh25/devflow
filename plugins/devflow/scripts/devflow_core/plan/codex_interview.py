"""Assemble Plan's interview from native visible messages and canonical questions."""
from datetime import datetime
import json

from ..common.errors import CaptureError
from ..common.counts import tally
from ..common.hosts.codex import read_conversation
from ..common.operations import public_command
from ..common.sessions import answer_content, prefilled_question, question_matches


def capture_interview(marker: dict, events: list[dict], project) -> dict:
    session, start = marker.get("session_id"), marker.get("turn_id")
    if not isinstance(session, str) or not session or not isinstance(start, str) or not start:
        raise CaptureError("Actual Codex session and start turn IDs are required")
    conversation = read_conversation(session, project)
    records = conversation.records
    unknown, ignored = dict(conversation.unknown_types), dict(conversation.ignored_types)
    first = next((i for i, row in enumerate(records) if row.turn_id == start and row.role == "user"), None)
    if first is None:
        raise CaptureError("The actual start input is missing from Codex conversation records")
    records = records[first:]
    original = next((event for event in events if event.get("hook_event_name") == "UserPromptSubmit"
                     and event.get("turn_id") == start and event.get("session_id") == session), None)
    if original is None or records[0].text != original.get("prompt"):
        raise CaptureError("The native start text does not match its recorded hook input")
    entries, asked, answered_keys, prefilled = [], {}, set(), 0
    turns, anchors = {}, {}
    for index, row in enumerate(records):
        turns.setdefault(row.turn_id, []).append(index)
        if row.role == "user" and public_command(row.text, "codex")[0] in {"status", "doctor"}:
            continue
        # API item order is authoritative, even when clocks go backwards.
        entries.append((index, 1, index, {"role": row.role, "content": row.text, "source": {
            "origin": "codex_api", "host": "codex", "session_id": session, "turn_id": row.turn_id,
            "item_id": row.item_id, "native_item_order": index,
            "kind": "prompt" if row.role == "user" else "assistant_text",
            **({"origin_kind": "unknown"} if row.role == "user" else {})}}))

    # A backwards native clock cannot establish exact hook placement within
    # that turn. Preserve API order and conservatively flag its merged hooks.
    clock_reversed_turns = {turn for turn, positions in turns.items()
                            if any(records[right].started_at_ms < records[left].started_at_ms
                                   for left, right in zip(positions, positions[1:]))}

    def add_event(event, index, role, content, kind):
        positions = turns.get(event.get("turn_id"))
        if not positions:
            raise CaptureError("Question turn is missing from the native Codex conversation")
        stamp = event.get("captured_at")
        anchor = positions[-1] + 1
        resolution, ambiguous = None, True
        if isinstance(stamp, str):
            try:
                captured = datetime.fromisoformat(stamp)
                if captured.tzinfo is None:
                    raise ValueError("missing timezone")
                timestamp = round(captured.timestamp() * 1000)
            except (ValueError, OverflowError) as exc:
                raise CaptureError("Question capture timestamp is invalid") from exc
            resolution = 1 if stamp[19:20] in {".", ","} else 1000
            upper = timestamp + resolution - 1
            anchor = next((position for position in positions[1:]
                           if records[position].started_at_ms > upper), anchor)
            ambiguous = (event.get("turn_id") in clock_reversed_turns
                         or resolution > 1 or any(timestamp <= records[position].started_at_ms <= upper
                                              for position in positions)
                         or timestamp < records[positions[0]].started_at_ms
                         or (positions[-1] + 1 < len(records)
                             and timestamp >= records[positions[-1] + 1].started_at_ms))
        # The anchor is the boundary BEFORE the next native item, whose time
        # is strictly later. Hooks at that boundary keep their recorded order.
        # With ordered native clocks, items at the same timestamp are already
        # to its left. Clock reversals and ties remain ordering_ambiguous.
        previous = anchors.get(event["turn_id"], positions[0] + 1)
        ambiguous = ambiguous or anchor < previous
        anchor = max(anchor, previous)
        anchors[event["turn_id"]] = anchor
        entries.append((anchor, 0, index, {"role": role, "content": content, "source": {
            "origin": "canonical_event", "host": "codex", "session_id": session,
            "turn_id": event["turn_id"], "tool_use_id": event.get("tool_use_id"),
            "hook_event_index": index, "captured_at": stamp, "kind": kind,
            "capture_resolution_ms": resolution, "ordering_ambiguous": ambiguous,
            "outcome": event.get("outcome", "unknown"),
            **({"is_error": event["is_error"]} if "is_error" in event else {})}}))

    for index, event in enumerate(events):
        if event.get("host") != "codex" or event.get("session_id") != session:
            continue
        if event.get("hook_event_name") not in {"PreToolUse", "PostToolUse"}:
            continue  # Ordinary prompts are read through the native API above.
        scope = event.get("agent_scope")
        if scope != "main":
            tally(ignored if scope == "subagent" else unknown,
                  "agent_scope:" + scope if isinstance(scope, str) else None, "agent_scope:(unnamed)")
            continue
        key = event.get("tool_use_id")
        if event.get("hook_event_name") == "PreToolUse":
            if key in asked:
                if (not question_matches(asked[key], event)
                        or asked[key].get("tool_input") != event.get("tool_input")):
                    raise CaptureError("Repeated question ID does not match its recorded input")
                continue
            asked[key] = event
            prefilled += prefilled_question(event.get("tool_input"))
            add_event(event, index, "assistant", json.dumps(event["tool_input"], ensure_ascii=False),
                      "questionnaire_prefilled" if prefilled_question(event.get("tool_input")) else "questionnaire")
        if event.get("hook_event_name") != "PostToolUse":
            continue
        question = asked.get(key)
        if question is not None and not question_matches(question, event):
            raise CaptureError("Question result does not match its recorded question input or turn")
        content = None
        if (question is not None and not prefilled_question(question.get("tool_input"))
                and event.get("outcome") == "answered" and not event.get("is_error")):
            try:
                content = answer_content(event.get("tool_response"))
            except CaptureError:
                pass
        if content is not None:
            if key in answered_keys:
                raise CaptureError("Multiple answers for one native question call")
            answered_keys.add(key)
            kind = "questionnaire_answer"
        else:
            # Preserve the native failure/cancellation/unknown result exactly;
            # it is evidence of a tool outcome, never a human response.
            raw = event.get("source", {}).get("tool_response", event.get("tool_response"))
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            kind = ("questionnaire_prefilled_answer" if question is not None and prefilled_question(question.get("tool_input"))
                    else "questionnaire_unverified_answer" if question is None else "questionnaire_unanswered")
        add_event(event, index, "user", content, kind)
    messages = [entry[3] for entry in sorted(entries, key=lambda item: item[:3])]
    answered = len(answered_keys)
    unanswered = sum(key not in answered_keys and not prefilled_question(value.get("tool_input"))
                     for key, value in asked.items())
    return {"schema_version": 1, "messages": messages, "coverage": {
        "mode": "codex-native-api", "start_turn_id": start, "answered_questions": answered,
        "question_count_unit": "tool_call", "ordering": "native_item_order_and_hook_order",
        "unknown_types": unknown, "ignored_types": ignored,
        "unanswered_questions": unanswered, "prefilled_questions": prefilled, "user_responses": 0,
        "human_response_count": answered, "assistant_messages": sum(row["role"] == "assistant" for row in messages),
        "visible_records": len(records), "excluded": ["reasoning", "tool_output", "subagents", "status_doctor_inputs"],
        "reason": "Ordinary Codex inputs have unverified origin; only paired root question answers count."}}
