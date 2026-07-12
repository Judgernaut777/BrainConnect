# OKF.md — Open Knowledge Format support

> The product is **BrainConnect**. OKF is a **portable, human-readable projection**
> of a BrainConnect ledger. It is not BrainConnect's database format, and it is not
> a second source of truth.

This document covers **Stage 1 (the exporter)** and **Stage 2 (the validator).**
Import is a later stage and is stubbed at the end of this file.

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

## Import — next stage

`brainconnect import okf ./knowledge --by <human>` is **not implemented in this
build**. Import will flow bundle → structural validation → source/provenance
registration → safety scanning → candidate creation → normal human promotion.
Imported documents become PENDING candidates, never auto-promoted; an external id
can detect origin/duplication but can never overwrite a promoted claim without
governance. `OKFAdapter.import_bundle` raises `NotImplementedError` today.
