"""Scopes — the blast radius of a durable memory (LEDGER_SPEC.md §5.5).

Every claim is scoped. A repo-specific claim must never leak into another repo's
recall, while `global` facts stay visible everywhere. The matching rule lives in
`matches()` and is the single place recall consults.

Pure code, zero model calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# The closed vocabulary. `global` is the only type with an empty scope_id.
SCOPE_TYPES = (
    "global", "user", "project", "repo", "task",
    "manager", "worker", "model", "tool",
)

GLOBAL_TYPE = "global"


class ScopeError(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    scope_type: str
    scope_id: str = ""

    def __post_init__(self):
        if self.scope_type not in SCOPE_TYPES:
            raise ScopeError(
                f"unknown scope type {self.scope_type!r}; "
                f"expected one of {', '.join(SCOPE_TYPES)}")
        if self.scope_type == GLOBAL_TYPE and self.scope_id:
            raise ScopeError("the global scope takes no scope_id")
        if self.scope_type != GLOBAL_TYPE and not self.scope_id:
            raise ScopeError(f"scope type {self.scope_type!r} requires a scope_id")

    def __str__(self) -> str:
        return self.scope_type if self.scope_type == GLOBAL_TYPE \
            else f"{self.scope_type}:{self.scope_id}"

    def as_dict(self) -> dict:
        return {"scope_type": self.scope_type, "scope_id": self.scope_id}


GLOBAL = Scope(GLOBAL_TYPE)


def parse(text: str) -> Scope:
    """Parse `repo:my-app`, `model:qwen2.5-coder-14b`, or bare `global`.

    The scope_id may itself contain colons (a URI, say), so only the first is a
    separator.
    """
    text = (text or "").strip()
    if not text:
        raise ScopeError("empty scope")
    if ":" not in text:
        return Scope(text)
    kind, _, ident = text.partition(":")
    return Scope(kind.strip(), ident.strip())


def from_dict(d: dict) -> Scope:
    return Scope(d.get("scope_type", ""), d.get("scope_id", "") or "")


def dumps(scopes: list[Scope]) -> str:
    """JSON for the `proposed_scopes` column."""
    return json.dumps([s.as_dict() for s in scopes], sort_keys=True)


def loads(raw: str | None) -> list[Scope]:
    out = []
    for d in json.loads(raw or "[]"):
        try:
            out.append(from_dict(d))
        except ScopeError:
            continue  # stored data is data: a bad scope is skipped, never raised
    return out


def matches(claim_scope: Scope, requested: list[Scope]) -> bool:
    """The recall scope rule (LEDGER_SPEC.md §5.5).

    A claim is in scope iff it is `global`, or its scope is among those the caller
    asked for. Asking for nothing therefore yields global facts only — never the
    whole ledger — which is what keeps a repo claim out of an unscoped recall.
    """
    if claim_scope.scope_type == GLOBAL_TYPE:
        return True
    return any(claim_scope == r for r in requested)


def sql_predicate(requested: list[Scope]) -> tuple[str, list]:
    """The same rule as a SQL fragment, for callers that filter in the DB.

    Returns `(fragment, params)` where fragment is a parenthesised boolean over the
    columns `scope_type` / `scope_id` of whatever table is in scope.
    """
    if not requested:
        return "(scope_type = 'global')", []
    ors, params = ["scope_type = 'global'"], []
    for s in requested:
        if s.scope_type == GLOBAL_TYPE:
            continue
        ors.append("(scope_type = ? AND scope_id = ?)")
        params += [s.scope_type, s.scope_id]
    return "(" + " OR ".join(ors) + ")", params
