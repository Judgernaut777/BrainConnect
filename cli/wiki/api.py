"""The stable WikiBrain API (LEDGER_SPEC.md §6, §14).

One set of concepts, three transports: the Python API below, the `wiki` CLI, and
the MCP tools. AgentConnect's `MemoryAdapter` binds to exactly this shape and must
not need to know WikiBrain internals:

    recall(request)           -> RecallPack
    capture_candidate(request)-> CaptureResult
    record_feedback(request)  -> None
    health()                  -> dict

Fields WikiBrain does not own — `task_id`, `source_ref`, `origin_actor_id`,
`origin_actor_type` — are stored opaquely and echoed back. WikiBrain never resolves
them: AgentConnect owns what a task or an attempt *is*.

Every function takes a `Repo` first so callers control transaction scope. Requests
accept either their dataclass or a plain dict (the MCP/HTTP path).

Pure code, zero model calls.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .db import Repo
from . import backends, candidates, feedback as feedbackmod, profiles, refs, review
from . import scopes as scopesmod
from .recall import RecallPack, RecallRequest, recall as _recall
from .scopes import Scope


class ApiError(Exception):
    pass


# --- request/response shapes -------------------------------------------------
@dataclass
class CaptureRequest:
    text: str
    proposed_by: str
    proposed_by_type: str = "agent"
    source_id: int | None = None
    source_ref: str | None = None
    task_id: str | None = None
    proposed_scopes: list[Scope] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class CaptureResult:
    accepted: bool
    candidate_id: str
    status: str
    message: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MemoryFeedbackRequest:
    feedback: str
    actor_id: str
    actor_type: str = "agent"
    claim_id: str | int | None = None
    source_id: str | int | None = None
    note: str | None = None
    task_id: str | None = None
    metadata: dict = field(default_factory=dict)


# --- coercion helpers (the dict path) ----------------------------------------
def _scope_list(raw) -> list[Scope]:
    """Accept `[{"scope_type": "repo", "scope_id": "x"}]`, `["repo:x"]`, or Scopes."""
    out = []
    for s in raw or []:
        if isinstance(s, Scope):
            out.append(s)
        elif isinstance(s, dict):
            out.append(scopesmod.from_dict(s))
        elif isinstance(s, str):
            out.append(scopesmod.parse(s))
        else:
            raise ApiError(f"cannot read scope from {s!r}")
    return out


def _as_recall_request(req) -> RecallRequest:
    if isinstance(req, RecallRequest):
        return req
    d = dict(req or {})
    if "query" not in d:
        raise ApiError("recall requires a query")
    d["scopes"] = _scope_list(d.get("scopes"))
    known = RecallRequest.__dataclass_fields__
    unknown = set(d) - set(known)
    if unknown:
        raise ApiError(f"unknown recall fields: {', '.join(sorted(unknown))}")
    return RecallRequest(**d)


# LEDGER_SPEC.md §14: WikiBrain accepts the caller's vocabulary for who is
# proposing. AgentConnect's MemoryAdapter speaks `origin_actor_*`; the ledger
# speaks `proposed_by*`. They are the same field, so accept both rather than
# making every caller translate.
_CAPTURE_ALIASES = {
    "origin_actor_id": "proposed_by",
    "origin_actor_type": "proposed_by_type",
}


def _as_capture_request(req) -> CaptureRequest:
    if isinstance(req, CaptureRequest):
        return req
    d = dict(req or {})
    for alias, canonical in _CAPTURE_ALIASES.items():
        if alias in d:
            value = d.pop(alias)
            if d.get(canonical) not in (None, ""):
                if value not in (None, "", d[canonical]):
                    raise ApiError(
                        f"conflicting capture fields {alias!r} and {canonical!r}")
                continue
            if value is not None:
                d[canonical] = value
    d["proposed_scopes"] = _scope_list(d.get("proposed_scopes"))
    if isinstance(d.get("source_id"), str):
        d["source_id"] = refs.parse(d["source_id"], refs.SOURCE)
    unknown = set(d) - set(CaptureRequest.__dataclass_fields__)
    if unknown:
        raise ApiError(f"unknown capture fields: {', '.join(sorted(unknown))}")
    return CaptureRequest(**d)


def _as_feedback_request(req) -> MemoryFeedbackRequest:
    if isinstance(req, MemoryFeedbackRequest):
        return req
    d = dict(req or {})
    unknown = set(d) - set(MemoryFeedbackRequest.__dataclass_fields__)
    if unknown:
        raise ApiError(f"unknown feedback fields: {', '.join(sorted(unknown))}")
    return MemoryFeedbackRequest(**d)


# --- the four contract methods ------------------------------------------------
def recall(repo: Repo, request) -> RecallPack:
    """Trusted, bounded, scope-filtered context. Promoted-only by default."""
    return _recall(repo, _as_recall_request(request))


def capture_candidate(repo: Repo, request) -> CaptureResult:
    """File a PENDING memory candidate. Never promotes, under any argument."""
    req = _as_capture_request(request)
    cid = candidates.create(
        repo, req.text, proposed_by=req.proposed_by,
        proposed_by_type=req.proposed_by_type, source_id=req.source_id,
        source_ref=req.source_ref, task_id=req.task_id,
        proposed_scopes=req.proposed_scopes, tags=req.tags, metadata=req.metadata)
    return CaptureResult(
        accepted=True, candidate_id=refs.candidate(cid), status="pending",
        message=("Filed as a pending candidate. It is unvetted and will not appear "
                 "in trusted recall until a human promotes it."))


def record_feedback(repo: Repo, request) -> None:
    """Record retrieval quality. An observation, never a state transition."""
    req = _as_feedback_request(request)
    claim_id = (refs.parse(req.claim_id, refs.CLAIM)
                if req.claim_id is not None else None)
    source_id = (refs.parse(req.source_id, refs.SOURCE)
                 if req.source_id is not None else None)
    feedbackmod.record(
        repo, feedback=req.feedback, actor_id=req.actor_id, actor_type=req.actor_type,
        claim_id=claim_id, source_id=source_id, note=req.note, task_id=req.task_id,
        metadata=req.metadata)


def health(repo: Repo) -> dict:
    """Liveness + shape of the ledger, for an adapter's health check."""
    def n(sql, params=()):
        return repo.one(sql, params)["n"]

    try:
        backend_health = backends.get_backend(repo).health()
    except backends.BackendError as e:
        backend_health = {"ok": False, "error": str(e)}
    return {
        "ok": bool(backend_health.get("ok")),
        "service": "wikibrain",
        "role": "trusted memory ledger",
        "schema_version": repo.one("PRAGMA user_version")[0],
        "backend": backend_health,
        "profiles": list(profiles.NAMES),
        "ledger": {
            "sources": n("SELECT COUNT(*) AS n FROM sources"),
            "claims_promoted": n("SELECT COUNT(*) AS n FROM claims WHERE status='promoted'"),
            "claims_pending": n("SELECT COUNT(*) AS n FROM claims WHERE status='pending'"),
            "claims_superseded": n("SELECT COUNT(*) AS n FROM claims WHERE status='superseded'"),
            "candidates_pending": n("SELECT COUNT(*) AS n FROM memory_candidates WHERE status='pending'"),
            "contradictions_open": n("SELECT COUNT(*) AS n FROM contradictions WHERE status='open'"),
            "feedback": n("SELECT COUNT(*) AS n FROM recall_feedback"),
        },
    }


# --- review levers (human-gated; not part of the agent-facing contract) -------
def pending(repo: Repo, limit: int = 50) -> list[dict]:
    return candidates.listing(repo, status="pending", limit=limit)


def promote(repo: Repo, candidate_id, reviewer: str, confidence: str, scope=None,
            reviewer_type: str = "human", note: str | None = None) -> dict:
    """Promote a pending candidate. `scope` may be omitted when the candidate
    proposed exactly one — the reviewer is then accepting the proposal as filed.

    An ambiguous or absent proposal is an error, never a guess: silently promoting
    a claim into the wrong scope is how a repo fact leaks into global recall.
    Confidence is never guessed either — it is what the profiles filter on.
    """
    cid = refs.parse(candidate_id, refs.CANDIDATE)
    if scope is None:
        proposed = scopesmod.loads(
            candidates._require(repo, cid)["proposed_scopes"])
        if len(proposed) != 1:
            raise ApiError(
                f"candidate {refs.candidate(cid)} proposed {len(proposed)} scopes; "
                "pass an explicit scope to promote it")
        scope = proposed[0]
    if isinstance(scope, str):
        scope = scopesmod.parse(scope)
    elif isinstance(scope, dict):
        scope = scopesmod.from_dict(scope)
    claim_id = candidates.promote(
        repo, cid, reviewer=reviewer, confidence=confidence, scope=scope,
        reviewer_type=reviewer_type, note=note)
    row = repo.one("SELECT * FROM claims WHERE id = ?", (claim_id,))
    return {"id": refs.claim(claim_id), "text": row["text"], "status": row["status"],
            "confidence": row["confidence_label"], "scope": str(scope),
            "promoted_by": row["promoted_by"]}


def reject(repo: Repo, candidate_id, reviewer: str, reason: str,
           reviewer_type: str = "human") -> None:
    candidates.reject(repo, refs.parse(candidate_id, refs.CANDIDATE),
                      reviewer=reviewer, reason=reason, reviewer_type=reviewer_type)


def supersede(repo: Repo, old_claim_id, new_claim_id, reason: str,
              reviewer: str) -> None:
    review.supersede(repo, refs.parse(old_claim_id, refs.CLAIM),
                     refs.parse(new_claim_id, refs.CLAIM),
                     reason=reason, reviewer=reviewer)
