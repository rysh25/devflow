"""One error hierarchy for every workflow. An error never means approval."""


class DevFlowError(Exception):
    """An actionable DevFlow failure; never permission to continue."""


class UsageError(DevFlowError):
    """A malformed runner invocation or start command."""


class ReviewerError(DevFlowError):
    """An external reviewer could not produce a trustworthy result."""


class CaptureError(DevFlowError):
    """The interview record cannot be captured faithfully."""
