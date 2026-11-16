"""Explicit startup overrides; no dialogue and no persisted second source."""
import copy
from dataclasses import dataclass
import re

from .arguments import Parser
from .errors import DevFlowError, UsageError
from .review import validate_limit
from .settings import REVIEWER_KINDS, validate_reviewer, validate_reviewers

# How --max-reviews is written: ASCII digits only, with no sign or leading zero.
MAX_REVIEWS_TEXT = re.compile(r"[1-9][0-9]?")


@dataclass(frozen=True)
class Startup:
    body: str
    reviewers: tuple[tuple[str, str, str], ...] = ()
    max_reviews: int | None = None


def _max_reviews(values: list[str] | None) -> int | None:
    if not values:
        return None
    if len(values) > 1:
        raise UsageError("--max-reviews may be given only once")
    if not MAX_REVIEWS_TEXT.fullmatch(values[0]):
        raise UsageError("--max-reviews takes an integer from 1 to 99")
    try:
        return validate_limit(int(values[0]))
    except DevFlowError as exc:
        raise UsageError(str(exc)) from exc


def parse_start(raw: str) -> Startup:
    remainder = raw.lstrip()
    if not remainder.startswith("--"):
        return Startup(remainder)
    boundary = re.search(r"(?<!\S)--(?=\s|$)", remainder)
    if boundary is None:
        raise UsageError("Startup options require a standalone -- before the request or plan path")
    parser = Parser(prog="devflow startup", add_help=False, allow_abbrev=False)
    parser.add_argument("--reviewer", nargs=3, action="append", metavar=("AGENT", "MODEL", "EFFORT"))
    parser.add_argument("--max-reviews", action="append", metavar="N")
    args = parser.parse_args(remainder[:boundary.start()].split())
    for kind, model, effort in args.reviewer or []:
        if "'" in model or "'" in effort:
            raise UsageError("Startup model and effort must be names without quotes")
        validate_reviewer({"id": kind, "kind": kind, "command": [kind], "model": model,
                           "effort": effort, "timeout_seconds": 1800})
    return Startup(remainder[boundary.end():].lstrip(), tuple(tuple(row) for row in args.reviewer or []),
                   _max_reviews(args.max_reviews))


def apply_startup(config: dict, startup: Startup) -> dict:
    """Apply --max-reviews and --reviewer to the effective settings, then check
    the reviewer list against the resulting review limit."""
    if not startup.reviewers and startup.max_reviews is None:
        return config
    result = copy.deepcopy(config)
    if startup.max_reviews is not None:
        result["max_reviews"] = startup.max_reviews
    if startup.reviewers:
        result["reviewers"] = _replacement_reviewers(config, startup.reviewers)
    result["reviewers"] = validate_reviewers(result["reviewers"], result["max_reviews"])
    return result


def _replacement_reviewers(config: dict, rows: tuple[tuple[str, str, str], ...]) -> list:
    """The complete list from --reviewer rows. Command and timeout come from
    the same kind's configured reviewers when they all agree."""
    from .reviewers import CLIS
    reviewers, counts = [], {}
    for kind, model, effort in rows:
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
    return reviewers
