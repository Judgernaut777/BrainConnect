# STATUS.md — where BrainConnect stands

**Stable, and standalone.** BrainConnect is a trusted memory ledger. It runs on its
own, needs nothing else installed, and is not accepting new memory features. Work
from here is stabilisation, documentation, and the deferred items listed below.

Last verified: **2026-07-12**, at the 0.1.0 release prep.

## A note on the name

The GitHub repository is **`Judgernaut777/BrainConnect`** (the old `WikiBrain`
URL redirects), and as of 2026-07-12 **the code is renamed to match**: the Python
package is `brainconnect`, the CLI entry points are `brainconnect` and
`brainconnect-librarian`, the MCP tools remain `brain_*`, and the isolation
variable is `BRAINCONNECT_DB` (`WIKIBRAIN_DB` is honored with a
`DeprecationWarning` only while the new name is unset — the single shim the
rename kept, because a stale isolation setup that silently stopped working would
migrate a live DB). The local checkout directory is still `WikiBrain/`, and the
default live-DB path is still `~/.wiki-brain/wiki.db` — moving personal data on
disk was deliberately out of scope (a known limitation, not an oversight).

Heads-up for `mcp-agentconnect`: its `tests/test_wikibrain_integration.py`
does `importorskip("wiki.api")` and now **skips** instead of running; it should
import `brainconnect.api`.

## Current checkpoint

| Checkpoint | Commit | What it is |
|---|---|---|
| **Contract tip** | **`221e4f2`** | The consumer contract: fixtures in `tests/contract/`, and the refusal taxonomy in `cli/brainconnect/errors.py`. Additive only — no behaviour changed. |
| **Behaviour tip** | **`b128e65`** | Memory safety: `cli/brainconnect/safety/`, enforced at capture, recall and promotion. **The last commit that changed enforced behaviour.** Diff against this when asking "did anything move?" |
| **Trust behaviour** | **`b69e13c`** | `trusted_only` began meaning trusted; disputed claims stopped leaking as trusted. |
| **Tag** | **`v0.1.0-mvp-control-loop`** | Annotated, at `f10569d`. The MVP control-loop checkpoint, taken before safety landed. |

The earlier freeze marker (`c855af9`) and its "docs-only" policy are **superseded**
by this document. That freeze existed to hold the memory contract still during
AgentConnect's dogfood; the contract has since been extended, additively, by safety.

| | |
|---|---|
| Schema version | **9** (`schema.SCHEMA_VERSION == migrate.latest_version()`); unchanged by safety |
| Gate | **0 failures**; the check count varies with platform and optional extras (Linux core ≈ 950, Linux + `[semantic]` (numpy) ≈ 953; Windows is lower — the POSIX-only exec-bit and symlink-escape checks do not run there) |
| Retrieval backend | `sqlite_fts` (the only one implemented) |
| Transport | in-process Python API + MCP stdio + **`brainconnect serve` HTTP (default 127.0.0.1:8787)** |
| Content safety | enforced at `memory_candidate`, `memory_recall`, `memory_promotion` |
| Safety engines | `baseline` (built in, required) + 5 optional; `gliner` deferred |
| Consumer contract | pinned by fixtures in `tests/contract/` — see [CONTRACT.md](CONTRACT.md) |

Run the gate with:

```bash
python3 tests/acceptance.py       # from the repo root
```

The gate is **offline**. The only safety engine that runs is the pure-stdlib
baseline; every third-party adapter is exercised through a fake. Installing
`detect-secrets` does not change the count (it is exercised through a fake either
way), but the total is **not** a single fixed number: it depends on the platform
(Windows skips the POSIX-only exec-bit and symlink-escape checks) and on optional
extras (`[semantic]`/numpy adds the mixed-model checks). A suite that needs
TruffleHog installed is a suite that gets skipped.

---

## What BrainConnect is

A **trusted memory ledger** with a **pluggable retrieval backend**. It owns trust and
provenance; a backend owns search sophistication. Agents propose, humans promote.
Full design: **[LEDGER_SPEC.md](LEDGER_SPEC.md)**.

It owns: claims · candidates · promotion and rejection · provenance · supersession ·
contradictions · scopes · scoped recall · trust decisions · safety policy · the
Obsidian projection.

It does **not** own: task or workflow state, agent routing, model selection, tool
registration, or any live system's runtime. Those belong to the sibling services,
all of which are optional and none of which BrainConnect depends on. See
**[INTEGRATIONS.md](INTEGRATIONS.md)**.

## The trust contract

> **`trusted is True` is the authority signal. `status == "promoted"` is not.**

This is the single rule a consumer must not get wrong. A promoted claim in an open
contradiction is returned `status: "promoted"`, `trusted: false`,
`contradiction_status: "open"` — because a contradiction is a warning, not a deletion,
and the claim remains of record.

- **Absence of `trusted` means untrusted.** Never infer trust from `status`.
- Only BrainConnect — or a consumer's own ledger / locked decisions — may confer trust.
  A retrieval backend reporting `trusted: true` cannot grant itself authority. The
  verdict may only ever **downgrade**.
- **With the defaults (`trusted_only=true`, `include_pending=false`), every item in a
  RecallPack has `trusted: true`.** Disputed, pending and superseded material is
  withheld and announced in `warnings`; opting into any of it is explicit and labeled.
- A backend returns **ids and scores**, never content or status. Recall re-reads every
  authoritative field from the ledger by id. This is what makes the boundary
  structural rather than a matter of discipline.

Stated normatively in [LEDGER_SPEC.md §14.1](LEDGER_SPEC.md).

## Safety

> **`trusted` does not mean safe to expose. `safe` does not mean trusted.**

Promotion establishes *authority*. A scan judges *content*. They are independent, and
**no safety engine and no safety policy may set `trusted`** — the gate asserts this
structurally, by parsing every module in `cli/brainconnect/safety/` and checking the
identifier appears nowhere in its AST. Safety can withhold, mask, or block. It cannot
vouch.

| Surface | Enforced | Behaviour |
|---|---|---|
| `memory_candidate` | yes | secrets masked **before storage**; injection/tool-control quarantined |
| `memory_recall` | yes | secret in a trusted claim masked on the way out, claim stays trusted; high-risk content withheld and announced; the canonical claim text is never mutated |
| `memory_promotion` | yes | secrets and high-risk payloads block; human override requires a reason and retains the findings |
| `source_ingest` | **no** | specified, deferred |
| `obsidian_projection` | **no** | specified, deferred |

An engine that could not run is never mistaken for one that found nothing: six engine
states (`ok`, `disabled`, `unavailable`, `skipped`, `failed`, `timeout`) are kept
distinct, and a required engine that does not finish `ok` fails closed. Detection is
delegated to modular engines; the built-in baseline is a deliberately limited floor,
not a product. Full contract: **[SAFETY.md](SAFETY.md)**.

Safety is a **second** gate behind the human one, and it can only subtract. Agents
still cannot promote. A clean scan promotes nothing.

## Migration behaviour

**`Repo.open()` runs forward migrations on every open** — including the one
`build_server()` performs at MCP launch. Migrations are forward-only and additive.

**A temporary repo root is not isolation.** `root=` selects which `config.toml` is
read; the database lives at an absolute path *inside* that config. Set
**`BRAINCONNECT_DB`** to a scratch path in tests, scripts, MCP verification and
any `brainconnect serve` you point a test at (the deprecated `WIKIBRAIN_DB`
still works, with a warning, while the new name is unset). Full detail, the 2026-07-10
incident where a verification script migrated the live database, and the rules for
writing a migration: **[MIGRATIONS.md](MIGRATIONS.md)**.

## Repository boundaries

| Repository | Role | Relationship |
|---|---|---|
| **BrainConnect** (this) | trusted memory ledger | standalone; depends on nothing |
| [mcp-agentconnect](https://github.com/Judgernaut777/mcp-agentconnect) | control plane: tasks, artifacts, decisions, handoffs | **optional** consumer. Contract verified — see below |
| [ComputeConnect](https://github.com/Judgernaut777/ComputeConnect) | local inference / compute | **optional**, not integrated. Notes only |
| [ToolConnect](https://github.com/Judgernaut777/ToolConnect) | tool registry / governance | **optional**, not integrated. Notes only |

BrainConnect never imports any of them, and none of them may write trusted memory:
promotion is human-only, from every direction. Integration notes, including the two
that are not built, live in **[INTEGRATIONS.md](INTEGRATIONS.md)**.

## AgentConnect contract: verified

Re-verified on **2026-07-11** against `mcp-agentconnect@a07df7f` (its committed tip).
`tests/test_wikibrain_integration.py` passes, 32/32, and a direct probe of the seams
safety touches confirmed:

- a trusted claim carrying a raw credential crosses the boundary **trusted, with the
  credential masked**; the raw value never reaches AgentConnect, and the canonical
  claim text in the ledger is unchanged;
- an injection payload stored as a promoted claim is **withheld**, and the withholding
  is announced in `warnings`, which AgentConnect passes through;
- promoting a quarantined candidate is **refused across the adapter**;
- `health()` degrades correctly when a required safety engine cannot run.

Trust semantics are unchanged: the ranker still places a promoted, uncontradicted
claim at `WIKIBRAIN_PROMOTED`.

**The observability gap recorded yesterday is closed.** The three additive fields
BrainConnect emits — `safety` on a recall item, and `safety` and `quarantined` on a
capture result — are now consumed by AgentConnect's adapter as of `a07df7f`, which also
forwards `safety_override` / `override_reason` on promotion. The field names BrainConnect
pinned in [CONTRACT.md](CONTRACT.md) were adopted verbatim. See
[INTEGRATIONS.md](INTEGRATIONS.md#agentconnect-adoption).

## Transport: closed (2026-07-12)

**`brainconnect serve` exists and is tested over the wire.** It is a pure-stdlib
HTTP server (`http.server`, fresh `Repo` per request — the MCP server's pattern)
listening on `127.0.0.1:8787` by default, serving exactly the routes
AgentConnect's adapter calls:

```
POST /recall            POST /candidates/{candidate_id}/promote
POST /capture           GET  /candidates?status=pending&limit=
POST /feedback          GET  /health
```

Refusals use the canonical nested envelope via `errors.classify` /
`errors.http_status` / `errors.envelope`. The HTTP surface does **not** accept a
safety override (403 `forbidden`); overriding stays human-only, at the CLI.
Optional bearer-token auth (`--token` / `BRAINCONNECT_TOKEN`) guards every route
except `GET /health`. Full served contract: [CONTRACT.md](CONTRACT.md#the-served-contract-brainconnect-serve).

The gate starts the real server on an ephemeral port with a temp ledger and
exercises every route over a real socket — including a quarantined capture and a
409 safety refusal over the wire, asserted byte-equal to the in-process envelope.
On 2026-07-12 the pass was additionally cross-checked by driving
`mcp-agentconnect`'s real `WikiBrainMemoryAdapter` (httpx) against a live
`brainconnect serve`: health, capture, quarantine flag, promotion, a safety
refusal surfacing as `MemorySafetyRefused`, trusted recall, feedback and
`list_pending` all behaved as pinned.

## OKF export (Stage 1, 2026-07-12)

`brainconnect export okf --output <dir>` projects the ledger into a portable,
human-readable **Open Knowledge Format** bundle (Markdown + YAML frontmatter). It
is read-only — the ledger is canonical, the bundle is a projection — and applies
the `memory_recall` safety path on the way out (secrets masked, injection withheld
with a warning, no raw value in any file). Deterministic + byte-identical for
identical ledger state, atomically staged, and self-validated before success.
Supports `--scope`, `--trusted-only`, and `--include-superseded`. Pins **OKF 0.1**.
Module `cli/brainconnect/okf/`; contract in [OKF.md](OKF.md); rationale in
[adr/0004-okf-export.md](adr/0004-okf-export.md). **OKF-valid ≠ trusted/promoted/safe.**

## OKF validate / import / round-trip (Stages 2–4, 2026-07-13)

`brainconnect okf validate <dir>` structurally validates a bundle (STRUCTURAL ONLY,
hostile-input safe; [adr/0005](adr/0005-okf-validate.md)). `brainconnect import okf
<dir> --scope S` imports a bundle as **PENDING candidates** — never auto-promoted,
never overwriting a canonical claim, every document (body **and** retained
frontmatter) scanned through `memory_candidate` ([adr/0006](adr/0006-okf-import.md)).
`brainconnect okf roundtrip --report FILE` runs ledger → export → validate → import
into a **fresh** DB → compare, emitting a **machine-readable fidelity report** that
classifies each field as exactly-preserved / mapped / intentionally-omitted / lossy /
governance-only. It is honest: it does **not** claim complete round-trip fidelity —
trust, promotion status, audit history, and contradiction/supersession bookkeeping
are governance-only and ledger-owned, re-established only by human promotion
([adr/0007](adr/0007-okf-roundtrip.md)). Demos: `scripts/okf_{validate,import,roundtrip}_demo.py`.

## Known trust-boundary caveats

Two places where "promotion is human-only" / "only BrainConnect confers trust"
are weaker than intended once a caller crosses an HTTP or federation boundary
— documented, not yet enforced further: **[adr/0009](adr/0009-http-trust-boundary-honor-system.md)**
(HTTP `_promote` trusts a caller-declared `reviewer_type`; Decima federation
surfaces a foreign `instruction_eligible` bit as `trusted`, bypassing
`trust.is_trusted`).

## Deferred work

Ordered by how much each one blocks. Nothing here is started.

1. **`source_ingest` safety surface** — scan raw source documents on the way in, with
   the whole-file engines promotion already uses. This becomes load-bearing the moment
   anything ingests third-party text; see the ToolConnect notes.
2. **`obsidian_projection` safety surface** — redact before writing markdown. Lowest
   urgency of the three: the projection is regenerable from the database, so it is the
   one surface where a miss is repairable.
3. **GLiNER** as a custom Presidio recognizer, for PII recall above Presidio's own
   field-limited ~0.5 F1. Deliberately absent from the engine registry until it exists.
4. **Retrieval backends** beyond `sqlite_fts` — `graphiti`, `cognee`, `qdrant`,
   `chroma`, `llamaindex` are named in the registry and fail loudly.
5. **Consumer-side rename adoption** — `mcp-agentconnect`'s integration test
   still imports `wiki.api` and therefore skips; it should import
   `brainconnect.api`. (The rename itself shipped 2026-07-12.)

## Change policy

No new memory features. Do **not** add: recall profiles, retrieval backends, MCP
tools, promotion paths, recall semantics, ingestion behaviour, schema columns, or
new HTTP routes beyond the six `brainconnect serve` publishes.

Code changes are in scope only for a concrete:

- **field-shape mismatch** (a consumer needs a field recall does not emit, or emits
  differently),
- **trust, scope, or safety mismatch** (two repositories disagreeing about what is
  visible, trusted, or safe), or
- **migration issue**.

Everything else is documentation.
