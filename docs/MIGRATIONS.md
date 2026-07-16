# MIGRATIONS.md — schema evolution, and the live-DB hazard

## Status: server-side hazard fixed

`serve` and `mcp serve` no longer auto-migrate at startup. They open with
`db.open_for_server`, which checks the on-disk schema against `SCHEMA_VERSION`
and **refuses to start** (`SchemaBehindError`, with a clear message) if it is
behind — unless the operator opts in with `--auto-migrate` or
`BRAINCONNECT_AUTO_MIGRATE=1`. Every per-request/per-tool-call `Repo.open`
after that startup check passes `migrate=False`, so a request handler can never
trigger a migration either. Use the new explicit `brainconnect migrate`
(`--check`/`--check-schema` to only report; snapshots via the same backup
helper as `brainconnect backup` before applying DDL, `--no-backup` to skip it)
to upgrade a database on purpose. See §4 of `docs/OPERATIONS.md` for the
upgrade procedure.

`Repo.open()`'s own **default** is unchanged: `migrate=True`, so the CLI and
library keep auto-migrating on open exactly as before (pass `migrate=False` to
opt out). The concurrent-first-open race described below is additionally closed
at the source: `migrate()` now runs the whole apply sequence inside
`BEGIN IMMEDIATE`, so two callers that both see a behind schema at once
serialize on SQLite's write lock instead of racing the DDL.

## The hazard, as it originally read (now server-scoped, not eliminated at the
## `Repo.open()` level)

**`Repo.open()` runs forward migrations on every open (by default).** Not on
`brainconnect migrate` specifically — literally every open that doesn't pass
`migrate=False`, which used to mean everything, including the one
`build_server()` performs to resolve the repo root, and the one every `wiki`
subcommand performs. The CLI and library paths still work this way; the
server entry points (`serve`, `mcp serve`) do not, as of the fix above.

**Passing a temp `root=` is not isolation.** `root` selects which `config.toml` is
read. The database lives at an absolute path *inside* that config
(`[paths] db`, default `~/.wiki-brain/wiki.db`), outside the working tree by design.
So this migrates the user's real database:

```python
# WRONG — `root` is a repo root, not a database.
build_server(root=Path("/home/mini/WikiBrain"))   # opens ~/.wiki-brain/wiki.db
```

Migrations are forward-only and additive, so this is not destructive. It is still a
side effect on real user state performed by a verification script, and that is a
category of bug worth naming rather than tolerating.

## Isolating a test, a script, or an MCP verification

Set **`BRAINCONNECT_DB`**. It overrides `[paths] db` and takes precedence over the
config file and `$HOME` alike. This is the lever; use it in anything throwaway.

```bash
BRAINCONNECT_DB=$(mktemp -d)/scratch.db python3 scripts/verify_mcp.py
```

```python
os.environ["BRAINCONNECT_DB"] = str(tmp / "scratch.db")
build_server(root=repo_root)          # now provably cannot touch the live DB
```

Setting an isolated `$HOME` also works (the default path is `~`-relative), but only
by accident of the default — a config with an explicit absolute `db =` ignores
`$HOME` entirely. Prefer `BRAINCONNECT_DB`, which cannot be defeated that way. (The pre-rename
`WIKIBRAIN_DB` is still honored, with a `DeprecationWarning`, while the new name
is unset — so an old script keeps isolating instead of silently migrating the
live DB.)

`tests/acceptance.py` is already safe: `make_repo()` writes a `config.toml` whose
`db` points inside a `tempfile.mkdtemp()`. Follow that pattern, or set the env var.

## What actually happened (2026-07-10)

While verifying that the ledger rework's new MCP tools registered correctly against
the real FastMCP SDK, a check called `build_server(root=Path("/home/mini/WikiBrain"))`
with the developer's real `$HOME`. That resolved `~/.wiki-brain/wiki.db` and carried
it from v8 to v9.

Verified afterwards on the live database: `PRAGMA integrity_check` → `ok`, zero
`foreign_key_check` violations, all 5 claims / 1 source / 7 entities / 6 relations
intact, `scope_type` backfilled to `global` and `confidence_label` derived from
`confidence` as intended. Nothing was lost. A backup was taken.

The lesson is not "the migration was fine." It is that **verification must not be
able to reach production state**, and the mechanism that made it possible — an
implicit migration on open, plus a `root` parameter that reads as isolating but is
not — is documented here so the next person does not rediscover it the same way.

## Writing a migration

`schema.py` holds `CORE_DDL` / `EXT_DDL` / `LEDGER_DDL` — the **fresh-install** shape,
always the latest — and `SCHEMA_VERSION`. `migrate.py` carries an **existing**
database forward. Both must move together: `tests/acceptance.py` asserts
`schema.SCHEMA_VERSION == migrate.latest_version()`.

Each `MIGRATIONS[v]` is the list of statements that brings a DB up to
`user_version == v`. A statement runs only when its target exceeds the DB's current
`user_version`, so a fresh install is a no-op, an old DB applies exactly the missing
steps once, and re-running is idempotent.

Rules learned the hard way:

- **`ALTER TABLE ... ADD COLUMN`** is metadata-only and safe on a populated table,
  but the new column must be nullable or carry a constant `DEFAULT`. A column with a
  `REFERENCES` clause **must** default to `NULL` (SQLite refuses otherwise).
- **Order matters for `REFERENCES`.** Create the target table before the `ALTER` that
  points at it. Foreign keys are not validated at DDL time, but do not rely on that.
- **Backfill explicitly.** A `DEFAULT` fills existing rows, but write the `UPDATE`
  anyway when the intent matters — v9's `scope_type='global'` backfill is a no-op
  that documents *why* every pre-ledger claim is global (it preserves the old recall
  behaviour exactly).
- **Derive, don't guess.** v9's `confidence_label` is computed from the numeric
  `confidence` the gate already compares on, so old claims answer both questions
  consistently. Keep the thresholds in sync with `confidence.py`.
- **Test the migration on a populated fixture**, not just a fresh install. The v9
  work exposed that the acceptance fixture's synthetic v1 database was missing
  `contradictions` — a table that has existed since v1 — which the migration
  legitimately alters. The fixture was wrong, not the migration.
- **Verify on a copy of a real database** before shipping: `PRAGMA integrity_check`,
  `PRAGMA foreign_key_check`, row counts before and after, and an idempotent re-run.

## Version history

| version | change |
|---|---|
| 2 | source typing + labels (`mime_type`, `category`, `tags`) |
| 3 | `embeddings` — local semantic index (`[semantic]` extra) |
| 4 | `skills` + `skill_claims` (Phase 6) |
| 5 | `skill_versions` — version history + rollback |
| 6 | hot-path indexes |
| 7 | `claim_triage` — librarian advisory recommendations |
| 8 | `escalations.proposal` |
| 9 | the trusted memory ledger: `memory_candidates`, `claim_sources`, `supersessions`, `recall_feedback`; scope/tags/ordinal-confidence/validity/promotion-provenance on `claims`; resolution provenance on `contradictions`. See [LEDGER_SPEC.md](LEDGER_SPEC.md). |
