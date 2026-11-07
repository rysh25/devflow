"""Explicit startup overrides; no dialogue and no persisted second source."""
import copy
from dataclasses import dataclass
import re

from .arguments import Parser
from .errors import UsageError
from .settings import REVIEWER_KINDS, validate_reviewer, validate_reviewers


@dataclass(frozen=True)
class Startup:
    body: str
    reviewers: tuple[tuple[str, str, str], ...] = ()


def parse_start(raw: str) -> Startup:
    remainder = raw.lstrip()
    if not remainder.startswith("--"):
        return Startup(remainder)
    boundary = re.search(r"(?<!\S)--(?=\s|$)", remainder)
    if boundary is None:
        raise UsageError("Startup options require a standalone -- before the request or plan path")
    parser = Parser(prog="devflow startup", add_help=False, allow_abbrev=False)
    parser.add_argument("--reviewer", nargs=3, action="append", metavar=("AGENT", "MODEL", "EFFORT"))
    args = parser.parse_args(remainder[:boundary.start()].split())
    for kind, model, effort in args.reviewer or []:
        if "'" in model or "'" in effort:
            raise UsageError("Startup model and effort must be names without quotes")
        validate_reviewer({"id": kind, "kind": kind, "command": [kind], "model": model,
                           "effort": effort, "timeout_seconds": 1800})
    return Startup(remainder[boundary.end():].lstrip(), tuple(tuple(row) for row in args.reviewer or []))


def override_reviewers(config: dict, startup: Startup) -> dict:
    if not startup.reviewers:
        return config
    from .reviewers import CLIS
    result = copy.deepcopy(config)
    reviewers, counts = [], {}
    for kind, model, effort in startup.reviewers:
        if kind not in REVIEWER_KINDS:
            raise UsageError("Unknown reviewer kind: " + kind)
        candidates = [row for row in config["reviewers"] if row["kind"] == kind]
        if candidates:
            candidate = candidates[0]
            if any((row["command"], row["timeout_seconds"]) !=
                   (candidate["command"], candidate["timeout_seconds"]) for row in candidates[1:]):
                raise UsageError("Reviewer command/timeout is ambiguous for " + kind)
            command, timeout = candidate["command"], candidate["timeout_seconds"]
        else:
            command, timeout = list(CLIS[kind].command), 1800
        counts[kind] = counts.get(kind, 0) + 1
        identifier = kind + (f"-{counts[kind]}" if counts[kind] > 1 else "")
        reviewers.append({"id": identifier, "kind": kind, "model": model, "effort": effort,
                          "command": copy.deepcopy(command), "timeout_seconds": timeout})
    result["reviewers"] = validate_reviewers(reviewers, result["max_reviews"])
    return result
