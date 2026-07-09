"""The SQLite FTS5 backend — the one WikiBrain ships with.

Wraps the existing `search.py` (FTS5 + bm25) and, when the optional `[semantic]`
extra is installed, `embed.py`'s reciprocal-rank fusion. Both already exist and are
already tested; this adapter's whole job is to speak `BackendSearchRequest` and
hand back ids.

Indexing is a **no-op**: `claims_fts` is an FTS5 external-content table kept in
sync by AFTER INSERT/UPDATE/DELETE triggers on `claims` (see `schema.py`). The DB
*is* the index. A remote backend (Graphiti, Qdrant) will do real work here.

Zero model calls. The `[semantic]` extra runs a local embedding model, never a
remote API.
"""
from __future__ import annotations

from ..db import Repo
from .. import embed as embedmod
from .. import search as searchmod
from .base import (CLAIM, SUMMARY, BackendCandidate, BackendError,
                   BackendSearchRequest, BackendSearchResult)

NAME = "sqlite_fts"


class SqliteFtsBackend:
    def __init__(self, repo: Repo):
        self.repo = repo

    @property
    def backend_name(self) -> str:
        return NAME

    # --- indexing: the FTS triggers already did it ---------------------------
    def index_source(self, source_id: int) -> None:
        """No-op: summaries_fts is trigger-maintained."""

    def index_claim(self, claim_id: int) -> None:
        """No-op: claims_fts is trigger-maintained."""

    def delete_or_deindex(self, entity_id: str) -> None:
        """No-op: the FTS delete trigger fires with the row."""

    # --- search --------------------------------------------------------------
    def search(self, request: BackendSearchRequest) -> BackendSearchResult:
        if not (request.query or "").strip():
            raise BackendError("empty query")

        # `promoted_only` is only a hint here — it lets FTS skip rows we would
        # discard anyway. When the caller wants pending material in the running
        # (include_pending), we must NOT push the filter down, or the pending rows
        # never surface as candidates in the first place.
        promoted_only = tuple(request.statuses) == ("promoted",)

        want_claims = CLAIM in request.kinds
        want_summaries = SUMMARY in request.kinds

        # Hybrid only when semantic search can actually contribute. An empty
        # `embeddings` table makes hybrid_search return the FTS ranking verbatim,
        # so reporting "hybrid" there would overstate the retrieval quality the
        # pack was built from — `mode` is a claim about how results were found.
        if want_claims:
            rows, mode, degraded = self._claims(request, promoted_only)
        else:
            rows, mode, degraded = [], "fts", None

        if want_summaries:
            rows = rows + [r for r in searchmod.search(
                               self.repo, request.query,
                               promoted_only=promoted_only, limit=request.limit)
                           if r.get("kind") == SUMMARY]

        candidates: list[BackendCandidate] = []
        for i, r in enumerate(rows[:request.limit]):
            score = r.get("rrf") or r.get("score") or 0.0
            candidates.append(BackendCandidate(
                kind=r.get("kind", CLAIM), id=r["id"], score=float(score), rank=i))
        return BackendSearchResult(backend=NAME, candidates=candidates,
                                   mode=mode, degraded=degraded)

    # Recall is RANKED retrieval, not a precise lookup. A caller assembling a
    # context pack asks a question ("refresh token expiry design decisions"), and
    # ANDing every term matches nothing — an empty pack that reads as "the ledger
    # knows nothing" when it means "your query was a sentence". OR + bm25 ranks
    # claims matching more terms first, and the trust filter still runs after.
    # `wiki search` keeps AND: there, the user typed exactly what they meant.
    MATCH_ALL = False

    def _fts(self, request: BackendSearchRequest, promoted_only: bool) -> list[dict]:
        return [r for r in searchmod.search(
                    self.repo, request.query, promoted_only=promoted_only,
                    limit=request.limit, match_all=self.MATCH_ALL)
                if r.get("kind") == CLAIM]

    def _claims(self, request: BackendSearchRequest,
                promoted_only: bool) -> tuple[list[dict], str, str | None]:
        """Rank claims, preferring hybrid retrieval when it can contribute."""
        if not self.repo.one("SELECT 1 FROM embeddings LIMIT 1"):
            return (self._fts(request, promoted_only), "fts",
                    "no embeddings indexed; keyword-only (run `wiki embed --all`)")
        try:
            rows = embedmod.hybrid_search(self.repo, request.query, k=request.limit,
                                          promoted_only=promoted_only,
                                          match_all=self.MATCH_ALL)
            return rows, "hybrid", None
        except embedmod.EmbedError as e:
            return (self._fts(request, promoted_only), "fts",
                    f"semantic search unavailable ({e}); keyword-only")

    # --- health --------------------------------------------------------------
    def health(self) -> dict:
        claims = self.repo.one("SELECT COUNT(*) AS n FROM claims")["n"]
        promoted = self.repo.one(
            "SELECT COUNT(*) AS n FROM claims WHERE status='promoted'")["n"]
        try:
            embedded = self.repo.one("SELECT COUNT(*) AS n FROM embeddings")["n"]
        except Exception:
            embedded = 0
        return {
            "backend": NAME,
            "ok": True,
            "indexed_claims": claims,
            "promoted_claims": promoted,
            "embedded_claims": embedded,
            "semantic": embedded > 0,
            "note": "FTS5 external-content index maintained by triggers; the DB is the index",
        }
