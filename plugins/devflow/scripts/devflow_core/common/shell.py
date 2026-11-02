"""Bounded shell subset shared by the workflow tool guards.

This is a workflow boundary, not an operating-system sandbox: every accepted
command still passes through Claude Code's normal permission checks.

Bash commands are read the way bash reads them (quotes, backslashes, comments,
separators), then every part must be a command from ``COMMANDS`` that neither
writes files (contents or metadata) nor starts other programs. The only
writes allowed are the transient temporary files a command creates and removes
itself in the system temp directory (``sort`` spilling to ``TMPDIR``).
The DevFlow runner never joins a pipeline: it is checked separately, alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import Callable

from .errors import ShellPolicyError

MAX_COMMAND = 32768
_STRICT_OPERATORS = frozenset("$`;&|<>(){}[]!\r\n\x00")
#: Unquoted characters a restricted command's arguments may contain.
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=,:+@%")
#: Unquoted characters bash or zsh (EXTENDED_GLOB) may expand into file names.
_GLOB = frozenset("*?[]^#")
_BLANK = " \t"
_WORD_END = frozenset(" \t\n;|&<>")
_REJECT_OUTSIDE = frozenset("$`(){}")

EXPANSION_MESSAGE = ("Shell expansions, substitutions, subshells, groups, comments, background jobs and line "
                     "continuations are not allowed")
REDIRECT_MESSAGE = "Output can be redirected only to /dev/null; write files with the Write tool"
GIT_MESSAGE = "git needs --no-optional-locks and --no-pager before the subcommand"


def _split(command: object) -> list[str]:
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND:
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
    """Tokens of the runner command: no shell operators anywhere, even inside quotes."""
    tokens = _split(command)
    if any(ch in _STRICT_OPERATORS for ch in command):  # type: ignore[union-attr]
        raise ShellPolicyError("Shell operators, substitutions and multiline commands are forbidden.")
    return tokens


# -- reading a command the way bash does ---------------------------------------------------

@dataclass(frozen=True)
class Word:
    text: str           # after quote and backslash removal
    unsafe: bool        # has an unquoted character outside _SAFE (globs included)


def _control(ch: str) -> bool:
    return (ord(ch) < 32 and ch not in "\t\n") or ord(ch) == 127


def _read_word(command: str, i: int) -> tuple[Word, int]:
    """One word starting at ``i`` (not a blank or an operator); stops before an operator."""
    chars: list[str] = []
    unsafe = False
    start = i
    n = len(command)
    while i < n:
        ch = command[i]
        if ch in _WORD_END:
            break
        if _control(ch):
            raise ShellPolicyError("Control characters are not allowed.")
        if ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise ShellPolicyError("Unsupported shell quoting.")
            content = command[i + 1:end]
            if "\\\n" in content:
                raise ShellPolicyError(EXPANSION_MESSAGE)
            if any(_control(c) for c in content):
                raise ShellPolicyError("Control characters are not allowed.")
            chars.append(content)
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while True:
                if i >= n:
                    raise ShellPolicyError("Unsupported shell quoting.")
                c = command[i]
                if c == '"':
                    i += 1
                    break
                if c in "$`":
                    raise ShellPolicyError(EXPANSION_MESSAGE)
                if _control(c):
                    raise ShellPolicyError("Control characters are not allowed.")
                if c == "\\":
                    if i + 1 >= n:
                        raise ShellPolicyError("Unsupported shell quoting.")
                    following = command[i + 1]
                    if following == "\n" or following in "$`":
                        raise ShellPolicyError(EXPANSION_MESSAGE)
                    if following in '"\\':
                        chars.append(following)
                    else:
                        chars.append("\\" + following)
                    i += 2
                    continue
                chars.append(c)
                i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                raise ShellPolicyError("Unsupported shell quoting.")
            following = command[i + 1]
            if following == "\n" or following in "$`":
                raise ShellPolicyError(EXPANSION_MESSAGE)
            if _control(following):
                raise ShellPolicyError("Control characters are not allowed.")
            chars.append(following)
            unsafe = unsafe or following not in _SAFE
            i += 2
            continue
        if ch in _REJECT_OUTSIDE:
            raise ShellPolicyError(EXPANSION_MESSAGE)
        if ch == "#" and i == start:
            raise ShellPolicyError(EXPANSION_MESSAGE)
        if ch == "~" and i == start:
            chars.append(ch)
            i += 1
            continue
        chars.append(ch)
        unsafe = unsafe or ch not in _SAFE or ch in _GLOB
        i += 1
    return Word("".join(chars), unsafe), i


def _skip_blanks(command: str, i: int) -> int:
    while i < len(command) and command[i] in _BLANK:
        i += 1
    return i


def _redirect_target(command: str, i: int) -> tuple[str, int]:
    i = _skip_blanks(command, i)
    if i >= len(command) or command[i] in _WORD_END:
        raise ShellPolicyError(REDIRECT_MESSAGE)
    word, i = _read_word(command, i)
    return word.text, i


def split_words(command: object) -> list[list[Word]]:
    """The command's parts (between |, ||, &&, ; and newlines) as words.

    Redirections are checked here and removed from the words. Raises
    ShellPolicyError for anything outside the supported subset.
    """
    return [segment for _, segment in split_list(command)]


def split_list(command: object) -> list[tuple[str, list[Word]]]:
    """Like ``split_words``, with the separator before each part ("" first, then
    "|", "&&", "||" or ";" for ; and newlines), as bash runs them."""
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND:
        raise ShellPolicyError("The command must be a bounded nonempty string.")
    segments: list[tuple[str, list[Word]]] = []
    current: list[Word] = []
    has_redirect = False
    need_next = False  # after |, || or &&
    separator = ""     # what joins the next part to the previous one
    i, n = 0, len(command)

    def close(joined: str | None) -> None:
        """``joined`` is |, && or || (both sides needed); None for ; and newlines."""
        nonlocal current, need_next, has_redirect, separator
        if joined:
            if not current:
                raise ShellPolicyError("A pipe, && or || needs a command on both sides.")
            segments.append((separator, current))
            need_next, separator = True, joined
        else:
            if current:
                segments.append((separator, current))
                separator = ";"
            elif need_next or has_redirect:
                raise ShellPolicyError("A pipe, && or || needs a command on both sides.")
            elif segments:
                separator = ";"
            need_next = False
        current, has_redirect = [], False

    while i < n:
        ch = command[i]
        if ch in _BLANK:
            i += 1
            continue
        if ch in "\n;":
            if ch == ";" and command[i + 1:i + 2] == ";":
                raise ShellPolicyError(EXPANSION_MESSAGE)
            close(None)
            i += 1
            continue
        if ch == "|":
            following = command[i + 1:i + 2]
            if following == "&":
                raise ShellPolicyError(EXPANSION_MESSAGE)
            close("||" if following == "|" else "|")
            i += 2 if following == "|" else 1
            continue
        if ch == "&":
            following = command[i + 1:i + 2]
            if following == "&":
                close("&&")
                i += 2
                continue
            if following == ">":
                if command[i + 2:i + 3] in (">", "|", "&"):
                    raise ShellPolicyError(REDIRECT_MESSAGE)
                target, i = _redirect_target(command, i + 2)
                if target != "/dev/null":
                    raise ShellPolicyError(REDIRECT_MESSAGE)
                has_redirect = True
                continue
            raise ShellPolicyError(EXPANSION_MESSAGE)
        fd = re.match(r"[0-9]+(?=[<>])", command[i:])
        if fd or ch in "<>":
            number = int(fd.group(0)) if fd else None
            i += len(fd.group(0)) if fd else 0
            op = command[i]
            following = command[i + 1:i + 2]
            if op == ">":
                if following == "&":
                    if number == 2 and command[i + 2:i + 3] == "1" and (i + 3 >= n or command[i + 3] in _WORD_END):
                        i += 3
                        has_redirect = True
                        continue
                    raise ShellPolicyError(REDIRECT_MESSAGE)
                if following in (">", "|") or number not in (None, 1, 2):
                    raise ShellPolicyError(REDIRECT_MESSAGE)
                target, i = _redirect_target(command, i + 1)
                if target != "/dev/null":
                    raise ShellPolicyError(REDIRECT_MESSAGE)
            else:
                if following in ("<", "&", ">", "(") or number not in (None, 0):
                    raise ShellPolicyError(REDIRECT_MESSAGE)
                _, i = _redirect_target(command, i + 1)
            has_redirect = True
            continue
        word, i = _read_word(command, i)
        current.append(word)
    close(None)
    if need_next:
        raise ShellPolicyError("A pipe, && or || needs a command on both sides.")
    if not segments:
        raise ShellPolicyError("Empty command.")
    return segments


# -- the allowed commands -------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """``forbidden``: options that write or run programs; ``validate``: a full allowlist check."""
    forbidden: frozenset = frozenset()
    validate: Callable[[list[str], "Context"], None] | None = None

    @property
    def restricted(self) -> bool:
        return bool(self.forbidden) or self.validate is not None


@dataclass(frozen=True)
class Context:
    cd_allowed: Callable[[str], bool]
    project: str


def _forbidden_option(name: str, token: str, forbidden: frozenset) -> None:
    """Exact, abbreviated (GNU/git accept unique prefixes) and clustered forms."""
    error = ShellPolicyError(f"{name} {token.split('=', 1)[0]} can run programs or write files.")
    if token.startswith("---") and "---" in forbidden:
        raise error
    if token.startswith("--"):
        option = token.split("=", 1)[0]
        if len(option) >= 3 and any(item.startswith("--") and item.startswith(option) for item in forbidden):
            raise error
        return
    if token.startswith("-") and len(token) > 1:
        if token in forbidden or token.split("=", 1)[0] in forbidden:
            raise error
        letters = {item[1] for item in forbidden if len(item) == 2 and item[0] == "-" and item[1] != "-"}
        if letters & set(token[1:]):
            raise error


def _sed(args: list[str], context: Context) -> None:
    script = None
    for token in args:
        if token.startswith("-"):
            if not re.fullmatch(r"-[nEr]+", token):
                raise ShellPolicyError("sed accepts only -n, -E and -r with a script of p commands")
        elif script is None:
            script = token
    if script is None:
        raise ShellPolicyError("sed needs a script of p commands")
    _sed_script(script)


def _sed_script(script: str) -> None:
    """``[address[,address]]p`` commands separated by ; or newlines; addresses are
    numbers, $ or /regex/ (no unescaped '[' or newline in the regex)."""
    error = ShellPolicyError("sed scripts may contain only [address[,address]]p commands "
                             "(addresses: numbers, $ or /regex/ without '[')")
    i, n = 0, len(script)

    def address(i: int) -> int:
        if i < n and script[i].isdigit():
            while i < n and script[i].isdigit():
                i += 1
            return i
        if i < n and script[i] == "$":
            return i + 1
        if i < n and script[i] == "/":
            i += 1
            while i < n and script[i] != "/":
                if script[i] in "[\n":
                    raise error
                if script[i] == "\\":
                    if i + 1 >= n or script[i + 1] == "\n":
                        raise error
                    i += 1
                i += 1
            if i >= n:
                raise error
            return i + 1
        return i

    def blanks(i: int) -> int:
        while i < n and script[i] in " \t":
            i += 1
        return i

    while True:
        i = address(blanks(i))
        if i < n and script[i] == ",":
            after = address(i + 1)
            if after == i + 1:
                raise error
            i = after
        i = blanks(i)
        if i >= n or script[i] != "p":
            raise error
        i = blanks(i + 1)
        if i == n:
            return
        if script[i] not in ";\n":
            raise error
        i = blanks(i + 1)
        if i == n:
            return


GIT_SUBCOMMANDS = frozenset({"status", "log", "diff", "grep", "show", "rev-parse", "ls-files", "blame", "merge-base"})
# --help opens the manual (man, info or a browser) and --ext-grep may call grep(1)
# (builds differ), both other programs.
GIT_FORBIDDEN = frozenset({"--output", "--ext-diff", "--ext-grep", "--open-files-in-pager", "--show-signature",
                           "--help"})
# Only git grep's -O runs a program (it opens matches in a pager); elsewhere -O<file>
# is diff's order file. Its short options that take a value end a cluster.
GIT_GREP_VALUE_OPTIONS = frozenset("efABCm")


def _git_grep_short(token: str) -> None:
    if not token.startswith("-") or token.startswith("--"):
        return
    for letter in token[1:]:
        if letter == "O":
            raise ShellPolicyError("git -O can run programs or write files.")
        if letter in GIT_GREP_VALUE_OPTIONS:
            return  # The rest of the word is this option's value.


def _git(args: list[str], context: Context) -> None:
    if any("%G" in token for token in args):
        raise ShellPolicyError("git %G… format placeholders run gpg to verify signatures")
    seen, i = set(), 0
    while i < len(args) and args[i].startswith("-"):
        token = args[i]
        if token in ("--no-optional-locks", "--no-pager"):
            seen.add(token)
        elif token == "-C":
            if i + 1 >= len(args) or not context.cd_allowed(args[i + 1]):
                raise ShellPolicyError(f"git -C is allowed only for an absolute path without '..' "
                                       f"inside this project ({context.project})")
            i += 1
        else:
            raise ShellPolicyError(GIT_MESSAGE)
        i += 1
    if seen != {"--no-optional-locks", "--no-pager"}:
        raise ShellPolicyError(GIT_MESSAGE)
    if i >= len(args) or args[i] not in GIT_SUBCOMMANDS:
        raise ShellPolicyError("git allows only " + ", ".join(sorted(GIT_SUBCOMMANDS)))
    for token in args[i + 1:]:
        _forbidden_option("git", token, GIT_FORBIDDEN)
        if args[i] == "grep":
            _git_grep_short(token)


_DATE_FLAGS = frozenset({"-u", "--utc", "--universal", "-R", "--rfc-email", "-j"})


def _date(args: list[str], context: Context) -> None:
    error = ShellPolicyError("date accepts only display options (-u, -R, -I, -j, -r VALUE, -v VALUE) and +FORMAT; "
                             "-d, -f, -s, -t and --set can set the clock on some systems")
    i = 0
    while i < len(args):
        token = args[i]
        if (token in _DATE_FLAGS or re.fullmatch(r"-I[A-Za-z]*", token)
                or re.fullmatch(r"--iso-8601(=[A-Za-z]+)?", token) or re.fullmatch(r"--rfc-3339=[A-Za-z]+", token)
                or re.fullmatch(r"--reference=.+", token) or re.fullmatch(r"-v.+", token)
                or token.startswith("+")):
            i += 1
        elif token in ("-r", "-v"):
            if i + 1 >= len(args):
                raise error
            i += 2
        else:
            raise error


_UNIQ_LONG = frozenset({"--count", "--repeated", "--unique", "--ignore-case", "--zero-terminated"})


def _uniq(args: list[str], context: Context) -> None:
    error = ShellPolicyError("uniq reads only standard input here: use options only (a second operand is "
                             "an output file); -f, -s and -w take a number")
    i = 0
    while i < len(args):
        token = args[i]
        if (re.fullmatch(r"-[cdDuiz]+", token) or re.fullmatch(r"-[fsw][0-9]+", token) or token in _UNIQ_LONG
                or re.fullmatch(r"--(all-repeated|group)(=[A-Za-z]+)?", token)
                or re.fullmatch(r"--(skip-fields|skip-chars|check-chars)=[0-9]+", token)):
            i += 1
        elif token in ("-f", "-s", "-w") and i + 1 < len(args) and re.fullmatch(r"[0-9]+", args[i + 1]):
            i += 2
        else:
            raise error


def _command(args: list[str], context: Context) -> None:
    if len(args) < 2 or args[0] not in ("-v", "-V") or any(token.startswith("-") for token in args[1:]):
        raise ShellPolicyError("command is allowed only as command -v NAME or command -V NAME")


def _uv(args: list[str], context: Context) -> None:
    if args not in (["--version"], ["-V"]):
        raise ShellPolicyError("'uv' is not an allowed command here; run the DevFlow runner alone, without operators")


def _cd(args: list[str], context: Context) -> None:
    if len(args) != 1 or args[0].startswith("-") or not context.cd_allowed(args[0]):
        raise ShellPolicyError(f"cd is allowed only to an absolute path without '..' inside this project "
                               f"({context.project})")


COMMANDS: dict[str, Rule] = {
    **{name: Rule() for name in (
        "basename", "cat", "cmp", "comm", "cut", "df", "dirname", "du", "echo", "head", "ls", "md5sum", "nl",
        "od", "pwd", "readlink", "realpath", "sha256sum", "shasum", "stat", "tail", "tr", "type", "uname",
        "wc", "which")},
    # ugrep's dangerous options; Claude Code also diverts them, but DevFlow does not rely on that.
    "grep": Rule(frozenset({"--filter", "--pager", "--view", "--format-open", "--config", "--save-config",
                            "--query", "-Q", "-@", "---"})),
    "rg": Rule(frozenset({"--pre", "--pre-glob", "--hostname-bin", "-z", "--search-zip"})),
    "find": Rule(frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-rm",
                            "-fprint", "-fprint0", "-fprintf", "-fls"})),
    "diff": Rule(frozenset({"-l", "--paginate"})),
    "sort": Rule(frozenset({"-o", "--output", "--compress-program", "-T", "--temporary-directory"})),
    "file": Rule(frozenset({"-C", "--compile", "-z", "-Z", "--uncompress", "--uncompress-noreport",
                            "-S", "--no-sandbox", "-p", "--preserve-date"})),
    "uniq": Rule(validate=_uniq),
    "date": Rule(validate=_date),
    "command": Rule(validate=_command),
    "uv": Rule(validate=_uv),
    "cd": Rule(validate=_cd),
    "sed": Rule(validate=_sed),
    "git": Rule(frozenset(GIT_FORBIDDEN), validate=_git),
}
ALLOWED_SUMMARY = (
    "allowed commands (" + ", ".join(sorted(COMMANDS)) + "), which do not write project files or start other "
    "programs (except transient files in the trusted system temp directory), joined with |, &&, ||, ; or "
    "newlines, reading input with < file; output redirection is only to /dev/null"
)


def _leading_name(command: str) -> str:
    match = re.match(r"[ \t]*([^ \t\n;|&<>]*)", command)
    return match.group(1) if match else ""


def check_command(command: object, cd_allowed: Callable[[str], bool], project: str = "this project") -> bool:
    """True: every part is an allowed command. False: a single command that is not
    in the table (or ``uv run``): the caller checks it as the DevFlow runner.
    Raises ShellPolicyError for everything else."""
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND:
        raise ShellPolicyError("The command must be a bounded nonempty string.")
    try:
        segments = split_words(command)
    except ShellPolicyError:
        # A command that is not in the table (the runner) gets the runner's own,
        # stricter checks and messages; an allowed command gets this error.
        if _leading_name(command) not in COMMANDS or _leading_name(command) == "uv":
            return False
        raise
    first = [word.text for word in segments[0]]
    if len(segments) == 1 and (first[0] not in COMMANDS or first[:2] == ["uv", "run"]):
        return False
    context = Context(cd_allowed, project)
    for segment in segments:
        name, args = segment[0].text, segment[1:]
        rule = COMMANDS.get(name)
        if rule is None or (name == "uv" and args[:1] and args[0].text == "run"):
            raise ShellPolicyError(f"'{name}' is not an allowed command here; run the DevFlow runner alone, "
                                   "without operators")
        if rule.restricted and any(word.unsafe for word in args):
            raise ShellPolicyError(f"Quote arguments of {name} that contain characters other than letters, digits "
                                   "and -_./=,:+@%; pass glob patterns quoted, e.g. --include='*.py'")
        texts = [word.text for word in args]
        if rule.forbidden and name != "git":
            for token in texts:
                _forbidden_option(name, token, rule.forbidden)
        if rule.validate is not None:
            rule.validate(texts, context)
    return True


# -- PowerShell (the runner only) -----------------------------------------------------------

_PS_REJECT_OUTSIDE = frozenset("`$;|&(){}@<>#,%")
_PS_UNICODE_QUOTES = frozenset("‘’‚‛“”„")


def powershell_tokens(command: object) -> list[str]:
    """Words of a PowerShell runner command, where '\\' is an ordinary character.

    Quotes delimit literal text; anything PowerShell would expand, escape or
    treat as an operator is refused rather than interpreted.
    """
    if not isinstance(command, str) or not command or len(command) > MAX_COMMAND:
        raise ShellPolicyError("The command must be a bounded nonempty string.")
    if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 or ch in _PS_UNICODE_QUOTES for ch in command):
        raise ShellPolicyError("Control characters, newlines and typographic quotes are not allowed in PowerShell.")
    tokens: list[str] = []
    word: list[str] | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch in _BLANK:
            if word is not None:
                tokens.append("".join(word))
                word = None
            i += 1
            continue
        if word is None:
            word = []
        if ch in "\"'":
            end = command.find(ch, i + 1)
            if end < 0:
                raise ShellPolicyError("Unsupported PowerShell quoting.")
            content = command[i + 1:end]
            if ch == '"' and any(c in "$`" for c in content):
                raise ShellPolicyError("PowerShell expands $ and ` inside double quotes; they are not allowed.")
            if command[end + 1:end + 2] == ch:
                raise ShellPolicyError("PowerShell's doubled-quote escapes are not allowed.")
            word.append(content)
            i = end + 1
            continue
        if ch in _PS_REJECT_OUTSIDE:
            raise ShellPolicyError("PowerShell operators and special characters are not allowed outside quotes.")
        word.append(ch)
        i += 1
    if word is not None:
        tokens.append("".join(word))
    if not tokens:
        raise ShellPolicyError("Empty command.")
    return tokens
