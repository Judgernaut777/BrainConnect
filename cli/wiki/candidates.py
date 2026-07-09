"""Memory candidates — the proposal half of the gate (LEDGER_SPEC.md §5.2).

Agents propose; humans (or an approved librarian workflow) promote. This module is
where that asymmetry is *enforced in code* rather than merely by which MCP tools a
mode happens to expose:

  * `create()` writes `status='pending'`, always. There is no argument that makes
    it write anything else.
  * `promote()` refuses a reviewer whose actor type is an agent.

Pure code, zero model calls.
"""
from __future__ import annotations

import json

from .db import Repo
from . import confidence as conf
from . import ingest, refs, scopes, util
from .scopes import Scope

# Who may propose. Anyone, including agents — that is the point of a candidate.
PROPOSER_TYPES = ("human", "manager", "worker", "librarian", "agent", "tool")

# Who may *promote*. An agent proposing its own memory into trusted recall is the
# exact failure this ledger exists to prevent, so the agent types are absent.
REVIEWER_TYPES = ("human", "librarian")

STATUSES = ("pending", "promoted", "rejected", "archived")

# Only a pending candidate is reviewable. A promoted one already has a claim of
# record; a rejected/archived one must be re-proposed rather than silently revived.
_REVIEWABLE_FROM = ("pending",)

# The evidence type recorded on the claim_sources row a promotion creates: an
# agent *asserted* this text, a librarian did not extract it from the source.
PROMOTION_EVIDENCE_TYPE = "asserted"


class CandidateError(Exception):
    pass


def _require(repo: Repo, cid: int):
    row = repo.one("SELECT * FROM memory_candidates WHERE id = ?", (cid,))
    if not row:
        raise CandidateError(f"no candidate {refs.candidate(cid)}")
    return row


def _require_reviewable(row, action: str):
    if row["status"] not in _REVIEWABLE_FROM:
        raise CandidateError(
            f"candidate {refs.candidate(row['id'])} is {row['status']}; "
            f"cannot {action} (only pending candidates are reviewable)")


# --- propose ----------------------------------------------------------------
def create(repo: Repo, text: str, *, proposed_by: str, proposed_by_type: str,
           source_id: int | None = None, source_ref: str | None = None,
           task_id: str | None = None, proposed_scopes: list[Scope] | None = None,
           tags: list[str] | None = None, metadata: dict | None = None,
           harness: str | None = None) -> int:
    """File a PENDING memory candidate. Never promotes, by construction.

    When `source_id` is omitted the text is filed as its own evidence source (an
    `inbox/` capture, exactly as the pre-ledger `brain_capture` did), so a candidate
    always has provenance to point at. `source_ref` is an opaque external pointer
    (`agentconnect_attempt_123`) that WikiBrain stores and never resolves.
    """
    text = (text or "").strip()
    if not text:
        raise CandidateError("candidate text is empty")
    if proposed_by_type not in PROPOSER_TYPES:
        raise CandidateError(
            f"unknown proposer type {proposed_by_type!r}; "
            f"expected one of {', '.join(PROPOSER_TYPES)}")
    if not (proposed_by or "").strip():
        raise CandidateError("proposed_by is required (who is proposing this?)")

    if source_id is None:
        # ingest.capture files the inbox artifact + a `new` source and finalizes.
        source_id = ingest.capture(repo, harness or util.slug(proposed_by, 40), text)
    elif not repo.one("SELECT 1 FROM sources WHERE id = ?", (source_id,)):
        raise CandidateError(f"no source {refs.source(source_id)}")

    cur = repo.ex(
        """INSERT INTO memory_candidates
             (text, proposed_by, proposed_by_type, source_id, source_ref, task_id,
              proposed_scopes, tags, created_at, status, metadata)
           VALUES (?,?,?,?,?,?,?,?,?,'pending',?)""",
        (text, proposed_by, proposed_by_type, source_id, source_ref, task_id,
         scopes.dumps(proposed_scopes or []),
         json.dumps(sorted(tags or [])),
         util.now_iso(),
         json.dumps(metadata or {}, sort_keys=True)))
    cid = cur.lastrowid
    repo.finalize("capture-candidate",
                  f"{refs.candidate(cid)} pending, proposed by {proposed_by}")
    return cid


# --- read -------------------------------------------------------------------
def get(repo: Repo, cid: int) -> dict:
    row = _require(repo, cid)
    out = dict(row)
    out["ref"] = refs.candidate(row["id"])
    out["proposed_scopes"] = [s.as_dict() for s in scopes.loads(row["proposed_scopes"])]
    out["tags"] = json.loads(row["tags"] or "[]")
    out["metadata"] = json.loads(row["metadata"] or "{}")
    if row["promoted_claim_id"]:
        out["promoted_claim"] = refs.claim(row["promoted_claim_id"])
    return out


def listing(repo: Repo, status: str | None = "pending", limit: int = 50) -> list[dict]:
    if status and status not in STATUSES:
        raise CandidateError(f"unknown status {status!r}")
    if status:
        rows = repo.q("SELECT * FROM memory_candidates WHERE status = ?"
                      " ORDER BY id LIMIT ?", (status, limit))
    else:
        rows = repo.q("SELECT * FROM memory_candidates ORDER BY id LIMIT ?", (limit,))
    return [get(repo, r["id"]) for r in rows]


# --- review (human-gated) ---------------------------------------------------
def promote(repo: Repo, cid: int, *, reviewer: str, confidence: str, scope: Scope,
            reviewer_type: str = "human", note: str | None = None) -> int:
    """Promote a pending candidate into a scoped, trusted claim. Returns claim id.

    `reviewer_type` must not name an agent: promotion is the human gate, and an
    agent laundering its own proposal into trusted recall is precisely what this
    refuses. The MCP surface additionally hides these tools outside `--review`,
    but the check here holds even if a caller reaches the Python API directly.
    """
    if reviewer_type not in REVIEWER_TYPES:
        raise CandidateError(
            f"reviewer type {reviewer_type!r} may not promote; promotion is "
            f"human-gated (allowed: {', '.join(REVIEWER_TYPES)})")
    if not (reviewer or "").strip():
        raise CandidateError("reviewer is required (who is promoting this?)")
    label = confidence
    numeric = conf.to_numeric(label)  # raises ConfidenceError on a bad label

    row = _require(repo, cid)
    _require_reviewable(row, "promote")

    src = repo.one("SELECT origin FROM sources WHERE id = ?", (row["source_id"],))
    if not src:
        raise CandidateError(
            f"candidate {refs.candidate(cid)} has no evidence source; refusing to "
            "promote a claim with no provenance")
    now = util.now_iso()
    cur = repo.ex(
        """INSERT INTO claims
             (text, source_id, confidence, origin, status, created_at, reviewed_at,
              scope_type, scope_id, tags, confidence_label, learned_at,
              last_verified_at, promoted_by, candidate_id)
           VALUES (?,?,?,?,'promoted',?,?,?,?,?,?,?,?,?,?)""",
        (row["text"], row["source_id"], numeric, src["origin"], now, now,
         scope.scope_type, scope.scope_id, row["tags"], label,
         row["created_at"], now, reviewer, cid))
    claim_id = cur.lastrowid
    repo.ex(
        """INSERT INTO claim_sources
             (claim_id, source_id, evidence_type, quote_or_pointer, created_at)
           VALUES (?,?,?,?,?)""",
        (claim_id, row["source_id"], PROMOTION_EVIDENCE_TYPE, row["source_ref"], now))
    repo.ex(
        """UPDATE memory_candidates
              SET status='promoted', promoted_claim_id=?, reviewed_at=?,
                  reviewed_by=?, review_reason=?
            WHERE id=?""",
        (claim_id, now, reviewer, note, cid))
    repo.finalize("promote-candidate",
                  f"{refs.candidate(cid)} -> {refs.claim(claim_id)} "
                  f"({scope}, {label}) by {reviewer}")
    return claim_id


def reject(repo: Repo, cid: int, *, reviewer: str, reason: str,
           reviewer_type: str = "human") -> None:
    if reviewer_type not in REVIEWER_TYPES:
        raise CandidateError(
            f"reviewer type {reviewer_type!r} may not reject; review is human-gated")
    if not (reason or "").strip():
        raise CandidateError("a rejection reason is required")
    row = _require(repo, cid)
    _require_reviewable(row, "reject")
    repo.ex(
        """UPDATE memory_candidates
              SET status='rejected', reviewed_at=?, reviewed_by=?, review_reason=?
            WHERE id=?""",
        (util.now_iso(), reviewer, reason, cid))
    repo.finalize("reject-candidate", f"{refs.candidate(cid)}: {reason}")


def archive(repo: Repo, cid: int, *, reviewer: str, reason: str = "") -> None:
    """Retire a candidate without judging it. Captured items are not permanent."""
    row = _require(repo, cid)
    _require_reviewable(row, "archive")
    repo.ex(
        """UPDATE memory_candidates
              SET status='archived', reviewed_at=?, reviewed_by=?, review_reason=?
            WHERE id=?""",
        (util.now_iso(), reviewer, reason or None, cid))
    repo.finalize("archive-candidate", refs.candidate(cid))
