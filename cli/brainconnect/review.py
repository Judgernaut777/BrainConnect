"""Human/maintain-pass operations: promote, reject, supersede, contradictions,
escalations, summary promotion. These are the judgment levers the morning gate
(maintain.md) pulls; the CLI only performs the bookkeeping.
"""
from __future__ import annotations

from .db import Repo
from . import gate as gatemod, render, util


def _require_claim(repo: Repo, cid: int):
    row = repo.one("SELECT * FROM claims WHERE id = ?", (cid,))
    if not row:
        raise SystemExit(f"error: no claim #{cid}")
    return row


# State-transition guards: a claim already settled by supersede (or already
# rejected) may not be silently re-promoted, and a superseded claim — which
# already has a replacement of record — may not be re-rejected either. The
# common pending->promoted / pending->rejected paths stay open, as does
# promoted->rejected (reviewers walking back a promotion).
_PROMOTE_BLOCKED_FROM = {"rejected", "superseded"}
_REJECT_BLOCKED_FROM = {"superseded"}


def promote(repo: Repo, cids: list[int]) -> None:
    for cid in cids:
        row = _require_claim(repo, cid)
        if row["status"] in _PROMOTE_BLOCKED_FROM:
            raise SystemExit(
                f"error: claim #{cid} is {row['status']}; cannot promote "
                "(use supersede for superseded claims)")
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (util.now_iso(), cid))
        render.mark_dirty_for_claim(repo, cid)
    repo.finalize("promote", "claims " + ",".join(f"#{c}" for c in cids))


def reject(repo: Repo, cids: list[int]) -> None:
    for cid in cids:
        row = _require_claim(repo, cid)
        if row["status"] in _REJECT_BLOCKED_FROM:
            raise SystemExit(f"error: claim #{cid} is {row['status']}; cannot reject")
        repo.ex("UPDATE claims SET status='rejected', reviewed_at=? WHERE id=?",
                (util.now_iso(), cid))
        render.mark_dirty_for_claim(repo, cid)
    repo.finalize("reject", "claims " + ",".join(f"#{c}" for c in cids))


def supersede(repo: Repo, old_id: int, new_id: int, *, reason: str = "",
              reviewer: str | None = None) -> None:
    """Retire `old_id` in favour of `new_id`.

    `claims.superseded_by` stays the denormalised pointer the renderer and search
    already read; the `supersessions` row carries the reason and the reviewer that
    a single column cannot (LEDGER_SPEC.md §5.6).
    """
    if old_id == new_id:
        raise SystemExit(f"error: claim #{old_id} cannot supersede itself")
    _require_claim(repo, old_id)  # validate existence; raises if missing
    new = _require_claim(repo, new_id)
    now = util.now_iso()
    # session/* and autoresearch claims may never auto-supersede; this is a
    # human/maintain action so it is allowed, but we record provenance.
    repo.ex("UPDATE claims SET status='superseded', superseded_by=?, reviewed_at=? WHERE id=?",
            (new_id, now, old_id))
    if new["status"] == "pending":
        repo.ex("UPDATE claims SET status='promoted', reviewed_at=? WHERE id=?",
                (now, new_id))
    repo.ex(
        """INSERT OR REPLACE INTO supersessions
             (old_claim_id, new_claim_id, reason, created_at, created_by)
           VALUES (?,?,?,?,?)""",
        (old_id, new_id, reason or "", now, reviewer))
    render.mark_dirty_for_claim(repo, old_id)
    render.mark_dirty_for_claim(repo, new_id)
    suffix = f": {reason}" if reason else ""
    repo.finalize("supersede", f"#{old_id} superseded by #{new_id}{suffix}")


def promote_summary(repo: Repo, source_id: int) -> None:
    row = repo.one("SELECT * FROM summaries WHERE source_id = ?", (source_id,))
    if not row:
        raise SystemExit(f"error: no summary for source #{source_id}")
    repo.ex("UPDATE summaries SET status='promoted' WHERE source_id = ?", (source_id,))
    render.mark_dirty_for_source(repo, source_id)
    repo.finalize("promote-summary", f"source #{source_id}")


# --- contradictions ---------------------------------------------------------
def contradiction_list(repo: Repo, status: str | None = "open") -> list:
    if status:
        return repo.q("SELECT * FROM contradictions WHERE status = ? ORDER BY id", (status,))
    return repo.q("SELECT * FROM contradictions ORDER BY id")


def contradiction_propose(repo: Repo, cid: int, proposal: str) -> None:
    if not repo.one("SELECT 1 FROM contradictions WHERE id = ?", (cid,)):
        raise SystemExit(f"error: no contradiction #{cid}")
    repo.ex("UPDATE contradictions SET proposal = ? WHERE id = ?", (proposal, cid))
    repo.finalize("contradiction-propose", f"#{cid}")


# A pure-code contradiction pre-filter (ZERO model calls) resolves the clear
# cases so the librarian's model is spent only on genuinely even pairs — the
# "newer AND more specific AND corroborated wins" heuristic from BUILD_SPEC. It
# only DRAFTS a proposal (via contradiction_propose); it never resolves or
# supersedes, exactly like the model pass.
PREADJUDICATE_MIN_CORROBORATION = 2  # the winning side must itself be corroborated


def _specificity(repo: Repo, claim) -> tuple[int, int]:
    """Specificity proxy: (# linked entities, text length). More entities, then
    longer text, reads as the more specific claim."""
    n = repo.one("SELECT COUNT(*) AS n FROM claim_entities WHERE claim_id = ?",
                 (claim["id"],))["n"]
    return (n, len(claim["text"] or ""))


def preadjudicate_contradiction(repo: Repo, row) -> dict:
    """Deterministic resolution proposal for one open contradiction, or a
    deferral. Returns {decided, proposal?, confidence?}.

    Decides only when one claim STRICTLY dominates: strictly newer, strictly
    more specific, at least as corroborated, and itself corroborated by >= 2
    sources. Anything short of that (an even pair, a newer-but-vaguer claim, a
    weakly-supported challenger) is left for the model. Fail-soft: any
    corroboration-query error defers to the model rather than guessing.
    """
    undecided = {"decided": False}
    a = repo.one("SELECT * FROM claims WHERE id = ?", (row["claim_a"],))
    b = repo.one("SELECT * FROM claims WHERE id = ?", (row["claim_b"],))
    if not a or not b:
        return undecided
    try:
        corr = {a["id"]: gatemod._corroborating_sources(repo, a),
                b["id"]: gatemod._corroborating_sources(repo, b)}
    except gatemod.GateCheckError:
        return undecided
    spec = {a["id"]: _specificity(repo, a), b["id"]: _specificity(repo, b)}

    for win, lose in ((a, b), (b, a)):
        cw, cl = corr[win["id"]], corr[lose["id"]]
        if (win["created_at"] > lose["created_at"]
                and spec[win["id"]] > spec[lose["id"]]
                and cw >= cl and cw >= PREADJUDICATE_MIN_CORROBORATION):
            proposal = (
                f"Supersede claim #{lose['id']} with the newer, more specific "
                f"#{win['id']} ({spec[win['id']][0]} entities vs "
                f"{spec[lose['id']][0]}, {cw} corroborating sources vs {cl}). "
                f"Human confirms via `brainconnect supersede {lose['id']} {win['id']}`.")
            return {"decided": True, "proposal": proposal, "confidence": 0.8}
    return undecided


# A contradiction is a warning, never an automatic deletion (LEDGER_SPEC.md §5.7).
# `false_positive` closes the pair without implying either claim was wrong.
CONTRADICTION_RESOLUTIONS = ("resolved", "false_positive")


def contradiction_resolve(repo: Repo, cid: int, resolution: str,
                          *, reviewer: str | None = None,
                          status: str = "resolved") -> None:
    if status not in CONTRADICTION_RESOLUTIONS:
        raise SystemExit(
            f"error: unknown contradiction status {status!r}; expected one of "
            f"{', '.join(CONTRADICTION_RESOLUTIONS)}")
    row = repo.one("SELECT * FROM contradictions WHERE id = ?", (cid,))
    if not row:
        raise SystemExit(f"error: no contradiction #{cid}")
    repo.ex(
        """UPDATE contradictions
              SET status=?, resolution=?, resolved_at=?, resolved_by=?
            WHERE id=?""",
        (status, resolution, util.now_iso(), reviewer, cid))
    render.mark_dirty_for_claim(repo, row["claim_a"])
    render.mark_dirty_for_claim(repo, row["claim_b"])
    repo.finalize("contradiction-resolve", f"#{cid} ({status})")


# --- claim detail -----------------------------------------------------------
def claim_detail(repo: Repo, cid: int) -> dict:
    """Everything the ledger knows about one claim: provenance, scope, validity,
    supersession, contradictions, feedback tally."""
    import json

    from . import confidence as conf, feedback as feedbackmod, refs

    row = _require_claim(repo, cid)
    scope = (row["scope_type"] if row["scope_type"] == "global"
             else f"{row['scope_type']}:{row['scope_id']}")
    out = {
        "id": refs.claim(cid), "text": row["text"], "status": row["status"],
        "confidence": conf.label_of(row), "confidence_numeric": row["confidence"],
        "scope": scope, "origin": row["origin"], "tags": json.loads(row["tags"] or "[]"),
        "created_at": row["created_at"], "reviewed_at": row["reviewed_at"],
        "learned_at": row["learned_at"], "last_verified_at": row["last_verified_at"],
        "valid_from": row["valid_from"], "valid_until": row["valid_until"],
        "promoted_by": row["promoted_by"],
        "sources": [dict(r) for r in repo.q(
            "SELECT source_id, evidence_type, quote_or_pointer FROM claim_sources"
            " WHERE claim_id = ? ORDER BY id", (cid,))],
        "feedback": feedbackmod.tally(repo, cid),
    }
    if row["candidate_id"]:
        out["from_candidate"] = refs.candidate(row["candidate_id"])
    if row["superseded_by"]:
        out["superseded_by"] = refs.claim(row["superseded_by"])
    sup = repo.q(
        "SELECT new_claim_id, reason, created_by, created_at FROM supersessions"
        " WHERE old_claim_id = ? ORDER BY id", (cid,))
    if sup:
        out["superseded_reason"] = [
            {"by": refs.claim(r["new_claim_id"]), "reason": r["reason"],
             "by_whom": r["created_by"], "at": r["created_at"]} for r in sup]
    supersedes = repo.q(
        "SELECT old_claim_id FROM supersessions WHERE new_claim_id = ? ORDER BY id",
        (cid,))
    if supersedes:
        out["supersedes"] = [refs.claim(r["old_claim_id"]) for r in supersedes]
    cons = repo.q(
        "SELECT id, claim_a, claim_b, status, resolution FROM contradictions"
        " WHERE claim_a = ? OR claim_b = ? ORDER BY id", (cid, cid))
    if cons:
        out["contradictions"] = [
            {"id": r["id"], "with": refs.claim(r["claim_b"] if r["claim_a"] == cid
                                               else r["claim_a"]),
             "status": r["status"], "resolution": r["resolution"]} for r in cons]
    return out


# --- escalations ------------------------------------------------------------
def escalation_list(repo: Repo, status: str | None = "open") -> list:
    if status:
        return repo.q("SELECT * FROM escalations WHERE status = ? ORDER BY id", (status,))
    return repo.q("SELECT * FROM escalations ORDER BY id")


def escalation_propose(repo: Repo, eid: int, proposal: str) -> None:
    if not repo.one("SELECT 1 FROM escalations WHERE id = ?", (eid,)):
        raise SystemExit(f"error: no escalation #{eid}")
    repo.ex("UPDATE escalations SET proposal = ? WHERE id = ?", (proposal, eid))
    repo.finalize("escalation-propose", f"#{eid}")


def escalation_close(repo: Repo, eid: int) -> None:
    if not repo.one("SELECT 1 FROM escalations WHERE id = ?", (eid,)):
        raise SystemExit(f"error: no escalation #{eid}")
    repo.ex("UPDATE escalations SET status='closed' WHERE id = ?", (eid,))
    repo.finalize("escalation-close", f"#{eid}")
