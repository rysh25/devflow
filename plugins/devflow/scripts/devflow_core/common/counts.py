"""Bounded counts of names DevFlow does not know in advance (event types, tool names)."""
from __future__ import annotations

from .storage import writable

#: Distinct names counted per group; any further names are counted as "(other)".
MAX_NAMES = 32


def tally(counts: dict[str, int], name: object, unnamed: str) -> None:
    """Count ``name`` (cut to 64 characters) in ``counts``; ``unnamed`` when it is
    missing or a record cannot hold it (an unpaired surrogate)."""
    label = name[:64] if isinstance(name, str) and name else unnamed
    if not writable(label):
        label = unnamed
    if label not in counts and len(counts) >= MAX_NAMES:
        label = "(other)"
    counts[label] = counts.get(label, 0) + 1
