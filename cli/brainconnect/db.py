"""DB connection, init, dump, and the Repo context object.

The Repo bundles config + an open sqlite connection and provides the
mutation-finalize step (refresh db/dump.sql + append to log.md) that every
mutating command must run (BUILD_SPEC.md §2, §3.2).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config
from .schema import ALL_DDL, SCHEMA_VERSION
from .migrate import migrate
from . import util


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
             write_projections: bool = True) -> "Repo":
        """Open the repo's database, applying any pending forward migrations.

        **This mutates real state.** Migrations run on EVERY open — including the
        one `mcp_server.build_server()` performs. `start` selects which
        `config.toml` is read; the database lives at an absolute path *inside* that
        config (`[paths] db`, default `~/.wiki-brain/wiki.db`), so passing a temp
        `start` is NOT isolation and will still migrate the user's live DB.

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
        # Carry an existing DB forward if SCHEMA_VERSION has bumped since it was
        # created. No-op (a single PRAGMA read) once the DB is current.
        migrate(conn)
        return cls(cfg, conn, write_projections=write_projections)

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
