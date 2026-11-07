"""Explicit host operations, retaining the original argument text."""
OPERATIONS = ("plan", "implement", "status", "resume", "doctor")


def public_command(prompt: str, host: str, names=OPERATIONS) -> tuple[str | None, str]:
    prefix = {"claude": "/devflow:", "codex": "$devflow:"}[host]
    for name in names:
        command = prefix + name
        if prompt == command:
            return name, ""
        if prompt.startswith(command) and prompt[len(command):len(command) + 1].isspace():
            return name, prompt[len(command):].lstrip()
    return None, ""
