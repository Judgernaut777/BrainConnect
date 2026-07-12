"""`brainconnect backup` / `brainconnect restore` — WAL-safe snapshot & recovery.

The ledger is a WAL-mode SQLite database (see `db.Repo.open`), which means a
naive `cp wiki.db backup.db` is *wrong*: recently-committed rows live in the
`-wal` sidecar file, and copying the main file alone loses them (or, worse,
copies a torn mix of the two). This module uses SQLite's online **backup API**
(`sqlite3.Connection.backup`), which walks a transactionally-consistent image of
every committed page — including committed WAL frames — into a fresh single-file
database, without stopping the writer.

    backup:  live WAL DB  --backup API-->  one self-contained .db snapshot
    restore: snapshot .db  --backup API-->  target path (stale -wal/-shm dropped)

Both directions run `PRAGMA integrity_check` and refuse to trust a corrupt image,
so a backup is verified when it is written and again before it is restored.

Rollback procedure (documented, and exercised by the acceptance suite):

    1. Stop `brainconnect serve` (a restore replaces the file it serves).
    2. `brainconnect backup --out pre-restore.db`   # capture current state first
    3. `brainconnect restore --from <known-good>.db`  # atomic-ish page copy
    4. Restart serve. If the restore was itself wrong, step 2's snapshot is the
       roll-*forward* — restore it in turn. Every state is a file; nothing is lost.

Pure code, zero model calls.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import Config
from .db import Repo


class BackupError(Exception):
    """A backup or restore could not be completed safely."""


def _integrity(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "unknown"


def _counts(conn: sqlite3.Connection) -> dict:
    """A cheap fingerprint of ledger contents, for round-trip assertions."""
    def n(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            return 0
    return {
        "claims": n("SELECT COUNT(*) FROM claims"),
        "memory_candidates": n("SELECT COUNT(*) FROM memory_candidates"),
        "sources": n("SELECT COUNT(*) FROM sources"),
        "recall_feedback": n("SELECT COUNT(*) FROM recall_feedback"),
    }


def backup(repo: Repo, dest: Path | str) -> dict:
    """Write a verified single-file snapshot of `repo`'s database to `dest`.

    Uses the online backup API, so committed WAL frames are included and no
    reader/writer needs to be stopped. Returns a summary dict (path, byte size,
    integrity result, schema version, row counts).
    """
    dest = Path(dest)
    if dest.exists() and dest.is_dir():
        raise BackupError(f"backup destination {dest} is a directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Ensure everything committed to the WAL is visible to the backup read
    # transaction. (The backup API already sees committed frames; this keeps the
    # snapshot's main file self-contained and small.)
    try:
        repo.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except sqlite3.Error:
        pass  # a checkpoint is an optimization, never a correctness requirement

    tmp = dest.with_suffix(dest.suffix + ".partial")
    for stale in (tmp, tmp.with_name(tmp.name + "-wal"),
                  tmp.with_name(tmp.name + "-shm")):
        stale.unlink(missing_ok=True)
    bck = sqlite3.connect(str(tmp))
    try:
        repo.conn.backup(bck)  # source=repo.conn, target=bck (whole database)
        integrity = _integrity(bck)
        if integrity != "ok":
            raise BackupError(
                f"snapshot failed integrity_check ({integrity!r}); not writing "
                f"a corrupt backup to {dest}")
        version = bck.execute("PRAGMA user_version").fetchone()[0]
        counts = _counts(bck)
    finally:
        bck.close()
    # Move the verified snapshot into place; drop any stale sidecars at dest.
    for stale in (dest.with_name(dest.name + "-wal"),
                  dest.with_name(dest.name + "-shm")):
        stale.unlink(missing_ok=True)
    tmp.replace(dest)
    return {
        "backup": str(dest),
        "bytes": dest.stat().st_size,
        "integrity": integrity,
        "schema_version": version,
        "counts": counts,
    }


def restore(source: Path | str, target: Path | str, *,
            make_pre_restore: Path | str | None = None) -> dict:
    """Replace the database at `target` with the verified snapshot at `source`.

    The caller must have stopped any `brainconnect serve` pointed at `target`
    (a restore rewrites the file underneath it). The backup is integrity-checked
    *before* it is trusted; the target's stale `-wal`/`-shm` sidecars are dropped
    so they cannot shadow the restored main file.

    If `make_pre_restore` is given, the current target (when it exists) is
    snapshotted there first, so an accidental restore is itself reversible.
    """
    source = Path(source)
    target = Path(target)
    if not source.exists():
        raise BackupError(f"no backup at {source}")

    src_conn = sqlite3.connect(str(source))
    try:
        integrity = _integrity(src_conn)
        if integrity != "ok":
            raise BackupError(
                f"refusing to restore from {source}: it fails integrity_check "
                f"({integrity!r})")
        src_counts = _counts(src_conn)
        src_version = src_conn.execute("PRAGMA user_version").fetchone()[0]

        # Snapshot the current target first (roll-forward safety net).
        pre_restore_info = None
        if make_pre_restore is not None and target.exists():
            pre = Path(make_pre_restore)
            pre.parent.mkdir(parents=True, exist_ok=True)
            tconn = sqlite3.connect(str(target))
            try:
                tconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                pbck = sqlite3.connect(str(pre))
                try:
                    tconn.backup(pbck)
                finally:
                    pbck.close()
            finally:
                tconn.close()
            pre_restore_info = str(pre)

        # Drop the target file and its sidecars, then copy the snapshot's pages
        # into a fresh file. Removing the old -wal is safe precisely because we
        # are overwriting the whole database.
        target.parent.mkdir(parents=True, exist_ok=True)
        for old in (target, target.with_name(target.name + "-wal"),
                    target.with_name(target.name + "-shm")):
            old.unlink(missing_ok=True)
        dst_conn = sqlite3.connect(str(target))
        try:
            src_conn.backup(dst_conn)
            restored_integrity = _integrity(dst_conn)
            restored_counts = _counts(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    if restored_integrity != "ok":
        raise BackupError(
            f"restore produced a database that fails integrity_check "
            f"({restored_integrity!r})")
    return {
        "restored": str(target),
        "from": str(source),
        "integrity": restored_integrity,
        "schema_version": src_version,
        "counts": restored_counts,
        "counts_match": restored_counts == src_counts,
        **({"pre_restore_backup": pre_restore_info} if pre_restore_info else {}),
    }


def resolve_db_path(start: Path | None = None) -> Path:
    """The database path the CLI's backup/restore operate on (env override aware)."""
    return Config.load(start).db_path
