"""Phase 5 two-speed gate (BUILD_SPEC.md §7.1).

Auto-promotes a pending claim iff ALL hold:
  - confidence >= gate.auto_promote_confidence
  - no open contradiction touches it
  - corroborated: >= 2 independent sources assert it, OR origin == 'clip'
  - it does not conflict with a promoted claim (approximated via the
    contradiction check, plus an explicit opposite-polarity FTS scan)
Everything else stays pending for the human. Pure code; no model calls.
"""
from __future__ import annotations

from .db import Repo
from . import render, util

CORROBORATION_JACCARD = 0.5


class GateCheckError(Exception):
    """A safety check's FTS query failed. Callers must fail closed (hold the
    claim) rather than treat the failure as "check passed"."""


def _has_open_contradiction(repo: Repo, cid: int) -> bool:
    return bool(repo.one(
        "SELECT 1 FROM contradictions WHERE status='open' AND (claim_a=? OR claim_b=?)",
        (cid, cid)))


def _corroborating_sources(repo: Repo, claim) -> int:
    """Distinct source ids (incl. this claim's) asserting a similar fact.

    Raises GateCheckError (after logging) if the FTS query fails, so the
    caller holds the claim instead of silently treating it as uncorroborated-
    but-otherwise-fine — same fail-closed contract as `_conflicts_with_promoted`.
    """
    try:
        rows = repo.q(
            """SELECT c.id, c.text, c.source_id FROM claims_fts f
               JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status IN ('promoted','pending')""",
            (util.fts_or_query(claim["text"]),))
    except Exception as e:
        repo.log("gate", f"corroboration FTS query failed for claim #{claim['id']}: {e}")
        raise GateCheckError(f"corroboration query failed: {e}") from e
    sources = {claim["source_id"]}
    for r in rows:
        if r["id"] == claim["id"]:
            continue
        if util.jaccard(claim["text"], r["text"]) >= CORROBORATION_JACCARD:
            sources.add(r["source_id"])
    return len(sources)


def _conflicts_with_promoted(repo: Repo, claim) -> bool:
    """Raises GateCheckError (after logging) if the FTS query fails, so the
    caller fails closed (holds the claim) instead of promoting on the
    assumption that "no rows" means "no conflict"."""
    try:
        rows = repo.q(
            """SELECT c.text FROM claims_fts f JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status='promoted'""",
            (util.fts_or_query(claim["text"]),))
    except Exception as e:
        repo.log("gate", f"conflict FTS query failed for claim #{claim['id']}: {e}")
        raise GateCheckError(f"conflict query failed: {e}") from e
    return any(util.polarity_conflict(claim["text"], r["text"]) for r in rows)


def hold_reasons(repo: Repo, claim) -> list[str]:
    """Why the auto-gate would HOLD this pending claim (empty list => auto-promotable).
    Read-only: no mutation, so the librarian triage pass can reuse it. Safety
    checks fail CLOSED — an FTS error becomes a hold reason, never a silent pass."""
    thresh = float(repo.cfg.gate("auto_promote_confidence"))
    reasons: list[str] = []
    if claim["confidence"] < thresh:
        reasons.append(f"confidence {claim['confidence']:.2f} < {thresh}")
    if _has_open_contradiction(repo, claim["id"]):
        reasons.append("open contradiction")
    if claim["origin"] != "clip":
        try:
            if _corroborating_sources(repo, claim) < 2:
                reasons.append("not corroborated (need 2 sources or origin=clip)")
        except GateCheckError as e:
            reasons.append(f"corroboration check failed — held fail-closed: {e}")
    try:
        if _conflicts_with_promoted(repo, claim):
            reasons.append("conflicts with promoted claim")
    except GateCheckError as e:
        reasons.append(f"conflict check failed — held fail-closed: {e}")
    return reasons


def gate(repo: Repo) -> dict:
    pending = repo.q("SELECT * FROM claims WHERE status = 'pending' ORDER BY id")
    promoted, held = [], []
    for c in pending:
        reasons = hold_reasons(repo, c)
        if reasons:
            held.append({"id": c["id"], "reasons": reasons})
            continue
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (util.now_iso(), c["id"]))
        render.mark_dirty_for_claim(repo, c["id"])
        promoted.append(c["id"])

    if promoted:
        repo.finalize("gate", f"auto-promoted {len(promoted)}; held {len(held)}")
    else:
        repo.conn.commit()
    return {"promoted": promoted, "held": held}
