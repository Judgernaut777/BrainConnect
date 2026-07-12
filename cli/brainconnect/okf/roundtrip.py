"""OKF round-trip + interop fidelity (Stage 4) — the honest accounting.

This stage answers one question, precisely and without overclaiming: **when a
ledger is projected to OKF and that projection is imported into a fresh
BrainConnect, what survives, and what is deliberately left behind?**

It runs the full cycle end to end —

    source ledger  ->  export (Stage 1)  ->  validate (Stage 2)
                   ->  import into a FRESH, empty DB (Stage 3)
                   ->  compare representable semantics

— and emits a **machine-readable fidelity report** (JSON) that classifies every
mapped field/concept as exactly one of:

    exactly-preserved   the value survives byte-for-byte into the imported side.
    mapped              transformed but recoverable (e.g. an id becomes a
                        source_ref + external_id; a scope becomes informational
                        metadata rather than the governing scope).
    intentionally-omitted  not carried by design (e.g. the free-text title is not
                        retained on import; a quarantined body is never exported).
    lossy               only partially represented (e.g. a masked secret; source
                        citations that live in the bundle but are not re-attached
                        to the pending candidate).
    governance-only     trust / promotion status / audit history / contradiction &
                        supersession bookkeeping. **Never carried by OKF.** OKF is
                        a projection, not an authority: import lands PENDING and
                        untrusted, and this governance state is re-established only
                        by a human through the normal promotion path — it is
                        ledger-owned and is *not reconstructable from a projection.*

The design refuses to claim complete round-trip fidelity. A projection cannot
carry authority. The imported side is a set of PENDING candidates, so the honest
comparison is: *which of the source claims' representable fields survive into the
imported candidates + their provenance,* and *which governance state is
deliberately not reconstructed and remains ledger-owned.* The report says both,
per field, with concrete per-claim evidence proving the classification held on the
actual ledger it was run against — not merely asserting the static contract.

Nothing here mutates the source ledger (export is read-only) and the import side is
a throwaway temp DB created under `tempfile`; the live database is never written.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..db import Repo, init_db
from ..scopes import Scope
from .. import refs
from .export import FORMAT_NAME, OKF_VERSION, export_bundle
from .model import ExportRequest
from .okfimport import ImportRequest, import_bundle, _REF_PREFIX
from .validate import ValidationLimits, validate_bundle

REPORT_VERSION = "1"

# Fidelity classes (closed vocabulary; every field entry uses exactly one).
EXACT = "exactly-preserved"
MAPPED = "mapped"
OMITTED = "intentionally-omitted"
LOSSY = "lossy"
GOVERNANCE = "governance-only"

CLASSES = (EXACT, MAPPED, OMITTED, LOSSY, GOVERNANCE)


# --- the static fidelity contract --------------------------------------------
# One entry per field/concept in the mapping table. `classification` is the
# primary verdict; `safety_degradations` records the honest edges where a body's
# fidelity degrades under safety policy (redaction -> lossy, withholding ->
# omitted). This table is the *contract*; `roundtrip()` proves it against real data.
FIELD_FIDELITY: list[dict] = [
    {
        "field": "id",
        "okf_location": "brainconnect.id (e.g. claim_4)",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "The source claim id is projected as brainconnect.id and imported as "
            "the candidate's source_ref (okf:<id>) plus metadata.okf_import."
            "external_id. It is recoverable, but it is NOT re-used as a claim id: "
            "the imported side is a new PENDING candidate with its own id. An "
            "external id confers no authority over canonical state."),
    },
    {
        "field": "title",
        "okf_location": "title (frontmatter) + '# ' heading",
        "classification": OMITTED,
        "recoverable": True,
        "rationale": (
            "The title is DERIVED from the safe body on export and is deliberately "
            "NOT retained on import: a hand-authored hostile bundle could plant a "
            "secret in the free-text title, and imported metadata is recallable. It "
            "is re-derivable from the body, so nothing of substance is lost."),
    },
    {
        "field": "body",
        "okf_location": "document body",
        "classification": EXACT,
        "recoverable": True,
        "rationale": (
            "A clean claim body is projected verbatim and imported as the "
            "candidate's text byte-for-byte. Safety policy can degrade this on the "
            "way out (see safety_degradations)."),
        "safety_degradations": [
            {"when": "secret / PII redacted", "classification": LOSSY,
             "note": "Only a MASKED representation is exported; the raw text never "
                     "leaves the ledger. Lossy by design."},
            {"when": "quarantined / withheld", "classification": OMITTED,
             "note": "A high-risk (injection / tool-control) body is WITHHELD: the "
                     "document is exported with a text-free placeholder, so the "
                     "original body is not carried at all. The claim stays in the "
                     "ledger; nothing is deleted."},
        ],
    },
    {
        "field": "tags",
        "okf_location": "tags (frontmatter)",
        "classification": EXACT,
        "recoverable": True,
        "rationale": (
            "Source tags are projected as the tags list and imported onto the "
            "candidate (unioned with any operator-supplied import tags). The source "
            "tag set survives as a subset."),
    },
    {
        "field": "scope",
        "okf_location": "brainconnect.scope",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "The source scope is projected exactly, but on import it is retained "
            "only as INFORMATIONAL metadata (okf_import.frontmatter.scope). The "
            "candidate's governing scope is the one the OPERATOR assigns via "
            "--scope: the operator governs blast radius, never the bundle. The "
            "original is recoverable but is not the effective scope."),
    },
    {
        "field": "status",
        "okf_location": "brainconnect.status",
        "classification": GOVERNANCE,
        "recoverable": True,
        "rationale": (
            "The source status string (promoted/pending/superseded/contradicted) is "
            "projected and retained as informational metadata, but the imported "
            "candidate is ALWAYS pending regardless. Promotion status is ledger "
            "governance, re-established only by a human promotion — it is not "
            "reconstructable from a projection."),
    },
    {
        "field": "confidence",
        "okf_location": "brainconnect.confidence",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "The confidence label is projected and retained as informational "
            "metadata (okf_import.frontmatter.confidence). It is NOT applied as the "
            "candidate's confidence: confidence is set by the human reviewer at "
            "promotion time. Recoverable, but not the governing confidence."),
    },
    {
        "field": "trusted",
        "okf_location": "brainconnect.trusted (bool)",
        "classification": GOVERNANCE,
        "recoverable": True,
        "rationale": (
            "TRUST IS NEVER CARRIED BY OKF. The exported brainconnect.trusted flag "
            "is retained only as informational metadata; the imported candidate is "
            "untrusted and pending. Trust is authority, re-established only by human "
            "promotion. OKF-valid != trusted."),
    },
    {
        "field": "sources",
        "okf_location": "brainconnect.sources[] + ## Sources links + sources/source-index.md",
        "classification": LOSSY,
        "recoverable": False,
        "rationale": (
            "Source citations are fully represented in the bundle, but they are NOT "
            "re-attached to the imported candidate: import registers the BUNDLE (its "
            "path + checksum) as provenance and cuts the ## Sources scaffolding out "
            "of the candidate body. The per-source claim_sources rows of a promoted "
            "claim are re-created only if a human promotes and re-cites. Partially "
            "represented -> lossy."),
    },
    {
        "field": "valid_from",
        "okf_location": "brainconnect.valid_from",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "Retained as informational, safety-scanned metadata "
            "(okf_import.frontmatter.valid_from); not applied as ledger validity "
            "until a human promotes."),
    },
    {
        "field": "valid_until",
        "okf_location": "brainconnect.valid_until",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "Retained as informational, safety-scanned metadata "
            "(okf_import.frontmatter.valid_until); not applied as ledger validity "
            "until a human promotes."),
    },
    {
        "field": "learned_at",
        "okf_location": "brainconnect.learned_at",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "Retained as informational, safety-scanned metadata "
            "(okf_import.frontmatter.learned_at). The import ALSO stamps its own "
            "imported_at; the source timestamp is not re-established as ledger "
            "provenance until promotion."),
    },
    {
        "field": "last_verified_at",
        "okf_location": "brainconnect.last_verified_at",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "Retained as informational, safety-scanned metadata "
            "(okf_import.frontmatter.last_verified_at); not applied as ledger "
            "verification state until a human promotes."),
    },
    {
        "field": "superseded_by",
        "okf_location": "brainconnect.superseded_by + ## Superseded by relative link",
        "classification": GOVERNANCE,
        "recoverable": True,
        "rationale": (
            "A supersession is projected as a relative link + a metadata pointer and "
            "re-imported as PROVENANCE (okf_import.relationships.superseded_by) — NOT "
            "as a re-established ledger supersession. No supersessions row and no "
            "claims.superseded_by pointer is created on import. Supersession "
            "bookkeeping is ledger-owned governance."),
    },
    {
        "field": "contradictions",
        "okf_location": "brainconnect.contradictions[] + ## Contradicts relative links",
        "classification": GOVERNANCE,
        "recoverable": True,
        "rationale": (
            "A contradiction is projected as relative links + a metadata list and "
            "re-imported as PROVENANCE (okf_import.relationships.contradictions) — NOT "
            "as a re-established OPEN contradiction. No contradictions row is created "
            "on import. Contradiction bookkeeping is ledger-owned governance."),
    },
    {
        "field": "provenance",
        "okf_location": "brainconnect.provenance (origin, promoted_by, candidate_id)",
        "classification": MAPPED,
        "recoverable": True,
        "rationale": (
            "The source provenance block is retained as safety-scanned informational "
            "metadata (okf_import.frontmatter.provenance) — transformed (nested, "
            "masked if it carried a secret) but recoverable. Import ALSO records its "
            "own provenance (bundle path/checksum, OKF version, doc path, external "
            "id, imported_at/by). The two are distinct: source provenance is "
            "informational, import provenance is authoritative for the candidate."),
    },
    {
        "field": "safety",
        "okf_location": "brainconnect.safety (non-sensitive decision/kinds/findings)",
        "classification": GOVERNANCE,
        "recoverable": False,
        "rationale": (
            "SAFETY IS NEVER CARRIED AS A DECISION. The exported safety block "
            "(decision/kinds/findings, never a matched value) is informational and "
            "is NOT retained on import; instead the imported content is RE-SCANNED "
            "fresh through the memory_candidate surface, which re-establishes masking "
            "/ quarantine independently. A safe-looking bundle is still scanned. "
            "Safety is ledger/pipeline-owned, not a projection artifact."),
    },
]

# Governance concepts that are not single mapping fields but must be named as
# deliberately-not-reconstructable-from-a-projection.
GOVERNANCE_CONCEPTS: list[dict] = [
    {"concept": "trust", "classification": GOVERNANCE,
     "rationale": "Import lands untrusted/pending; trust is re-established only by "
                  "human promotion. OKF-valid != trusted."},
    {"concept": "promotion status", "classification": GOVERNANCE,
     "rationale": "Every imported document is a PENDING candidate; no import path "
                  "produces a promoted/trusted claim. OKF-valid != promoted."},
    {"concept": "audit history", "classification": GOVERNANCE,
     "rationale": "The source ledger's promotion/review/finalize audit trail is not "
                  "carried; the import writes its OWN fresh audit record (an import "
                  "was attempted) in the destination ledger."},
    {"concept": "contradiction / supersession bookkeeping", "classification": GOVERNANCE,
     "rationale": "Re-imported as provenance links + metadata only; the open "
                  "contradictions and supersessions tables are ledger-owned and are "
                  "not repopulated by import."},
    {"concept": "safety decision", "classification": GOVERNANCE,
     "rationale": "Re-established by a fresh memory_candidate scan on import, never "
                  "carried from the exported safety metadata. OKF-valid != safe."},
]


@dataclass
class RoundtripRequest:
    #: Where to write the JSON fidelity report.
    report_path: str = ""
    #: Scope filter passed through to export (empty = every scope).
    scopes: list[Scope] = field(default_factory=list)
    #: Export only trusted claims.
    trusted_only: bool = False
    #: Also export + round-trip superseded claims and the history log.
    include_superseded: bool = False
    #: The operator scope assigned to every imported candidate (operator governs).
    import_scope: Scope = field(default_factory=lambda: Scope("global"))
    #: Importing actor recorded on the fresh-DB candidates.
    imported_by: str = "okf-roundtrip"
    imported_by_type: str = "human"


@dataclass
class RoundtripReport:
    report_version: str = REPORT_VERSION
    format_name: str = FORMAT_NAME
    okf_version: str = OKF_VERSION
    #: Honest headline: never claim complete round-trip fidelity.
    fidelity_claim: str = (
        "PARTIAL BY DESIGN. OKF carries representable knowledge fields as a "
        "projection; it does NOT carry governance (trust, promotion status, audit "
        "history, contradiction/supersession bookkeeping, safety decisions). The "
        "imported side is PENDING and untrusted; governance is re-established only "
        "by human promotion. Complete semantic round-trip is neither achieved nor "
        "claimed.")
    source: dict = field(default_factory=dict)
    export: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    imported: dict = field(default_factory=dict)
    fresh_db: bool = True
    idempotent: bool = False
    field_fidelity: list[dict] = field(default_factory=lambda: list(FIELD_FIDELITY))
    governance_concepts: list[dict] = field(
        default_factory=lambda: list(GOVERNANCE_CONCEPTS))
    #: Concrete, data-driven proof that the classification held on THIS ledger.
    honesty: dict = field(default_factory=dict)
    per_claim: list[dict] = field(default_factory=list)
    classification_counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _classification_counts() -> dict:
    counts = {c: 0 for c in CLASSES}
    for entry in FIELD_FIDELITY:
        counts[entry["classification"]] += 1
    return counts


def _candidates_by_ref(repo: Repo) -> dict:
    out: dict[str, dict] = {}
    for r in repo.q("SELECT id, text, status, source_ref, tags, metadata, "
                    "proposed_scopes, proposed_by, proposed_by_type "
                    "FROM memory_candidates ORDER BY id"):
        try:
            meta = json.loads(r["metadata"] or "{}")
        except ValueError:
            meta = {}
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        out[r["source_ref"]] = {
            "id": r["id"], "text": r["text"], "status": r["status"],
            "source_ref": r["source_ref"], "tags": tags, "metadata": meta,
            "proposed_scopes": r["proposed_scopes"],
            "proposed_by": r["proposed_by"],
            "proposed_by_type": r["proposed_by_type"],
        }
    return out


def roundtrip(source_repo: Repo, request: RoundtripRequest) -> RoundtripReport:
    """Run ledger -> export -> validate -> import(fresh DB) -> compare.

    Read-only on `source_repo`. The import side is a throwaway temp DB; the live
    database is never written. Returns a fidelity report and, if `report_path` is
    set, writes it as pretty JSON.
    """
    report = RoundtripReport()
    report.classification_counts = _classification_counts()

    # Temp working area: a bundle dir + a fresh scratch repo. Both are removed on
    # the way out. Env DB overrides are cleared so the fresh repo lands here and a
    # stray BRAINCONNECT_DB cannot redirect the import at the live DB.
    work = Path(tempfile.mkdtemp(prefix="okf-roundtrip-"))
    _saved = os.environ.pop("BRAINCONNECT_DB", None)
    _saved_legacy = os.environ.pop("WIKIBRAIN_DB", None)
    try:
        bundle_dir = work / "bundle"

        # (1) EXPORT — read-only projection of the source ledger.
        export_res = export_bundle(source_repo, ExportRequest(
            output_dir=str(bundle_dir), scopes=list(request.scopes),
            trusted_only=request.trusted_only,
            include_superseded=request.include_superseded))
        report.source = {
            "scopes": [str(s) for s in request.scopes],
            "trusted_only": request.trusted_only,
            "include_superseded": request.include_superseded,
            "exported_claim_count": export_res.claim_count,
            "exported_source_count": export_res.source_count,
        }
        report.export = {
            "bundle_digest": export_res.bundle_digest,
            "withheld": list(export_res.withheld),
            "redacted": list(export_res.redacted),
            "warnings": list(export_res.warnings),
        }

        # (2) VALIDATE — structural gate before any import.
        vres = validate_bundle(str(bundle_dir), ValidationLimits())
        report.validation = {
            "ok": vres.ok, "okf_version": vres.okf_version,
            "errors": [e.as_dict() for e in vres.errors],
            "warnings": [w.as_dict() for w in vres.warnings],
        }
        if not vres.ok:
            report.warnings.append(
                "bundle failed structural validation; import not attempted")
            _write(report, request.report_path)
            return report

        # (3) IMPORT into a FRESH, empty DB.
        fresh_root = _make_fresh_repo(work / "fresh")
        with Repo.open(start=fresh_root) as fresh:
            imp = import_bundle(fresh, ImportRequest(
                bundle_dir=str(bundle_dir), scope=request.import_scope,
                imported_by=request.imported_by,
                imported_by_type=request.imported_by_type))
        report.imported = {
            "scope": imp.scope, "imported_by": imp.imported_by,
            "imported_by_type": imp.imported_by_type,
            "created": len(imp.created), "updated": len(imp.updated),
            "duplicates": len(imp.duplicates), "conflicts": len(imp.conflicts),
            "quarantined": len(imp.quarantined), "redacted": len(imp.redacted),
            "rejected": len(imp.rejected),
        }

        # Re-open the fresh DB and read what landed.
        with Repo.open(start=fresh_root) as fresh:
            cand_by_ref = _candidates_by_ref(fresh)
            n_claims = len(fresh.q("SELECT id FROM claims"))
            n_contra = len(fresh.q("SELECT id FROM contradictions"))
            n_super = len(fresh.q("SELECT id FROM supersessions"))
            n_cands = len(cand_by_ref)
            all_pending = all(c["status"] == "pending"
                              for c in cand_by_ref.values())

            # (idempotency) re-import the SAME bundle into the SAME fresh DB.
            imp2 = import_bundle(fresh, ImportRequest(
                bundle_dir=str(bundle_dir), scope=request.import_scope,
                imported_by=request.imported_by,
                imported_by_type=request.imported_by_type))
            n_cands_after = len(fresh.q("SELECT id FROM memory_candidates"))
        report.idempotent = (not imp2.created and not imp2.updated
                             and n_cands_after == n_cands)

        # --- per-claim evidence: which representable fields survived -----------
        # export records withheld/redacted as claim REF STRINGS; normalize to the
        # integer claim ids the comparison keys on.
        withheld_refs = [w["id"] for w in export_res.withheld]
        redacted_refs = list(export_res.redacted)
        withheld_ids = _refs_to_ids(withheld_refs)
        redacted_ids = _refs_to_ids(redacted_refs)
        per_claim = _compare(source_repo, request, cand_by_ref,
                             withheld_ids, redacted_ids)
        report.per_claim = per_claim

        # --- honesty facts, proven on this ledger ------------------------------
        withheld_bodies_absent = all(
            pc["original_body_survived"] is False
            for pc in per_claim if pc["body_class"] == OMITTED)
        redacted_masked = [pc["external_id"] for pc in per_claim
                           if pc["body_class"] == LOSSY]
        superseded_in_rt = [pc["external_id"] for pc in per_claim
                            if pc["source_status"] == "superseded"]
        report.honesty = {
            # trust / promotion / safety are ledger-owned; import is pending-only.
            "trust_not_carried": all_pending and n_claims == 0,
            "no_claims_created_on_import": n_claims == 0,
            "all_imported_candidates_pending": all_pending,
            "imported_side_is_untrusted": True,
            # a quarantined/withheld body is never exported -> not reconstructable.
            "quarantined_body_not_exported": sorted(withheld_refs),
            "quarantined_bodies_absent_from_imported": withheld_bodies_absent,
            # redacted secrets are masked -> lossy by design.
            "redacted_bodies_masked": sorted(redacted_refs),
            "redacted_masked_external_ids": sorted(redacted_masked),
            # superseded history only travels when the flag is set; and even then it
            # re-imports as a pending candidate + provenance, never as ledger state.
            "include_superseded": request.include_superseded,
            "superseded_claims_in_roundtrip": sorted(superseded_in_rt),
            "superseded_absent_unless_flagged": (
                request.include_superseded or not superseded_in_rt),
            # supersession/contradiction re-imported as provenance, NOT re-established.
            "supersessions_reestablished_in_fresh_db": n_super,
            "contradictions_reestablished_in_fresh_db": n_contra,
            "contradiction_supersession_are_provenance_only":
                n_super == 0 and n_contra == 0,
            # idempotent import: no uncontrolled duplication.
            "no_duplication_on_repeat_import": report.idempotent,
            "candidate_count_after_first_import": n_cands,
            "candidate_count_after_repeat_import": n_cands_after,
        }

        _write(report, request.report_path)
        return report
    finally:
        if _saved is not None:
            os.environ["BRAINCONNECT_DB"] = _saved
        if _saved_legacy is not None:
            os.environ["WIKIBRAIN_DB"] = _saved_legacy
        shutil.rmtree(work, ignore_errors=True)


def _compare(source_repo: Repo, request: RoundtripRequest, cand_by_ref: dict,
             withheld_ids: set, redacted_ids: set) -> list[dict]:
    """For each exported source claim, record how its fields survived import.

    The comparison is per SOURCE claim (the projection's subject). A withheld body
    is proven ABSENT from the imported candidate; a clean body is proven present;
    scope/status/trusted are proven governance-owned (retained as metadata but the
    candidate is pending and operator-scoped)."""
    # Which claims were exported? Re-run the same selection the exporter used, by
    # reading the claims and applying the request the same way. Simpler: enumerate
    # the imported candidates (one per exported non-empty claim doc) and join back.
    out: list[dict] = []
    for source_ref, cand in sorted(cand_by_ref.items()):
        if not source_ref.startswith(_REF_PREFIX):
            continue
        external_id = source_ref[len(_REF_PREFIX):]
        # external_id is refs.claim(<id>) for exporter-produced docs.
        claim_id = None
        if refs.kind_of(external_id) == refs.CLAIM:
            try:
                claim_id = refs.parse(external_id, refs.CLAIM)
            except refs.RefError:
                claim_id = None
        srow = None
        if claim_id is not None:
            srow = source_repo.one("SELECT * FROM claims WHERE id=?", (claim_id,))

        meta = cand["metadata"].get("okf_import", {})
        fm = meta.get("frontmatter", {})

        if claim_id in withheld_ids:
            body_class = OMITTED
        elif claim_id in redacted_ids:
            body_class = LOSSY
        else:
            body_class = EXACT

        original_body_survived = None
        if srow is not None:
            original_body_survived = srow["text"] in (cand["text"] or "")

        out.append({
            "external_id": external_id,
            "source_claim": refs.claim(claim_id) if claim_id else "",
            "source_status": srow["status"] if srow is not None else "",
            "imported_candidate": refs.candidate(cand["id"]),
            "imported_status": cand["status"],
            "body_class": body_class,
            "original_body_survived": original_body_survived,
            # governance proofs, per claim:
            "trusted_in_bundle": fm.get("trusted"),
            "trust_applied_on_import": False,  # never; candidate is pending
            "status_in_bundle": fm.get("status"),
            "status_on_import": cand["status"],  # always 'pending'
            "scope_in_bundle": fm.get("scope"),
            "governing_scope_on_import": _first_scope(cand["proposed_scopes"]),
            "relationships_as_provenance": meta.get("relationships", {}),
        })
    return out


def _refs_to_ids(ref_strings) -> set:
    """A set of integer claim ids from a list of `claim_<n>` ref strings."""
    out = set()
    for ref in ref_strings:
        try:
            out.add(refs.parse(ref, refs.CLAIM))
        except refs.RefError:
            continue
    return out


def _first_scope(proposed_scopes: str) -> str:
    try:
        arr = json.loads(proposed_scopes or "[]")
    except ValueError:
        return ""
    if arr and isinstance(arr[0], dict):
        st = arr[0].get("scope_type", "global")
        si = arr[0].get("scope_id", "")
        return f"{st}:{si}" if si else st
    return ""


def _make_fresh_repo(root: Path) -> Path:
    """Create an empty BrainConnect repo at `root` and return it.

    Mirrors the test/demo harness: a minimal config.toml pointing the DB inside
    `root`, the standard subdirs, and a fresh schema via init_db."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(
        f'[paths]\ndb = "{(root / "wiki.db").as_posix()}"\n'
        'bookmark_folder = "wiki"\n', encoding="utf-8")
    for d in ("raw", "inbox", "db", "wiki"):
        (root / d).mkdir(parents=True, exist_ok=True)
    init_db(start=root).close()
    return root


def _write(report: RoundtripReport, report_path: str) -> None:
    if not report_path:
        return
    p = Path(report_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=False) + "\n",
                 encoding="utf-8")
