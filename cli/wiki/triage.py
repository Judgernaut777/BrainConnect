"""Read side of librarian triage (pure code, ZERO model calls).

The librarian records promote/reject/hold RECOMMENDATIONS into `claim_triage`.
This module only READS them, joined against the pending claims, so the human (or
interactive `/maintain`) sees a pre-triaged review queue. Acting on a
recommendation is still the human gate: `wiki promote`/`wiki reject`.
"""
from __future__ import annotations

from .db import Repo


def listing(repo: Repo, *, recommendation: str | None = None) -> list[dict]:
    """Pending claims with their latest librarian triage recommendation (if any).
    Optionally filter to a single recommendation (promote|reject|hold)."""
    sql = """
        SELECT c.id, c.text, c.confidence, c.origin, c.source_id,
               s.title AS source_title,
               t.recommendation, t.reason, t.confidence AS triage_confidence,
               t.model, t.created_at AS triaged_at
        FROM claims c
        JOIN sources s ON s.id = c.source_id
        LEFT JOIN claim_triage t ON t.claim_id = c.id
        WHERE c.status = 'pending'
    """
    params: tuple = ()
    if recommendation:
        sql += " AND t.recommendation = ?"
        params = (recommendation,)
    sql += " ORDER BY (t.recommendation IS NULL), t.recommendation, c.id"
    return [dict(r) for r in repo.q(sql, params)]


def summary(repo: Repo) -> dict:
    """Counts of pending claims by recommendation (untriaged bucketed as None)."""
    out = {"promote": 0, "reject": 0, "hold": 0, "untriaged": 0}
    for r in repo.q(
        """SELECT COALESCE(t.recommendation, 'untriaged') AS rec, COUNT(*) AS n
           FROM claims c LEFT JOIN claim_triage t ON t.claim_id = c.id
           WHERE c.status = 'pending' GROUP BY rec"""):
        out[r["rec"]] = r["n"]
    return out
