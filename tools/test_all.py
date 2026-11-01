#!/usr/bin/env python3
"""Run every test package in one Python process with unittest discovery."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
AREAS = ("common", "plan", "implement", "router")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", choices=AREAS, action="append", help="Run only selected test packages; default runs all tests")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    start = [str(ROOT / "tests" / area) for area in args.area] if args.area else [str(ROOT / "tests")]
    failures = 0
    for directory in start:
        command = [sys.executable, "-m", "unittest", "discover", "-s", directory, "-t", str(ROOT),
                   "-p", "test_*.py", "-q" if args.quiet else "-v"]
        failures += subprocess.run(command, cwd=ROOT, env=environment, shell=False).returncode != 0
    if failures:
        print("Some tests failed.", file=sys.stderr)
        return 1
    print("All requested tests passed. Real provider/model integration is not exercised.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
