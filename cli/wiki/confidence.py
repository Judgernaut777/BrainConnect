"""Confidence, in both representations (LEDGER_SPEC.md §5.3).

The auto-gate (`gate.py`) and the contradiction pre-adjudicator compare confidence
*numerically*, so `claims.confidence REAL` stays. The ledger API speaks the ordinal
label. This module is the only place the two are mapped, so they cannot drift.

Ordering matters: `rank()` is what profile minimum-confidence filters compare on.
"""
from __future__ import annotations

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
VERIFIED = "verified"

# Ascending. Index in this tuple is the ordinal rank.
LABELS = (LOW, MEDIUM, HIGH, VERIFIED)

# The numeric a label promotes to. `high` sits exactly on gate.auto_promote_confidence
# (0.85) so a human promoting at `high` and the auto-gate agree on the same claim.
_TO_NUMERIC = {LOW: 0.3, MEDIUM: 0.6, HIGH: 0.85, VERIFIED: 0.95}

# Lower bound of each label when deriving one from a number. Descending scan.
_FROM_NUMERIC = ((0.95, VERIFIED), (0.85, HIGH), (0.5, MEDIUM), (0.0, LOW))


class ConfidenceError(ValueError):
    pass


def rank(label: str) -> int:
    try:
        return LABELS.index(label)
    except ValueError:
        raise ConfidenceError(
            f"unknown confidence {label!r}; expected one of {', '.join(LABELS)}")


def to_numeric(label: str) -> float:
    if label not in _TO_NUMERIC:
        raise ConfidenceError(
            f"unknown confidence {label!r}; expected one of {', '.join(LABELS)}")
    return _TO_NUMERIC[label]


def from_numeric(value: float | None) -> str:
    """Derive a label from a numeric confidence. Pre-ledger claims have no label
    stored, so recall derives one rather than reporting `None`."""
    if value is None:
        return LOW
    for floor, label in _FROM_NUMERIC:
        if value >= floor:
            return label
    return LOW


def label_of(row) -> str:
    """The label for a claims row, stored or derived. Accepts a sqlite3.Row."""
    try:
        stored = row["confidence_label"]
    except (KeyError, IndexError):
        stored = None
    if stored in _TO_NUMERIC:
        return stored
    try:
        return from_numeric(row["confidence"])
    except (KeyError, IndexError):
        return LOW


def at_least(label: str, minimum: str) -> bool:
    return rank(label) >= rank(minimum)
