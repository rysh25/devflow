"""The only place that knows where the installed plugin and its runner live.

Nothing here creates files or directories.
"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PACKAGE_ROOT.parents[1]
RUNNER = PLUGIN_ROOT / "scripts" / "devflow.py"

