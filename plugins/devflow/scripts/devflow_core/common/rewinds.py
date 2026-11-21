"""Detect a rewound Claude Code transcript from record structure alone.

A Claude Code transcript is append-only. Rewinding the conversation (Esc Esc
or ``/rewind``) keeps the abandoned branch in the file and appends the new
branch; the new branch's first record points back at an earlier record. This
module reads only chain structure: ``uuid``/``parentUuid`` of ``user``,
``assistant``, ``attachment`` and ``system`` records, whether a record is a
reply (``assistant``), a tool result, or carries a ``tool_use`` id. Message
content never leaves the line parser.

The same rule serves the interview seal (``plan/transcript.py`` feeds the lines
it reads) and answer verification (``common/sessions.py`` feeds a reverse-read
range in file order).
"""
from __future__ import annotations

import json
import math
from typing import NamedTuple

#: Record types that join a transcript's parentUuid chain.
CHAIN_TYPES = frozenset({"user", "assistant", "attachment", "system"})


def _invalid_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


class Line(NamedTuple):
    """The structure of one transcript line: everything a caller feeds in order.

    ``unreadable`` marks a line that is not UTF-8 or not JSON; callers decide
    what it means. Whitespace-only lines, non-object JSON, and objects that are
    not chain records parse to the all-default value and are skipped.
    """
    unreadable: bool = False
    chain: bool = False
    uuid: str | None = None
    parent: str | None = None
    assistant: bool = False
    tool_result: bool = False
    tool_use_ids: tuple[str, ...] = ()


def record_line(record: object) -> Line:
    """The chain structure of an already-decoded transcript record."""
    if not isinstance(record, dict) or record.get("isSidechain"):
        return Line()
    kind, uuid = record.get("type"), record.get("uuid")
    if not isinstance(kind, str) or kind not in CHAIN_TYPES or not isinstance(uuid, str) or not uuid:
        return Line()
    parent = record.get("parentUuid")
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else ()
    return Line(
        chain=True, uuid=uuid, parent=parent if isinstance(parent, str) else None,
        assistant=kind == "assistant",
        tool_result=kind == "user" and any(b.get("type") == "tool_result" for b in blocks),
        tool_use_ids=tuple(b["id"] for b in blocks
                           if kind == "assistant" and b.get("type") == "tool_use"
                           and isinstance(b.get("id"), str)))


def parse_line(line: str) -> Line:
    """Reduce one transcript line to the fields the rewind rule reads."""
    if not line.strip():
        return Line()
    try:
        record = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite)
    except (ValueError, RecursionError):
        return Line(unreadable=True)
    return record_line(record)


def parse_bytes(raw: bytes) -> Line:
    """``parse_line`` for a raw line; undecodable bytes are an unreadable line."""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return Line(unreadable=True)
    return parse_line(text)


class BranchTracker:
    """Feed ``Line`` values in file order; report rewinds and discarded records.

    A branch head is the second or later chain record that is not a copy (its
    ``uuid`` was already fed), not an ``assistant`` or tool-result record, whose
    ``parentUuid`` is set and differs from the previous chain record's ``uuid``.
    Records without a ``parentUuid`` (compaction boundaries) are never heads.
    The discarded range is the chain records after the head's parent record (or
    the range start when the parent is outside the fed range) up to the head.
    A rewind is a discarded range holding a reply that is not a copy.
    """

    def __init__(self):
        self._fed = 0
        self._chain = 0
        self._prev_uuid = None
        self._seen = set()
        self._uuid_pos = {}       # A head's parent resolves to the first record with that uuid:
                                  # a copy inside the discarded range must not hide abandoned replies.
        self._heads = []          # (head position, parent position or None)
        self._assistants = set()  # Chain positions of replies that are not copies.
        self._tool_use = {}       # tool_use id -> position of the latest non-copy record (copies are not recorded).
        self.unreadable = []      # Fed-line indexes the caller could not parse.

    def feed(self, line: Line) -> None:
        index = self._fed
        self._fed += 1
        if line.unreadable:
            self.unreadable.append(index)
            return
        if not line.chain:
            return
        position = self._chain
        self._chain += 1
        duplicate = line.uuid in self._seen
        self._seen.add(line.uuid)
        self._uuid_pos.setdefault(line.uuid, position)
        if line.assistant and not duplicate:
            self._assistants.add(position)
            for tool_id in line.tool_use_ids:
                self._tool_use[tool_id] = position
        previous = self._prev_uuid
        self._prev_uuid = line.uuid  # Copies and parent-less records chain on, too.
        if (position == 0 or duplicate or line.assistant or line.tool_result
                or line.parent is None or line.parent == previous):
            return
        self._heads.append((position, self._uuid_pos.get(line.parent)))

    @staticmethod
    def _discarded_range(head: int, parent: int | None):
        """Chain positions abandoned by a head: after its parent, before the head."""
        start = parent + 1 if parent is not None else 0
        return range(max(start, 0), head)

    def rewound(self) -> int | None:
        """The first branch head whose discarded range holds a reply, or None."""
        for head, parent in self._heads:
            if any(position in self._assistants for position in self._discarded_range(head, parent)):
                return head
        return None

    def discarded(self, tool_use_ids) -> set:
        """Ids whose ``tool_use`` record sits inside some discarded range.

        Such a record is a non-copy reply itself, so its range is always a
        rewind. Ids not found in the fed range are not reported; the caller
        treats "not found" itself.
        """
        found = set()
        for tool_id in tool_use_ids:
            position = self._tool_use.get(tool_id)
            if position is None:
                continue
            if any(head > position and (parent is None or parent < position)
                   for head, parent in self._heads):
                found.add(tool_id)
        return found
