#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Portable DevFlow entry point using an installed, compatible Python."""
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from devflow_core.cli import main
    raise SystemExit(main())
