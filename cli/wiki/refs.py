"""Prefixed entity refs — `claim_4`, `candidate_12`, `source_7`.

Internal ids stay INTEGER (load-bearing for FTS5 `content_rowid` and every foreign
key). The API, MCP tools and CLI speak prefixed refs instead, which is what lets
`wiki promote 12` (a claim, the pre-ledger morning-gate path) and
`wiki promote candidate_12` (the ledger path) coexist unambiguously.

A bare integer is accepted everywhere a ref is, and means "of the expected kind".
"""
from __future__ import annotations

import re

CLAIM = "claim"
CANDIDATE = "candidate"
SOURCE = "source"

_KINDS = (CLAIM, CANDIDATE, SOURCE)
_PATTERN = re.compile(r"^(%s)_(\d+)$" % "|".join(_KINDS))


class RefError(ValueError):
    pass


def fmt(kind: str, rowid: int) -> str:
    """`fmt("claim", 4) -> "claim_4"`."""
    if kind not in _KINDS:
        raise RefError(f"unknown ref kind {kind!r}")
    return f"{kind}_{rowid}"


def claim(rowid: int) -> str:
    return fmt(CLAIM, rowid)


def candidate(rowid: int) -> str:
    return fmt(CANDIDATE, rowid)


def source(rowid: int) -> str:
    return fmt(SOURCE, rowid)


def kind_of(text: str) -> str | None:
    """The kind a ref names, or None for a bare integer / unparseable text."""
    m = _PATTERN.match((text or "").strip())
    return m.group(1) if m else None


def parse(text: str | int, expect: str) -> int:
    """Resolve a ref (or bare integer) of kind `expect` to its rowid.

    Raises RefError when the ref names a different kind — mixing up a claim and a
    candidate id must be loud, since they index different tables and promotion of
    the wrong one is unrecoverable.
    """
    if expect not in _KINDS:
        raise RefError(f"unknown ref kind {expect!r}")
    if isinstance(text, int):
        return text
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    m = _PATTERN.match(text)
    if not m:
        raise RefError(
            f"not a valid ref: {text!r} (expected {expect}_<n> or a bare integer)")
    got, rowid = m.group(1), int(m.group(2))
    if got != expect:
        raise RefError(f"expected a {expect} ref, got {text!r}")
    return rowid
