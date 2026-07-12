# ADR 0007 — OKF round-trip fidelity: an honest, machine-readable accounting

Status: accepted (2026-07-13, OKF Stage 4)
Scope: `cli/brainconnect/okf/roundtrip.py` (new), `brainconnect okf roundtrip` (new
CLI subcommand), `OKFAdapter.roundtrip`, `docs/OKF.md`,
`scripts/okf_roundtrip_demo.py` (new), `tests/acceptance.py::_okf_roundtrip_checks`
(new). No schema change; no new or broadened safety surface; no ledger mutation.

## Context

Stages 1–3 export a bundle, structurally validate one, and import one as pending
candidates. Stage 4 must answer — verifiably, and without overclaiming — *what
survives a full round-trip and what does not.* The temptation is to assert
"BrainConnect round-trips OKF." That assertion would be **false**: OKF is a portable
projection, not an authority, and the importer lands PENDING candidates by design. A
projection cannot carry trust, promotion status, audit history, or
contradiction/supersession bookkeeping; those are re-established only by a human
through the promotion gate. The deliverable is therefore an *honest* fidelity report,
not a fidelity *guarantee*.

## Decision

1. **The round-trip is real and read-only on the source.** `roundtrip(repo, request)`
   runs the actual cycle: `export_bundle` (Stage 1, read-only) → `validate_bundle`
   (Stage 2) → a **fresh, empty temp DB** into which `import_bundle` (Stage 3) runs →
   comparison. The source ledger is never mutated (asserted by table-fingerprint in
   the acceptance suite); the import side is a `tempfile` DB removed on the way out,
   and `BRAINCONNECT_DB` / `WIKIBRAIN_DB` are cleared for its lifetime so a stray env
   override cannot redirect the import at the live database.

2. **Every field carries exactly one of five fidelity classes:**
   `exactly-preserved`, `mapped`, `intentionally-omitted`, `lossy`, `governance-only`.
   The `FIELD_FIDELITY` table (the static contract) classifies all 17 mapping-table
   fields; `roundtrip()` proves the contract against real data (per-claim body class,
   whether the original body survived, governance proofs).

3. **Trust and safety are `governance-only`, and the report says so loudly.**
   `trusted` and `safety` are never carried: the exported `trusted` flag is
   informational metadata only, and the exported safety block is informational — on
   import the content is **re-scanned fresh** through `memory_candidate`. Promotion
   `status`, `superseded_by`, and `contradictions` are likewise `governance-only`:
   supersession/contradiction re-import as **provenance links + metadata**, never as
   re-established ledger state (the fresh DB's `supersessions` and `contradictions`
   tables stay empty — proven). OKF-valid ≠ trusted, ≠ promoted, ≠ safe.

4. **The honest edges are represented, not hidden.** A body's classification is
   `exactly-preserved` for a clean body but **degrades**: a redacted secret →
   `lossy` (masked only), a quarantined/injection body → `intentionally-omitted`
   (withheld, never exported). `sources` is `lossy` (citations live in the bundle but
   are not re-attached to the pending candidate). `title` is `intentionally-omitted`
   (re-derivable; not retained on import to avoid smuggling a secret into recallable
   metadata). Superseded history travels only with `--include-superseded`.

4a. **A body's `exactly-preserved` class is PROVEN, not asserted from safety
   membership.** The original defect: `_compare()` set the body class purely from
   withheld/redacted membership and never cross-checked the separately-computed
   `original_body_survived`, so a body that was neither withheld nor redacted but did
   not survive byte-for-byte was still reported `exactly-preserved`. Fixed: a
   non-withheld, non-redacted body is `exactly-preserved` **only if**
   `original_body_survived` is `True`; otherwise it is downgraded to `lossy` with a
   `body_class_reason`. A hard guard (`_assert_body_honesty` → `RoundtripHonestyError`)
   makes emitting `exactly-preserved` for a non-surviving body impossible, and the
   aggregate `field_fidelity[body].observed` block reconciles the best-case contract
   with per-claim reality. **Two known lossy body transforms** are documented in
   `docs/OKF.md`: (a) **trailing-whitespace normalization** (exporter `rstrip` +
   importer `strip`) — left as-is, honestly reported `lossy` (`"normalized"`); and (b)
   **embedded-heading ambiguity** — a body containing its own `## Sources` heading used
   to be truncated on import. **Delimiter hardened:** the exporter now writes an
   explicit `<!-- okf:body-end -->` machine marker after every body and the importer
   cuts there instead of at a human heading, so such a body survives byte-for-byte and
   is honestly `exactly-preserved`. The header-cutting heuristic remains only as a
   tolerant fallback for a foreign bundle without the marker. The marker is an HTML
   comment (invisible, no Markdown link), so Stage 1/2/3 and export determinism are
   unaffected.

5. **The report never claims complete fidelity.** `RoundtripReport.fidelity_claim` is
   a fixed "PARTIAL BY DESIGN" statement naming exactly what is not carried. This is a
   required field, not an optional footnote.

6. **The report is machine-readable and leak-free.** It is emitted as pretty JSON to
   `--report FILE` and as `--json`. It carries `field_fidelity`, `governance_concepts`,
   `classification_counts`, a data-driven `honesty` block (trust-not-carried,
   quarantined-body-not-exported, redacted-masked, contradiction/supersession not
   re-established, idempotent), and `per_claim` evidence. It contains only finding
   *kinds* — no raw secret or injection value ever appears (asserted).

7. **Idempotency is proven, not assumed.** `roundtrip` re-imports the same bundle into
   the same fresh DB and asserts no new candidates are created, surfacing
   `honesty.no_duplication_on_repeat_import`.

## Consequences

- `OKFAdapter.roundtrip(repo, RoundtripRequest) -> RoundtripReport` is added to the
  adapter seam alongside export/validate/import. The `KnowledgeFormatAdapter` Protocol
  is unchanged (round-trip is a composition of the three existing operations, not a
  new format capability).
- `brainconnect okf roundtrip --report FILE [--scope …] [--trusted-only]
  [--include-superseded] [--import-scope S] [--by ACTOR] [--json]` opens the live repo
  read-only and prints a summary; the acceptance CLI check `chdir`s into a scratch
  repo. Because the command never mutates the source ledger, it produces no dump/log
  churn on the source.
- No schema migration and no safety change: round-trip reuses export, validate, and
  import verbatim. The demo `scripts/okf_roundtrip_demo.py` and
  `tests/acceptance.py::_okf_roundtrip_checks` exercise the rich-ledger cycle, the
  classification of every field, the governance-only invariants, the honest edges, and
  idempotency, all against a scratch DB.
