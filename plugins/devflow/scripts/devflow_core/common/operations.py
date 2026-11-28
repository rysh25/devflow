"""Explicit host operations, retaining the original argument text."""
OPERATIONS = ("plan", "implement", "flow", "status", "resume", "doctor")
#: The human command that runs Plan and then, in the same conversation, Implement.
FLOW = "flow"
#: The Plan endings a flow continues from into Implement.
CHAIN_STATUSES = frozenset({"completed", "accepted", "auto_accepted"})
#: ``request.json``'s ``intent.action`` of an Implement run chained from a flow's Plan run.
CHAINED = "start_from_plan"


def public_command(prompt: str, host: str, names=OPERATIONS) -> tuple[str | None, str]:
    from .hosts import host_profile
    prefix = host_profile(host).command_prefix
    for name in names:
        command = prefix + name
        if prompt == command:
            return name, ""
        if prompt.startswith(command) and prompt[len(command):len(command) + 1].isspace():
            return name, prompt[len(command):].lstrip()
    return None, ""


def flow_requested(request: dict, host: str) -> bool:
    """Whether a Plan run's ``request.json`` records a human flow command (its ``raw_prompt``)."""
    return public_command(request.get("raw_prompt", ""), host)[0] == FLOW
