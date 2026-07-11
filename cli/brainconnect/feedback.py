"""Recall feedback — retrieval quality, reported by whoever consumed the pack.

An observation, never a state transition (LEDGER_SPEC.md §5.8). Recording `wrong`
against a claim does not demote it: that would hand agents an indirect promotion
lever (mark a rival claim wrong, watch it disappear). Feedback accumulates and
surfaces in the human review queue.

Pure code, zero model calls.
"""
from __future__ import annotations

import json

from .db import Repo
from . import refs, util

FEEDBACK_VALUES = (
    "useful", "irrelevant", "stale", "wrong", "too_broad", "missing_context",
)

ACTOR_TYPES = ("human", "manager", "worker", "librarian", "agent", "tool")

# Feedback that says "this memory has gone bad" rather than "this retrieval missed".
# `pending_review()` surfaces these; nothing acts on them automatically.
NEGATIVE = ("stale", "wrong")


class FeedbackError(Exception):
    pass


def record(repo: Repo, *, feedback: str, actor_id: str, actor_type: str,
           claim_id: int | None = None, source_id: int | None = None,
           note: str | None = None, task_id: str | None = None,
           metadata: dict | None = None) -> int:
    if feedback not in FEEDBACK_VALUES:
        raise FeedbackError(
            f"unknown feedback {feedback!r}; expected one of {', '.join(FEEDBACK_VALUES)}")
    if actor_type not in ACTOR_TYPES:
        raise FeedbackError(f"unknown actor type {actor_type!r}")
    if not (actor_id or "").strip():
        raise FeedbackError("actor_id is required")
    if claim_id is None and source_id is None:
        raise FeedbackError("feedback needs a claim_id or a source_id to attach to")
    if claim_id is not None and not repo.one("SELECT 1 FROM claims WHERE id=?", (claim_id,)):
        raise FeedbackError(f"no claim {refs.claim(claim_id)}")
    if source_id is not None and not repo.one("SELECT 1 FROM sources WHERE id=?", (source_id,)):
        raise FeedbackError(f"no source {refs.source(source_id)}")

    cur = repo.ex(
        """INSERT INTO recall_feedback
             (claim_id, source_id, actor_id, actor_type, feedback, note, task_id,
              created_at, metadata)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (claim_id, source_id, actor_id, actor_type, feedback, note, task_id,
         util.now_iso(), json.dumps(metadata or {}, sort_keys=True)))
    target = refs.claim(claim_id) if claim_id else refs.source(source_id)
    repo.finalize("feedback", f"{target}: {feedback} by {actor_id}")
    return cur.lastrowid


def for_claim(repo: Repo, claim_id: int) -> list[dict]:
    return [dict(r) for r in repo.q(
        "SELECT * FROM recall_feedback WHERE claim_id = ? ORDER BY id", (claim_id,))]


def tally(repo: Repo, claim_id: int) -> dict[str, int]:
    """Counts per feedback value for one claim. Used by the ledger projection."""
    return {r["feedback"]: r["n"] for r in repo.q(
        "SELECT feedback, COUNT(*) AS n FROM recall_feedback"
        " WHERE claim_id = ? GROUP BY feedback ORDER BY feedback", (claim_id,))}


def pending_review(repo: Repo) -> list[dict]:
    """Promoted claims carrying negative feedback — a human review queue, not an
    automatic demotion list."""
    marks = ",".join("?" for _ in NEGATIVE)
    rows = repo.q(
        f"""SELECT c.id, c.text, c.scope_type, c.scope_id,
                   COUNT(f.id) AS n, GROUP_CONCAT(DISTINCT f.feedback) AS kinds
              FROM claims c JOIN recall_feedback f ON f.claim_id = c.id
             WHERE c.status = 'promoted' AND f.feedback IN ({marks})
             GROUP BY c.id ORDER BY n DESC, c.id""", NEGATIVE)
    return [{"claim": refs.claim(r["id"]), "id": r["id"], "text": r["text"],
             "scope": (r["scope_type"] if r["scope_type"] == "global"
                       else f"{r['scope_type']}:{r['scope_id']}"),
             "count": r["n"], "kinds": sorted((r["kinds"] or "").split(","))}
            for r in rows]
