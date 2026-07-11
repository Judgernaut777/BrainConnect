# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [brainconnect 0.1.0] — 2026-07-12

The first release under the product's real name. The version restarts at 0.1.0
because the *package* is new: `brainconnect` replaces `wiki-brain-cli` (whose
entries continue below).

### Changed — the rename (clean, no long-lived shims)
- Python package `wiki` → **`brainconnect`**; console scripts `wiki` /
  `wiki-librarian` → **`brainconnect`** / **`brainconnect-librarian`**; repo-root
  wrappers `wiki.{sh,cmd,ps1}` → `brainconnect.{sh,cmd,ps1}`.
- MCP server name `wiki-brain` → **`brainconnect`** (the tools stay `brain_*`);
  `health()` now reports `"service": "brainconnect"`.
- Isolation variable `WIKIBRAIN_DB` → **`BRAINCONNECT_DB`**. The one shim kept:
  `WIKIBRAIN_DB` is honored with a `DeprecationWarning` while `BRAINCONNECT_DB`
  is unset, so a pre-rename isolation setup keeps isolating instead of silently
  migrating a live DB.
- Packaging moved to a root `pyproject.toml` (`pip install .` from the repo
  root): name `brainconnect`, version `0.1.0`, `package-dir` mapping onto `cli/`.
- Contract fixtures regenerated: the `service` string and one degraded-retrieval
  warning message changed; no field shapes moved.
- **Known limitation:** the default live-DB path is still `~/.wiki-brain/wiki.db`
  — moving personal data on disk was deliberately out of scope.

### Added — `brainconnect serve`
- A real HTTP transport onto the ledger (default `127.0.0.1:8787`), pure stdlib,
  serving exactly the routes AgentConnect's `WikiBrainMemoryAdapter` calls:
  `POST /recall`, `POST /capture`, `POST /candidates/{id}/promote`,
  `GET /candidates?status=&limit=`, `POST /feedback`, `GET /health`.
- Refusals answer with the canonical nested envelope via
  `errors.classify`/`http_status`/`envelope`; the HTTP surface refuses
  `safety_override` as `forbidden` (overrides stay human-only, at the CLI).
- Optional bearer-token auth (`--token` / `BRAINCONNECT_TOKEN`) on every route
  except `GET /health`; constant-time comparison; failures are `forbidden`.
- Over-the-wire acceptance tests: a real server on an ephemeral port with a temp
  ledger, all six routes, a quarantined capture and a 409 safety refusal on the
  wire, asserted byte-equal to the in-process envelope; bearer-token mode.
- Served contract published in `docs/CONTRACT.md`.

## [0.2.0]

The librarian: a provider-agnostic, model-bearing second half alongside the
zero-model `wiki` CLI, plus a round of hardening on the review/gate machinery
it now sits on top of.

### Added
- `wiki-librarian`, a new console script and package (`cli/librarian/`) that
  speaks the OpenAI-compatible chat API (local Ollama/LM Studio or a hosted
  endpoint) over stdlib `http` — no required dependency, key-free by default,
  configured via `[librarian]` in `config.toml`. Kept as a strictly separate
  process/binary from `wiki` so the CLI's zero-model-call guarantee holds.
- `wiki-librarian extract` — the extraction pass: pending source -> extraction
  JSON -> `file-claims`, with a validation-error retry loop.
- `wiki-librarian catch-up` — runs `extract` over every pending source.
- `wiki-librarian triage` — advisory promote/reject/hold recommendations over
  gate-held pending claims, stored in the new `claim_triage` table; read via
  `wiki triage`. Never changes claim status.
- `wiki-librarian adjudicate` — advisory proposals for open contradictions
  (`contradictions.proposal`) and escalations (new `escalations.proposal`
  column); never resolves or closes.
- `wiki-librarian synthesize` — model-drafted page synthesis prose (written
  through the same `wiki synthesis set` path the interactive session uses) and
  skill drafts (status `draft` only); never approves/installs a skill.
- `wiki-librarian maintain` — the one-command judgment cycle: catch-up, triage,
  adjudicate, synthesize, then the pure-code housekeeping tail (render, digest,
  lint, health); preflights that the model endpoint is reachable and keeps
  going past a single failing stage; `--commit` is opt-in.
- `wiki-librarian watch` — event-driven loop over the drop folder and browser
  bookmark files; on a detected change, runs the matching pure-code ingest then
  `extract.catch_up`. Uses `watchdog` (the `[watch]` extra) if present, falls
  back to a dependency-free poll otherwise.
- `wiki-librarian status` for at-a-glance librarian state.
- `claim_triage` table (schema v7) and `escalations.proposal` column (schema
  v8) backing the triage and adjudicate passes.
- Hot-path indexes (schema v6): `claims_status`, `claims_source_id`,
  `claim_entities_entity_id`, `relations_dst`, closing query-time gaps on
  status filtering, per-source claim lookup, and graph traversal.
- Explicit entity `kind`s (`person | org | tool | concept | event | place`);
  new entities are created with a given/defaulted kind, and a concrete kind
  upgrades an existing default `concept` in place (never downgraded).
- POSIX parity: a `wiki.sh` wrapper and setup docs alongside the existing
  Windows scripts, plus a cron/systemd example for running the librarian
  unattended on Linux/macOS.
- ruff CI lint job (`cli/pyproject.toml [tool.ruff]`, scoped to real-bug rules:
  `F`, `E9`) wired into `.github/workflows/ci.yml`.
- A CI smoke step that installs the CLI and runs `wiki --help` and
  `wiki-librarian --help`, catching packaging/entry-point breakage that the
  acceptance suite alone wouldn't.
- CHANGELOG.md (this file).

### Changed
- `db/dump.sql` now debounces to at most one write per `Repo` lifetime instead
  of one per `finalize()` call, cutting redundant writes across a multi-command
  pass like `maintain`.
- Mixed-model FTS search relevance and result limiting fixed (`wiki search`).
- First-run/failure UX: a not-a-repo guard, model-reachability checks, and
  actionable hints (e.g. "is the endpoint at <base_url> running?") instead of
  bare stack traces when the librarian's configured endpoint is unreachable.
- README now leads with a librarian Quickstart and a model-choice guide.

### Fixed
- `wiki render` no longer over-renders pages with no actual dirty dependency.
- Review-state transition guards: promote/reject/supersede now validate the
  claim's current status before mutating it, closing a class of invalid
  transitions surfaced in review.
- Fail-closed gate: `wiki gate` now holds a claim (with a stated reason)
  instead of silently promoting it when a corroboration or conflict check
  itself raises, rather than assuming "no conflict found".
- Orphan raw files (registered sources whose artifact never made it into a
  bucketed `raw/` path) are now caught and repairable via `wiki evidence file`.
- CRLF-vs-LF source hashing drift on Windows-authored raw artifacts.
