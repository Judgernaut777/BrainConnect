"""Read side of librarian triage (pure code, ZERO model calls).

The librarian records promote/reject/hold RECOMMENDATIONS into `claim_triage`.
This module only READS them, joined against the pending claims, so the human (or
interactive `/maintain`) sees a pre-triaged review queue. Acting on a
recommendation is still the human gate: `wiki promote`/`wiki reject`.
"""
from __future__ import annotations

from .db import Repo
from . import gate as gatemod, util

# A pure-code triage classifier resolves the *clear* cases so the librarian's
# model is spent only on the ambiguous residue — the "hybrid pre-filter" that
# lets a small local CPU model keep up. Thresholds are deliberately conservative:
# a wrong deterministic promote/reject costs more than deferring to the model.
PRETRIAGE_DUP_JACCARD = 0.9     # near-duplicate of a promoted claim => reject
PRETRIAGE_PROMOTE_MARGIN = 0.10  # confidence within this of the gate threshold
                                 # (and otherwise clean) => promote near-miss
PRETRIAGE_MIN_TOKENS = 2        # fewer significant tokens => degenerate => reject


def _near_duplicate_of_promoted(repo: Repo, claim) -> int | None:
    """The id of a PROMOTED claim this one near-duplicates (Jaccard >= threshold),
    or None. Fails soft: any FTS error returns None so the case is left for the
    model rather than deterministically rejected on a shaky signal."""
    try:
        rows = repo.q(
            """SELECT c.id, c.text FROM claims_fts f JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status='promoted' AND c.id != ?""",
            (util.fts_or_query(claim["text"]), claim["id"]))
    except Exception:
        return None
    for r in rows:
        if util.jaccard(claim["text"], r["text"]) >= PRETRIAGE_DUP_JACCARD:
            return r["id"]
    return None


def pretriage(repo: Repo, claim) -> dict:
    """Deterministic triage recommendation for one pending claim (ZERO model calls).

    Returns {recommendation, reason, confidence, decided}. When `decided` is True
    the recommendation is a clear-cut rule outcome and the librarian records it
    without calling the model; when False the claim is genuinely ambiguous
    (contradiction, conflict, mid confidence, uncorroborated) and is left to the
    model. Advisory only, exactly like the model pass — never mutates status.

    Reuses the gate's own hold logic (`gate.hold_reasons`, fail-closed) and
    `util.jaccard`, so rule and gate can never silently drift apart.
    """
    text = (claim["text"] or "").strip()

    # reject (decided): degenerate/too-short text — not a durable fact.
    if len(util.tokens(text)) < PRETRIAGE_MIN_TOKENS:
        return {"recommendation": "reject", "confidence": 0.9, "decided": True,
                "reason": "degenerate claim text (too few tokens to be a durable fact)"}

    # reject (decided): near-duplicate of a claim we already promoted.
    dup = _near_duplicate_of_promoted(repo, claim)
    if dup is not None:
        return {"recommendation": "reject", "confidence": 0.9, "decided": True,
                "reason": f"near-duplicate of promoted claim #{dup} (already recorded)"}

    reasons = gatemod.hold_reasons(repo, claim)

    # promote (decided): the gate would already promote it (nothing held it).
    if not reasons:
        return {"recommendation": "promote", "confidence": 0.9, "decided": True,
                "reason": "auto-gate promotable: confident, corroborated, uncontested"}

    # promote (decided): the ONLY thing holding it is a soft confidence near-miss.
    # A single confidence hold reason implies the claim is corroborated, carries
    # no open contradiction, and does not conflict with a promoted claim — those
    # would each add their own reason. So a near-miss here is safe to recommend.
    thresh = float(repo.cfg.gate("auto_promote_confidence"))
    if (len(reasons) == 1 and reasons[0].startswith("confidence")
            and claim["confidence"] >= thresh - PRETRIAGE_PROMOTE_MARGIN):
        return {"recommendation": "promote", "confidence": 0.75, "decided": True,
                "reason": (f"corroborated and uncontested; extractor confidence "
                           f"{claim['confidence']:.2f} just below the auto-gate "
                           f"threshold {thresh:.2f}")}

    # residue (undecided): open contradiction / conflict / genuinely mid
    # confidence / uncorroborated — leave it for the model to judge.
    return {"recommendation": "hold", "confidence": 0.0, "decided": False,
            "reason": "ambiguous — deferred to the model: " + "; ".join(reasons)}


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
