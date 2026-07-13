# Observability — BrainConnect Lane 8

Status: **shipped**. Contract for ADR 0008 Lane 8 ("Worker orchestration, multi-model
roles, independent verification, **and the observability event model → AgentConnect**").

## What this is

BrainConnect (BC) makes its orchestration **decisions** observable by **emitting them
into AgentConnect's (AC) existing observability provider seam, using AC's existing
`EventType` vocabulary.** BC is one more *emitter* into AC's stream. It does **not** own
the stream, and it does **not** define a competing event model.

This is the binding boundary from ADR 0008:

> BC *emits into* the `AgentObservabilityProvider` seam using the existing `EventType`
> vocabulary; it does not define a competing event stream, timeline, or token ledger.

> **Do NOT duplicate AgentConnect's router, delegation, governor, or observability model.**
> … no parallel `EventType` …

So BC has **no** BC-native `EventType` enum, **no** timeline, **no** run-history, **no**
token ledger. It mirrors *only the subset of AC wire values it emits* as plain string
constants, and pins them to AC's real `EventType` with a conformance test.

## The seam (what AC owns, in the AgentConnect repo)

- `core/observability/model.py` — `EventType` (stable wire strings, e.g. `"subtask.routed"`),
  `AgentObservationEvent`, `ObservationState`, `ObservationOutcome`,
  `DEFAULT_STATE_FOR_EVENT`.
- `core/observability/provider.py` — `AgentObservabilityProvider` base; the fan-out method
  is `append_event(event) -> None`.
- providers: `noop`, `structured_log` (JSONL), `composite`, `otlp`, `tmux`, `herdr`.

BC does not modify any of it.

## The BC side (this repo)

`cli/brainconnect/observability.py`:

- **Mirrored vocabulary (not forked).** `EVT_SUBTASK_ROUTED`, `EVT_DECISION_RECORDED`,
  `EVT_MEMORY_CAPTURED`, `EVT_COMPUTE_PLACED` are plain string constants equal to AC's wire
  values. A **conformance pin** (`tests/acceptance.py::_observability_checks`) imports AC's
  real `EventType` when it is importable and asserts every BC constant byte-matches, plus that
  a BC-emitted event round-trips into AC's `AgentObservationEvent` unchanged. It **skips
  cleanly** when AC is not importable — exactly like Lane 4's privacy pin. `agentconnect` is
  therefore **never a required dependency**.
- **`Emitter`** assigns a monotonic per-emitter `sequence`, resolves `state` from the mirrored
  `DEFAULT_STATE_FOR_EVENT`, builds a dict with AC's full `AgentObservationEvent` field set,
  and fans it out to a sink. `emit()` is wrapped so **any** sink error (or build error) is
  logged and swallowed — it can never propagate into the orchestration operation.
- **Sinks.** `NoopSink` (default, disabled), `StructuredLogSink` (one JSON object per line,
  byte-shaped like AC's own JSONL provider), and — when `agentconnect` is importable —
  `_AgentConnectProviderSink`, which converts the dict to AC's `AgentObservationEvent` and
  calls AC's **real** provider (`try_agentconnect_sink`). BC thus emits into AC's own
  provider with zero forked code when AC is present.

### Selection (env-driven, default OFF)

| `BRAINCONNECT_OBSERVABILITY` | sink |
| --- | --- |
| unset / `off` / `noop` / `none` / `disabled` | `NoopSink` (disabled) |
| `structured_log` / `jsonl` / `log` | `StructuredLogSink` |
| `agentconnect` | AC's real provider if importable, else Noop |
| anything else | Noop (warned, never crashes) |

`BRAINCONNECT_OBSERVABILITY_LOG_PATH` sets the JSONL path (default
`~/.brainconnect/observability/events.jsonl`). **Unset ⇒ Noop ⇒ no file, no side effect.**
The decision points default to `observability.default_emitter()` (built once from the
environment; degrades to Noop on any resolution error).

## The EventType mapping (BC decision → AC `EventType`)

| BC decision point (module) | AC `EventType` | wire value | `outcome` | metadata (ids + scalars only) |
| --- | --- | --- | --- | --- |
| Lane 1 registry capability-claim seed (`registry.seed`) | `memory_captured` | `memory.captured` | `succeeded` if any filed, else `unknown` | `decision_class="registry-capability-seed"`, `filed_count`, `idempotent_noop` |
| Lane 4 delegation/routing decision (`delegate.delegate`) | `subtask_routed` | `subtask.routed` | `succeeded` if delegated, else `unknown` | `decision_class` (`delegated`/`deferred`), `delegated`, `fallback`, `capability_class`, `privacy_effective`, `has_routing`, `has_placement`, `rejected_offbox`, `error_count` |
| Lane 6 role assignment (`roles.assign_roles`) | `decision_recorded` | `decision.recorded` | `succeeded` if all resolved, else `denied` | `decision_class="role-assignment"`, `assigned_count`, `refused_count`, `collision_count`, `ok` |
| Lane 7 perfcapture pass (`perfcapture.capture`) | `memory_captured` | `memory.captured` | `succeeded` if any captured, else `unknown` | `decision_class="perfcapture-telemetry"`, `cc_available`, `observed`, `captured_count`, `duplicates`, `skipped_count`, `error_count` |

`registry` and `perfcapture` both map to `memory.captured` because both *capture a memory
candidate into BC's ledger*; `metadata.decision_class` disambiguates. `state` follows AC's
`DEFAULT_STATE_FOR_EVENT` for each type (`subtask.routed → starting`, the others → `working`).

## Guarantees

- **Non-fatal.** A sink that raises on every append never breaks the operation: the registry
  seed, delegation, role assignment, and perfcapture all still succeed and record provenance.
  Proven in `_observability_checks`.
- **Optional.** With observability off (the default), every decision point runs normally and
  records provenance. BC fully functions with observability disabled; AC is not a dependency.
- **No secrets / no raw private context.** An event carries **only** the correlation id set,
  an event type, a state, an outcome, and a `metadata` dict restricted **by construction** to
  small scalars (a decision class, counts, booleans, enum strings). Never a prompt, a
  completion, a raw request/decision body, or a URL (which may carry credentials).
  `_scrub_metadata` drops any nested dict/list value and bounds string length, so even a
  mistaken caller cannot smuggle a payload through metadata. Proven by emitting a decision
  derived from a secret-bearing engine response and asserting the event contains none of it.
- **Deterministic + side-effect-light.** `event_id` is a stable digest of
  `(trace_id, sequence, event_type)` (an idempotency key AC dedupes on); `sequence` is
  monotonic per emitter. The Noop default writes nothing.

## Reading the stream

When enabled with a JSONL sink, one event is one line. Because the format matches AC's own
`structured_log` provider, the same file is readable by AC's `agents events` reader and by
`StructuredLogSink.read_events()` (re-sorted by `(sequence, timestamp)`). When
`BRAINCONNECT_OBSERVABILITY=agentconnect`, BC writes through AC's real provider instead, so
the events land in AC's configured surface directly.
