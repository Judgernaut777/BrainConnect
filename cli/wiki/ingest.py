"""Ingest pipeline: add (file/URL), capture, file-claims.

Every source enters through here with provenance — the "one door" (§1).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .db import Repo
from .entities import get_or_create_entity, ENTITY_KINDS
from . import fetch, util, extract, evidence


class IngestError(Exception):
    pass


# --- add --------------------------------------------------------------------
def _register_source(repo: Repo, *, content: bytes, rel_path: str,
                     title: str | None, url: str | None, origin: str,
                     fetched_at: str | None, status: str = "new",
                     mime_type: str | None = None) -> int:
    h = util.sha256_bytes(content)
    dup = repo.one("SELECT id, path FROM sources WHERE hash = ?", (h,))
    if dup:
        raise IngestError(
            f"exact duplicate of source #{dup['id']} ({dup['path']}) — refused"
        )
    cur = repo.ex(
        """INSERT INTO sources(hash, path, title, url, origin, fetched_at,
                               ingested_at, status, mime_type)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (h, rel_path, title, url, origin, fetched_at, util.now_iso(), status,
         mime_type),
    )
    return cur.lastrowid


def _near_dupe_warnings(repo: Repo, title: str | None, url: str | None) -> list[str]:
    warns = []
    if url:
        prior = repo.one("SELECT id FROM sources WHERE url = ?", (url,))
        if prior:
            warns.append(f"warning: a source with the same URL already exists (#{prior['id']})")
    if title:
        toks = util.tokens(title)
        if len(toks) >= 2:
            try:
                rows = repo.q(
                    "SELECT rowid FROM claims_fts WHERE claims_fts MATCH ? LIMIT 1",
                    (util.fts_query(title),),
                )
                if rows:
                    warns.append(
                        "warning: existing claims match this source's title "
                        "(possible near-duplicate)"
                    )
            except Exception as e:
                repo.log("ingest", f"near-dupe FTS query failed for title {title!r}: {e}")
    return warns


def add(repo: Repo, target: str, *, origin: str = "clip",
        title: str | None = None) -> tuple[int, list[str]]:
    """Add a URL or local file as a source. Returns (source_id, warnings)."""
    if fetch.is_url(target):
        md, fetched_title = fetch.fetch_url(target)  # may raise FetchError
        title = title or fetched_title
        content = md.encode("utf-8")
        h8 = util.sha256_bytes(content)[:8]
        name = f"{util.slug(title or target)}-{h8}.md"
        dest = repo.root / "raw" / name
        # write_bytes, not write_text: keep on-disk bytes == the hashed content.
        # write_text translates \n -> \r\n on Windows, which breaks sources.hash
        # (evidence filing verifies the hash before moving the artifact).
        dest.write_bytes(content)
        rel = repo.rel(dest)
        warns = _near_dupe_warnings(repo, title, target)
        try:
            sid = _register_source(repo, content=content, rel_path=rel, title=title,
                                   url=target, origin=origin, fetched_at=util.now_iso())
        except IngestError:
            # refused (exact-hash duplicate) — leave no stray file behind
            dest.unlink(missing_ok=True)
            raise
    else:
        src = Path(target).expanduser()
        if not src.exists():
            raise IngestError(f"file not found: {target}")
        content = src.read_bytes()
        dest = repo.root / "raw" / src.name
        if dest.exists() and dest.read_bytes() != content:
            # avoid clobbering a different file with the same name
            h8 = util.sha256_bytes(content)[:8]
            dest = repo.root / "raw" / f"{src.stem}-{h8}{src.suffix}"
        shutil.copyfile(src, dest)
        rel = repo.rel(dest)
        title = title or src.stem
        warns = _near_dupe_warnings(repo, title, None)
        sid = _register_source(repo, content=content, rel_path=rel, title=title,
                               url=None, origin=origin, fetched_at=None)
    repo.finalize("add", f"source #{sid} ({origin}) {rel}")
    return sid, warns


# --- capture ----------------------------------------------------------------
def capture(repo: Repo, origin_harness: str, text: str) -> int:
    text = text.strip()
    if not text:
        raise IngestError("capture text is empty")
    ts = util.now_iso().replace(":", "").replace("-", "")
    first_line = text.splitlines()[0].strip()
    name = f"{ts}-{util.slug(first_line, 40)}.md"
    dest = repo.root / "inbox" / name
    body = f"# capture: {origin_harness}\n\n_captured {util.now_iso()}_\n\n{text}\n"
    content = body.encode("utf-8")
    # write_bytes, not write_text: keep on-disk bytes == the hashed content (avoid
    # the Windows \n -> \r\n translation that would break sources.hash).
    dest.write_bytes(content)
    rel = repo.rel(dest)
    sid = _register_source(
        repo, content=content, rel_path=rel,
        title=first_line[:120], url=None,
        origin=f"session/{origin_harness}", fetched_at=util.now_iso(),
    )
    repo.finalize("capture", f"source #{sid} session/{origin_harness}")
    return sid


# --- transcribe -------------------------------------------------------------
def transcribe(repo: Repo, target: str, *, whisper_model: str = "base") -> int:
    """Ingest a video/audio transcript (YouTube captions or local Whisper) as a
    pending source with origin 'transcript'."""
    md, title = extract.transcribe(target, whisper_model=whisper_model)
    content = md.encode("utf-8")
    h8 = util.sha256_bytes(content)[:8]
    dest = repo.root / "raw" / f"{util.slug(title or target)}-{h8}.md"
    # write_bytes, not write_text: keep on-disk bytes == the hashed content
    # (Windows write_text emits CRLF and would break sources.hash).
    dest.write_bytes(content)
    url = target if fetch.is_url(target) else None
    try:
        sid = _register_source(
            repo, content=content, rel_path=repo.rel(dest), title=title, url=url,
            origin="transcript", fetched_at=util.now_iso() if url else None,
            mime_type="text/plain")
    except IngestError:
        # refused (exact-hash duplicate) — leave no stray file behind
        dest.unlink(missing_ok=True)
        raise
    repo.finalize("transcribe", f"source #{sid} transcript: {target}")
    return sid


# --- file-claims ------------------------------------------------------------
_MACHINE_ORIGINS = ("autoresearch",)


def _is_machine_origin(origin: str) -> bool:
    return origin in _MACHINE_ORIGINS or origin.startswith("session/")


def _is_valid_entity_ref(e) -> bool:
    """An entity/relation-endpoint ref is EITHER a plain name string (kind
    defaults to 'concept') OR an object {"name": str, "kind": str} with kind
    in entities.ENTITY_KINDS."""
    if isinstance(e, str):
        return bool(e.strip())
    if isinstance(e, dict):
        name = e.get("name")
        kind = e.get("kind", "concept")
        return (isinstance(name, str) and bool(name.strip())
                and isinstance(kind, str) and kind in ENTITY_KINDS)
    return False


def _entity_ref(e) -> tuple[str, str]:
    """Normalize a validated entity/relation-endpoint ref to (name, kind)."""
    if isinstance(e, dict):
        return e["name"].strip(), e.get("kind", "concept")
    return e.strip(), "concept"


def _validate(data: dict, source_id: int) -> None:
    def fail(msg: str):
        raise IngestError(f"extraction JSON invalid: {msg}")

    if not isinstance(data, dict):
        fail("top-level value must be an object")
    if data.get("source_id") != source_id:
        fail(f"source_id ({data.get('source_id')!r}) does not match --source ({source_id})")
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        fail("summary must be a string")
    if len(summary) > 1500:
        fail(f"summary exceeds 1500 chars ({len(summary)})")
    claims = data.get("claims")
    if not isinstance(claims, list):
        fail("claims must be a list")
    for i, c in enumerate(claims):
        where = f"claims[{i}]"
        if not isinstance(c, dict):
            fail(f"{where} must be an object")
        text = c.get("text")
        if not isinstance(text, str) or not text.strip():
            fail(f"{where}.text must be a non-empty string")
        if len(text) > 400:
            fail(f"{where}.text exceeds 400 chars ({len(text)})")
        conf = c.get("confidence")
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            fail(f"{where}.confidence must be a number")
        if not (0.0 <= float(conf) <= 1.0):
            fail(f"{where}.confidence must be in [0,1] (got {conf})")
        loc = c.get("location")
        if loc is not None and not isinstance(loc, str):
            fail(f"{where}.location must be a string or omitted")
        ents = c.get("entities", [])
        if not isinstance(ents, list) or not all(_is_valid_entity_ref(e) for e in ents):
            fail(f"{where}.entities must be a list of name strings or "
                 f"{{name, kind}} objects (kind in {sorted(ENTITY_KINDS)})")
        rels = c.get("relations", [])
        if not isinstance(rels, list):
            fail(f"{where}.relations must be a list")
        for j, r in enumerate(rels):
            rw = f"{where}.relations[{j}]"
            if not isinstance(r, dict):
                fail(f"{rw} must be an object")
            for k in ("src", "dst"):
                if not _is_valid_entity_ref(r.get(k)):
                    fail(f"{rw}.{k} must be a name string or "
                         f"{{name, kind}} object (kind in {sorted(ENTITY_KINDS)})")
            if not isinstance(r.get("rel"), str) or not r["rel"].strip():
                fail(f"{rw}.rel must be a non-empty string")
    lc = data.get("low_confidence", False)
    if not isinstance(lc, bool):
        fail("low_confidence must be a boolean")
    pq = data.get("proposed_questions", [])
    if not isinstance(pq, list) or not all(isinstance(q, str) for q in pq):
        fail("proposed_questions must be a list of strings")
    cat = data.get("category")
    if cat is not None and not isinstance(cat, str):
        fail("category must be a string or omitted")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        fail("tags must be a list of strings")


def _detect_contradictions(repo: Repo, new_claim_id: int, text: str) -> int:
    """Open contradiction rows vs promoted claims that FTS-match with opposite
    polarity. Returns number of rows opened."""
    opened = 0
    try:
        rows = repo.q(
            """SELECT c.id, c.text FROM claims_fts f
               JOIN claims c ON c.id = f.rowid
               WHERE claims_fts MATCH ? AND c.status = 'promoted'""",
            (util.fts_or_query(text),),
        )
    except Exception as e:
        repo.log("ingest", f"contradiction FTS query failed for claim #{new_claim_id}: {e}")
        rows = []
    for r in rows:
        if r["id"] == new_claim_id:
            continue
        if not util.polarity_conflict(text, r["text"]):
            continue
        repo.ex(
            "INSERT INTO contradictions(claim_a, claim_b, status) VALUES (?,?, 'open')",
            (r["id"], new_claim_id),
        )
        opened += 1
    return opened


def file_claims(repo: Repo, source_id: int, json_path: str, *,
                refile: bool = False) -> dict:
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise IngestError(f"extraction JSON is not valid JSON: {e}")
    return file_claims_data(repo, source_id, data, refile=refile)


def _clear_prior_extraction(repo: Repo, source_id: int) -> None:
    """Delete a source's existing claims (and their dependent rows) so a re-file
    replaces rather than duplicates. Refuses if any are promoted/superseded —
    those are truth or referenced; supersede them by hand instead."""
    rows = repo.q("SELECT id, status FROM claims WHERE source_id = ?", (source_id,))
    locked = [r["id"] for r in rows if r["status"] in ("promoted", "superseded")]
    if locked:
        raise IngestError(
            f"source #{source_id} has promoted/superseded claim(s) "
            + ", ".join(f"#{i}" for i in locked)
            + " — refile would drop them; supersede or reject manually first")
    ids = [r["id"] for r in rows]
    for cid in ids:
        # No ON DELETE CASCADE on these FKs, so clear dependents first (order
        # matters with foreign_keys=ON). embeddings/skill_claims do cascade.
        repo.ex("DELETE FROM contradictions WHERE claim_a = ? OR claim_b = ?", (cid, cid))
        repo.ex("DELETE FROM relations WHERE claim_id = ?", (cid,))
        repo.ex("DELETE FROM claim_entities WHERE claim_id = ?", (cid,))
        repo.ex("DELETE FROM claims WHERE id = ?", (cid,))


def file_claims_data(repo: Repo, source_id: int, data: dict, *,
                     refile: bool = False) -> dict:
    """File an already-parsed extraction object. Entry point for the librarian
    (which holds a dict, not a file); `file_claims` wraps it for the CLI path.

    Idempotency guard: a source is extracted once. Re-filing an already-extracted
    source is refused unless refile=True, which first clears the prior (non-
    promoted) claims so the result replaces rather than silently duplicates."""
    src = repo.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not src:
        raise IngestError(f"no source #{source_id}")
    if src["status"] != "new":
        if not refile:
            raise IngestError(
                f"source #{source_id} is already {src['status']!r}, not 'new' — "
                "already extracted. Pass refile=True (CLI: --refile) to replace it.")
        _clear_prior_extraction(repo, source_id)
    _validate(data, source_id)

    origin = src["origin"]
    ceiling = float(repo.cfg.gate("machine_confidence_ceiling"))
    machine = _is_machine_origin(origin)

    inserted_claims = 0
    contradictions = 0
    summary_text = data.get("summary", "").strip()
    if summary_text:
        if repo.one("SELECT 1 FROM summaries WHERE source_id = ?", (source_id,)):
            repo.ex("UPDATE summaries SET text = ?, status = 'pending' WHERE source_id = ?",
                    (summary_text, source_id))
        else:
            repo.ex("INSERT INTO summaries(source_id, text, status) VALUES (?,?, 'pending')",
                    (source_id, summary_text))

    for c in data["claims"]:
        conf = float(c["confidence"])
        if machine:
            conf = min(conf, ceiling)
        cur = repo.ex(
            """INSERT INTO claims(text, source_id, location, confidence, origin,
                                  status, created_at)
               VALUES (?,?,?,?,?, 'pending', ?)""",
            (c["text"].strip(), source_id, c.get("location"), conf, origin,
             util.now_iso()),
        )
        cid = cur.lastrowid
        inserted_claims += 1

        ent_ids = {}
        for eref in c.get("entities", []):
            ename, ekind = _entity_ref(eref)
            eid = get_or_create_entity(repo, ename, ekind)
            ent_ids[ename.lower()] = eid
            repo.ex(
                "INSERT OR IGNORE INTO claim_entities(claim_id, entity_id) VALUES (?,?)",
                (cid, eid),
            )
        for r in c.get("relations", []):
            src_name, src_kind = _entity_ref(r["src"])
            dst_name, dst_kind = _entity_ref(r["dst"])
            sid_e = get_or_create_entity(repo, src_name, src_kind)
            dst_e = get_or_create_entity(repo, dst_name, dst_kind)
            repo.ex(
                """INSERT OR IGNORE INTO relations(src, rel, dst, claim_id)
                   VALUES (?,?,?,?)""",
                (sid_e, r["rel"].strip(), dst_e, cid),
            )
            # A relation's endpoints are mentioned by the claim, so link them too
            # — this keeps relation targets page-worthy and avoids dangling wikilinks.
            for eid in (sid_e, dst_e):
                repo.ex(
                    "INSERT OR IGNORE INTO claim_entities(claim_id, entity_id) VALUES (?,?)",
                    (cid, eid))
        contradictions += _detect_contradictions(repo, cid, c["text"])

    # Optional session-assigned label (used to route images/files in the DB).
    cat = data.get("category")
    tags = data.get("tags")
    if cat is not None or tags:
        repo.ex(
            "UPDATE sources SET category = COALESCE(?, category), "
            "tags = COALESCE(?, tags) WHERE id = ?",
            (cat, json.dumps(tags) if tags else None, source_id))

    repo.ex("UPDATE sources SET status = 'extracted' WHERE id = ?", (source_id,))
    filed = evidence.file_source(repo, source_id)
    evidence_index = evidence.write_index(repo)

    escalated = False
    if data.get("low_confidence", False):
        repo.ex(
            "INSERT INTO escalations(source_id, reason, status) VALUES (?,?, 'open')",
            (source_id, "extractor flagged low_confidence"),
        )
        escalated = True

    queued = 0
    for q in data.get("proposed_questions", []):
        q = q.strip()
        if not q:
            continue
        if repo.one("SELECT 1 FROM research_queue WHERE question = ? AND status='open'", (q,)):
            continue
        repo.ex(
            """INSERT INTO research_queue(question, priority, origin, status, created_at)
               VALUES (?, 0.5, 'ingest', 'open', ?)""",
            (q, util.now_iso()),
        )
        queued += 1

    repo.finalize(
        "file-claims",
        f"source #{source_id}: +{inserted_claims} claims, "
        f"{contradictions} contradictions, {queued} questions"
        + (", escalated" if escalated else ""),
    )
    return {
        "claims": inserted_claims,
        "contradictions": contradictions,
        "questions": queued,
        "escalated": escalated,
        "summary": bool(summary_text),
        "filed": filed,
        "evidence_index": evidence_index,
    }
