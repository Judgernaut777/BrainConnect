# OKF.md — Open Knowledge Format support

> The product is **BrainConnect**. OKF is a **portable, human-readable projection**
> of a BrainConnect ledger. It is not BrainConnect's database format, and it is not
> a second source of truth.

This document covers **Stage 1 (the exporter)**, **Stage 2 (the validator)**, and
**Stage 3 (the importer)** — see the Import section at the end.

---

## Why

BrainConnect's canonical store is a SQLite ledger (see [LEDGER_SPEC.md](LEDGER_SPEC.md)).
That is right for trust, provenance, and governance, and wrong for two things:
reading knowledge without the database, and moving it between systems. OKF is a
directory of Markdown documents with YAML frontmatter that a human can read in an
editor, a static site can render, and another tool can ingest — while every
BrainConnect-specific concept (scope, trust, supersession, contradiction,
provenance) travels alongside in a documented `brainconnect:` extension block.

## The boundary: ledger vs. projection

The load-bearing rule, enforced in code and asserted in the acceptance suite:

> **The ledger is canonical. An OKF bundle is a read-only projection of it.**

- **Export never mutates the ledger.** No `finalize`, no `UPDATE`, no `INSERT`.
  Every table's fingerprint is identical before and after an export.
- **Editing an exported file changes nothing.** A projection is downstream. Import
  (a later stage) will be the *only* way a file can affect the ledger, and it will
  do so by creating PENDING candidates through the normal safety + human-promotion
  pipeline — never by editing a promoted claim.
- **OKF-valid ≠ trusted, promoted, or safe.** A well-formed bundle says nothing
  about whether its claims are vouched-for. Trust metadata is *preserved and
  labeled*, never conferred by the format.

## Supported OKF version

This build writes and pins **OKF `0.1`**. The version is written into every
bundle (the `.okf-bundle` marker and each document's `okf_version` frontmatter
key). The validator (Stage 2) rejects an unsupported **major** version and warns
on a newer compatible **minor**, preserving unknown fields it does not recognize.

## Bundle layout

```
<output-dir>/
  .okf-bundle                 marker: format + version (identifies a bundle)
  index.md                    navigation, filters used, withheld/redaction notes
  claims/
    claim_<id>.md             one document per exported claim
  sources/
    source-index.md           evidence the exported claims cite (anchored by id)
  history/
    log.md                    supersession log — only with --include-superseded
```

File naming and ordering are deterministic: claims are named by their id and
listed in id order; nothing carries a wall-clock timestamp. Identical ledger state
and identical request flags produce a **byte-identical** bundle.

## Mapping table

A claim/candidate becomes a Markdown concept document. BrainConnect-owned fields
live under the `brainconnect:` frontmatter key.

| BrainConnect concept | OKF location |
|---|---|
| claim id | `brainconnect.id` (e.g. `claim_4`) |
| derived title | `title` (front) + `# ` heading — from the *safe* body |
| claim text | document body (masked or withheld per safety; see below) |
| tags | `tags` (front) |
| scope | `brainconnect.scope` (e.g. `repo:my-app`, `global`) |
| status | `brainconnect.status` (`promoted`/`pending`/`superseded`/`contradicted`) |
| trust | `brainconnect.trusted` (bool) — authority, not status |
| confidence | `brainconnect.confidence` (`low`/`medium`/`high`/`verified`) |
| sources | `brainconnect.sources[]` + a `## Sources` link list |
| valid_from / valid_until | `brainconnect.valid_from` / `valid_until` |
| learned_at / last_verified_at | `brainconnect.learned_at` / `last_verified_at` |
| supersession | `brainconnect.superseded_by` + a `## Superseded by` relative link |
| contradiction | `brainconnect.contradictions[]` + a `## Contradicts` relative link |
| provenance | `brainconnect.provenance` (`origin`, `promoted_by`, `candidate_id`) |
| safety (non-sensitive) | `brainconnect.safety` — decision/kinds/findings, never a value |
| relationships | relative Markdown links between documents in the bundle |
| navigation | `index.md` |
| history | `history/log.md` (opt-in) |

Relationships are **relative Markdown links** (`claim_9.md`,
`../sources/source-index.md#source_7`) so the bundle is self-contained and a
supersession or contradiction resolves by clicking.

## Export examples

```bash
# global export of every scope, current facts only
brainconnect export okf --output ./knowledge

# only trusted (promoted, non-contradicted) claims
brainconnect export okf --output ./knowledge --trusted-only

# a scope-filtered bundle (global facts stay visible)
brainconnect export okf --output ./knowledge --scope repo:my-app --scope user:matthew

# include superseded claims and a supersession history log
brainconnect export okf --output ./knowledge --include-superseded

# machine-readable result (counts, digest, withheld/redacted, warnings)
brainconnect export okf --output ./knowledge --json
```

Filtering semantics:

- **default (current-only):** every claim except `rejected`, `archived`, and
  `superseded`. That is promoted, pending, and contradicted claims.
- **`--trusted-only`:** only claims that are `trusted` — `promoted` **and** not
  party to an open contradiction. Pending and disputed claims are excluded.
- **`--scope`:** repeatable. A claim is included iff it is `global` **or** its scope
  is among those requested — the same rule recall uses, so `global` facts stay
  visible in a scoped bundle while another repo's claims never leak in. Omitting
  `--scope` exports every scope.
- **`--include-superseded`:** additionally exports `superseded` claims and writes
  `history/log.md`.

## Export safety behavior

Before any human/agent-readable body is written, the claim text runs through the
existing **`memory_recall` safety surface** (see [SAFETY.md](SAFETY.md)). No new
safety policy was added — export is exactly the recall exposure problem (stored
text reaching a reader), so it reuses that path:

- **Secrets and PII are masked.** The body is the masked representation (`█` runs);
  the raw value never reaches the file. The canonical claim text in the ledger is
  unchanged.
- **High-risk injection / tool-control content is WITHHELD.** The claim's document
  is still written (identity and trust metadata preserved), but its body is replaced
  with a withheld notice and a warning is recorded in `index.md` and in the export
  result. Nothing is deleted from the ledger. **Withheld, not silently dropped.**
- **A required safety engine that cannot run withholds the body** (fail-closed):
  content that could not be scanned is not treated as clean.
- **The exported `brainconnect.safety` block never contains a matched value** — and
  is deliberately narrower than the recall verdict: decision, kinds, and per-finding
  `rule`/`severity`/`span`/`engine` only. No raw secret finding, no unsafe original
  span, ever reaches a file or the safety metadata.

Export masks/withholds even content that another process wrote straight to the
ledger. It does not scan the ledger at rest — a claim is caught on the way *out*,
consistent with the recall surface.

## Rules that hold across all stages

- **No auto-promotion.** A future import creates PENDING candidates only; agents
  can never promote, and OKF cannot bypass the human gate.
- **No bidirectional sync.** No directory watching, no auto-merge, no
  auto-supersede, no silent conflict resolution. A projection is downstream of the
  ledger, full stop.
- **Never described as the database.** OKF is interchange + a readable projection.
  The SQLite ledger remains the source of truth.

## Validation (Stage 2)

`brainconnect okf validate ./knowledge` structurally validates a bundle and
returns structured errors + warnings. `brainconnect okf inspect ./knowledge`
prints a one-screen summary (version, document/claim/source counts, ids, and any
findings). Both exit non-zero on an invalid bundle; add `--json` for a
machine-readable `ValidationResult`.

```bash
brainconnect okf validate ./knowledge          # human output, exit != 0 if invalid
brainconnect okf validate ./knowledge --json    # {ok, errors[], warnings[], …}
brainconnect okf inspect  ./knowledge           # summary + findings
```

The Python surface is `OKFAdapter().validate_bundle(path, limits=None)` →
`ValidationResult(ok, errors[], warnings[], okf_version, document_count,
claim_count, source_count, ids[])`. Each `ValidationIssue` carries a machine-stable
`code`, a human `message`, and the offending bundle-relative `path`.

> **Structural only. Validity is not trust.** A `ValidationResult.ok == True`
> means the directory is a well-formed OKF bundle — *nothing* about whether its
> claims are vouched for. The result deliberately carries **no** `trusted` or
> `safe` field. A perfectly valid bundle can be entirely hostile; import (Stage 3)
> is where content enters the normal candidate + safety pipeline as PENDING, and
> only a human can promote. The validator never imports, never executes, and never
> trusts bundle content — and a malformed or unsafe-structure bundle is **never**
> reported `ok`.

### What is checked

Errors (each makes the bundle invalid):

| code | meaning |
|---|---|
| `not_found` / `not_a_directory` | the path is missing or not a directory |
| `missing_marker` | no `.okf-bundle` marker at the root |
| `bad_marker` | marker is not `format=okf` / has no `version=` |
| `bad_version` / `unsupported_version` | version is unparseable, or an unsupported MAJOR |
| `missing_frontmatter` | a `claims/*.md` document has no YAML frontmatter |
| `malformed_frontmatter` / `malformed_yaml` | the frontmatter block is unterminated or unparseable |
| `missing_field` / `bad_field` | a required frontmatter field (`okf_version`, `brainconnect.id`) is absent or the wrong type |
| `duplicate_id` | two documents claim the same `brainconnect.id` |
| `broken_link` | a relative Markdown link does not resolve inside the bundle |
| `absolute_link` | a link uses an absolute path |
| `link_traversal` | a `../` link escapes the bundle root |
| `symlink_escape` | a symlink points outside the root (it is **never followed**) |
| `unsafe_filename` | a path component has a control / bidi / zero-width char, a separator, or a reserved name |
| `invalid_encoding` | a file is not valid UTF-8 |
| `file_too_large` / `bundle_too_large` | a single file, or the whole bundle, exceeds its size cap (the oversized file is **not read**) |
| `too_many_files` / `too_deep` | the tree exceeds the entry-count / nesting caps |
| `broken_relationship` | a `superseded_by` / `contradictions` target has no document |

Warnings (reported, never fatal):

| code | meaning |
|---|---|
| `newer_minor_version` | a newer compatible MINOR than this build writes; unknown fields are preserved |
| `unknown_field` | an unknown top-level or `brainconnect.*` field — **preserved, not dropped** |
| `id_filename_mismatch` | `brainconnect.id` does not match the filename stem |
| `missing_title` | a claim document has no `title` |
| `relationship_cycle` | a supersession cycle (contradictions are symmetric and are **not** counted) |
| `symlink_present` | a symlink that stays inside the bundle (not followed during validation) |
| `private_key_marker` | a bare PEM private-key delimiter appears in a document body |

### Security posture (the validator is hardened against a hostile bundle)

The validator assumes the bundle is adversarial and protects the host:

- **Never follows a symlink out.** Symlinks are classified *lexically* with
  `os.readlink` — the target is never resolved through the filesystem, so an
  escaping symlink is rejected and its target is never opened.
- **Never reads unbounded.** Every file's size is taken from its stat entry first;
  anything over the per-file cap is flagged and skipped, and the running total is
  bounded so an oversized bundle fails closed.
- **Never executes or imports content.** Frontmatter is parsed by a tiny, bounded,
  stdlib subset parser that only ever produces plain containers and scalars — there
  is no object construction, no `eval`, no code path that imports bundle content.
- **Resolves every path against the real root.** Traversal (`../`) and absolute
  paths are classified lexically, so a malicious link can never make the validator
  touch the host filesystem.
- **Never hangs.** The directory walk is depth- and count-bounded, frontmatter
  nesting is bounded, and relationship-cycle detection is an iterative,
  finite-graph DFS (no recursion limit to blow, no loop to spin on).

Size and count caps are configurable via `ValidationLimits` (per-file bytes, total
bundle bytes, max files, max directory depth, max YAML nesting) and default to
sane values (2 MiB/file, 64 MiB/bundle, 10 000 files).

### Round-trip

A Stage-1 export validates clean: `export okf` → `okf validate` is a structural
round trip (asserted in the acceptance suite and demonstrated by
`scripts/okf_validate_demo.py`, which also rejects a battery of hostile bundles).

## Import (Stage 3)

    brainconnect import okf ./knowledge --scope repo:my-app --by matthew [--by-type human] [--dry-run] [--json]

Import is the highest-risk stage and the most conservative one. Its entire
authority is: **turn documents from an external bundle into PENDING memory
candidates.** Nothing it does can produce trusted or promoted knowledge — that
stays a separate, human-only step (see `docs/SAFETY.md`, LEDGER_SPEC §5.2).

### The flow, in order

1. **Structural validation** — the Stage-2 validator runs first. An invalid bundle
   is refused **whole**: nothing is imported (no partial import). A hostile bundle
   is inert input at this point, never executed.
2. **Provenance registration** — for each claim document, import records bundle
   path, a bundle checksum (the "source checksum"), the OKF version, the document
   path, the external id, a per-document content checksum, the imported-at
   timestamp, the importing actor and type, and the document's relative
   relationships (`superseded_by`, `contradictions`). These land in the candidate's
   `metadata.okf_import`.
3. **Import safety scan** — every document runs through the existing
   `memory_candidate` safety surface **before** it is stored anywhere. This covers
   **both the claim body and every retained free-text frontmatter value** (notably
   `provenance`): retained metadata is untrusted bundle content too, and it lands in
   recallable candidate storage (the `metadata` column, `candidate` get/listing,
   `db/dump.sql`, `log.md`), so it gets the same scan — the body is *not* the only
   scanned field. On that surface a secret is **masked** before it can reach an inbox
   artifact or a candidate row; injection / tool-control content is **quarantined**
   (accepted-but-quarantined, needs a human override at promotion, exactly like a
   quarantined capture); and a retained free-text value that a **required engine could
   not scan is dropped fail-closed** — never stored unscanned. No raw unsafe span is
   ever written to a log or to recallable metadata.
4. **Candidate creation** — a PENDING candidate, via the same `candidates.create_checked`
   path a normal capture uses. There is no argument that makes it anything else.
5. **Stop.** Human promotion is separate and unchanged.

### Invariants (each one a critical bug if broken)

- **No auto-promotion, ever.** Every created row is `status='pending'`. Import calls
  `create_checked` and never `promote`.
- **No bypass of the human gate.** An `agent` actor may *propose* an import — that is
  what a candidate is for — but the resulting row is pending like any other. Nothing
  in import can make an agent's content trusted. `--by-type agent` changes only the
  recorded actor type, never the outcome.
- **An external id confers no write authority over canonical state.** If an imported
  document's external id already traces to a **promoted** claim, import **refuses to
  touch that claim** and returns an explicit `conflict` requiring operator action. It
  never edits, supersedes, or overwrites a canonical claim. (Because import only ever
  writes pending candidates, a canonical claim is unreachable by construction; the
  conflict check makes the refusal *explicit and visible* rather than silent.)
- **OKF-valid is not trusted and is not safe.** A structurally valid bundle is still
  untrusted, unsafe-until-scanned input. All bundle content is DATA, never
  instructions.

### The operator governs scope, not the bundle

`--scope` sets the scope of every candidate the import creates. A document's own
`scope:` field is retained only as informational metadata — a bundle that claims
`global` scope can never land global recall on its own say-so.

### Idempotency and conflict

Identity is keyed on the **external id** (`brainconnect.id`), recorded as the
candidate `source_ref` `okf:<id>`. Content change is detected by a per-document
checksum.

- **Duplicate** (same external id, same checksum): idempotent — no new candidate is
  created; the existing one is reported. Re-importing a bundle N times produces no
  duplication.
- **Update** (same external id, *changed* content, and no promoted claim owns it): an
  **explicit new PENDING candidate** is created and reported as an update, linked to
  the prior candidate(s). Never a silent overwrite of the earlier candidate.
- **Conflict** (the external id already owns a **promoted** claim): refused. An
  explicit `conflict` result is returned for operator action; the canonical claim is
  byte-for-byte unchanged. Resolving it (if the operator wants the new text) is done
  through the normal claim-supersession governance path, **not** through import.

External ids are namespaced by the *exporting* ledger; BrainConnect does not assume
they are globally unique. A collision between two ledgers surfaces as an update or a
conflict for human resolution — never a silent overwrite.

### Import safety details

Import reuses the `memory_candidate` safety surface rather than adding a new one:
that surface already masks secrets before storage and quarantines injection /
tool-control content, which is exactly import's requirement (see ADR 0006). A safety
**block** (should a future engine map a category that way on this surface) stores
nothing; the attempt is recorded in the result and the audit log, carrying finding
*kinds* only — never the matched value.

**Retained frontmatter is scanned, not just the body.** The candidate keeps a
*subset* of the document's `brainconnect:` frontmatter as informational metadata —
the structured, low-risk keys (`status`, `scope`, `confidence`, `trusted`, the
`valid_*`/`learned_at`/`last_verified_at` timestamps, and the `superseded_by` /
`contradictions` relationships) plus the **free-text `provenance`**. Every string
value in that retained subset (recursively, including nested structures like the
`provenance` dict) is routed through the **same `memory_candidate` scan the body
gets**, before storage:

- a **secret or PII** in a retained value is **masked** (the stored value is the
  masked representation), so no raw credential ever reaches the `metadata` column,
  `candidate` get/listing, `db/dump.sql`, or `log.md`;
- **high-risk injection / tool-control** content in a retained value **quarantines
  the whole candidate**, exactly as it would in the body;
- if a **required safety engine is unavailable**, the retained free-text is
  **dropped fail-closed** — unscanned free-text is never stored — matching the body
  path's refusal to store what it could not clear.

A clean retained value round-trips verbatim; masking, quarantine, and dropping
engage only on a finding. An audit-safe record of what the metadata scan saw
(decision, kinds, findings — never a matched value) is kept at
`metadata.okf_import.metadata_safety`. This closes the gap where a secret planted in
`brainconnect.provenance` with a clean body would have been stored raw while the body
scan reported clean.

### What import is not

No directory watching, no bidirectional sync, no auto-merge, no auto-supersede, no
silent conflict resolution. Import is a one-shot, operator-invoked, human-gated
intake — not a live mirror of an external store.

A runnable demonstration is `scripts/okf_import_demo.py` (scratch DB): it imports a
valid bundle to pending candidates, imports a secret (redacted) and an injection
(quarantined), re-imports idempotently, refuses an external-id overwrite of a
promoted claim, and shows an agent-actor import still landing only pending.

## Round-trip fidelity (Stage 4)

Stage 4 runs the whole cycle end to end and produces a **machine-readable fidelity
report** so that no one has to *trust* a prose claim about what survives:

```bash
# ledger -> export -> validate -> import into a FRESH DB -> compare
brainconnect okf roundtrip --report ./fidelity.json
brainconnect okf roundtrip --report ./fidelity.json --scope repo:my-app --json
brainconnect okf roundtrip --report ./fidelity.json --include-superseded --trusted-only
```

The export leg is **read-only** on the live ledger; the import leg lands in a
**throwaway temporary database**, so `roundtrip` never writes the live DB. The
imported side is a set of **PENDING candidates** (import never auto-promotes), so
the honest comparison is: *which of the source claims' representable fields survive
into the imported candidates + their provenance*, and *which governance state is
deliberately not reconstructed from a projection and remains ledger-owned.*

**We do not claim complete round-trip fidelity.** OKF is a portable projection, not
an authority. A projection cannot carry trust, promotion status, audit history, or
contradiction/supersession bookkeeping — those are re-established only by a human
through the normal promotion path. The report's headline says exactly this.

### Fidelity classification

Every mapping-table field is classified as exactly one of:

| Class | Meaning |
|---|---|
| **exactly-preserved** | survives byte-for-byte into the imported side |
| **mapped** | transformed but recoverable |
| **intentionally-omitted** | not carried by design (re-derivable or safety-withheld) |
| **lossy** | only partially represented (by design) |
| **governance-only** | trust / promotion / audit / relationship bookkeeping — **never carried by OKF**, ledger-owned, re-established only by human promotion |

| Field | Class | Why |
|---|---|---|
| `id` | mapped | source claim id → candidate `source_ref` `okf:<id>` + `metadata.external_id`; not re-used as a claim id (imported side is a new pending candidate) |
| `title` | intentionally-omitted | derived from the safe body on export; not retained on import (a free-text title could smuggle a secret); re-derivable from the body |
| `body` | exactly-preserved | a clean body imports verbatim as the candidate text. **Degrades under safety:** a redacted secret → **lossy** (masked only), a quarantined/injection body → **intentionally-omitted** (withheld, never exported) |
| `tags` | exactly-preserved | source tags survive as a subset of the candidate tags |
| `scope` | mapped | retained as informational metadata; the **operator's** `--import-scope` governs the candidate, never the bundle |
| `status` | governance-only | the status string is retained as metadata, but the candidate is always PENDING; promotion status is re-established only by a human |
| `confidence` | mapped | retained as metadata; the governing confidence is set by the reviewer at promotion |
| `trusted` | **governance-only** | **trust is never carried.** The exported flag is informational only; the imported candidate is untrusted. OKF-valid ≠ trusted |
| `sources` | lossy | citations are fully in the bundle but not re-attached to the pending candidate; import registers the *bundle* as provenance |
| `valid_from` / `valid_until` | mapped | retained as safety-scanned metadata; not applied as ledger validity until promotion |
| `learned_at` / `last_verified_at` | mapped | retained as metadata; import also stamps its own `imported_at` |
| `superseded_by` | **governance-only** | re-imported as a provenance link (`relationships.superseded_by`), **not** a re-established supersession — no `supersessions` row, no `claims.superseded_by` |
| `contradictions` | **governance-only** | re-imported as provenance (`relationships.contradictions`), **not** a re-established open contradiction — no `contradictions` row |
| `provenance` | mapped | retained as safety-scanned metadata (masked if it carried a secret); import also records its *own* provenance block |
| `safety` | **governance-only** | the exported non-sensitive safety block is informational; on import the content is **re-scanned fresh** — safety is never carried as a decision. OKF-valid ≠ safe |

Concept-level governance that is **deliberately not reconstructable from a
projection**: trust, promotion status, audit history, and contradiction /
supersession bookkeeping. The report lists these under `governance_concepts`.

### The honest edges

- **A withheld (quarantined) body is never exported.** An injection / tool-control
  body is projected as a text-free placeholder; the round-trip proves the original
  is absent from the imported side. Classified intentionally-omitted.
- **Redacted secrets are masked → lossy by design.** Only the masked representation
  travels; the raw text never leaves the ledger.
- **Superseded history travels only with `--include-superseded`** — and even then it
  re-imports as a pending candidate + provenance, never as ledger supersession state.
- **Contradiction and supersession become relative links + metadata**, re-imported as
  provenance, not as re-established ledger contradiction/supersession state.
- **Trust and safety are governance-only / ledger-owned.** Import lands PENDING and
  untrusted; the report's `honesty.trust_not_carried` is proven on the actual ledger
  (no canonical claims created, every candidate pending).
- **Idempotent.** A repeat round-trip creates no new candidates (proven in the report
  as `no_duplication_on_repeat_import`).

### The report

`RoundtripReport` (also emitted as pretty JSON to `--report FILE`) carries:
`fidelity_claim` (the honest "PARTIAL BY DESIGN" headline), `source` / `export` /
`validation` / `imported` summaries, `field_fidelity` (the table above),
`governance_concepts`, `classification_counts`, a data-driven `honesty` block, and
`per_claim` evidence (each exported claim's body class + whether its original body
survived + its governance proofs). No raw secret or injection value ever appears in
the report — only finding *kinds*.

A runnable demonstration is `scripts/okf_roundtrip_demo.py` (scratch DB): it seeds a
rich ledger (promoted / pending / superseded / contradicted / redacted-secret /
withheld-injection across scopes), runs the full cycle, and prints the report plus
the key honesty facts — trust not carried, quarantined body not exported, secret
masked, contradiction/supersession not re-established, no duplication on repeat.
