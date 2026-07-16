"""Forward-only schema migrations (pure code, ZERO model calls).

`schema.py` holds `CORE_DDL` (the *fresh-install* shape — always the latest) and
`SCHEMA_VERSION`. This module carries an **existing** database forward when the
version bumps. Each `MIGRATIONS[v]` is the list of statements that brings a DB up
to `user_version == v`; a statement runs only when its target > the DB's current
`user_version`, so:

- a fresh install (created by `CORE_DDL`, stamped at `SCHEMA_VERSION`) → no-op,
- the live DB (older `user_version`) → applies exactly the missing steps once,
- re-running is idempotent.

Keep `schema.SCHEMA_VERSION == latest_version()` (asserted in tests).
"""
from __future__ import annotations

import sqlite3

# target user_version -> DDL statements that bring the DB UP TO that version.
# ALTER TABLE ... ADD COLUMN is metadata-only and safe on a populated table as
# long as new columns are nullable or carry a DEFAULT.
MIGRATIONS: dict[int, list[str]] = {
    2: [  # source typing + labels (drop folder, image vision)
        "ALTER TABLE sources ADD COLUMN mime_type TEXT",
        "ALTER TABLE sources ADD COLUMN category TEXT",
        "ALTER TABLE sources ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
    ],
    3: [  # local-embedding index for semantic search ([semantic] extra)
        "CREATE TABLE embeddings ("
        " claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,"
        " model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL,"
        " created_at TEXT NOT NULL)",
    ],
    4: [  # Phase 6: skills authored from promoted claims (see BUILD_SPEC §8)
        "CREATE TABLE skills ("
        " id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
        " description TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
        " allowed_tools TEXT, status TEXT NOT NULL DEFAULT 'draft',"
        " input_hash TEXT, installed INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL, reviewed_at TEXT)",
        "CREATE TABLE skill_claims ("
        " skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,"
        " claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,"
        " PRIMARY KEY (skill_id, claim_id))",
    ],
    5: [  # Phase 6.1: skill version history + rollback
        "ALTER TABLE skills ADD COLUMN version INTEGER NOT NULL DEFAULT 0",
        "CREATE TABLE skill_versions ("
        " id INTEGER PRIMARY KEY,"
        " skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,"
        " version INTEGER NOT NULL, description TEXT NOT NULL, body TEXT NOT NULL,"
        " allowed_tools TEXT, input_hash TEXT, claim_ids TEXT NOT NULL DEFAULT '[]',"
        " note TEXT, created_at TEXT NOT NULL, UNIQUE(skill_id, version))",
    ],
    6: [  # hot-path indexes (status/source/entity/relation lookups)
        "CREATE INDEX claims_status ON claims(status)",
        "CREATE INDEX claims_source_id ON claims(source_id)",
        "CREATE INDEX claim_entities_entity_id ON claim_entities(entity_id)",
        "CREATE INDEX relations_dst ON relations(dst)",
    ],
    7: [  # librarian triage recommendations (advisory; never promotes)
        "CREATE TABLE claim_triage ("
        " claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,"
        " recommendation TEXT NOT NULL, reason TEXT NOT NULL, confidence REAL,"
        " model TEXT, created_at TEXT NOT NULL)",
    ],
    8: [  # librarian adjudicate proposals for open escalations (advisory)
        "ALTER TABLE escalations ADD COLUMN proposal TEXT",
    ],
    9: [  # the trusted memory ledger (LEDGER_SPEC.md §5)
        # Candidates first: claims.candidate_id references this table. A
        # REFERENCES column added by ALTER TABLE must default to NULL, which it
        # does, but the target table should exist before the DML that uses it.
        "CREATE TABLE memory_candidates ("
        " id INTEGER PRIMARY KEY, text TEXT NOT NULL,"
        " proposed_by TEXT NOT NULL, proposed_by_type TEXT NOT NULL,"
        " source_id INTEGER REFERENCES sources(id), source_ref TEXT, task_id TEXT,"
        " proposed_scopes TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]',"
        " created_at TEXT NOT NULL, reviewed_at TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " promoted_claim_id INTEGER REFERENCES claims(id),"
        " review_reason TEXT, reviewed_by TEXT,"
        " metadata TEXT NOT NULL DEFAULT '{}')",
        "CREATE INDEX memory_candidates_status ON memory_candidates(status)",
        # Scope, ordinal confidence, tags, validity, promotion provenance.
        "ALTER TABLE claims ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'global'",
        "ALTER TABLE claims ADD COLUMN scope_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE claims ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE claims ADD COLUMN confidence_label TEXT",
        "ALTER TABLE claims ADD COLUMN valid_from TEXT",
        "ALTER TABLE claims ADD COLUMN valid_until TEXT",
        "ALTER TABLE claims ADD COLUMN learned_at TEXT",
        "ALTER TABLE claims ADD COLUMN last_verified_at TEXT",
        "ALTER TABLE claims ADD COLUMN promoted_by TEXT",
        "ALTER TABLE claims ADD COLUMN candidate_id INTEGER REFERENCES memory_candidates(id)",
        "CREATE INDEX claims_scope ON claims(scope_type, scope_id)",
        # Existing claims are global-scoped: exactly today's recall behaviour.
        # (The DEFAULT already did this for existing rows; the UPDATE is a
        # belt-and-braces no-op that also documents the intent.)
        "UPDATE claims SET scope_type='global', scope_id=''"
        " WHERE scope_type IS NULL OR scope_type=''",
        # Derive the ordinal label from the number the gate already compares on,
        # so pre-ledger claims answer both questions. Thresholds mirror
        # confidence.py: verified >= .95, high >= .85, medium >= .5, else low.
        "UPDATE claims SET confidence_label = CASE"
        "  WHEN confidence >= 0.95 THEN 'verified'"
        "  WHEN confidence >= 0.85 THEN 'high'"
        "  WHEN confidence >= 0.5  THEN 'medium'"
        "  ELSE 'low' END"
        " WHERE confidence_label IS NULL",
        # Many-to-many provenance, seeded from the single source_id each claim
        # already carries.
        "CREATE TABLE claim_sources ("
        " id INTEGER PRIMARY KEY,"
        " claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,"
        " source_id INTEGER NOT NULL REFERENCES sources(id),"
        " evidence_type TEXT NOT NULL DEFAULT 'extracted',"
        " quote_or_pointer TEXT, created_at TEXT NOT NULL,"
        " UNIQUE(claim_id, source_id, evidence_type))",
        "CREATE INDEX claim_sources_claim_id ON claim_sources(claim_id)",
        "INSERT INTO claim_sources(claim_id, source_id, evidence_type, quote_or_pointer, created_at)"
        " SELECT id, source_id, 'extracted', location, created_at FROM claims",
        # Supersession edges, seeded from the denormalised pointer.
        "CREATE TABLE supersessions ("
        " id INTEGER PRIMARY KEY,"
        " old_claim_id INTEGER NOT NULL REFERENCES claims(id),"
        " new_claim_id INTEGER NOT NULL REFERENCES claims(id),"
        " reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, created_by TEXT,"
        " UNIQUE(old_claim_id, new_claim_id))",
        "INSERT INTO supersessions(old_claim_id, new_claim_id, reason, created_at, created_by)"
        " SELECT id, superseded_by, 'backfilled from claims.superseded_by',"
        "        COALESCE(reviewed_at, created_at), NULL"
        " FROM claims WHERE superseded_by IS NOT NULL",
        # Contradiction resolution provenance.
        "ALTER TABLE contradictions ADD COLUMN resolved_at TEXT",
        "ALTER TABLE contradictions ADD COLUMN resolved_by TEXT",
        # Retrieval-quality feedback.
        "CREATE TABLE recall_feedback ("
        " id INTEGER PRIMARY KEY,"
        " claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,"
        " source_id INTEGER REFERENCES sources(id),"
        " actor_id TEXT NOT NULL, actor_type TEXT NOT NULL,"
        " feedback TEXT NOT NULL, note TEXT, task_id TEXT,"
        " created_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')",
        "CREATE INDEX recall_feedback_claim_id ON recall_feedback(claim_id)",
    ],
}


def latest_version() -> int:
    """The highest version this code knows how to produce."""
    return max(MIGRATIONS) if MIGRATIONS else 1


def schema_status(conn: sqlite3.Connection) -> dict:
    """Read-only: current `user_version` vs `latest_version()`. Never mutates —
    safe to call from a `--check` path or a server startup probe."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = latest_version()
    return {"current": current, "latest": latest, "behind": current < latest}


def _apply(conn: sqlite3.Connection, stmt: str) -> None:
    """Execute one migration statement, tolerating "already applied" errors.

    The primary defense against two processes racing to migrate the same
    behind-schema DB is the `BEGIN IMMEDIATE` lock in `migrate()` below — it
    serializes the whole sequence, so under normal operation (any caller that
    goes through `Repo.open`, which sets `busy_timeout` first) the loser simply
    blocks and then finds nothing left to do. This is the second line of
    defense for a caller that runs DDL directly against the file without that
    lock (e.g. `sqlite3 wiki.db` by hand, or a future caller that forgets the
    lock): `ALTER TABLE ... ADD COLUMN` and `CREATE TABLE`/`CREATE INDEX`
    re-runs raise a specific, recognizable error rather than corrupting
    anything, so we swallow exactly those and re-raise everything else.
    """
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        raise


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in ascending order. Returns the new user_version.

    Cheap and safe to call on every open: when the DB is already current it does
    a single `PRAGMA user_version` read and returns — no lock is taken.

    When a migration IS pending, the whole apply sequence runs inside
    `BEGIN IMMEDIATE`: this takes SQLite's RESERVED write lock up front, so a
    second process that opens the same behind-schema DB at the same moment
    (the concurrent-first-open race after an upgrade) blocks on that lock
    instead of racing the DDL. `Repo.open` sets `PRAGMA busy_timeout` before
    calling this, so the blocked caller waits rather than failing with
    "database is locked"; once unblocked it re-reads `user_version` (the
    `current = conn.execute(...)` inside the `try:` below) and finds the
    migration already applied, so it does nothing.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= latest_version():
        return current

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read: a concurrent migrator may have finished while we waited for
        # the lock above.
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        applied = False
        for target in sorted(MIGRATIONS):
            if target > current:
                for stmt in MIGRATIONS[target]:
                    _apply(conn, stmt)
                conn.execute(f"PRAGMA user_version={target}")
                current = target
                applied = True
        conn.commit() if applied else conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    return current
