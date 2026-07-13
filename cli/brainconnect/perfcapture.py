"""The performance-capture adapter (ADR 0008 Lane 7).

Lane 7 closes the capability-registry loop. ComputeConnect (CC) and AgentConnect
(AC) MEASURE; BrainConnect (BC) only CAPTURES what they report as **pending**
candidates that a human then promotes. This module is that capture half: it reads
CC's side-effect-free telemetry (`GET /health`, `GET /models`, `GET /models/loaded`,
and optionally a `POST /route/estimate` rationale) through an injectable client,
turns each observed model availability/performance fact into an ordinary PENDING
`model_performance` memory candidate (LEDGER_SPEC §7, `model:` scope §5.5), and
stops there.

The hard boundary (ADR 0008 / CLAUDE.md):

* **Zero model calls.** The telemetry client can reach ONLY health/inventory/
  residency reads and the side-effect-free `/route/estimate`; it has no path to
  `/generate` or any chat endpoint. BC captures facts; it never generates.
* **Untrusted data, never instructions.** Telemetry is untrusted input. Every
  captured fact is safety-scanned before it becomes a row (a credential in a
  telemetry field is masked/quarantined, never stored raw), and the observed
  string values are placed in the *scanned* candidate text — not smuggled past the
  scanner in structured metadata.
* **PENDING only, never auto-promoted.** Each candidate enters the ordinary
  human/librarian promotion gate (`candidates.promote`, whose `REVIEWER_TYPES`
  excludes every agent type). A model/agent can never self-promote a capability
  claim about itself.
* **No fabricated numbers.** Only what telemetry actually reports is captured, and
  each fact is SOURCE-LABELLED with `source=computeconnect-telemetry` and a
  `kind` of `measured` (an observed residency/availability fact) or `estimate`
  (CC's operator-declared heuristic — comparable only within one CC deployment).
* **No live state held.** BC records the observation as a candidate and forgets;
  it never becomes a residency/warm-state table (that is CC's, ADR 0008).

Idempotency (requirement 4): each observation carries an UNFORGEABLE per-fact
fingerprint over `(source, subject, metric, kind, value)`
(`candidates.PERFCAPTURE_OBSERVATION_KEY`). A re-run that sees the SAME value finds
the marker and files nothing (no duplicate); a CHANGED value produces a different
fingerprint and IS filed (a genuinely new observation is never silently
suppressed). The `observed_at` logical marker is recorded but deliberately kept
OUT of the fingerprint, so wall-clock drift alone never forges a "new" observation.

If CC is unavailable (no client, or any transport failure), the adapter captures
nothing and reports cleanly — it never crashes.

Pure code, zero model calls.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from .db import Repo
from . import candidates, ingest, refs, safety, util
from . import observability as obsmod
from .registry import MODEL_PERFORMANCE_TAG
from .scopes import Scope
from .delegate_clients import (
    COMPUTECONNECT, DelegationClientError, TelemetryClient, EstimateClient,
)

#: The source label stamped on every captured fact. It says WHERE the fact came
#: from so a human (and any later consumer) can never mistake a captured telemetry
#: observation for a first-party BrainConnect measurement.
SOURCE_TELEMETRY = "computeconnect-telemetry"

#: Observation kinds. `measured` = an observed residency/availability fact ("CC
#: reports this model is loaded"); `estimate` = CC's operator-declared heuristic
#: from a `/route/estimate` rationale (NOT a measured benchmark).
KIND_MEASURED = "measured"
KIND_ESTIMATE = "estimate"

#: A tag applied to every perfcapture candidate so the read surface can list them
#: deterministically. It is a plain, squattable tag used ONLY for the human-facing
#: listing filter — dedup and identity ride the unforgeable fingerprint marker, not
#: this tag.
PERFCAPTURE_TAG = "perfcapture-telemetry"

#: The metric names captured from a `/route/estimate` rationale. Fixed, first-party
#: vocabulary (not read from telemetry), so a hostile telemetry field can never
#: introduce a new metric name.
_ESTIMATE_METRICS = (
    "estimated_tokens_per_second",
    "estimated_quality",
    "estimated_queue_seconds",
)


@dataclass(frozen=True)
class PerfObservation:
    """One captured telemetry fact about ONE model. Identity is
    `(source, subject, metric, kind, value)`; `context` carries safe descriptive
    strings that also flow into the scanned candidate text.

    `subject` is the model id (the fact is always `model:`-scoped, §7). `value` is a
    JSON-scalar (bool for availability, a number for an estimate) — never a raw
    string, so no untrusted string is smuggled into the dedup key or metadata.
    """
    subject: str                 # the model id (model: scope)
    subject_kind: str            # always "model" (§7 confines model_performance)
    metric: str
    value: object                # bool | int | float | str (scalar)
    kind: str                    # KIND_MEASURED | KIND_ESTIMATE
    source: str = SOURCE_TELEMETRY
    context: dict = field(default_factory=dict)  # safe descriptive strings for text

    def _value_sig(self) -> str:
        """A stable, order-free signature of the value for the fingerprint. Floats
        are rendered with `repr` so 40.0 and 40 do not collide by accident yet an
        identical value re-hashes identically."""
        return json.dumps(self.value, sort_keys=True, default=str)

    def fingerprint(self) -> str:
        """The unforgeable per-observation identity. Excludes `observed_at`: a
        re-run at a different wall-clock time but the SAME value must dedupe, while a
        changed value must NOT."""
        payload = "\x1f".join((
            self.source, self.subject_kind, self.subject, self.metric, self.kind,
            self._value_sig()))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --- turning telemetry dicts into observations (pure, no I/O) ----------------
def _loaded_observations(models_payload: object) -> list[PerfObservation]:
    """Model availability facts from `/models/loaded` (or `/models`).

    Each entry with a usable string `id` becomes a `measured` availability
    observation. This is the fact that refreshes the DEPLOYED-model claim
    (requirement 2): "CC reports model X currently loaded/resident" — captured as a
    PENDING candidate a human then promotes to correct the registry.
    """
    out: list[PerfObservation] = []
    if not isinstance(models_payload, dict):
        return out
    models = models_payload.get("models")
    if not isinstance(models, list):
        return out
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        meta = m.get("metadata") if isinstance(m.get("metadata"), dict) else {}
        provider = meta.get("provider_id")
        placement = meta.get("placement_class")
        runtime = m.get("runtime")
        loaded = bool(m.get("loaded"))
        out.append(PerfObservation(
            subject=mid.strip(), subject_kind="model", metric="loaded",
            value=loaded, kind=KIND_MEASURED,
            context={
                "provider_id": provider if isinstance(provider, str) else None,
                "placement_class": placement if isinstance(placement, str) else None,
                "runtime": runtime if isinstance(runtime, str) else None,
            }))
    return out


def _estimate_observations(estimate: object) -> list[PerfObservation]:
    """CC operator-heuristic facts from a `/route/estimate` rationale.

    Each present numeric metric on the `reason` rationale (or top-level) becomes an
    `estimate` observation for the estimate's `selected_model`. These are NOT
    measured benchmarks — CC's own contract defines `estimated_quality` as an
    operator-declared heuristic comparable only within one deployment, so the
    candidate text says so and the kind is `estimate`.
    """
    out: list[PerfObservation] = []
    if not isinstance(estimate, dict):
        return out
    model = estimate.get("selected_model")
    if not isinstance(model, str) or not model.strip():
        return out
    reason = estimate.get("reason") if isinstance(estimate.get("reason"), dict) else {}
    placement = reason.get("placement_class")
    provider = reason.get("provider_id")
    for metric in _ESTIMATE_METRICS:
        val = estimate.get(metric)
        if val is None:
            val = reason.get(metric)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        # Untrusted CC telemetry: a non-finite metric (NaN/Infinity/-Infinity) is
        # never a real measurement. Skip it before it can become an observation — a
        # third layer of poison defense behind the delegate-clients ingress reject
        # and the structural DB guard (candidates.create_checked, allow_nan=False).
        if not math.isfinite(val):
            continue
        out.append(PerfObservation(
            subject=model.strip(), subject_kind="model", metric=metric,
            value=val, kind=KIND_ESTIMATE,
            context={
                "provider_id": provider if isinstance(provider, str) else None,
                "placement_class": placement if isinstance(placement, str) else None,
            }))
    return out


def _sort_key(o: PerfObservation) -> tuple:
    return (o.subject, o.metric, o.kind, o.fingerprint())


def observations_from_telemetry(*, models_inventory: object = None,
                                models_loaded: object = None,
                                estimate: object = None) -> list[PerfObservation]:
    """All observations derivable from the given telemetry payloads, in a stable,
    deterministic order. Pure — takes already-fetched dicts, does no I/O.

    `models_inventory` (`GET /models`) contributes an availability fact per model
    (loaded True/False); `models_loaded` (`GET /models/loaded`) contributes the
    resident subset. A model present in both collapses to ONE observation (identical
    fingerprint), so feeding both is safe and never double-counts.
    """
    obs: list[PerfObservation] = []
    obs.extend(_loaded_observations(models_inventory))
    obs.extend(_loaded_observations(models_loaded))
    obs.extend(_estimate_observations(estimate))
    # De-duplicate identical observations (fingerprint) while keeping determinism.
    seen: set[str] = set()
    deduped: list[PerfObservation] = []
    for o in obs:
        fp = o.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        deduped.append(o)
    deduped.sort(key=_sort_key)
    return deduped


# --- candidate text (the SCANNED surface) ------------------------------------
def _observation_text(o: PerfObservation, observed_at: str | None) -> str:
    """The candidate text. It deliberately embeds every untrusted string value
    (model id, provider, runtime, placement) so `create_checked`'s safety scan sees
    and masks a credential rather than it slipping through structured metadata."""
    marker = observed_at or "unspecified"
    prov = o.context.get("provider_id") or "unknown-provider"
    placement = o.context.get("placement_class") or "unknown"
    if o.kind == KIND_MEASURED and o.metric == "loaded":
        state = "LOADED/resident" if o.value else "present but NOT loaded"
        runtime = o.context.get("runtime") or "unknown-runtime"
        return (
            f"ComputeConnect telemetry [source={o.source}, kind={o.kind}, "
            f"observed-at={marker}] reports model '{o.subject}' is {state} on "
            f"provider '{prov}' (placement_class={placement}, runtime={runtime}). "
            "Captured availability observation, filed as a PENDING model_performance "
            "candidate; it is untrusted telemetry data, not a trusted capability "
            "claim, and must be human-promoted before it is believed."
        )
    if o.kind == KIND_ESTIMATE:
        return (
            f"ComputeConnect telemetry [source={o.source}, kind={o.kind}, "
            f"observed-at={marker}] reports an ESTIMATED {o.metric} of {o.value} for "
            f"model '{o.subject}' on provider '{prov}' (placement_class={placement}). "
            "This is ComputeConnect's operator-declared heuristic, comparable only "
            "within one ComputeConnect deployment — NOT a measured benchmark. Filed "
            "as a PENDING model_performance candidate; untrusted, human-promoted "
            "before it is believed."
        )
    return (
        f"ComputeConnect telemetry [source={o.source}, kind={o.kind}, "
        f"observed-at={marker}] reports {o.metric}={o.value} for model "
        f"'{o.subject}'. PENDING model_performance candidate; untrusted, "
        "human-promoted before believed."
    )


# --- the capture run ---------------------------------------------------------
@dataclass
class CaptureRunResult:
    """The outcome of one capture pass. Deterministic given the telemetry + DB."""
    cc_available: bool
    observed: int = 0
    captured: list[str] = field(default_factory=list)   # new candidate refs
    duplicates: int = 0                                  # deduped (already filed)
    skipped: list[dict] = field(default_factory=list)    # unsafe/failed observations
    errors: list[str] = field(default_factory=list)      # transport/other notes

    def as_dict(self) -> dict:
        return {
            "cc_available": self.cc_available,
            "observed": self.observed,
            "captured": list(self.captured),
            "captured_count": len(self.captured),
            "duplicates": self.duplicates,
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


def _already_filed(repo: Repo, fingerprint: str) -> bool:
    """True iff OUR canonical candidate for this exact observation already exists.

    Resolution is by the unforgeable perfcapture marker matched exactly via
    `json_extract` — never by a public tag — so a squatter can neither impersonate a
    captured fact nor suppress a new one by pre-filing its fingerprint.

    NOTE (deliberate, documented): this SELECT-then-INSERT dedup is technically a
    TOCTOU. A structural partial-UNIQUE index on
    `json_extract(metadata,'$.perfcapture_observation')` would reject a duplicate at
    the DB layer, but that touches the shared `memory_candidates` schema/migration
    path and risks breaking every other candidate write, so it is intentionally NOT
    added here. The window is harmless in practice: BrainConnect is a single-user,
    human-gated CLI, a capture run is not executed concurrently with itself, and a
    duplicate PENDING candidate is at worst a redundant row a human ignores or
    archives — never a trust or correctness defect. If capture ever becomes
    concurrent, add the partial index in a dedicated migration."""
    row = repo.one(
        "SELECT 1 AS x FROM memory_candidates "
        "WHERE json_extract(metadata, ?) = ? LIMIT 1",
        ("$." + candidates.PERFCAPTURE_OBSERVATION_KEY, fingerprint))
    return row is not None


def _capture_one(repo: Repo, o: PerfObservation, *, observed_at: str | None,
                 proposed_by: str, proposed_by_type: str,
                 result: CaptureRunResult) -> None:
    fp = o.fingerprint()
    if _already_filed(repo, fp):
        result.duplicates += 1
        return

    # Defense in depth (records the skip an upstream filter cannot). A non-finite
    # value (NaN/Infinity) is not a real observation and would be refused by the
    # structural DB guard anyway; skip it here with an audit-safe reason so the run
    # stays clean, explicit, and never attempts to persist ledger-poisoning JSON.
    if isinstance(o.value, float) and not math.isfinite(o.value):
        result.skipped.append({
            "metric": o.metric, "kind": o.kind, "fingerprint": fp,
            "reason": "non-finite value (NaN/Infinity); not captured"})
        return

    # The model id becomes the claim SCOPE (`model:<id>`), and scope_id is NOT
    # safety-scanned. So refuse to scope on a subject that itself carries a
    # credential/injection payload: scan it first and skip (never store) if it is
    # anything but clean. A benign model id ("qwen3-30b-a3b") passes untouched.
    subj_verdict = safety.scan_for(repo, o.subject, safety.MEMORY_CANDIDATE)
    if not subj_verdict.clean:
        result.skipped.append({
            "metric": o.metric, "kind": o.kind, "fingerprint": fp,
            "reason": f"unsafe subject ({subj_verdict.reason()}); not scoped/stored"})
        return

    text = _observation_text(o, observed_at)
    metadata = {
        "kind": "perfcapture-observation",
        "source": o.source,
        "observation_kind": o.kind,
        "metric": o.metric,
        "subject_kind": o.subject_kind,
        "model": o.subject,          # clean (subject verdict checked above)
        "observed_at": observed_at,
        "value": o.value if isinstance(o.value, (bool, int, float)) else None,
        # Honesty flags: a captured telemetry fact is NOT a trust signal.
        "provenance_only": True,
        "trusted": False,
    }
    try:
        cid, _verdict = candidates.create_checked(
            repo, text, proposed_by=proposed_by, proposed_by_type=proposed_by_type,
            proposed_scopes=[Scope("model", o.subject)],
            tags=[PERFCAPTURE_TAG, MODEL_PERFORMANCE_TAG,
                  f"metric:{o.metric}", f"source:{o.source}"],
            metadata=metadata, harness="perfcapture",
            perfcapture_observation=fp)
    except candidates.SafetyRefused as e:
        # A telemetry field carried a block-level payload: the RAW value is refused,
        # never stored. Degrade-never-crash — record an audit-safe skip and continue.
        result.skipped.append({
            "metric": o.metric, "kind": o.kind, "fingerprint": fp,
            "reason": f"safety-refused ({e.result.reason()})"})
        return
    except ingest.IngestError as e:
        # Duplicate inbox content-hash for a genuinely-distinct observation is
        # near-impossible (text carries metric+value+model), but never crash.
        result.errors.append(f"{o.subject}/{o.metric}: {e}")
        return
    except Exception as e:  # noqa: BLE001 — degrade-never-crash boundary.
        result.errors.append(
            f"{o.subject}/{o.metric}: capture failed ({type(e).__name__}: {e})")
        return
    result.captured.append(refs.candidate(cid))


def capture(repo: Repo, *, telemetry_client: TelemetryClient | None = None,
            estimate_client: EstimateClient | None = None,
            estimate_body: dict | None = None,
            estimate_privacy_header: str | None = None,
            observed_at: str | None = None,
            proposed_by: str = "perfcapture",
            proposed_by_type: str = "tool",
            trace_id: str | None = None,
            emitter: "obsmod.Emitter | None" = None) -> CaptureRunResult:
    """Read CC telemetry and file each observed model fact as a PENDING candidate.

    `telemetry_client` is injected (an `HttpTelemetryClient` in production, a fake in
    tests). It being None — or any transport failure — means "CC unavailable": the
    run captures NOTHING and reports cleanly (`cc_available=False`), never crashes.

    When `estimate_client` + `estimate_body` are supplied, the side-effect-free
    `POST /route/estimate` rationale is also read and its heuristic numbers captured
    as `kind=estimate` observations. No generation is ever performed.
    """
    if observed_at is None:
        observed_at = util.now_iso()
    result = CaptureRunResult(cc_available=False)

    def _finish(res: CaptureRunResult) -> CaptureRunResult:
        # Lane 8: emit the capture DECISION into AgentConnect's observability seam
        # (AC EventType `memory.captured`). NON-FATAL + carries ONLY counts +
        # availability — never a model id, a telemetry value, or a raw body.
        obsmod.emit_decision(
            emitter, obsmod.EVT_MEMORY_CAPTURED,
            trace_id=trace_id or "bc-perfcapture", task_id=None,
            outcome=(obsmod.OUTCOME_SUCCEEDED if res.captured
                     else obsmod.OUTCOME_UNKNOWN),
            decision_class="perfcapture-telemetry",
            agent_role="registrar",
            metadata={
                "cc_available": res.cc_available,
                "observed": res.observed,
                "captured_count": len(res.captured),
                "duplicates": res.duplicates,
                "skipped_count": len(res.skipped),
                "error_count": len(res.errors),
            })
        return res

    if telemetry_client is None:
        result.errors.append(f"{COMPUTECONNECT}: no telemetry client (unavailable)")
        return _finish(result)

    # /health is the liveness gate: if it cannot be read, CC is unavailable —
    # capture nothing and report cleanly (never crash). It is otherwise only
    # supplementary context; observations come from the model endpoints.
    try:
        telemetry_client.health()
        result.cc_available = True
    except DelegationClientError as e:
        result.errors.append(str(e))
        return _finish(result)

    # /models (full inventory) + /models/loaded (resident subset). Each is
    # best-effort: a failure of one is noted, not fatal. /models/loaded is the
    # residency truth the deployed-model refresh (requirement 2) keys on.
    inventory: object = None
    residency: object = None
    try:
        inventory = telemetry_client.models(loaded_only=False)
    except DelegationClientError as e:
        result.errors.append(str(e))
    try:
        residency = telemetry_client.models(loaded_only=True)
    except DelegationClientError as e:
        result.errors.append(str(e))

    estimate: object = None
    if estimate_client is not None and estimate_body is not None:
        try:
            estimate = estimate_client.estimate(
                estimate_body, privacy_header=estimate_privacy_header)
        except DelegationClientError as e:
            result.errors.append(str(e))
            estimate = None

    obs = observations_from_telemetry(
        models_inventory=inventory, models_loaded=residency, estimate=estimate)
    result.observed = len(obs)
    for o in obs:
        _capture_one(repo, o, observed_at=observed_at, proposed_by=proposed_by,
                     proposed_by_type=proposed_by_type, result=result)
    return _finish(result)


# --- the deterministic read surface ------------------------------------------
def listing(repo: Repo, *, status: str | None = "pending",
            limit: int = 200) -> list[dict]:
    """Captured telemetry candidates, in stable order (by id).

    Filters to OUR canonical perfcapture facts by the unforgeable marker, never the
    squattable tag. Deterministic: two calls against an unchanged ledger are
    byte-identical. Each row surfaces identity + source label + trust status so an
    operator can see exactly what was captured and then promote via the ordinary
    human gate.
    """
    marker = "$." + candidates.PERFCAPTURE_OBSERVATION_KEY
    if status:
        if status not in candidates.STATUSES:
            raise ValueError(f"unknown status {status!r}")
        rows = repo.q(
            "SELECT id FROM memory_candidates "
            "WHERE json_extract(metadata, ?) IS NOT NULL AND status = ? "
            "ORDER BY id LIMIT ?", (marker, status, limit))
    else:
        rows = repo.q(
            "SELECT id FROM memory_candidates "
            "WHERE json_extract(metadata, ?) IS NOT NULL "
            "ORDER BY id LIMIT ?", (marker, limit))
    out: list[dict] = []
    for r in rows:
        cand = candidates.get(repo, r["id"])
        meta = cand["metadata"]
        out.append({
            "ref": cand["ref"],
            "status": cand["status"],
            "model": meta.get("model"),
            "metric": meta.get("metric"),
            "source": meta.get("source"),
            "observation_kind": meta.get("observation_kind"),
            "value": meta.get("value"),
            "observed_at": meta.get("observed_at"),
            "scope": f"model:{meta.get('model')}" if meta.get("model") else None,
            "trusted": False,   # a captured candidate is NEVER trusted
            "promoted": cand["status"] == "promoted",
            "fingerprint": meta.get(candidates.PERFCAPTURE_OBSERVATION_KEY),
        })
    return out
