# SCHEMA.md — canonical conventions

This file is the living contract for *how the database is used* — vocabularies,
state machines, and the heuristics that code applies. BUILD_SPEC.md is the
starting design; this file co-evolves as conventions are refined. Claude Code
maintains it. The DDL itself lives in `cli/brainconnect/schema.py`.

## Provenance: `origin` values
Stored on `sources.origin` and copied onto `claims.origin` at extraction time.

| origin | meaning | gate treatment |
|---|---|---|
| `clip` | human-curated clip / manual `brainconnect add` | trusted: bypasses corroboration |
| `bookmark` | synced from a browser `wiki` folder | normal |
| `autoresearch` | fetched by the night gather pass | machine: conf ceiling 0.9, never auto-supersedes |
| `session/<harness>` | live capture from a session (e.g. `session/claude-code`, or `session/mcp` from a `brain_capture` MCP call) | machine: conf ceiling 0.9, never auto-supersedes |

## State machines
- `sources.status`: `new` → `extracted` → (`failed` | `quarantined`). `new` =
  awaiting extraction (shows in `brainconnect pending`). `failed` = fetch failed
  (counts toward health). `quarantined` = manually distrusted.
- `claims.status`: `pending` → (`promoted` | `rejected` | `superseded` |
  `contradicted` | `archived`). Only `promoted` claims render on entity pages.
  `superseded` rows keep `superseded_by` pointing at the replacement.
  `rejected` and `archived` claims are **never** recallable, under any flag
  (`recall.NEVER_RECALLED`).
- `memory_candidates.status`: `pending` → (`promoted` | `rejected` | `archived`).
  Only `pending` is reviewable; a rejected candidate must be re-proposed, never
  silently revived. Promotion writes a `claims` row and back-links it via
  `promoted_claim_id` / `claims.candidate_id`. See LEDGER_SPEC.md §5.2.
- `summaries.status`: `pending` → `promoted`. Source pages show the summary
  regardless of status; promotion is a curation signal.
- `contradictions.status`: `open` → (`resolved` | `false_positive`), with
  `resolution` (the note), `resolved_at`, `resolved_by`. A contradiction is a
  *warning*, never an automatic deletion: recall returns both sides and flags
  them.
- `research_queue.status`: `open` → (`done` | `parked`). Parked after 3 attempts.
- `escalations.status`: `open` → `closed`.
- `skills.status`: `draft` → `promoted`-equivalent `approved` → `archived`. Only
  `approved` skills render to `.claude/skills/`; `draft` skills live in the DB but
  never touch disk (the gate). `approve` is human-only (skills are instructions).

## Ledger vocabularies (v9; see LEDGER_SPEC.md)

**Scope** (`claims.scope_type` / `scope_id`, `wiki/scopes.py`):
`global | user | project | repo | task | manager | worker | model | tool`.
`global` is the only type with an empty `scope_id`. Rendered `repo:my-app`.
Recall rule: a claim matches iff it is `global` **or** its `(type, id)` is among
the scopes the caller asked for — so a repo claim never leaks into another repo's
recall, while global facts stay visible everywhere. An unscoped recall therefore
returns global facts only. Pre-ledger claims backfill to `global`.

**Confidence** is stored twice, deliberately (`wiki/confidence.py` is the only
place they are mapped, so they cannot drift):
- `claims.confidence REAL` — what the auto-gate and the contradiction
  pre-adjudicator compare numerically. Unchanged.
- `claims.confidence_label` — the ordinal the ledger API speaks:
  `low(0.3) | medium(0.6) | high(0.85) | verified(0.95)`. `high` sits exactly on
  `gate.auto_promote_confidence`, so a human promoting at `high` and the auto-gate
  agree. Derived from the number when absent.

**Claim tags** (`claims.tags`, JSON array) are the classification substrate. They
flow from `memory_candidates.tags` at promotion and drive *both* the recall
profiles and the Obsidian ledger sections — keeping classification pure code, no
model call. Recognised: `decision`, `constraint`, `known-failure`, `failure`,
`gotcha`, `risk`, `criteria`, `interface`, `output-requirement`, `preference`,
`model-performance`. Untagged promoted claims fall through to "Project facts" and
still qualify for `manager_brief` (which imposes no tag filter).

**Feedback** (`recall_feedback.feedback`):
`useful | irrelevant | stale | wrong | too_broad | missing_context`. An
observation, **never** a state transition — marking a claim `wrong` queues it for
human review, it does not demote it. (Otherwise an agent could demote a rival
claim by flagging it.)

**Proposer vs reviewer types.** Anyone may propose (`memory_candidates
.proposed_by_type ∈ human|manager|worker|librarian|agent|tool`). Only
`human|librarian` may promote or reject — `candidates.promote()` raises on any
other reviewer type, independent of which MCP tools a mode exposes.

**Provenance.** `claims.source_id` stays NOT NULL and single (load-bearing for the
renderer, the gate's corroboration count, and every pre-ledger query).
`claim_sources` adds many-to-many evidence with
`evidence_type ∈ extracted | quoted | derived | asserted` and a
`quote_or_pointer`. A claim promoted from an agent's candidate records `asserted`
— the agent asserted the text, a librarian did not extract it from the source.
`memory_candidates.source_ref` and `.task_id` are **opaque** external pointers
(e.g. `agentconnect_attempt_123`); WikiBrain stores and echoes them, never
resolves them.

## Entity `kind`
`person | org | tool | concept | event | place`. A claim's `entities` (and a
relation's `src`/`dst`) may each be given as a plain name string — kind
defaults to `concept` — or as an object `{"name": str, "kind": str}` with an
explicit kind from the set above. `brainconnect file-claims` creates new entities with
the given (or defaulted) kind; if an entity already exists with the default
`concept` kind and a concrete kind arrives later, it is upgraded in place (a
concrete kind is never downgraded back to `concept`). The maintain pass may
still correct kinds by hand. Pages route by kind: `concept` → `wiki/concepts/`,
everything else → `wiki/entities/`.

## Relation vocabulary (`relations.rel`)
Open vocabulary; common verbs: `uses`, `part_of`, `contradicts`, `influences`,
`depends_on`, `created_by`, `located_in`, `succeeds`. Each relation row may cite
an evidence `claim_id`; relations render only when their evidence claim is
`promoted` (or evidence is null).

## Pages
`pages.kind`: `entity | concept | source | synthesis | index`. Source pages have
no `entity_id`; their owning source id is recorded in `synthesis_input_hash` as
the marker `src:<id>` (keeps the page path stable without an extra column).
`synthesis` is the ONLY free-prose field; the renderer injects it verbatim
between `<!-- synthesis:start -->` and `<!-- synthesis:end -->`.

## Raw evidence filing
`sources.path` is the canonical pointer to the immutable primary-source artifact.
Fresh sources may begin in flat staging (`raw/` for added/fetched artifacts,
`inbox/` for captures), but after `brainconnect file-claims` accepts an extraction the
CLI verifies `sources.hash`, moves the artifact into a deterministic bucket under
`raw/<bucket>/<year>/`, verifies the hash again, updates `sources.path`, and
marks the source page dirty.

Bucket rules:
- `session/*` → `raw/sessions/<year>/`
- `transcript` → `raw/transcripts/<year>/`
- `image/*` mime type → `raw/images/<year>/`
- URL-backed sources → `raw/web/<year>/`
- dataset-like tags/extensions → `raw/datasets/<year>/`
- document MIME/extensions → `raw/documents/<year>/`
- fallback → `raw/uncategorized/<year>/`

`raw/INDEX.md` is a generated convenience index from the `sources` table. The DB
remains authoritative; the index is for humans and agents to quickly pull primary
evidence by source id, bucket, path, hash, and claim counts. Use `brainconnect evidence
file --all` to backfill/repair paths and `brainconnect evidence index` to rebuild only
the index.

### Synthesis freshness
`synthesis_input_hash` = sha256 of the sorted promoted-claim ids + relation ids
feeding the page. `brainconnect synthesis set` stores the current hash (marking the
prose "approved against these inputs"). On `brainconnect render`, if the recomputed hash
differs from the stored one, the page is reported **needs synthesis review**.

## Determinism rules (renderer)
- Everything sorted (claims by id, relations by rel then name).
- No wall-clock time in page bodies. Frontmatter `updated` is derived from the
  max claim timestamp (entity pages) or `ingested_at` (source pages), so an
  unchanged DB re-renders byte-for-byte (zero git diff).
- Cross-references use `[[slug|Display Name]]` wikilinks (slug = page filename
  stem) so Obsidian resolves them and the graph view works.

## Heuristics applied by code
- **FTS recall** (`util.fts_or_query`): OR of significant tokens (stopwords and
  negation tokens dropped) retrieves candidates; precision comes from a Jaccard
  token-overlap filter. `brainconnect search` instead uses AND (`util.fts_query`).
- **Contradiction detection** (`brainconnect file-claims`): a new claim that retrieves a
  `promoted` claim with Jaccard ≥ 0.4 **and opposite polarity** (negation-token
  presence differs) opens a `contradictions` row.
- **Corroboration** (`brainconnect gate`): ≥ 2 distinct source ids assert a similar fact
  (Jaccard ≥ 0.5 among promoted+pending), or origin is `clip`.

## Two-speed gate (`brainconnect gate`, BUILD_SPEC §7.1)
Auto-promote iff ALL: confidence ≥ `gate.auto_promote_confidence` (0.85); no open
contradiction touching it; corroborated (above); not conflicting with a promoted
claim. Machine-origin claims (`autoresearch`, `session/*`) are capped at
`gate.machine_confidence_ceiling` (0.9) at extraction and never auto-supersede.

## Extension tables (beyond BUILD_SPEC §3.1)
- `gather_events(day, kind, qid, created_at)` — budget ledger for Phase 4.
  `kind` ∈ {`query`, `fetch`}. Lets the CLI enforce per-question / per-night
  budgets across separate process invocations. `day` is the local night bucket.
- `skills(name, description, body, allowed_tools, status, input_hash, installed,
  version, …)` + `skill_claims(skill_id, claim_id)` + `skill_versions(skill_id,
  version, body, …)` — Phase 6 skill authoring (BUILD_SPEC §12).
  `body` is the only free-prose field (the SKILL.md content, the skills analog of
  `pages.synthesis`). `input_hash` = sha256 of the sorted `promoted` linked claim
  ids + their review timestamps (the drift basis, analog of
  `synthesis_input_hash`); recomputed ≠ stored ⇒ the skill **drifted** and
  `brainconnect skill check`/`audit` flags it. `skill_claims` records provenance
  (promoted-only) and feeds the hash. `name` is a kebab-case slug = the
  `.claude/skills/` dir name; `wiki-maintainer` is reserved. Generated dirs carry a
  `.generated` marker so the renderer only ever deletes dirs it owns.
  **Versioning (Phase 6.1):** `skill_versions` is append-only — every `approve`/
  `revert` snapshots full state as the next per-skill `version`; `skills.version`
  is the current one. `brainconnect skill revert --to N` restores a snapshot (recorded as a
  new version, so history never forks). **Redundancy:** `brainconnect skill audit` flags
  skill pairs whose linked-claim sets or description+body text overlap (Jaccard ≥
  0.5); `brainconnect skill merge` reconciles them (human-gated).

## Librarian tables (the model-bearing half; advisory only)
The `wiki-librarian` process writes only PROPOSALS/RECOMMENDATIONS here — never
truth. Both are read by pure-code `brainconnect` readers for the human gate.
- `claim_triage(claim_id PK, recommendation, reason, confidence, model,
  created_at)` (schema v7) — one row per pending claim, the librarian `triage`
  pass's `promote | reject | hold` recommendation. Cascades away with the claim.
  Surfaced by `brainconnect triage`; acting on it is still `brainconnect promote`/`brainconnect reject`.
- `escalations.proposal` (schema v8) — a nullable column added to the existing
  `escalations` table, holding the librarian `adjudicate` pass's suggested action
  for a low-confidence source, mirroring `contradictions.proposal`. The pass never
  closes the escalation; `brainconnect escalation close` stays human.

## CLI conventions
Every mutating command commits, refreshes `db/dump.sql`, and appends a line to
`log.md` (`## [YYYY-MM-DD HH:MM] <op> | <summary>`). Read-only commands do not.
`brainconnect render`/`brainconnect gate`/`brainconnect lint` finalize only when they actually changed
state, so no-op runs leave the tree clean.
