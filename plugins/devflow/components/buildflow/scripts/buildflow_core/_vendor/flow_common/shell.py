"""Bounded shell subset shared by the workflow tool guards.

This is a workflow boundary, not an operating-system sandbox: every accepted
command still passes through Claude Code's normal permission checks.
"""

from __future__ import annotations

import shlex


class ShellPolicyError(ValueError):
    """The command is outside the accepted shell subset."""


_OPERATORS = frozenset("$`;&|<>(){}[]!\r\n\x00")

# Read-only search commands, with the options that would make them run another
# program or write a file.  Add a command here only if that list is complete.
READ_ONLY_COMMANDS: dict[str, frozenset[str]] = {
    "grep": frozenset(),
    "rg": frozenset({"--pre", "--pre-glob", "--hostname-bin"}),
    "find": frozenset({
        "-exec", "-execdir", "-ok", "-okdir", "-delete",
        "-fprint", "-fprint0", "-fprintf", "-fls",
    }),
    "ls": frozenset(),
    "cat": frozenset(),
    "head": frozenset(),
    "tail": frozenset(),
    "wc": frozenset(),
}
READ_ONLY_SUMMARY = (
    "read-only search commands (" + ", ".join(READ_ONLY_COMMANDS) + ") without pipes or redirection"
)


def _split(command: object) -> list[str]:
    if not isinstance(command, str) or not command or len(command) > 32768:
        raise ShellPolicyError("The command must be a bounded nonempty string.")
    if any(ord(ch) < 32 and ch != "\t" for ch in command):
        raise ShellPolicyError("Control characters and multiline commands are forbidden.")
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError as exc:
        raise ShellPolicyError("Unsupported shell quoting.") from exc
    if not tokens:
        raise ShellPolicyError("Empty command.")
    return tokens


def shell_tokens(command: object) -> list[str]:
    """Tokens of a command with no shell operators anywhere, even inside quotes."""
    tokens = _split(command)
    if any(ch in _OPERATORS for ch in command):  # type: ignore[union-attr]
        raise ShellPolicyError("Shell operators, substitutions and multiline commands are forbidden.")
    return tokens


def _outside_single_quotes(command: str) -> str:
    """The command text that Bash interprets, i.e. without single-quoted text."""
    kept: list[str] = []
    quote = None
    escaped = False
    for ch in command:
        if quote == "'":
            if ch == "'":
                quote = None
            continue
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch == "'":
            quote = "'"
            continue
        elif ch == '"':
            quote = '"'
        kept.append(ch)
    return "".join(kept)


def read_only_tokens(command: object) -> list[str] | None:
    """Tokens of an accepted Bash read-only search command, or None.

    Shell operators are accepted only inside single quotes, which Bash treats
    literally (e.g. a grep pattern such as 'foo|bar$').
    """
    tokens = _split(command)
    name = tokens[0]
    if name not in READ_ONLY_COMMANDS:
        return None
    if any(ch in _OPERATORS for ch in _outside_single_quotes(command)):  # type: ignore[arg-type]
        raise ShellPolicyError(
            "Read-only commands accept shell operators only inside single quotes; "
            "pipes, redirection and substitutions are forbidden."
        )
    forbidden = READ_ONLY_COMMANDS[name]
    for token in tokens[1:]:
        option = token.split("=", 1)[0]
        if option in forbidden:
            raise ShellPolicyError(f"{name} {option} can run programs or write files.")
    return tokens
