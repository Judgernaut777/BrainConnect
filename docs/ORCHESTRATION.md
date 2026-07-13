# BrainConnect as a deterministic orchestration layer — lane plan

This is the sequenced execution plan for the "BrainConnect as the deterministic
orchestration layer above Decima" epic. It is governed by
[ADR 0008](adr/0008-orchestration-boundary.md), which decides — per capability — what
BrainConnect (BC) **owns** versus **delegates** to ComputeConnect (CC), AgentConnect (AC),
and Decima (D). The one-line rule: **BC reasons about capabilities and records decisions;
it never re-implements routing, placement, scheduling, worker orchestration, or the
observability stream.**

Ownership legend: **A** = BC-native new · **B** = delegate to an existing CC/AC/Decima
contract · **C** = thin BC adapter over an existing contract · **D** = genuinely missing
everywhere.

## Lanes (dependency-ordered)

| Lane | Capability | Ownership | Owner | First deliverable | Depends on |
|---|---|---|---|---|---|
| 1 | Capability registry + tier hierarchy (small → general-doc → high-capability-local → frontier-managers) + preferred-model declaration | **A** (trusted registry) + **B** (runtime tiers to AC/CC) | BC | ✅ **done** — `cli/brainconnect/registry.py` + `brainconnect registry` CLI + [REGISTRY.md](REGISTRY.md). Capability facts are ordinary claims bound to LEDGER_SPEC §7 `model_performance` + §5.5 `model:`/`worker:` scope; the tier hierarchy is a **data-driven** seed (no code branches on tier name). `Qwen3.6-35B-A3B` recorded as the *declared preferred* high-capability-local model (no numbers, not a required dependency); `qwen3-30b-a3b` as the *deployed* model. Seeding files **pending** candidates only; promotion is human/librarian-only (no auto-promote path). **No benchmark numbers.** | none (foundation) |
| 2 | Published Decima capability-reasoning read-contract (planning/approvals/workspaces/knowledge/agents/artifacts) | **B** (surface lives in Decima) | Decima (BC consumes) | A versioned read-contract *in the Decima repo* stabilizing `projections.{tasks,approvals,agents,knowledge,activity}` with `instruction_eligible` exposed. BC codes against the contract, not Python objects. | none (parallel to L1) |
| 3 | Transport for the registry (BC↔AC/CC memory link, `:8787`) | **A** (claim endpoint) + **B** (consumption) | BC + AC | ✅ **BC side done** — read-only `GET /registry` (+ `/registry/capabilities` alias) on the existing `:8787` server serves ONLY trusted, human-promoted capability claims (`registry.trusted_view`), bearer-authed like every other route, pending/squatted facts excluded, no fabricated numbers, deterministic. See [REGISTRY.md §6](REGISTRY.md). **AC side delegated to the AgentConnect repo:** AC's `RoutingEngine` pulls this endpoint and weights it in place of self-conferred `learned_quality` — out of scope for BC. | L1 |
| 4 | Capability router + warm-aware swap-minimizing scheduler | **B** (fully) | AC (routing/residency) + CC (placement) | ✅ **done** — a thin BC delegation trigger (`cli/brainconnect/delegate.py` + `delegate_clients.py`, `brainconnect delegate`, [DELEGATION.md](DELEGATION.md)) assembles a request from **trusted** registry claims + a workload, calls AC `RoutingEngine.route` and CC `/route/estimate` through injectable clients, and records the returned decision + rationale as **PENDING** decision-provenance (never auto-promoted). Deterministic no-SPOF fallback when AC/CC are down/malformed/hostile; privacy is clamped and never widened. **Zero routing/placement math in BC.** AC's decision-only HTTP endpoint is the one gap (recon): BC binds the faithful `RoutingContext -> RoutingDecision` shape via an injectable client and smokes against a fake. | L3, L2 |
| 5 | Unified knowledge abstraction (adapters → WikiBrain → graph → OKF → external), federating Decima knowledge | **A** (core) | BC | ✅ **done** — `cli/brainconnect/federation.py` (`DecimaKnowledgeBackend` + injectable `DecimaKnowledgeSource`) surfaces Decima knowledge at READ TIME via the L2 read-contract and honors `instruction_eligible` **exactly** as BC honors `trusted` (fail-closed: surfaced trusted only when the bit is a real `True`; untrusted is DATA, opt-in). **Federate, do not fork** — nothing is written to the BC ledger. Implemented as a **sibling read-time seam**, not a §8 backend: §8 `BackendCandidate`s are content-free and re-read by integer id from BC's `claims` table, so a foreign Decima `str` id would be dropped ([LEDGER_SPEC §8bis](LEDGER_SPEC.md)); Decima re-resolves its own items and BC merges them after native recall. Foreign text passes the SAME read-door safety pass; hostile/oversized data is bounded; optional + **non-fatal** (a missing/erroring source contributes nothing, `decima` never a required dependency); conformance-pinned to Decima's `READ_CONTRACT_VERSION`. Contract [FEDERATION.md](FEDERATION.md). | L2 |
| 6 | Multi-model collaboration roles (planning/coding/reviewer/verifier/docs) + independent verification | **B** (fully) | AC (D executes) | ✅ **done** — BC MAPS a plan's role requirements to existing AC model-manager profiles (`general_coder`/`coding_specialist`/`review_worker`/`critic`) via a data-driven table and RECORDS the role-assignment as **PENDING** provenance; it flags reviewer/implementer profile collisions as a **recommendation**. AgentConnect EXECUTES (triggers `RouterService` decompose→execute→synthesize with the `review.*` lifecycle) and, with Decima, ENFORCES ownership/independence. BC spawns nothing, makes zero model calls, assigns no ownership. Code `cli/brainconnect/roles.py`, CLI `brainconnect roles`, contract [ROLES.md](ROLES.md). **No role engine/verifier in BC.** | L4 |
| 7 | Performance (prompt caching, benchmarking, telemetry, queue analytics, load prediction) feeding the registry | **B** (measurement) + **A** (capture/promote loop) | CC/AC (measure) → BC (trusted capture) | ✅ **BC capture side done** — `cli/brainconnect/perfcapture.py` + `brainconnect perfcapture` + [PERFCAPTURE.md](PERFCAPTURE.md). Reads CC's side-effect-free telemetry (`/health`, `/models`, `/models/loaded`, optional `/route/estimate` rationale) through an injectable bounded client and files each observed model availability/perf fact as a **PENDING** `model_performance` candidate — source-labelled (`kind` `measured` vs `estimate`), `model:`-scoped, safety-scanned, idempotent (unforgeable per-observation fingerprint), **never auto-promoted**. The deployed-model refresh is a captured candidate, not a mutation of the trusted claim. **Zero model calls; no fabricated numbers.** MEASUREMENT stays delegated to CC/AC. | L1, L4 |
| 8 | Observability (queued work, active agents, utilization, provider health, routing decisions, timelines, token accounting, swap history) | **B** (fully) | AC (event model) | BC emits its orchestration decisions (registry promotion, delegation trigger, role assignment) **into** the existing `AgentObservabilityProvider` using the shipped `EventType` vocabulary. **No parallel event stream/timeline/token ledger in BC.** | L4, L6 |

## Binding prohibitions (from ADR 0008)

- Do **not** re-implement ComputeConnect's placement engine (`select_placement`, warm-state
  table, queue-seconds math, provider snapshot). Call CC `/route/estimate`, read the rationale.
- Do **not** re-implement AgentConnect's capability router (`capability_overlap`, eligibility
  gating, `RoutingEngine.route`). Call AC, record the returned `RoutingDecision`.
- Do **not** re-implement AC's swap-minimizing residency policy (`residency_bonus`,
  `model_switch_penalty`, `queue_delay_penalty`, `min_batch_size_for_switch`).
- Do **not** re-implement AC's delegation/roles/governor (recursive decompose→execute→
  synthesize, `review.*`, reviewer/critic/verifier, worker spawning).
- Do **not** define a parallel observability event model. Emit into AC's
  `AgentObservabilityProvider` with the existing vocabulary.
- Do **not** touch Decima's execution/authorization internals (Weft, leases,
  `capability_proof`, `implementation_digest`, worker IPC). Submit intent; read projections.
- Do **not** fork Decima's knowledge ledger. Federate over `projections/knowledge.py` and
  honor `instruction_eligible`.
- Do **not** let any model/agent promote a capability claim (about itself or any model).
  Promotion is human/librarian-only (LEDGER_SPEC §2). AC's self-conferred `learned_quality`
  is an **input candidate**, never a trusted claim.
- Do **not** hold live orchestration state in BC (active tasks, worker runs, loaded model).
  BC records decisions and captures candidates; it never re-holds runtime state.
- Do **not** make any provider a required dependency. The registry and knowledge planes
  function with zero models loaded.

## Cross-repo dependencies & open questions (do not resolve unilaterally inside BC)

1. **Model name** — `Qwen3.6-35B-A3B` (brief) exists nowhere; deployed model is
   `qwen3-30b-a3b`. Handled by the registry as declared-preferred vs deployed (see ADR 0008
   "Model-name reconciliation"); a one-word user correction updates the preference claim.
2. **`:8787` transport ownership (L3)** — ✅ **resolved for the BC side.** BC gained a
   minimal read-only HTTP surface for trusted claims: `GET /registry` on the existing
   `brainconnect serve` server (`cli/brainconnect/server.py`), serving `registry.trusted_view`.
   AC pulls it and weights the result — that consumption side lives in the AgentConnect repo.
3. **Decima read-contract authorship (L2)** — the projections exist and look stable, but the
   versioned external contract belongs *in the Decima repo*; whether this epic authorizes
   BC's lead to open that contract there is a cross-repo governance question.

## Status

- **Lane 0 (boundary ADR):** ✅ complete — ADR 0008 accepted.
- **Lane 1 (capability registry + tier hierarchy):** ✅ complete — trusted,
  human-gated, `model_performance`-scoped registry with the data-driven tier
  hierarchy and the declared-preferred/deployed model distinction. See
  [REGISTRY.md](REGISTRY.md); code in `cli/brainconnect/registry.py`, read surface
  `brainconnect registry list`.
- **Lane 3 (`:8787` transport, BC side):** ✅ complete — read-only `GET /registry`
  on the existing server serves trusted-only capability claims for AC to pull; the
  AC-side pull + weighting is delegated to the AgentConnect repo. See
  [REGISTRY.md §6](REGISTRY.md); code in `cli/brainconnect/server.py`
  (`registry.trusted_view`).
- **Lane 4 (delegation trigger):** ✅ complete — a thin BC trigger assembles a
  routing/placement request from **trusted** registry claims + a workload, calls
  AgentConnect `RoutingEngine.route` and ComputeConnect `/route/estimate` through
  injectable HTTP clients, and records the returned decision as **PENDING**
  decision-provenance (never trusted, never self-promoted). Deterministic
  no-single-point-of-failure fallback when either or both engines are
  down/timing-out/malformed, and a privacy floor that BC never widens (a hostile
  off-box decision for restricted work is refused, not obeyed). Code in
  `cli/brainconnect/delegate.py` + `cli/brainconnect/delegate_clients.py`,
  CLI `brainconnect delegate`, contract in [DELEGATION.md](DELEGATION.md). The one
  cross-repo gap (recon): AgentConnect exposes no bare "decision-only" HTTP
  endpoint yet, so BC binds the faithful `RoutingContext -> RoutingDecision`
  shape via an injectable client and smokes against a fake — when AC ships the
  endpoint, only the client URL changes.
- **Lane 7 (performance capture, BC side):** ✅ complete — a thin capture adapter
  reads ComputeConnect's side-effect-free telemetry (`/health`, `/models`,
  `/models/loaded`, and an optional `/route/estimate` rationale) through an
  injectable bounded client and files each observed model availability/performance
  fact as a **PENDING** `model_performance` candidate: source-labelled
  (`source=computeconnect-telemetry`, `kind` `measured` vs `estimate`),
  `model:`-scoped, safety-scanned (a secret in a telemetry field is masked, never
  stored raw), and idempotent by an unforgeable per-observation fingerprint (a
  re-run dedupes, a changed value is captured, a genuine new observation is never
  suppressed). **Never auto-promoted** — promotion stays human/librarian-only; the
  deployed-model refresh (e.g. `Qwen3.6-35B-A3B` on the live node) is captured as a
  candidate and never auto-mutates the trusted `deployed` claim. **Zero model
  calls; no fabricated numbers** — MEASUREMENT stays delegated to CC/AC. Code in
  `cli/brainconnect/perfcapture.py` + `cli/brainconnect/delegate_clients.py`
  (`HttpTelemetryClient`), CLI `brainconnect perfcapture`, contract in
  [PERFCAPTURE.md](PERFCAPTURE.md).
- **Lane 6 (agent-role assignment):** ✅ complete — BC MAPS a plan's role
  requirements (implementer / test_reviewer / security_reviewer /
  documentation_reviewer / verifier / research_agent / integration_agent) to
  existing AgentConnect model-manager profiles through a deterministic DATA table
  (`ROLE_TABLE`; nothing branches on a role name, and a swap is a data edit —
  provider portability), and RECORDS the assignment as **PENDING** provenance
  (never trusted, never self-promoted). An unknown role is **fail-closed**
  (refused, never mapped). Reviewer independence is a **recommendation**: BC flags
  when a reviewer/verifier would share the implementer's profile so an
  operator/AC can preserve independent review — but BC does not enforce it,
  spawn workers, execute, or assign ownership. AgentConnect executes
  (`RouterService` decompose→execute→synthesize, `review.*`) and, with Decima,
  enforces ownership/independence/concurrency. A role assignment composes with the
  Lane-4 delegation trigger on the shared Lane-1 tier vocabulary. Code in
  `cli/brainconnect/roles.py`, CLI `brainconnect roles`, contract in
  [ROLES.md](ROLES.md).
- **Lane 8 (observability):** ✅ complete — BC EMITS its orchestration decisions
  INTO AgentConnect's existing `AgentObservabilityProvider` seam using AC's
  existing `EventType` vocabulary; it defines **no** competing event stream,
  timeline, run-history, or token ledger (ADR 0008 prohibition). BC mirrors only
  the subset of AC wire values it emits (`subtask.routed`, `decision.recorded`,
  `memory.captured`) as plain constants pinned to AC's real `EventType` by a
  **conformance test** (skips when AC absent — like the Lane-4 privacy pin), so
  `agentconnect` is **never a required dependency**. Emit sites: registry
  capability-seed (Lane 1 → `memory.captured`), delegation (Lane 4 →
  `subtask.routed`), role assignment (Lane 6 → `decision.recorded`), perfcapture
  (Lane 7 → `memory.captured`) — each carries correlation ids + a decision class +
  small scalars, **never** a prompt/completion/raw body/URL. Default sink is Noop
  (disabled); a StructuredLog JSONL sink or AC's own provider is env-selectable.
  Emission is **non-fatal** (a sink error is swallowed, never breaks the
  operation) and **optional** (BC fully functions with it off). Code in
  `cli/brainconnect/observability.py`, contract in [OBSERVABILITY.md](OBSERVABILITY.md).
- **Lane 5 (unified knowledge abstraction — federating Decima knowledge):** ✅
  complete — BC surfaces **Decima's own knowledge** inside a recall pack at READ
  TIME, read through Decima's published Lane-2 read-contract, and **never forks it**
  into the BC ledger (nothing is written; a federated item vanishes when Decima
  retracts it). It honors Decima's `instruction_eligible` bit **exactly** as BC
  honors its own `trusted` bit: surfaced `trusted` only when the bit is a real
  `True` (fail-closed on any disagreement), untrusted federated text is DATA and
  opt-in (`trusted_only=false`) exactly like BC's own untrusted material. Foreign
  provenance is untrusted input, so it passes the SAME read-door safety pass
  (mask / withhold / quarantine); hostile/oversized data is bounded and
  deterministically ordered. Implemented as a **sibling read-time seam**, not a §8
  backend — §8 `BackendCandidate`s are content-free and re-read by integer id from
  BC's `claims` table, so a foreign Decima string id has no row and would be dropped
  ([LEDGER_SPEC §8bis](LEDGER_SPEC.md)); Decima re-resolves its own items and BC
  merges them after native recall. The source is **injectable**
  (`DecimaKnowledgeSource`, stub for tests + an optional `DECIMA_SRC`/`DECIMA_WEFT`
  real adapter) and **optional + non-fatal**: absent/erroring Decima contributes
  nothing and BC retrieval is unaffected — `decima` is **never** a required
  dependency. A **conformance pin** asserts BC's expected `READ_CONTRACT_VERSION` +
  knowledge field set match Decima's when importable (skips otherwise). Zero model
  calls. Code in `cli/brainconnect/federation.py` + `recall._federate`, contract in
  [FEDERATION.md](FEDERATION.md).
- **Lane 2:** delegated to the Decima repo (the versioned read-contract lives
  there and is published — `decima.read_contract`, `READ_CONTRACT_VERSION` 0.1);
  BC consumes it in Lane 5. **All BC-side lanes (1, 3, 4, 5, 6, 7, 8) are shipped.**
