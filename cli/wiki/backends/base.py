"""The retrieval backend contract (LEDGER_SPEC.md §8).

WikiBrain owns trust and provenance. A backend owns search and indexing
sophistication. The seam between them is deliberately narrow, and its shape is
what makes the trust boundary structural rather than a matter of discipline:

    **a backend returns ids and scores, never claim content or status.**

Recall re-reads every authoritative field (status, scope, confidence, tags) from
the ledger by id. A backend that hallucinates a status, ignores a scope hint, or
returns a rejected claim's id cannot widen trust — the worst it can do is waste a
slot in the pack, which the trust filter then drops. That is why `search()` returns
`BackendCandidate` rows carrying only `(kind, id, score, rank)`.

`hints` are an optimisation. A backend may honour them to fetch fewer rows, or
ignore them entirely; WikiBrain re-applies every predicate afterwards regardless.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..scopes import Scope

# Kinds a backend may be asked to search over.
CLAIM = "claim"
SUMMARY = "summary"


class BackendError(Exception):
    """A backend could not serve the request. Recall degrades or surfaces this;
    it never silently returns an empty pack as if nothing matched."""


@dataclass(frozen=True)
class BackendSearchRequest:
    """What WikiBrain asks a backend for.

    `limit` is already over-fetched relative to the caller's `max_items` (see
    `[retrieval] overfetch`), because trust/scope filtering runs *after* retrieval
    and will discard some of what comes back.
    """
    query: str
    limit: int = 32
    kinds: tuple[str, ...] = (CLAIM,)
    # --- hints: advisory, never load-bearing for trust ---
    scopes: tuple[Scope, ...] = ()
    statuses: tuple[str, ...] = ("promoted",)


@dataclass(frozen=True)
class BackendCandidate:
    """One retrieval hit. Deliberately content-free: an id and a ranking signal."""
    kind: str
    id: int
    score: float = 0.0
    rank: int = 0


@dataclass
class BackendSearchResult:
    backend: str
    candidates: list[BackendCandidate] = field(default_factory=list)
    mode: str = ""            # e.g. "fts", "hybrid", "vector"
    degraded: str | None = None  # set when the backend fell back (e.g. no [semantic])


@runtime_checkable
class RetrievalBackend(Protocol):
    @property
    def backend_name(self) -> str: ...

    def index_source(self, source_id: int) -> None: ...

    def index_claim(self, claim_id: int) -> None: ...

    def search(self, request: BackendSearchRequest) -> BackendSearchResult: ...

    def delete_or_deindex(self, entity_id: str) -> None: ...

    def health(self) -> dict: ...
