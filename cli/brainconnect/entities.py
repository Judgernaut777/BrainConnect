"""Entity resolution helpers."""
from __future__ import annotations

import json

from .db import Repo

# Kinds that render under wiki/concepts/, everything else under wiki/entities/.
CONCEPT_KINDS = {"concept"}
ENTITY_KINDS = {"person", "org", "tool", "event", "place", "concept"}


def find_entity(repo: Repo, name: str):
    """Match by exact name, then by alias (case-insensitive)."""
    row = repo.one("SELECT * FROM entities WHERE name = ? COLLATE NOCASE", (name,))
    if row:
        return row
    for r in repo.q("SELECT * FROM entities"):
        aliases = json.loads(r["aliases"] or "[]")
        if any(a.lower() == name.lower() for a in aliases):
            return r
    return None


def get_or_create_entity(repo: Repo, name: str, kind: str = "concept") -> int:
    name = name.strip()
    if not name:
        raise ValueError("entity name cannot be empty")
    if kind not in ENTITY_KINDS:
        kind = "concept"
    existing = find_entity(repo, name)
    if existing:
        # A concrete kind arriving for a previously-defaulted "concept" entity
        # upgrades it in place; an already-concrete kind is never downgraded.
        if existing["kind"] == "concept" and kind != "concept":
            repo.ex("UPDATE entities SET kind = ? WHERE id = ?", (kind, existing["id"]))
        return existing["id"]
    cur = repo.ex(
        "INSERT INTO entities(name, kind, aliases) VALUES (?, ?, '[]')",
        (name, kind),
    )
    return cur.lastrowid


def page_kind_for(kind: str) -> str:
    return "concept" if kind in CONCEPT_KINDS else "entity"
