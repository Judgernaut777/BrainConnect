# DELEGATION.md — the delegation trigger (ADR 0008 Lane 4)

Status: **active** (ADR 0008 Lane 4, 2026-07-13). Governed by
[ADR 0008](adr/0008-orchestration-boundary.md) and
[ORCHESTRATION.md](ORCHESTRATION.md). Consumes the Lane-1 registry
([REGISTRY.md](REGISTRY.md)) and binds to
[LEDGER_SPEC.md](LEDGER_SPEC.md) §2 (promotion is human-only) and §14 (the
`:8787` memory link / transport).

Code: `cli/brainconnect/delegate.py` (the trigger) and
`cli/brainconnect/delegate_clients.py` (the two thin HTTP client adapters).
CLI: `brainconnect delegate`.

---

## 1. What this is (and is not)

The delegation trigger is BrainConnect's Lane-4 seam onto the two engines that
own routing and placement. It does exactly three things:

1. **Assembles** a routing/placement request from **trusted** capability claims
   (the Lane-1 registry `trusted_view`) plus a **workload spec**.
2. **Calls** AgentConnect's capability router (`RoutingEngine.route`) and
   ComputeConnect's placement estimate (`POST /route/estimate`) as a *client*.
3. **Records** the returned decision + rationale as ordinary BrainConnect
   decision-provenance memory.

It contains **zero** routing / placement / scheduler / residency math. Per
ADR 0008 that math ships in AgentConnect (`RoutingEngine.route`,
`capability_overlap`, `residency_bonus`, `model_switch_penalty`) and
ComputeConnect (`select_placement`, queue-seconds), and is **delegated, never
duplicated**. BrainConnect supplies requirements and records the answer.

It is **not** a task ledger, a scheduler, or a live-state holder. It records a
decision; it never holds the run.

---

## 2. The two client adapters

Thin HTTP clients that speak the *exact* shapes the engines own — request in,
decision out — and hold no logic of their own (`delegate_clients.py`).

| Engine | Contract | Request | Response |
|---|---|---|---|
| AgentConnect router | `RoutingEngine.route(ctx, status) -> RoutingDecision` | `RoutingContext` (task_id, privacy_class, needed_capabilities, require_exact_model, est_input/output_tokens, allow_external/paid/rented, cloud_safe, priority, quality, …) | `RoutingDecision` (decision, selected_provider, selected_model, rejected_options, scores, policy_version) |
| ComputeConnect placement | `POST /route/estimate` | body (model, required_capabilities, context_tokens, max_output_tokens, latency_preference, quality_preference, privacy_tier) + `X-Privacy-Tier` header | `{eligible, selected_model, runtime, loaded, estimated_queue_seconds, estimated_tokens_per_second, estimated_quality, reason}` |

**Transport note (the recon's critical gap).** AgentConnect does **not** yet
publish a clean "give me a decision" HTTP endpoint: the router package is an MCP
stdio server whose tools *execute* a generation, and the `agentconnect-api` HTTP
surface exposes a different subtask-router shape (`GET
/subtasks/{id}/route -> RouteExplanation`). So BrainConnect binds to the faithful
`RoutingContext -> RoutingDecision` contract through an **injectable** client:
`HttpRoutingClient` POSTs a `RoutingContext` and expects a `RoutingDecision`, and
the trigger is smoked against an in-process fake honouring the same shape. When
AgentConnect ships a bare decision endpoint, only the client's URL/verb changes —
no BrainConnect logic moves. ComputeConnect's `/route/estimate` is a real,
shipped endpoint and `HttpEstimateClient` speaks it directly.

Every transport failure — connection refused, timeout, non-2xx, unparseable body
— is raised as a single `DelegationClientError`, which the trigger treats as
"engine unavailable" and turns into a deterministic fallback (§4). Network I/O is
allowed (a service call is not a model call); no API keys, no model generation.

---

## 3. Assembly — trusted claims ⊕ workload

`assemble_request(repo, workload)` builds both engine requests:

- **Requirements** (the `needed_capabilities` / `required_capabilities`) come
  from the registry tier's **structural** capabilities, and the `require_exact_model`
  / CC `model` is pinned **only** to a model whose registry claim a human has
  **promoted** (read from `registry.trusted_view`, which excludes pending and
  squatted facts). BrainConnect assembles from trusted claims only.
- **Sizing, priority, quality, latency** come from the workload spec.
- **Privacy** is clamped to a floor (§4) and never relaxed downstream.

The workload's `allow_external` / `allow_paid` / `allow_rented` are **ceilings**:
BrainConnect ANDs them with the privacy floor, so it can only ever *tighten* them.

---

## 4. Privacy: BrainConnect never widens

The workload declares a privacy tier. BrainConnect's canonical scale is a
byte-for-byte mirror of AgentConnect's `PRIVACY_STRICTNESS` (loosest `public` = 0
… strictest `secret_sensitive` = 4), which ComputeConnect also mirrors.

- **Fail closed.** An absent / non-string / unknown tier resolves to
  `secret_sensitive`, `assumed=True` — exactly ComputeConnect's default-deny.
- **Never widen downstream.** The canonical tier maps to AgentConnect's
  `PrivacyClass` vocabulary such that the emitted value is never looser
  (`public_redacted → low_sensitive`, `local_only → restricted`, others
  identity). The CC `privacy_tier` is the canonical string, and the
  `X-Privacy-Tier` header is set equal to it — by CC's
  `resolve_privacy_precedence` (more-restrictive-wins) a header can only ever
  confirm the floor, never widen it.
- **Off-box only when permitted.** Only `public` / `public_redacted` may leave
  the box (mirrors CC `CLOUD_PERMITTING_TIERS`). For any stricter tier,
  `allow_external` / `allow_paid` / `allow_rented` / `cloud_safe` are forced
  `False`.
- **A hostile response cannot widen.** BrainConnect derives privacy only from the
  workload floor, never from an engine response. If AgentConnect returns an
  off-box placement (`route_to_rented_node` / `route_to_cloud_provider`) for a
  tier the floor keeps on-box, the decision is **refused** (recorded as
  `rejected_decision`) and the safe fallback is used instead.

---

## 5. Deterministic no-SPOF fallback

No provider is a required dependency. If AgentConnect **or** ComputeConnect is
unavailable, times out, errors, returns malformed data, or returns a
privacy-widening decision, the trigger does **not** crash. It emits a
deterministic fallback:

- `outcome_class = "deferred"`, `fallback = True`, `delegated = False`.
- No provider / model selected; nothing routed externally (keep on-box / queue
  for human dispatch).
- Privacy stays at the workload floor.
- An explicit `fallback_reason` names which engine(s) failed and why; any partial
  information (e.g. a valid CC estimate when AC is down) is retained.

With **both** engines down BrainConnect still fully functions and records the
deferred decision. This is the ADR-0008 "no provider may be a required
dependency" rule made structural.

---

## 6. Provenance — recorded, never promoted

The decision (delegated or fallback) is filed via `api.capture_candidate` as an
ordinary **PENDING** memory candidate, scoped `task:<task_id>`, tagged
`orchestration-decision` / `delegation-provenance`, with the full assembled
request + AC decision + CC estimate in `metadata` for later explainability. It
carries `provenance_only: true` and `trusted: false`.

It is **never** auto-trusted and **never** self-promoted: promotion is
human/librarian-only (LEDGER_SPEC §2), and no agent/tool reviewer type can confer
trust. ADR 0008 §3 calls decision provenance "trusted memory" as an *aspiration
after human promotion*; the trigger honestly files it PENDING, because a decision
recorded by a tool cannot self-confer trust. A malformed or hostile engine
response therefore can never cause BrainConnect to record something as trusted.

The provenance text carries a deterministic decision fingerprint so two distinct
decisions never collide on the ingest content hash; an exact re-decision dedups,
and a duplicate is non-fatal (the trigger degrades to a note, never crashes).

---

## 7. CLI

```
brainconnect delegate <task_id> <capability_class> \
    [--privacy-tier T] [--input-tokens N] [--output-tokens N] \
    [--priority P] [--quality Q] [--latency L] [--capability CAP ...] \
    [--allow-external] [--allow-paid] [--allow-rented] [--pin-registry-model] \
    [--ac-url URL] [--ac-token T] [--cc-url URL] [--cc-token T] \
    [--no-record] [--json]
```

An absent `--ac-url` / `--cc-url` means that engine is unavailable, so the
command returns the deterministic fallback (never a crash). No model is ever
called; the CLI stays key-free and model-free.

A guarded live smoke exists in the acceptance suite behind
`BRAINCONNECT_LANE4_LIVE` (+ `BRAINCONNECT_AC_URL` / `BRAINCONNECT_CC_URL`);
it is skipped by default so the gate stays offline and hermetic.
