# ADR 0004 — OKF export: a portable projection, not a second source of truth

Status: accepted (2026-07-12, OKF Stage 1)
Scope: `cli/brainconnect/okf/` (new module), `brainconnect export okf` (new CLI),
`docs/OKF.md`

## Context

BrainConnect needs a portable, human-readable interchange format so a ledger's
knowledge can be read, reviewed, and moved between systems without a database.
The Open Knowledge Format (OKF) — a directory of Markdown documents with YAML
frontmatter — is that format. This ADR covers **Stage 1: the exporter only.**
Validation (Stage 2) and import (Stage 3) are deliberately out of scope.

The risk is not the file format. The risk is that a projection quietly becomes an
authority — that "it is in the OKF bundle" starts to mean "it is true / trusted /
safe", or that exporting starts to change the thing it is projecting.

## Decision

1. **The ledger is canonical; the bundle is a projection.** Export is strictly
   read-only. It issues no `finalize()`, no `UPDATE`, no `INSERT`. The acceptance
   suite fingerprints every table before and after an export and asserts equality.

2. **A narrow adapter, not a plugin framework.** One `KnowledgeFormatAdapter`
   Protocol with one implementation, `OKFAdapter`. The Protocol declares
   `export_bundle`, `validate_bundle`, and `import_bundle` so the seam is stable,
   but Stage 1 implements only export; the other two raise `NotImplementedError`.
   This isolates BrainConnect from a draft external format — it is not speculative
   extensibility, and there is no service and no generalized registry.

3. **OKF-valid ≠ trusted/promoted/safe.** Trust metadata is preserved and labeled
   (`brainconnect.trusted`, `status`, `contradictions`), never conferred. A
   `--trusted-only` filter narrows *what is projected*; it does not change what the
   projection *means*.

4. **Reuse the recall output safety path; add no new policy.** Before any
   human/agent-readable body is written, the claim text runs through the existing
   `memory_recall` safety surface (docs/SAFETY.md). Secrets and PII are **masked**;
   high-risk injection / tool-control content is **withheld** with a visible
   warning; a required-engine failure withholds the body (fail-closed). The
   existing surface covers export cleanly, so **no `okf_export` policy was added** —
   broadening safety for a read projection that is exactly the recall exposure
   problem would have been redundant surface area. The exported `brainconnect.safety`
   block is deliberately *narrower* than `verdict.summary()`: it carries decision,
   kinds, and per-finding rule/severity/span/engine only — **never** a matched
   value, and never the message string. Canonical claim text in the ledger is never
   mutated.

5. **Deterministic, atomic, self-validated.** Output is ordered by id, carries no
   wall-clock time, and uses a fixed frontmatter key order and a stdlib-only YAML
   emitter, so identical ledger state + identical request → byte-identical bundle.
   The bundle is built in a sibling staging directory, structurally self-checked
   (frontmatter present, OKF version pinned, ids match filenames, relative links
   resolve, no withheld body leaks its text), then swapped into place atomically. A
   mid-write failure removes the staging dir and leaves any existing bundle intact.
   A non-empty directory that is not itself an OKF bundle is refused, never
   clobbered.

6. **A stdlib-only YAML emitter.** The exporter writes its own deterministic YAML
   rather than depend on PyYAML. A clean wheel install carries no PyYAML, and
   PyYAML's emit ordering is not a stable contract across versions — either would
   break byte-identical reproducibility. The emitted text is verified to parse as
   real YAML in the acceptance suite (when PyYAML is present), but nothing at
   runtime depends on it.

## Consequences

- A consumer of an OKF bundle inherits authority guarantees from
  `brainconnect.trusted` and exposure guarantees from the safety pass, and must not
  confuse the two — exactly the LEDGER_SPEC §14 contract, now on disk.
- Editing an exported file changes nothing until an explicit import (Stage 3), which
  will enter the normal candidate + safety + human-promotion pipeline as PENDING.
  There is no bidirectional sync, no directory watching, no auto-merge, no
  auto-promotion.
- The pinned `OKF_VERSION = "0.1"` is written into every bundle. A future importer
  rejects an unsupported major and warns on a newer compatible minor.
- Export masks/withholds content a process wrote straight to the ledger, but it does
  not scan the ledger at rest — a claim is caught on the way out, consistent with
  the recall surface (docs/SAFETY.md, "scanning happens at capture, recall, and
  promotion — not at rest").
