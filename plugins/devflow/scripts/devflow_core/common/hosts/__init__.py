"""Small native-host adapters; interview assembly belongs to the Plan workflow."""
import copy
from typing import NotRequired, TypedDict

from ..errors import DevFlowError

HOSTS = frozenset({"claude", "codex"})
EXECUTABLES = {"claude": "claude", "codex": "codex"}
QUESTION_TOOLS = {"claude": "AskUserQuestion", "codex": "request_user_input"}


class CanonicalPayload(TypedDict):
    canonical_version: int
    host: str
    session_id: str
    agent_scope: str
    origin: NotRequired[str]
    operation: str | None
    outcome: str
    source: dict
    captured_at: NotRequired[str]
    tool_use_id: NotRequired[str]
    tool_input: NotRequired[dict]
    tool_response: NotRequired[object]
    prefilled: NotRequired[bool]


class CanonicalEvent(TypedDict):
    event_id: str
    recorded_at: str
    kind: str
    payload: CanonicalPayload


def validate_host(host):
    if not isinstance(host, str) or host not in HOSTS:
        raise DevFlowError("Use an explicit supported --host (claude or codex)")
    return host


def executable_name(host: str) -> str:
    return EXECUTABLES[validate_host(host)]


def question_tool_name(host: str) -> str:
    return QUESTION_TOOLS[validate_host(host)]


def question_context(payload: dict, events: list[dict]) -> dict:
    """Classify a native question using this host's measured root/child evidence."""
    host = validate_host(payload.get("host"))
    if payload.get("agent_id"):
        scope = "subagent"
    elif host == "claude":
        scope = "main"
    else:
        root_turns = {event["payload"].get("turn_id") for event in events
                      if event["kind"] == "prompt" and event["payload"].get("host") == host
                      and event["payload"].get("session_id") == payload.get("session_id")
                      and not event["payload"].get("agent_id")}
        turn = payload.get("turn_id")
        scope = "main" if turn and turn in root_turns else "unknown"
    return dict(payload, agent_scope=scope)


def canonical_payload(payload: dict, kind: str) -> dict:
    """DevFlow's common event vocabulary, with the native JSON retained as source.

    AskUserQuestion is a semantic tag here. It is never written into the host's
    conversation record or represented as the name of a native Codex tool.
    """
    host = validate_host(payload.get("host"))
    if payload.get("canonical_version") == 1:
        return copy.deepcopy(payload)
    result = copy.deepcopy(payload)
    # Claude's supported hook contract uses agent_id to identify subagents.
    # Codex also needs the root-turn correlation established by question_context.
    scope = ("subagent" if payload.get("agent_id") else
             "main" if host == "claude" else payload.get("agent_scope", "unknown"))
    result.update(host=host, canonical_version=1, source=copy.deepcopy(payload.get("source", payload)),
                  agent_scope=scope,
                  operation=None, outcome="unknown")
    if kind == "prompt":
        from ..operations import public_command
        result["operation"] = public_command(payload.get("prompt", ""), host)[0]
        result["origin"] = "human" if host == "claude" else "unknown"
        return result
    result["tool_name"] = "AskUserQuestion"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise DevFlowError("Observation capture requires its actual tool identity and input" if kind == "observation"
                           else "Question capture requires its actual tool identity, input and result")
    result["prefilled"] = bool(tool_input.get("answers") or tool_input.get("annotations"))
    result["outcome"] = "asked" if kind == "observation" else "unknown"
    if host == "codex" and kind == "question":
        from ..storage import parse_json
        native = payload.get("tool_response")
        try:
            value = parse_json(native.encode("utf-8"), "Codex question result") if isinstance(native, str) else None
            answers = value.get("answers") if isinstance(value, dict) else None
            questions = (payload.get("tool_input") or {}).get("questions")
            ids = [q["id"] for q in questions] if isinstance(questions, list) else []
            if (not ids or len(set(ids)) != len(ids) or not isinstance(answers, dict)
                    or set(answers) != set(ids)):
                raise ValueError("Missing or unrelated question answers")
            normalized = {}
            for key in ids:
                item = answers[key]
                strings = item.get("answers") if isinstance(item, dict) else None
                if (not isinstance(strings, list) or not strings
                        or any(not isinstance(text, str) or not text.strip() for text in strings)):
                    raise ValueError("No actual answer")
                normalized[key] = strings
            result["tool_response"] = {"answers": normalized}
            result["outcome"] = "answered"
        except (DevFlowError, UnicodeError, ValueError, TypeError, KeyError):
            result["outcome"] = "unknown"
    elif kind == "question":
        result["outcome"] = "failed" if payload.get("is_error") else "answered"
    return result
