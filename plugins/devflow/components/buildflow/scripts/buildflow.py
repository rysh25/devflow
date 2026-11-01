#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Independent implementation component entry point (Python 3.11+)."""
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from buildflow_core.cli import main
    raise SystemExit(main())
