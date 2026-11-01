"""Fresh, packet-only reviewer processes. No retries and no model fallback.

OpenCode protocols are explicit: v1 and v2 have different configurations and
model selectors. A version mismatch is rejected, never silently substituted.
These application permissions are not a sandbox for CLI plugins or MCP startup;
use trusted CLI configuration or an externally isolated account/container.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any


class AdapterError(Exception):
    """A reviewer did not produce one trustworthy, schema-valid result."""


# The engine accepts packets through 16 MiB; leave bounded room for the schema
# and reviewer instructions as in Implement's adapter.
MAX_PACKET_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
REVIEW_KEYS = {"schema_version", "verdict", "summary", "findings", "questions"}
FINDING_KEYS = {"id", "severity", "category", "location", "evidence", "recommendation", "requires_human"}
QUESTION_KEYS = {"id", "question", "why_human", "options"}


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(text: str) -> Any:
    def invalid(value: str) -> None:
        raise AdapterError(f"Invalid JSON constant: {value}")
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=invalid)
    except (ValueError, TypeError) as exc:
        raise AdapterError(f"Invalid JSON: {exc}") from exc


def _text(value: Any, label: str, limit: int = 32768) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AdapterError(f"{label} must be a nonempty string of at most {limit} characters")
    return value


def validate_review(value: Any) -> dict:
    """Validate the exact semantic contract, including cross-field invariants."""
    if not isinstance(value, dict) or set(value) != REVIEW_KEYS:
        raise AdapterError("Review must contain exactly schema_version, verdict, summary, findings, questions")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise AdapterError("Unsupported review schema_version")
    if value["verdict"] not in ("pass", "changes_required", "human_required"):
        raise AdapterError("Unsupported review verdict")
    _text(value["summary"], "summary", 32768)
    findings, questions = value["findings"], value["questions"]
    if not isinstance(findings, list) or len(findings) > 128:
        raise AdapterError("findings must be an array of at most 128 items")
    if not isinstance(questions, list) or len(questions) > 64:
        raise AdapterError("questions must be an array of at most 64 items")
    seen: set[str] = set()
    for item in findings:
        if not isinstance(item, dict) or set(item) != FINDING_KEYS:
            raise AdapterError("Finding has missing or unknown fields")
        for key in FINDING_KEYS - {"requires_human"}:
            _text(item[key], f"finding.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise AdapterError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if item["severity"] not in ("critical", "high", "medium", "low"):
            raise AdapterError("Unsupported finding severity")
        if type(item["requires_human"]) is not bool:
            raise AdapterError("requires_human must be a boolean")
    for item in questions:
        if not isinstance(item, dict) or set(item) != QUESTION_KEYS:
            raise AdapterError("Question has missing or unknown fields")
        for key in ("id", "question", "why_human"):
            _text(item[key], f"question.{key}", 32768 if key != "id" else 128)
        if item["id"] in seen:
            raise AdapterError("Finding and question IDs must be unique")
        seen.add(item["id"])
        if not isinstance(item["options"], list) or len(item["options"]) > 12:
            raise AdapterError("question.options must be an array of at most 12 strings")
        for option in item["options"]:
            _text(option, "question.option")
    human = bool(questions) or any(f["requires_human"] for f in findings)
    if value["verdict"] == "pass" and (findings or questions):
        raise AdapterError("A pass cannot contain findings or questions")
    if value["verdict"] == "changes_required" and (not findings or human):
        raise AdapterError("changes_required needs findings and cannot include human decisions")
    if value["verdict"] == "human_required" and not questions:
        raise AdapterError("human_required needs at least one explicit question for the human")
    return value


def parse_codex_events(text: str) -> dict:
    """Inspect JSONL independently of the final file; any tool invalidates it."""
    threads, answers = set(), []
    started = completed = False
    for line in text.splitlines():
        if not line.strip():
            continue
        event = _json(line)
        if not isinstance(event, dict):
            raise AdapterError("Codex event is not an object")
        kind = event.get("type")
        if completed:
            raise AdapterError("Codex emitted events after completion")
        if kind == "thread.started":
            threads.add(_text(event.get("thread_id"), "thread_id", 256))
        elif kind == "turn.started":
            if started:
                raise AdapterError("Codex returned multiple turns")
            started = True
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not started or not isinstance(item, dict) or item.get("type") not in ("agent_message", "reasoning"):
                raise AdapterError("Packet-only Codex reviewer attempted a tool or unsupported item")
            if kind == "item.completed" and item["type"] == "agent_message":
                answers.append(_text(item.get("text"), "agent_message", MAX_OUTPUT_BYTES))
        elif kind == "turn.completed":
            if not started:
                raise AdapterError("Codex completed a turn that never started")
            completed = True
        else:
            raise AdapterError(f"Codex emitted an error or unsupported event: {kind}")
    if not started or not completed or len(threads) != 1 or len(answers) != 1:
        raise AdapterError("Codex did not return exactly one completed review")
    return validate_review(_json(answers[0]))


def parse_opencode_events(text: str) -> dict:
    """Decode v1 JSONL events; never treat an event wrapper as the review."""
    parts: list[str] = []
    part_ids: dict[str, str] = {}
    message_ids: set[str] = set()
    session_ids: set[str] = set()
    finished = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        event = _json(line)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise AdapterError(f"Unsupported OpenCode event on line {line_number}")
        kind = event["type"]
        if event.get("sessionID"):
            session_ids.add(str(event["sessionID"]))
        if kind == "error":
            raise AdapterError("OpenCode emitted an error event; see stdout.jsonl")
        if kind in ("tool_use", "tool-call", "tool_result"):
            raise AdapterError("Packet-only reviewer attempted a tool call")
        if kind not in ("text", "step_start", "step_finish", "reasoning"):
            raise AdapterError(f"Unsupported OpenCode event type: {kind}")
        part = event.get("part")
        if not isinstance(part, dict):
            raise AdapterError(f"Missing OpenCode event part on line {line_number}")
        if part.get("messageID"):
            message_ids.add(str(part["messageID"]))
        if kind == "step_finish":
            if part.get("reason") != "stop":
                raise AdapterError("OpenCode did not finish normally (possible truncation or tool request)")
            finished = True
        elif kind == "text":
            if finished:
                raise AdapterError("Text appeared after OpenCode finished")
            content = part.get("text")
            if not isinstance(content, str):
                raise AdapterError("OpenCode text part is not a string")
            part_id = part.get("id")
            if isinstance(part_id, str) and part_id in part_ids:
                if part_ids[part_id] != content:
                    raise AdapterError("Conflicting duplicate OpenCode text parts")
                continue
            if isinstance(part_id, str):
                part_ids[part_id] = content
            parts.append(content)
    if not finished or not parts:
        raise AdapterError("OpenCode JSONL lacks completed review text")
    if len(message_ids) > 1 or len(session_ids) > 1:
        raise AdapterError("OpenCode returned multiple messages/sessions; refusing ambiguous review")
    return validate_review(_json("".join(parts)))


def _provider(provider: dict) -> tuple[str, list[str], str, str]:
    kind = provider.get("kind")
    if kind not in ("codex", "opencode"):
        raise AdapterError(f"Unsupported reviewer kind: {kind}")
    command = provider.get("command", [kind])
    if not isinstance(command, list) or not command or len(command) > 12:
        raise AdapterError("Reviewer command must be a short argv prefix array")
    if any(not isinstance(p, str) or not p or "\0" in p for p in command):
        raise AdapterError("Reviewer command contains an invalid argument")
    model, effort = provider.get("model"), provider.get("effort")
    for label, token in (("model", model), ("effort", effort)):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]*", token):
            raise AdapterError(f"Invalid reviewer {label}; do not embed CLI flags")
    if kind == "opencode" and (type(provider.get("protocol_version", 1)) is not int
                                or provider.get("protocol_version", 1) not in (1, 2)):
        raise AdapterError("OpenCode protocol_version must be explicitly 1 or 2; no automatic fallback")
    return kind, list(command), model, effort


def _round_agent_id(output_dir: Path) -> str:
    """Avoid falling through to a pre-existing fixed agent if config is ignored."""
    digest = hashlib.sha256(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return "planflow-reviewer-" + digest


def _runtime_config(model: str, protocol: int = 1, agent_id: str = "planflow-reviewer") -> dict:
    if protocol == 2:
        deny = [{"action": "*", "resource": "*", "effect": "deny"}]
        return {
            "update": "disable",
            "snapshots": False,
            "compaction": {"auto": False},
            "warming": False,
            "permissions": deny,
            "policies": [
                {"action": action, "resource": "*", "effect": "deny"}
                for action in ("tool.use", "mcp.use", "skill.use")
            ],
            "agents": {
                "title": {"disabled": True},
                agent_id: {
                    "description": "Packet-only independent plan reviewer",
                    "mode": "primary",
                    "permissions": deny,
                    "system": "Review only the attached PlanFlow packet. Never use tools. Return only the requested JSON object.",
                }
            },
        }
    # String deny replaces inherited permission objects instead of retaining
    # accidentally permissive specific keys. Managed policy still has priority.
    return {
        "model": model,
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "compaction": {"auto": False},
        "permission": "deny",
        "agent": {
            "planflow-reviewer": {
                "description": "Packet-only independent plan reviewer",
                "mode": "primary",
                "model": model,
                "permission": "deny",
                "prompt": "Review only the attached PlanFlow packet. Never use tools. Return only the requested JSON object.",
            }
        },
    }


def build_command(provider: dict, packet: Path, schema: Path, output_dir: Path,
                  project: Path) -> tuple[list[str], dict[str, str]]:
    """Return safe argv plus environment *overrides*, not a shell command.

    `project` is provenance only. The subprocess working directory is always a
    fresh per-round workspace. Credentials stay in their normal CLI environment.
    """
    kind, prefix, model, effort = _provider(provider)
    output_dir = output_dir.resolve()
    if kind == "codex":
        overrides = [f'model_reasoning_effort="{effort}"', "project_doc_max_bytes=0",
                     'web_search="disabled"', 'approval_policy="never"', "mcp_servers={}",
                     "features.code_mode.enabled=false"]
        overrides.extend(f"features.{flag}=false" for flag in (
            "shell_tool", "shell_snapshot", "multi_agent", "hooks", "memories", "apps",
            "remote_plugin", "plugins", "skill_mcp_dependency_install", "goals"))
        argv = prefix + ["--strict-config", "exec", "--ignore-user-config", "--ignore-rules", "--model", model]
        for option in overrides:
            argv.extend(["--config", option])
        argv.extend(["--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--json",
                     "--output-schema", str(schema.resolve()),
                     "--output-last-message", str(output_dir / "final-response.json"), "-"])
        return argv, {}
    if provider.get("protocol_version", 1) == 2:
        # v2 uses a private server and a model#variant selector. The dedicated
        # config directory retains normal auth storage; it does not copy keys.
        return prefix + [
            "run", "--standalone", "--agent", _round_agent_id(output_dir),
            "--model", f"{model}#{effort}", "--format", "json",
            "--file", str(output_dir / "workspace" / "review-input.md"),
            "Review the attached PlanFlow packet. Return only the exact JSON schema requested in that packet. Do not use tools.",
        ], {
            # Current v2 skips parent project discovery when cwd equals its
            # global config directory. Auth/data directories are unchanged.
            "OPENCODE_CONFIG_DIR": str(output_dir / "workspace"),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        }
    return prefix + [
        "--pure", "run", "--agent", "planflow-reviewer", "--model", model,
        "--variant", effort, "--format", "json",
        "--title", "PlanFlow independent review",
        "Review the attached PlanFlow packet. Return only the exact JSON schema requested in that packet. Do not use tools.",
        # OpenCode v1's --file is a greedy array. Keep it last so the prompt
        # cannot be consumed as an additional filename.
        "--file", str(output_dir / "workspace" / "review-input.md"),
    ], {
        "OPENCODE_CONFIG_CONTENT": json.dumps(_runtime_config(model), separators=(",", ":")),
        "OPENCODE_PERMISSION": json.dumps("deny"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
        "OPENCODE_DISABLE_AUTOCOMPACT": "true",
        "OPENCODE_PURE": "true",
    }


def _resolve(prefix: list[str]) -> list[str]:
    executable = shutil.which(prefix[0])
    if executable is None:
        raise AdapterError(f"Reviewer executable not found: {prefix[0]}")
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        raise AdapterError("Windows reviewer must use a native .exe, or a Node executable plus its CLI script; .cmd/.bat shell shims are not supported")
    return [executable, *prefix[1:]]


def probe_version(provider: dict) -> str:
    """Local version/help only: no model request and no automatic installation."""
    kind, prefix, _, _ = _provider(provider)
    prefix = _resolve(prefix)
    try:
        result = subprocess.run(prefix + ["--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"Cannot read {kind} CLI version: {exc}") from exc
    if result.returncode != 0:
        raise AdapterError(f"{kind} --version failed (exit {result.returncode})")
    try:
        version = result.stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        raise AdapterError("CLI version is not UTF-8") from exc
    if not version or len(version) > 1024:
        raise AdapterError("CLI version output is empty or unsupported")
    if kind == "opencode":
        match = re.search(r"(?<![0-9])v?([0-9]+)\.[0-9]+\.[0-9]+", version)
        expected = provider.get("protocol_version", 1)
        if not match or int(match.group(1)) != expected:
            raise AdapterError(f"OpenCode {version!r} does not match configured v{expected} protocol; no automatic major-version fallback")
        if expected == 2:
            semantic = tuple(int(x) for x in re.search(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version).groups())
            if semantic < (2, 2, 3):
                raise AdapterError("OpenCode v2 must be at least 2.2.3 for mcp.use and skill.use deny policies")
    help_text = ""
    for arguments in ((["exec", "--help"] if kind == "codex" else ["run", "--help"]), ["--help"]):
        try:
            result = subprocess.run(prefix + arguments, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=15, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError(f"Cannot read {kind} CLI help: {exc}") from exc
        if result.returncode or len(result.stdout) > 262144:
            raise AdapterError(f"{kind} local help probe failed or exceeded its limit")
        help_text += "\n" + result.stdout.decode("utf-8", errors="replace")
    required = (["--strict-config", "--ignore-user-config", "--ignore-rules", "--output-schema", "--json", "--ephemeral"]
                if kind == "codex" else ["--format", "--agent", "--model", "--file"] +
                (["--standalone"] if provider.get("protocol_version", 1) == 2 else ["--variant", "--pure"]))
    if any(flag not in help_text for flag in required):
        raise AdapterError(f"{kind} CLI lacks required review flags; update it (no weaker fallback)")
    return version


def _write_new(path: Path, text: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise AdapterError(f"Cannot create adapter output {path.name}: {exc}") from exc


def _stop_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # Native argv, never cmd.exe. Best effort tree cleanup, then kill parent.
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise AdapterError("Reviewer process did not terminate; stop it manually before resuming")


def _execute(argv: list[str], env: dict[str, str], cwd: Path, data: bytes,
             output_dir: Path, timeout: float, stdout_name: str = "stdout.jsonl") -> int:
    exceeded = threading.Event()
    io_errors: list[str] = []
    flags = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **flags)
    except OSError as exc:
        raise AdapterError(f"Cannot launch reviewer: {exc}") from exc

    def read_stream(stream: Any, path: Path) -> None:
        size = 0
        try:
            with path.open("xb") as handle:
                while chunk := stream.read(65536):
                    remain = MAX_OUTPUT_BYTES - size
                    if remain > 0:
                        handle.write(chunk[:remain])
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        exceeded.set()
                        break
        except OSError as exc:
            io_errors.append(str(exc))
            exceeded.set()
        finally:
            stream.close()

    def write_input() -> None:
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(target=read_stream, args=(process.stdout, output_dir / stdout_name), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, output_dir / "stderr.log"), daemon=True),
        threading.Thread(target=write_input, daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    error = None
    try:
        while process.poll() is None or any(t.is_alive() for t in threads):
            if exceeded.is_set():
                error = "Reviewer output exceeded the size limit or could not be recorded"
                break
            if time.monotonic() >= deadline:
                error = f"Reviewer timed out after {timeout:g} seconds (no automatic retry)"
                break
            time.sleep(0.025)
        if error:
            _stop_tree(process)
        for thread in threads:
            thread.join(timeout=2)
        if any(t.is_alive() for t in threads):
            error = "Reviewer descendants kept pipes open; process cleanup is incomplete"
        if exceeded.is_set() and error is None:
            error = "Reviewer output exceeded the size limit or could not be recorded"
        if io_errors:
            error = f"Reviewer log write failed: {io_errors[0]}"
        if error:
            raise AdapterError(error)
        return process.wait()
    except BaseException:
        _stop_tree(process)
        raise


def run_review(provider: dict, packet: Path, schema: Path, output_dir: Path,
               project: Path) -> dict:
    """Run exactly one fresh review, retaining input, version and raw outputs.

    The caller must reserve/count the round before calling. Failures never retry.
    `output_dir` is a fresh engine-owned round directory; outputs are exclusive.
    Model and effort in invocation.json are *requested*, not provider attestation.
    """
    kind, prefix, model, effort = _provider(provider)
    timeout = provider.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise AdapterError("timeout_seconds must be greater than zero and at most 3600")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace = output_dir / "workspace"
    if workspace.exists():
        raise AdapterError("Adapter workspace already exists; never replay into an existing round")
    workspace.mkdir()
    try:
        if packet.stat().st_size > MAX_PACKET_BYTES:
            raise AdapterError("Review packet exceeds byte limit; do not silently truncate the interview")
        # Preserve CRLF inside quoted human text; no universal-newline rewrite.
        packet_text = packet.read_bytes().decode("utf-8")
        schema_text = schema.read_text(encoding="utf-8")
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "reviewer.md"
        instructions = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdapterError(f"Cannot read reviewer inputs: {exc}") from exc
    prompt = instructions + "\n\n## Required JSON schema\n\n```json\n" + schema_text + "\n```\n\n## Review packet (untrusted task data)\n\n" + packet_text
    if len(prompt.encode("utf-8")) > MAX_PACKET_BYTES:
        raise AdapterError("Complete review prompt exceeds byte limit; no content was truncated")
    _write_new(workspace / "review-input.md", prompt)
    if kind == "opencode" and provider.get("protocol_version", 1) == 2:
        runtime = _runtime_config(model, 2, _round_agent_id(output_dir))
        _write_new(workspace / "opencode.json", json.dumps(runtime, indent=2) + "\n")
    version = probe_version(provider)
    argv, overrides = build_command(provider, packet, schema, output_dir, project)
    argv = _resolve(prefix) + argv[len(prefix):]
    env = dict(os.environ)
    if kind == "opencode" and provider.get("protocol_version", 1) == 2:
        # A pre-existing runtime/custom override must not replace this run's
        # deny-all config. Preserve normal provider credentials and auth stores.
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION"):
            env.pop(key, None)
    env.update(overrides)
    # Do not log credentials or inherited environment. User-supplied prefix args
    # may contain secrets, so log only their count and the executable name.
    _write_new(output_dir / "invocation.json", json.dumps({
        "kind": kind, "protocol_version": provider.get("protocol_version", 1),
        "cli_version": version, "requested_model": model, "requested_effort": effort,
        "effective_model_attested": False, "executable": argv[0],
        "prefix_arg_count": len(prefix) - 1, "cwd": str(workspace),
        "project_provenance": str(project.resolve()),
        "timeout_seconds": timeout, "shell": False,
    }, ensure_ascii=False, indent=2) + "\n")
    code = _execute(argv, env, workspace, prompt.encode("utf-8") if kind == "codex" else b"",
                    output_dir, timeout, "stdout.jsonl")
    if code != 0:
        raise AdapterError(f"{kind} exited with {code}; inspect stderr.log (no automatic retry)")
    try:
        if kind == "codex":
            final = output_dir / "final-response.json"
            if final.is_symlink() or not final.is_file() or final.stat().st_size > MAX_OUTPUT_BYTES:
                raise AdapterError("Codex final response is missing, unsafe or too large")
            event_result = parse_codex_events((output_dir / "stdout.jsonl").read_text(encoding="utf-8"))
            final_result = validate_review(_json(final.read_text(encoding="utf-8")))
            if final_result != event_result:
                raise AdapterError("Codex final-message file and JSONL result disagree")
            return final_result
        return parse_opencode_events((output_dir / "stdout.jsonl").read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise AdapterError(f"Cannot decode reviewer output: {exc}") from exc
