"""The only place that knows where the installed plugin and its runner live.

Nothing here creates files or directories.
"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PACKAGE_ROOT.parents[1]
RUNNER = PLUGIN_ROOT / "scripts" / "devflow.py"


def workflow_names() -> tuple[str, ...]:
    """Workflow packages shipped with this plugin (each has its own defaults)."""
    return tuple(sorted(path.name for path in PACKAGE_ROOT.iterdir()
                        if (path / "__init__.py").is_file() and (path / "defaults.json").is_file()))
