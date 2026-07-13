"""Lane 8 observability emitter (ADR 0008 — the orchestration boundary).

BrainConnect (BC) EMITS its orchestration *decisions* into AgentConnect's (AC)
observability provider seam, using AC's EXISTING ``EventType`` vocabulary. BC
does **not** define a competing event stream, timeline, run-history, or token
ledger (ADR 0008, explicit prohibition). The observability stream is AC's; BC is
one more emitter into it.

The hard boundary this module keeps (ADR 0008 / CLAUDE.md):

* **Reuse AC's vocabulary; never fork it.** BC does not re-declare AC's whole
  ``EventType`` enum. It mirrors *only the subset it emits* as plain string
  constants equal to AC's stable wire values, and a CONFORMANCE PIN test
  (``tests/acceptance.py``) imports AC's real ``EventType`` when it is importable
  and asserts every BC constant byte-matches — skipping cleanly when AC is not on
  the path. This is the same discipline as Lane 4's privacy pin. ``agentconnect``
  is therefore **never a required dependency** of BC.

* **Non-fatal + optional.** The default sink is :class:`NoopSink` (disabled). A
  sink is selectable by env/config (a StructuredLog JSONL sink, or — when AC is
  importable — AC's own provider). Emission is wrapped so a sink error is
  swallowed and logged and can **never** break the orchestration operation that
  triggered it. BC works fully with observability off.

* **No secrets, no private payloads.** An event carries ONLY the correlation id
  set (``trace_id``/``task_id``/…), an event type, a state, an outcome, and a
  ``metadata`` dict restricted BY CONSTRUCTION to small scalars (a decision
  class, counts, booleans, enum strings). A prompt, a completion, a raw
  request/decision body, a URL (which may carry credentials), or any private
  context is never routed here — non-scalar metadata values are dropped and
  strings are length-bounded before the event is built.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger("brainconnect.observability")

# ---------------------------------------------------------------------------
# AC EventType vocabulary — the SUBSET BC emits (mirrored, not forked).
#
# These strings are AgentConnect's stable wire identifiers (core/observability/
# model.py:EventType). They are NOT a new BC vocabulary: the conformance pin
# test asserts each equals AC's real EventType value when AC is importable.
# ---------------------------------------------------------------------------
EVT_SUBTASK_ROUTED = "subtask.routed"
EVT_DECISION_RECORDED = "decision.recorded"
EVT_MEMORY_CAPTURED = "memory.captured"
EVT_COMPUTE_PLACED = "compute.placed"

#: BC decision point -> AC EventType wire value. The single source of truth for
#: the Lane-8 mapping; the conformance pin verifies the right-hand side.
EMITTED_EVENT_TYPES: dict[str, str] = {
    "delegation-routing": EVT_SUBTASK_ROUTED,      # Lane 4 delegate
    "role-assignment": EVT_DECISION_RECORDED,      # Lane 6 roles
    "registry-capability-seed": EVT_MEMORY_CAPTURED,  # Lane 1 registry
    "perfcapture-telemetry": EVT_MEMORY_CAPTURED,  # Lane 7 perfcapture
}

#: Mirror of AC's DEFAULT_STATE_FOR_EVENT for the subset BC emits. Keeps a BC
#: event's ``state`` identical to what AC would resolve for the same type.
_DEFAULT_STATE: dict[str, str] = {
    EVT_SUBTASK_ROUTED: "starting",
    EVT_DECISION_RECORDED: "working",
    EVT_MEMORY_CAPTURED: "working",
    EVT_COMPUTE_PLACED: "starting",
}

# AC ObservationOutcome wire values BC uses (mirrored subset).
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_DENIED = "denied"
OUTCOME_UNKNOWN = "unknown"

#: The exact field set of AC's AgentObservationEvent (model.py). BC builds a dict
#: with these keys so a StructuredLog line is byte-shaped like AC's own, and so
#: AC's ``AgentObservationEvent(**event)`` round-trips (conformance pin).
_EVENT_FIELDS = (
    "event_id", "sequence", "timestamp", "event_type", "state", "outcome",
    "trace_id", "task_id", "delegation_id", "parent_delegation_id", "subtask_id",
    "session_id", "run_id", "review_id", "agent_id", "agent_role", "provider",
    "workspace_id", "metadata",
)

#: Metadata hygiene bounds. Only small scalars survive into an event; a string is
#: truncated so a stray large value cannot smuggle a payload through metadata.
_MAX_STR = 200


def _is_scalar(v: Any) -> bool:
    # bool is a subclass of int; both are allowed. dict/list/bytes are not.
    return isinstance(v, (str, bool, int, float)) or v is None


def _scrub_metadata(md: Optional[dict]) -> dict:
    """Keep only small scalars. Drop nested/non-scalar values, bound strings.

    This is the structural no-secret guarantee: even if a caller mistakenly hands
    a raw request body or a credentialed URL object, it cannot become an emitted
    field — a dict/list is dropped, and a long string is truncated.
    """
    out: dict[str, Any] = {}
    if not md:
        return out
    for k, v in md.items():
        key = str(k)
        if not _is_scalar(v):
            continue  # nested body / object — never emitted
        if isinstance(v, str) and len(v) > _MAX_STR:
            v = v[:_MAX_STR]
        out[key] = v
    return out


def _event_id(trace_id: str, sequence: int, event_type: str) -> str:
    """Deterministic idempotency key: same (trace, sequence, type) -> same id, so
    a re-emit of the same transition dedupes exactly as AC expects."""
    h = hashlib.sha1(f"{trace_id}:{sequence}:{event_type}".encode("utf-8"))
    return "bc-" + h.hexdigest()[:24]


# ---------------------------------------------------------------------------
# Sinks (providers). Base is a non-raising no-op, mirroring AC's provider seam.
# ---------------------------------------------------------------------------
class ObservabilitySink:
    """A provider BC fans a built event dict out to. The default is inert."""

    name: str = "abstract"

    def append_event(self, event: dict) -> None:
        return None


class NoopSink(ObservabilitySink):
    """The default: observability disabled. Records nothing."""

    name = "noop"

    def append_event(self, event: dict) -> None:
        return None


class StructuredLogSink(ObservabilitySink):
    """Append one JSON object per line — the same shape AC's
    StructuredLogObservabilityProvider writes, so the two logs are interchangeable.
    """

    name = "structured_log"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append_event(self, event: dict) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    def read_events(self, trace_id: Optional[str] = None) -> list[dict]:
        """Read events back, re-sorted by ``(sequence, timestamp)``."""
        if not os.path.exists(self.path):
            return []
        rows: list[dict] = []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if trace_id is not None and obj.get("trace_id") != trace_id:
                        continue
                    rows.append(obj)
        rows.sort(key=lambda o: (o.get("sequence", 0), o.get("timestamp", 0.0)))
        return rows


class _AgentConnectProviderSink(ObservabilitySink):
    """Adapter: fan a BC event dict into an AC ``AgentObservabilityProvider``.

    Constructed only when ``agentconnect`` is importable. It converts the dict to
    AC's ``AgentObservationEvent`` and calls the provider's ``append_event``, so
    BC emits into AC's REAL provider (JSONL/OTLP/tmux) with zero forked code.
    """

    name = "agentconnect"

    def __init__(self, provider: Any, event_cls: Any) -> None:
        self._provider = provider
        self._event_cls = event_cls

    def append_event(self, event: dict) -> None:
        self._provider.append_event(self._event_cls(**event))


class Emitter:
    """Assigns a monotonic per-emitter ``sequence``, builds a normalized event,
    and fans it out to a sink. Emission NEVER raises (advisory policy)."""

    def __init__(self, sink: Optional[ObservabilitySink] = None, *,
                 agent_id: str = "brainconnect",
                 provider_label: str = "brainconnect",
                 clock: Callable[[], float] = time.time) -> None:
        self.sink = sink or NoopSink()
        self.agent_id = agent_id
        self.provider_label = provider_label
        self._clock = clock
        self._lock = threading.RLock()
        self._seq = 0

    @property
    def enabled(self) -> bool:
        """True when a non-noop sink is configured."""
        return getattr(self.sink, "name", "noop") != "noop"

    def emit(self, event_type: str, *, trace_id: str,
             task_id: Optional[str] = None, outcome: Optional[str] = None,
             decision_class: Optional[str] = None,
             agent_role: str = "orchestrator",
             delegation_id: Optional[str] = None,
             subtask_id: Optional[str] = None,
             run_id: Optional[str] = None,
             review_id: Optional[str] = None,
             session_id: Optional[str] = None,
             workspace_id: Optional[str] = None,
             metadata: Optional[dict] = None) -> Optional[dict]:
        """Build and fan out one observation event. Returns the event dict (for
        inspection/tests) or ``None`` if it was swallowed. A sink failure — or any
        error building the event — is logged and never propagates."""
        try:
            with self._lock:
                self._seq += 1
                seq = self._seq
            trace = str(trace_id)
            md = _scrub_metadata(metadata)
            if decision_class is not None:
                md.setdefault("decision_class", str(decision_class)[:_MAX_STR])
            event = {
                "event_id": _event_id(trace, seq, event_type),
                "sequence": seq,
                "timestamp": float(self._clock()),
                "event_type": event_type,
                "state": _DEFAULT_STATE.get(event_type, "unknown"),
                "outcome": outcome,
                "trace_id": trace,
                "task_id": task_id,
                "delegation_id": delegation_id,
                "parent_delegation_id": None,
                "subtask_id": subtask_id,
                "session_id": session_id,
                "run_id": run_id,
                "review_id": review_id,
                "agent_id": self.agent_id,
                "agent_role": agent_role,
                "provider": self.provider_label,
                "workspace_id": workspace_id,
                "metadata": md,
            }
            self.sink.append_event(event)
            return event
        except Exception as e:  # noqa: BLE001 — non-fatal emission boundary.
            _log.warning("observability emit swallowed (%s): %s",
                         type(e).__name__, e)
            return None


# ---------------------------------------------------------------------------
# Env/config-driven selection. Default (unset) => Noop => no side effects.
# ---------------------------------------------------------------------------
_DISABLED = {"", "off", "noop", "none", "disabled", "0", "false"}
_STRUCTURED = {"structured_log", "jsonl", "log"}
_DEFAULT_LOG_PATH = "~/.brainconnect/observability/events.jsonl"


def sink_from_env(env: Optional[dict] = None) -> ObservabilitySink:
    """Select a sink from the environment. Unknown/unset is a SAFE Noop, never an
    error — observability is optional and must never fail startup."""
    env = os.environ if env is None else env
    mode = (env.get("BRAINCONNECT_OBSERVABILITY") or "").strip().lower()
    if mode in _DISABLED:
        return NoopSink()
    if mode == "agentconnect":
        sink = try_agentconnect_sink(env=env)
        if sink is not None:
            return sink
        _log.warning("BRAINCONNECT_OBSERVABILITY=agentconnect but agentconnect "
                     "is not importable; observability disabled")
        return NoopSink()
    if mode in _STRUCTURED:
        path = env.get("BRAINCONNECT_OBSERVABILITY_LOG_PATH") \
            or os.path.expanduser(_DEFAULT_LOG_PATH)
        try:
            return StructuredLogSink(path)
        except Exception as e:  # noqa: BLE001 — never fail on an unwritable path.
            _log.warning("structured_log sink unavailable (%s); disabled", e)
            return NoopSink()
    _log.warning("unknown BRAINCONNECT_OBSERVABILITY=%r; observability disabled",
                 mode)
    return NoopSink()


def try_agentconnect_sink(env: Optional[dict] = None) -> Optional[ObservabilitySink]:
    """Build a sink backed by AC's REAL StructuredLog provider when
    ``agentconnect`` is importable; otherwise ``None``. This is the "use the AC
    provider directly" path — BC emits into AC's own provider, no forked code."""
    env = os.environ if env is None else env
    try:
        from agentconnect.core.observability.model import AgentObservationEvent
        from agentconnect.core.observability.providers.structured_log import (
            StructuredLogObservabilityProvider,
        )
    except Exception:  # noqa: BLE001 — AC is optional; absence is not an error.
        return None
    path = env.get("BRAINCONNECT_OBSERVABILITY_LOG_PATH") \
        or os.path.expanduser(_DEFAULT_LOG_PATH)
    try:
        provider = StructuredLogObservabilityProvider(path)
    except Exception as e:  # noqa: BLE001
        _log.warning("agentconnect provider unavailable (%s)", e)
        return None
    return _AgentConnectProviderSink(provider, AgentObservationEvent)


_DEFAULT_EMITTER: Optional[Emitter] = None
_DEFAULT_LOCK = threading.Lock()


def default_emitter() -> Emitter:
    """A process-wide emitter built once from the environment. Never raises: any
    failure resolving a sink degrades to a disabled (Noop) emitter."""
    global _DEFAULT_EMITTER
    if _DEFAULT_EMITTER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_EMITTER is None:
                try:
                    _DEFAULT_EMITTER = Emitter(sink_from_env())
                except Exception as e:  # noqa: BLE001
                    _log.warning("observability disabled (env resolve failed: %s)",
                                 e)
                    _DEFAULT_EMITTER = Emitter(NoopSink())
    return _DEFAULT_EMITTER


def reset_default_emitter() -> None:
    """Drop the cached default emitter (tests re-read the environment)."""
    global _DEFAULT_EMITTER
    with _DEFAULT_LOCK:
        _DEFAULT_EMITTER = None


def emit_decision(emitter: Optional[Emitter], event_type: str, **kw) -> Optional[dict]:
    """The single call the decision points use. Resolves the default emitter when
    none is injected, and is itself non-fatal end to end (even resolving the
    default emitter cannot raise into the caller)."""
    try:
        em = emitter if emitter is not None else default_emitter()
        return em.emit(event_type, **kw)
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders non-fatal boundary.
        _log.warning("observability emit_decision swallowed (%s): %s",
                     type(e).__name__, e)
        return None
