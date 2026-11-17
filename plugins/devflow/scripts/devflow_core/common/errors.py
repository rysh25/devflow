"""One error hierarchy for every workflow. An error never means approval."""
import json

#: A model-chosen name shown in an error message is first cut to this length.
FIELD_NAME_CHARS = 64


def shown_name(name: str) -> str:
    """One model-chosen name in an error message: cut, then ASCII-escaped and quoted.

    ``json.dumps`` (``ensure_ascii``) escapes control and bidirectional
    characters and unpaired surrogates, so the name cannot disguise the
    message, and the message can go into a record or a request unchanged.
    """
    return json.dumps(name[:FIELD_NAME_CHARS])


class DevFlowError(Exception):
    """An actionable DevFlow failure; never permission to continue."""


class UsageError(DevFlowError):
    """A malformed runner invocation or start command."""


class ReviewerError(DevFlowError):
    """An external reviewer could not produce a trustworthy result."""


class AnswerRefusedError(ReviewerError):
    """An answer check refused the answer: it is not strict JSON, or it does not
    pass the workflow's checks. A refused answer, not a failed review."""


class CaptureError(DevFlowError):
    """The interview record cannot be captured faithfully."""
