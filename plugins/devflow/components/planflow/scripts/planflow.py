#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Portable entry point; run with a preinstalled Python 3.11+ interpreter."""

import sys

if __name__ == "__main__":
    # Keep cached plugin trees read-only and Windows pipe encoding deterministic.
    sys.dont_write_bytecode = True
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from planflow_core.cli import main
    raise SystemExit(main())

