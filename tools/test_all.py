#!/usr/bin/env python3
"""Run independent test suites in separate processes to avoid module collisions."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"planflow": "tests_planflow", "buildflow": "tests_buildflow", "unified": "tests_unified", "distribution": "tests"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=list(SUITES), action="append", help="Run only selected suites; default runs all suites")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    check = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_shared.py"), "--check"], cwd=ROOT, env=environment, shell=False)
    if check.returncode:
        return check.returncode
    failures = []
    for name in args.suite or list(SUITES):
        directory = ROOT / SUITES[name]
        if not directory.is_dir() or not list(directory.glob("test_*.py")):
            print(f"Missing expected test suite: {directory}", file=sys.stderr, flush=True)
            failures.append(name)
            continue
        print(f"Running {name} tests in a separate Python process.", flush=True)
        command = [sys.executable, "-m", "unittest", "discover", "-s", str(directory), "-p", "test_*.py", "-q" if args.quiet else "-v"]
        result = subprocess.run(command, cwd=ROOT, env=environment, shell=False)
        if result.returncode:
            failures.append(name)
    if failures:
        print("Failed suites: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("All requested suites passed. Real provider/model integration is not exercised.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
