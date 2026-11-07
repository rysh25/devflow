"""Native hosts' exact offline skill locations; no catalog or policy emulation."""
import os
from pathlib import Path

from . import validate_host


def skill_locations(project: Path, name: str, *, host: str, config_dir: Path | None = None) -> list:
    validate_host(host)
    if host == "codex":
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
