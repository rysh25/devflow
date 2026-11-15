"""Native hosts' exact offline skill locations; no catalog or policy emulation."""
import os
from pathlib import Path

from . import validate_host


#: Devin's skill directories under the project root (its own, then those it imports from other
#: tools), from "Where skills live", "Configuration Import" and ``devin skills paths`` (3000.12.57).
#: Devin does not walk up to parent directories for these.
DEVIN_PROJECT_SKILLS = (".devin/skills", ".cognition/skills", ".agents/skills", ".windsurf/skills", ".kiro/skills",
                        ".claude/skills", ".github/skills", ".codex/skills", ".codeium/skills")
#: Windsurf's global skills (~/.codeium/<channel>/skills) are not listed: Devin reads only the
#: directory of its own release channel, which no documented offline information names.


def devin_user_skill_roots(home: Path, environ=None, *, windows: bool | None = None) -> list[tuple[str, Path]]:
    """Devin's user skill directories, as (scope, directory)."""
    environ = os.environ if environ is None else environ
    windows = os.name == "nt" if windows is None else windows
    devin = (Path(environ["APPDATA"]) / "devin" / "skills" if windows and environ.get("APPDATA")
             else home / ".config" / "devin" / "skills")
    copilot = Path(environ["COPILOT_HOME"]) if environ.get("COPILOT_HOME") else home / ".copilot"
    return [("personal", devin), ("personal", home / ".config" / "cognition" / "skills"),
            ("personal", home / ".agents" / "skills"),
            ("personal", home / ".kiro" / "skills"), ("imported", copilot / "skills"),
            ("imported", home / ".codex" / "skills")]


def skill_locations(project: Path, name: str, *, host: str, config_dir: Path | None = None) -> list:
    validate_host(host)
    if host == "devin":
        home = Path(config_dir) if config_dir is not None else Path.home()
        project = Path(project).absolute()
        roots = [*devin_user_skill_roots(home), *(("project", project / path) for path in DEVIN_PROJECT_SKILLS)]
        locations = [(scope, "skill", root / name / "SKILL.md") for scope, root in roots]
    elif host == "codex":
        home = Path(config_dir) if config_dir is not None else Path.home()
        project = Path(project).resolve()
        roots = [*(path / ".agents" / "skills" for path in (project, *project.parents)),
                 home / ".agents" / "skills", Path(os.environ.get("CODEX_HOME") or home / ".codex") / "skills"]
        locations = [("native", "skill", root / name / "SKILL.md") for root in roots]
    else:
        personal = (config_dir if config_dir is not None else
                    Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")).expanduser().absolute()
        locations = [(scope, kind, path)
                     for scope, directory in (("personal", personal), ("project", Path(project).absolute() / ".claude"))
                     for kind, path in (("skill", directory / "skills" / name / "SKILL.md"),
                                        ("command", directory / "commands" / (name + ".md")))]
    unique = {}
    for location in locations:
        unique.setdefault(str(location[2].resolve(strict=False)), location)
    return list(unique.values())
