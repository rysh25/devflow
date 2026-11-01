#!/usr/bin/env python3
"""Mechanically vendor canonical shared code; --check never writes files."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sync(check=False):
    source = ROOT / "shared" / "flow_common"
    for name in ("planflow", "buildflow"):
        dest = ROOT / "plugins" / "devflow" / "components" / name / "scripts" / (name + "_core") / "_vendor" / "flow_common"
        for p in sorted(source.glob("*.py")):
            target = dest / p.name
            if target.exists() and target.read_bytes() == p.read_bytes():
                continue
            if check:
                raise SystemExit(f"Generated common code is stale: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    sync(parser.parse_args().check)
    print("Shared code is consistent.")
