"""One error hierarchy for every workflow. An error never means approval."""


class DevFlowError(Exception):
    """An actionable DevFlow failure; never permission to continue."""


class UsageError(DevFlowError):
    """A malformed runner invocation."""


class GuardError(DevFlowError):
    """A tool call the active workflow does not permit."""


class ShellPolicyError(DevFlowError):
    """A shell command outside the strict runner or read-only policy."""


class ReviewerError(DevFlowError):
    """An external reviewer could not produce a trustworthy result."""


class CaptureError(DevFlowError):
    """The interview record cannot be captured faithfully."""
