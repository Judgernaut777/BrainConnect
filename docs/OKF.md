# OKF.md — Open Knowledge Format support

> The product is **BrainConnect**. OKF is a **portable, human-readable projection**
> of a BrainConnect ledger. It is not BrainConnect's database format, and it is not
> a second source of truth.

This document covers **Stage 1: the exporter.** Validation and import are later
stages and are stubbed at the end of this file.

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
key). A future importer will reject an unsupported major version and warn on a
newer compatible minor.

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

## Validation — next stage

`brainconnect okf validate ./knowledge` is **not implemented in this build**. It
will structurally check a bundle (frontmatter, supported version, unique doc
identity, relative-link validity, path traversal, malformed YAML, unsafe
filenames, unsupported extension fields, broken relationships, size limits, symlink
behavior, encoding) and return structured errors + warnings. Validity will still
not mean trust or safety. `OKFAdapter.validate_bundle` raises `NotImplementedError`
today.

## Import — next stage

`brainconnect import okf ./knowledge --by <human>` is **not implemented in this
build**. Import will flow bundle → structural validation → source/provenance
registration → safety scanning → candidate creation → normal human promotion.
Imported documents become PENDING candidates, never auto-promoted; an external id
can detect origin/duplication but can never overwrite a promoted claim without
governance. `OKFAdapter.import_bundle` raises `NotImplementedError` today.
