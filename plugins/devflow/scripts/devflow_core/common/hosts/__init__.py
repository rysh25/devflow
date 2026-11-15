"""Small native-host adapters; interview assembly belongs to the Plan workflow.

Every difference between the supported hosts is one field of ``Host`` in the
``HOSTS`` table; shared code reads the table instead of naming a host.
"""
import copy
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from ..errors import DevFlowError


@dataclass(frozen=True)
class Host:
    name: str
    #: The product name used in messages.
    label: str
    executable: str
    #: The native question tool whose answers DevFlow records.
    question_tool: str
    #: How the human types an operation, e.g. ``/devflow:plan``.
    command_prefix: str
    #: The environment variable that names the project for a hook; None reads the hook's ``cwd``.
    project_variable: str | None
    #: Whether the hook may fall back to its ``cwd`` when ``project_variable`` is unset.
    project_from_cwd: bool
    #: Whether the host expands the plugin's placeholders in its skills. Hosts that do not
    #: receive the operation, session, project and runner as entry context from the prompt hook.
    expands_placeholders: bool
    #: How a question is attributed to the parent conversation: "main" when the host never
    #: gives a subagent the question tool (or names subagents with ``agent_id``), "turn" when
    #: it must match a turn the parent conversation started.
    question_scope: str
    #: Whether the prompt hook can tell a human prompt from one the host sent itself.
    human_prompts: bool
    #: How the Plan interview is read: "transcript" (Claude's conversation file with the
    #: hook records), "conversation_api" (Codex's native API), or "hooks" (hook records only).
    interview: str


HOSTS = {
    "claude": Host("claude", "Claude Code", "claude", "AskUserQuestion", "/devflow:", "CLAUDE_PROJECT_DIR",
                   True, True, "main", True, "transcript"),
    "codex": Host("codex", "Codex", "codex", "request_user_input", "$devflow:", None,
                  True, False, "turn", False, "conversation_api"),
    # Devin sends no cwd; DEVIN_PROJECT_DIR is set to the project root. Devin never gives
    # subagents ask_user_question, so every question belongs to the parent conversation.
    "devin": Host("devin", "Devin", "devin", "ask_user_question", "/devflow:", "DEVIN_PROJECT_DIR",
                  False, False, "main", False, "hooks"),
}


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
        raise DevFlowError("Use an explicit supported --host (" + ", ".join(HOSTS) + ")")
    return host


def host_profile(host: str) -> Host:
    return HOSTS[validate_host(host)]


def executable_name(host: str) -> str:
    return host_profile(host).executable


def question_tool_name(host: str) -> str:
    return host_profile(host).question_tool


def question_context(payload: dict, events: list[dict]) -> dict:
    """Classify a native question using this host's measured root/child evidence."""
    host = validate_host(payload.get("host"))
    if payload.get("agent_id"):
        scope = "subagent"
    elif HOSTS[host].question_scope == "main":
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
    # Claude's supported hook contract uses agent_id to identify subagents, and
    # Devin gives subagents no question tool. Codex also needs the root-turn
    # correlation established by question_context.
    scope = ("subagent" if payload.get("agent_id") else
             "main" if HOSTS[host].question_scope == "main" else payload.get("agent_scope", "unknown"))
    result.update(host=host, canonical_version=1, source=copy.deepcopy(payload.get("source", payload)),
                  agent_scope=scope,
                  operation=None, outcome="unknown")
    if kind == "prompt":
        from ..operations import public_command
        result["operation"] = public_command(payload.get("prompt", ""), host)[0]
        result["origin"] = "human" if HOSTS[host].human_prompts else "unknown"
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
    elif host == "devin" and kind == "question":
        result.update(_devin_answers(payload))
    elif kind == "question":
        result["outcome"] = "failed" if payload.get("is_error") else "answered"
    return result


#: The line Devin 3000.12.57 writes before the answers in ask_user_question's result.
DEVIN_ANSWER_PREFIX = "User answered your questions:\n"


def _devin_answers(payload: dict) -> dict:
    """Devin's answers as ``{"answers": {question: [selected, ...]}}``, read strictly.

    The result is ``{"success": true, "error": null, "output": PREFIX + JSON}``,
    the JSON keyed by each question's text with ``selected`` and ``skipped``.
    Anything else, a skipped or empty answer, or keys that are not exactly the
    asked questions is no answer (outcome ``unknown``); the native result stays
    in ``source``.
    """
    native = payload.get("tool_response")
    if not isinstance(native, dict) or native.get("success") is not True or native.get("error") is not None:
        return {"outcome": "failed" if isinstance(native, dict) and (
            native.get("success") is False or native.get("error")) else "unknown"}
    try:
        output = native.get("output")
        if not isinstance(output, str) or not output.startswith(DEVIN_ANSWER_PREFIX):
            raise ValueError("No answer line")
        from ..storage import parse_json
        value = parse_json(output[len(DEVIN_ANSWER_PREFIX):].encode("utf-8"), "Devin question result")
        questions = (payload.get("tool_input") or {}).get("questions")
        texts = [q.get("question") if isinstance(q, dict) else None for q in questions] \
            if isinstance(questions, list) else []
        if (not texts or any(not isinstance(text, str) or not text.strip() for text in texts)
                or len(set(texts)) != len(texts) or not isinstance(value, dict) or set(value) != set(texts)):
            raise ValueError("Missing or unrelated question answers")
        normalized = {}
        for text in texts:
            item = value[text]
            selected = item.get("selected") if isinstance(item, dict) else None
            if (item.get("skipped") is not False or not isinstance(selected, list) or not selected
                    or any(not isinstance(choice, str) or not choice.strip() for choice in selected)):
                raise ValueError("No actual answer")
            normalized[text] = selected
    except (DevFlowError, UnicodeError, ValueError, TypeError, AttributeError):
        return {"outcome": "unknown"}
    return {"tool_response": {"answers": normalized}, "outcome": "answered"}


#: Fields Devin's question screen adds to each question before PostToolUse, with
#: the value a question the model wrote without them has.
_DEVIN_QUESTION_DEFAULTS = {"multi_select": False}
_DEVIN_OPTION_DEFAULTS = {"description": ""}


def asked_questions(host: str, tool_input: dict) -> object:
    """What the model wrote in a question call, comparable before and after the screen.

    The screen adds the human's answers (``answers``, ``annotations``) on every
    host; Devin's also writes its defaults into each question. Nothing else is
    dropped or reshaped.
    """
    from ..sessions import SCREEN_FILLED_QUESTION_FIELDS
    value = {key: copy.deepcopy(item) for key, item in tool_input.items() if key not in SCREEN_FILLED_QUESTION_FIELDS}
    if host == "devin" and isinstance(value.get("questions"), list):
        for question in value["questions"]:
            if isinstance(question, dict):
                for key, default in _DEVIN_QUESTION_DEFAULTS.items():
                    question.setdefault(key, default)
                for option in question.get("options") if isinstance(question.get("options"), list) else ():
                    if isinstance(option, dict):
                        for key, default in _DEVIN_OPTION_DEFAULTS.items():
                            option.setdefault(key, default)
    return value
