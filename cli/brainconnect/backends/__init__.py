"""Retrieval backend registry (LEDGER_SPEC.md §8).

`[retrieval] backend` in config.toml selects the adapter. Only `sqlite_fts` ships;
the rest are named here so an unknown name fails with a message that says whether
it is a typo or an unbuilt adapter, rather than silently falling back to FTS and
quietly changing what recall means.
"""
from __future__ import annotations

from ..db import Repo
from .base import (CLAIM, SUMMARY, BackendCandidate, BackendError,
                   BackendSearchRequest, BackendSearchResult, RetrievalBackend)
from .sqlite_fts import NAME as SQLITE_FTS, SqliteFtsBackend

__all__ = [
    "CLAIM", "SUMMARY", "BackendCandidate", "BackendError", "BackendSearchRequest",
    "BackendSearchResult", "RetrievalBackend", "SQLITE_FTS", "SqliteFtsBackend",
    "get_backend", "available", "PLANNED",
]

_BUILDERS = {
    SQLITE_FTS: SqliteFtsBackend,
}

# Named in the spec, not yet implemented. Listed so the error message can say so.
PLANNED = ("graphiti", "cognee", "qdrant", "chroma", "llamaindex")


def available() -> tuple[str, ...]:
    return tuple(sorted(_BUILDERS))


def get_backend(repo: Repo, name: str | None = None) -> RetrievalBackend:
    """Resolve the configured backend. Fails loudly on an unknown name."""
    name = name or repo.cfg.retrieval_cfg("backend") or SQLITE_FTS
    builder = _BUILDERS.get(name)
    if builder is None:
        if name in PLANNED:
            raise BackendError(
                f"retrieval backend {name!r} is specified in LEDGER_SPEC.md §8 but "
                f"not implemented yet; available now: {', '.join(available())}")
        raise BackendError(
            f"unknown retrieval backend {name!r}; available: {', '.join(available())}")
    return builder(repo)
