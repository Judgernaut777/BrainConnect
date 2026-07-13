# Performance capture — closing the registry loop (ADR 0008 Lane 7)

Lane 7 is the *capture-and-promotion* half of the capability registry. It is the
adapter that turns what ComputeConnect (CC) and AgentConnect (AC) **measure** into
what BrainConnect (BC) can eventually **trust** — without BC ever measuring,
scoring, or trusting anything on its own.

Governed by [ADR 0008](adr/0008-orchestration-boundary.md) (Lane 7) and
[LEDGER_SPEC](LEDGER_SPEC.md) §2 (human-only promotion), §7 (`model_performance`
profile), §5.5 (`model:` scope). Companion to [REGISTRY.md](REGISTRY.md) (Lane 1,
the trusted registry this feeds) and [DELEGATION.md](DELEGATION.md) (Lane 4).

## The boundary in one line

**CC/AC measure. BC captures the facts as PENDING candidates. A human promotes.**

BC makes **zero model calls**. It reads only ComputeConnect's side-effect-free
telemetry and files what it sees; it never generates, never benchmarks, never
promotes, and never mutates a trusted claim.

## What it does

`cli/brainconnect/perfcapture.py` (`brainconnect perfcapture`) reads CC telemetry
through an **injectable** client (`delegate_clients.HttpTelemetryClient`, reusing
the Lane-4 bounded-read + wall-clock-deadline transport) and files each observed
fact as an ordinary **PENDING** `model_performance` memory candidate:

| CC surface | What is captured | `kind` |
|---|---|---|
| `GET /health` | liveness gate (if it can't be read, CC is *unavailable* → capture nothing) | — |
| `GET /models` | availability per inventory model (`loaded` true/false) | `measured` |
| `GET /models/loaded` | the **resident** model(s) — the deployed-model refresh | `measured` |
| `POST /route/estimate` (optional) | `estimated_tokens_per_second` / `estimated_quality` / `estimated_queue_seconds` from the rationale | `estimate` |

`/route/estimate` is read-only and creates no run ([CC CONTRACT.md](../../ComputeConnect/docs/CONTRACT.md):
"cheap, side-effect-free, never touches a generation path"), so reading it is a
telemetry read, not a model call.

Every captured candidate is:

- **PENDING only.** It enters the ordinary human/librarian promotion gate
  (`candidates.promote`, whose `REVIEWER_TYPES` excludes every agent type). A model
  or agent can never self-promote a capability claim about itself.
- **`model:`-scoped** and tagged `model-performance`, binding it to the §7
  `model_performance` retrieval profile exactly as the Lane-1 registry does.
- **Source-labelled.** `source=computeconnect-telemetry`, a `kind` of `measured`
  (an observed residency/availability fact) or `estimate` (CC's operator-declared
  heuristic — comparable only within one CC deployment, **never** relabelled as a
  measured benchmark), and an `observed-at` logical marker.
- **Safety-scanned.** Telemetry is untrusted data, never instructions. The observed
  string values flow through the *scanned* candidate text, so a credential in a
  telemetry field is masked (or the observation refused) — never stored raw. A
  telemetry value that would become a `model:` scope is scanned first and the whole
  observation is skipped if it is anything but clean (a scope is not itself scanned).

## No fabricated numbers

The registry never invents a benchmark. Only what telemetry actually reports is
captured, each number carried verbatim and labelled `estimate` vs `measured`. A
model with no telemetry yields no numbers.

## The deployed-model refresh (Qwen3.6-35B-A3B vs qwen3-30b-a3b)

The registry records `deployed=qwen3-30b-a3b` as a **static declared** fact
([ADR 0008 model-name reconciliation](adr/0008-orchestration-boundary.md)). When
the live daily driver changes, perfcapture captures CC's `/models/loaded` report as
a **PENDING** candidate — it does **not** auto-mutate the trusted `deployed` claim.
The correction flows through the human promotion gate: an operator promotes the
captured loaded-model fact, and the trusted registry moves only then. BC never
connects to the engine port itself; it reads CC's residency report.

## Idempotency

Each observation carries an **unforgeable** per-fact fingerprint over
`(source, subject, metric, kind, value)`
(`candidates.PERFCAPTURE_OBSERVATION_KEY`, a reserved metadata key a public caller
cannot forge). A re-run that sees the **same** value finds the marker and files
nothing (no duplicate); a **changed** value produces a different fingerprint and
**is** filed — a genuinely new observation is never silently suppressed. The
`observed-at` marker is deliberately kept **out** of the fingerprint, so wall-clock
drift alone never forges a "new" observation.

## Degrade, never crash

If CC is unavailable — no client configured, or any transport failure (refused,
timeout, non-2xx, oversized/slow body) — the run captures nothing and reports
cleanly (`cc_available=false`). It never raises.

## CLI

```
# capture (reads CC telemetry, files PENDING candidates; --estimate also reads the rationale)
brainconnect perfcapture capture --cc-url http://127.0.0.1:8090 [--estimate] [--json]

# read surface: what was captured, with source label + trust status (deterministic)
brainconnect perfcapture list [--status pending] [--json]

# then a human promotes (never an agent):
brainconnect promote candidate_<n> --scope model:<id> --confidence <label>
```

With no `--cc-url`, `perfcapture capture` is a clean no-op (CC unavailable). The
read surface is deterministic — two reads against an unchanged ledger are
byte-identical, ordered by candidate id.

## What Lane 7 is NOT

- Not a measurement engine. It runs no benchmarks and computes no scores; CC/AC do.
- Not a live-state table. It records observations as candidates and forgets; it
  never holds residency/warm state (that is CC's, ADR 0008).
- Not a promotion path. It only ever files PENDING candidates; promotion stays
  human/librarian-only (LEDGER_SPEC §2).
- Not a model client. It has no path to `/generate` or any chat endpoint.
