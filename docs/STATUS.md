# STATUS.md — where wiki-brain stands

**Frozen for stabilisation.** WikiBrain is feature-complete for the current milestone
and deliberately held still while [mcp-agentconnect](https://github.com/Judgernaut777/mcp-agentconnect)
runs its manual dogfood of the proprietary-agent loop. The memory contract must not
move underneath that integration.

Last verified: **2026-07-10**, commit **`7cd2fe0`**, `main` in sync with origin,
working tree clean.

| | |
|---|---|
| Schema version | **9** (`schema.SCHEMA_VERSION == migrate.latest_version()`) |
| Gate | **435 checks pass, 0 failures** |
| Gate with `fascia_guard` stubbed | **439 checks pass, 0 failures** |
| Retrieval backend | `sqlite_fts` (the only one implemented) |
| Transport | in-process Python API + MCP stdio. **No HTTP server** — see below |

Run the gate with:

```bash
PYTHONPATH=/path/to/WikiBrain/cli python3 tests/acceptance.py
```

Four of those checks exercise the optional `fascia-guard` integration and are skipped
unless the package imports. They are otherwise dead code, so they rot silently — see
[MIGRATIONS.md](MIGRATIONS.md) for how the suite is kept honest.

---

## What WikiBrain is

A **trusted memory ledger** with a **pluggable retrieval backend**. It owns trust and
provenance; a backend owns search sophistication. Agents propose, humans promote.
Full design: **[LEDGER_SPEC.md](LEDGER_SPEC.md)**.

## The trust contract

> **`trusted is True` is the authority signal. `status == "promoted"` is not.**

This is the single rule a consumer must not get wrong. A promoted claim in an open
contradiction is returned `status: "promoted"`, `trusted: false`,
`contradiction_status: "open"` — because a contradiction is a warning, not a deletion,
and the claim remains of record.

- **Absence of `trusted` means untrusted.** Never infer trust from `status`.
- Only WikiBrain — or a consumer's own ledger / locked decisions — may confer trust.
  A retrieval backend reporting `trusted: true` cannot grant itself authority. The
  verdict may only ever **downgrade**.
- **With the defaults (`trusted_only=true`, `include_pending=false`), every item in a
  RecallPack has `trusted: true`.** Disputed, pending and superseded material is
  withheld and announced in `warnings`; opting into any of it is explicit and labeled.
- A backend returns **ids and scores**, never content or status. Recall re-reads every
  authoritative field from the ledger by id. This is what makes the boundary
  structural rather than a matter of discipline.

Stated normatively in [LEDGER_SPEC.md §14.1](LEDGER_SPEC.md).

## Migration behaviour

**`Repo.open()` runs forward migrations on every open** — including the one
`build_server()` performs at MCP launch. Migrations are forward-only and additive.

**A temporary repo root is not isolation.** `root=` selects which `config.toml` is
read; the database lives at an absolute path *inside* that config. Set **`WIKIBRAIN_DB`**
to a scratch path in tests, scripts and MCP verification. Full detail, the 2026-07-10
incident where a verification script migrated the live database, and the rules for
writing a migration: **[MIGRATIONS.md](MIGRATIONS.md)**.

## AgentConnect integration boundary

| Owner | Responsibility |
|---|---|
| **WikiBrain** | decides **trust** — promotion, provenance, scope, supersession, contradiction |
| **AgentConnect** | decides **context injection** — which bounded pack a manager or worker sees |
| **Cognee / Graphiti** | breadth and temporal recall. Neither is an authority |

The contract AgentConnect's `MemoryAdapter` binds to:

```python
recall(RecallRequest)           -> RecallPack
capture_candidate(CaptureRequest) -> CaptureResult
record_feedback(MemoryFeedbackRequest) -> None
health()                        -> dict
```

WikiBrain accepts `origin_actor_id` / `origin_actor_type` as aliases for
`proposed_by` / `proposed_by_type`, and stores `task_id` / `source_ref` opaquely —
it never resolves them. See [LEDGER_SPEC.md §14](LEDGER_SPEC.md).

Verified end-to-end by `mcp-agentconnect/tests/test_wikibrain_integration.py`, which
drives a real ledger through AgentConnect's adapter, ranker and ContextBuilder.

## Known gap: transport

**WikiBrain ships no HTTP server.** AgentConnect's `WikiBrainMemoryAdapter` expects a
REST service at `http://localhost:8787`:

```
POST /recall            POST /candidates/{candidate_id}/promote
POST /capture           GET  /candidates?status=pending&limit=
POST /feedback          GET  /health
```

The integration test closes this with a `transport` that dispatches those routes
straight into `wiki.api` in-process. That is deliberate, and sufficient for the
boundary it tests: real ledger, real promotion, real trust filter, real field shape.
It exercises **no wire plumbing** — no serialisation, status codes, auth, or timeouts.

> **A green integration suite means the semantics agree, not that the network path
> exists.**

`wiki serve` is the deferred follow-up that closes it. It is tracked separately, on
purpose, so the semantic boundary and the transport surface cannot be confused for one
another. It is **not** started, and should not begin until explicitly asked after the
AgentConnect dogfood run. See [LEDGER_SPEC.md §14.2](LEDGER_SPEC.md).

## Change policy while frozen

Stabilisation and documentation only. Do **not** add: new memory profiles, new backend
behaviour, new MCP tools, new promotion paths, new recall semantics, new ingestion
behaviour, or `wiki serve`.

Code changes are in scope only when AgentConnect finds a concrete:

- **field-shape mismatch** (a field it needs that recall does not emit, or emits differently),
- **trust or scope mismatch** (the two repos disagreeing about what is visible or trusted), or
- **migration issue**.

Anything else waits for the freeze to lift.
