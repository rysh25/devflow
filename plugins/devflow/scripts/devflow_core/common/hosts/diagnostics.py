"""Offline native-host prerequisites; no authentication or model calls."""
import shutil
import subprocess
from pathlib import Path
from ..environment import version_numbers

MINIMUM_CLAUDE_CODE = "2.1.316"
MINIMUM_CODEX = "0.166.0"


def claude_version() -> dict:
    executable = shutil.which("claude")
    report = {"minimum_version": MINIMUM_CLAUDE_CODE, "path": executable,
              "version": None, "supported": False, "reason": "Claude Code executable not found"}
    if not executable:
        return report
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=10, shell=False, check=False)
        # A version probe is diagnostic only and does not authenticate or invoke a model.
        found = version_numbers(result.stdout[:8192])
        if result.returncode != 0 or not found:
            report["reason"] = "Claude Code version could not be established"
        else:
            report.update(version=".".join(map(str, found)), supported=found >= version_numbers(MINIMUM_CLAUDE_CODE),
                          reason="Hook input with agent_id is version-gated; real hook execution is not verified")
    except (OSError, subprocess.SubprocessError) as exc:
        report["reason"] = str(exc)
    return report



def codex_version():
    executable = shutil.which("codex")
    report = {"minimum_version": MINIMUM_CODEX, "path": executable, "version": None, "supported": False,
              "reason": "Codex CLI executable not found", "runtime_verified": False}
    if executable:
        try:
            result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=10, shell=False)
            found = version_numbers(result.stdout[:8192])
            report.update(version=".".join(map(str, found)) if found else None,
                          supported=result.returncode == 0 and bool(found) and found >= version_numbers(MINIMUM_CODEX),
                          reason="Bundled hooks, trust and request_user_input need runtime verification")
        except (OSError, subprocess.SubprocessError) as exc:
            report["reason"] = str(exc)
    report["required_feature"] = "default_mode_request_user_input"
    report["activation"] = "codex --enable default_mode_request_user_input"
    report["feature_note"] = ("Under development and disabled by default in checked Codex 0.166.0; "
                              "explicit activation and real question/answer verification are required")
    return report
