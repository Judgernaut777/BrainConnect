"""DB connection, init, dump, and the Repo context object.

The Repo bundles config + an open sqlite connection and provides the
mutation-finalize step (refresh db/dump.sql + append to log.md) that every
mutating command must run (BUILD_SPEC.md §2, §3.2).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .config import Config
from .schema import ALL_DDL, SCHEMA_VERSION
from .migrate import migrate as _migrate_fn
from .migrate import schema_status
from . import util

#: Server opt-in to auto-migrate a behind-schema database at startup instead of
#: refusing to start. See `auto_migrate_enabled` / `open_for_server`.
AUTO_MIGRATE_ENV_VAR = "BRAINCONNECT_AUTO_MIGRATE"


class SchemaBehindError(RuntimeError):
    """Raised by `open_for_server` when the on-disk schema is behind
    `SCHEMA_VERSION` and auto-migration was not opted into. The fix for the
    2026-07-10 incident (docs/MIGRATIONS.md): a server used to silently
    migrate whatever DB it resolved to, on every launch. Now it checks and
    refuses by default; the operator runs `brainconnect migrate` (or opts in
    via `--auto-migrate` / `BRAINCONNECT_AUTO_MIGRATE`)."""


def auto_migrate_enabled(explicit: bool | None = None) -> bool:
    """Resolve the server auto-migrate opt-in. An explicit `True`/`False` (a
    `--auto-migrate` CLI flag) always wins; otherwise read
    `BRAINCONNECT_AUTO_MIGRATE` (1/true/yes/on -> enabled)."""
    if explicit is not None:
        return explicit
    return os.environ.get(AUTO_MIGRATE_ENV_VAR, "").strip().lower() in (
        "1", "true", "yes", "on")


class Repo:
    def __init__(self, config: Config, conn: sqlite3.Connection,
                 write_projections: bool = True):
        self.cfg = config
        self.conn = conn
        self._dump_pending = False
        # db/dump.sql and log.md are curation-workflow projections (BUILD_SPEC §2,
        # §3.2): a git-committed textual mirror of the DB and an ops log. They are
        # right for a human running one CLI command at a time. They are WRONG for
        # the HTTP service: rewriting db/dump.sql (a full `iterdump` of the whole
        # DB) on every capture/promote/feedback is an O(DB) cost per request that
        # collapses throughput, and several server threads writing the same
        # working-tree files is a corruption hazard the DB does not have. In
        # service mode the ledger DB stays the sole source of truth; a human can
        # regenerate the projections later with `brainconnect dump`.
        self.write_projections = write_projections

    # --- lifecycle -----------------------------------------------------------
    @classmethod
    def open(cls, start: Path | None = None, *, must_exist: bool = True,
             write_projections: bool = True, migrate: bool = True) -> "Repo":
        """Open the repo's database, by default applying any pending forward
        migrations.

        **`migrate=True` (the default) mutates real state.** Migrations run on
        every such open — the CLI and library default, unchanged from before.
        `start` selects which `config.toml` is read; the database lives at an
        absolute path *inside* that config (`[paths] db`, default
        `~/.wiki-brain/wiki.db`), so passing a temp `start` is NOT isolation and
        will still migrate the user's live DB.

        Pass `migrate=False` to open without applying any DDL — the caller gets
        the DB exactly as it is on disk (use `Repo.schema_status()` to check it
        afterwards). This is what server startup uses (see `open_for_server`);
        it is also correct for any read that must not have a mutation side
        effect. It does NOT create the database file's schema — a fresh/missing
        DB still needs `init_db` first.

        For tests, scripts and MCP verification, set `BRAINCONNECT_DB` to a scratch
        path. See docs/MIGRATIONS.md.
        """
        cfg = Config.load(start)
        if must_exist and not cfg.found:
            raise SystemExit(
                "error: not inside a wiki-brain repo — cd into one, or run "
                "`brainconnect init` to create one here"
            )
        db_path = cfg.db_path
        if must_exist and not db_path.exists():
            raise SystemExit(
                f"error: no database at {db_path}. Run `brainconnect init` first."
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # The librarian runs as a separate process against the same WAL DB;
        # wait for a writer instead of failing fast with "database is locked".
        conn.execute("PRAGMA busy_timeout=10000;")
        if migrate:
            # Carry an existing DB forward if SCHEMA_VERSION has bumped since it
            # was created. No-op (a single PRAGMA read) once the DB is current;
            # see migrate.migrate() for the concurrent-first-open lock.
            _migrate_fn(conn)
        return cls(cfg, conn, write_projections=write_projections)

    def schema_status(self) -> dict:
        """Read-only: `{"current", "latest", "behind"}` for this repo's open
        connection. Never mutates — safe after `Repo.open(migrate=False)`."""
        return schema_status(self.conn)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.flush()
        self.close()
        return False

    def flush(self):
        """Write db/dump.sql if a finalize() since the last dump left it pending.

        Every `with Repo.open() as r:` block calls this on exit, so N
        finalize() calls within one Repo lifetime still produce exactly one
        dump write (BUILD debounce), instead of one rewrite per mutation.
        """
        if self._dump_pending:
            self.dump()

    # --- paths ---------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.cfg.root

    def rel(self, p: Path) -> str:
        """Repo-relative POSIX path string for storage in the DB."""
        return p.resolve().relative_to(self.root).as_posix()

    # --- query convenience ---------------------------------------------------
    def q(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()

    def ex(self, sql: str, params=()):
        return self.conn.execute(sql, params)

    # --- mutation finalize ---------------------------------------------------
    def finalize(self, op: str, summary: str):
        """Commit, mark dump.sql for refresh, append to log.md. Call after a
        mutation. The dump itself is deferred (see `flush`) so a command that
        finalizes many times only rewrites db/dump.sql once, on Repo exit."""
        self.conn.commit()
        if not self.write_projections:
            # Service mode: the DB is the whole record. Skip the O(DB) dump.sql
            # rewrite and the shared-file log append (see __init__).
            return
        self._dump_pending = True
        self.log(op, summary)

    def dump(self):
        """Rewrite db/dump.sql immediately. Public for callers that force a
        refresh outside of finalize() (e.g. `brainconnect dump`, `brainconnect init`)."""
        self._dump_pending = False
        out = self.root / "db" / "dump.sql"
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for line in self.conn.iterdump():
            # embeddings are regenerable (`brainconnect embed --all`) and, packed as hex
            # float32 BLOBs, would otherwise bloat this git-committed file once
            # the [semantic] extra is in use. Keep the CREATE TABLE (schema stays
            # round-trippable) but drop the row data.
            if line.startswith('INSERT INTO "embeddings"'):
                continue
            lines.append(line)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def log(self, op: str, summary: str):
        logp = self.root / "log.md"
        header = f"## [{util.now_local_compact()}] {op} | {summary}\n"
        with open(logp, "a", encoding="utf-8") as fh:
            fh.write(header)


def init_db(start: Path | None = None) -> Repo:
    """Create the DB (apply DDL) and return an open Repo."""
    cfg = Config.load(start)
    db_path = cfg.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(ALL_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.commit()
    return Repo(cfg, conn)


def open_for_server(start: Path | None = None, *, write_projections: bool = True,
                    auto_migrate: bool | None = None) -> Repo:
    """The server-startup open: never silently migrates.

    Opens with `migrate=False`, then checks the on-disk schema against
    `SCHEMA_VERSION`. A current schema opens normally. A behind schema either:

      * raises `SchemaBehindError` (the default) — the operator runs
        `brainconnect migrate` — or
      * is migrated in place, if the caller opted in (`auto_migrate=True`, a
        `--auto-migrate` flag) or the `BRAINCONNECT_AUTO_MIGRATE` environment
        variable is set.

    Callers that use this for startup should open every subsequent (e.g.
    per-request) `Repo` with `migrate=False` too: this check already ran, so
    later opens should not pay for — or risk — another migration pass.
    """
    repo = Repo.open(start, write_projections=write_projections, migrate=False)
    status = repo.schema_status()
    if status["behind"]:
        if auto_migrate_enabled(auto_migrate):
            _migrate_fn(repo.conn)
        else:
            current, latest = status["current"], status["latest"]
            repo.close()
            raise SchemaBehindError(
                f"database schema is v{current}, code expects v{latest}. "
                "Refusing to auto-migrate a server database on startup "
                "(docs/MIGRATIONS.md). Run `brainconnect migrate` to upgrade "
                f"it first, or opt in with --auto-migrate / "
                f"{AUTO_MIGRATE_ENV_VAR}=1 if you understand the risk of a "
                "concurrent server doing the same."
            )
    return repo
