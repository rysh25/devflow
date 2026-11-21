"""Test package: make the bundled plugin package importable for every test module."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "devflow" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Hooks take the project from CLAUDE_PROJECT_DIR when it is set. Tests run from
# inside Claude Code must not inherit the outer session's value. The runner also
# compares --session with CLAUDE_CODE_SESSION_ID when it is set.
os.environ.pop("CLAUDE_PROJECT_DIR", None)
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
