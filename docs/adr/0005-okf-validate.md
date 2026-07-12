# ADR 0005 — OKF validation: structural only, and hardened against a hostile bundle

Status: accepted (2026-07-12, OKF Stage 2)
Scope: `cli/brainconnect/okf/validate.py` (new), `brainconnect okf validate` /
`brainconnect okf inspect` (new CLI), `OKFAdapter.validate_bundle`,
`cli/brainconnect/safety/baseline/secrets.py` (PEM marker rule), `docs/OKF.md`

## Context

Stage 1 exports an OKF bundle. Stage 3 will import one. Between them, a consumer
needs to answer a narrow question about an *untrusted* directory that arrived from
somewhere else: **is this a well-formed OKF bundle?** — before any of its content
is allowed near the ledger.

Two risks dominate. First, that "valid" silently grows to mean "trusted" or
"safe", so a well-formed bundle of hostile claims gets waved through. Second, that
the validator itself becomes an attack surface: a bundle that makes it follow a
symlink out of the tree, read `/etc/passwd` through a `../` link, exhaust memory on
a multi-gigabyte "document", blow the recursion stack, or spin forever on a
relationship cycle. A validator that is unsafe against the thing it validates is
worse than none.

## Decision

1. **Structural only. Validity is never trust or safety.** `validate_bundle`
   returns a `ValidationResult(ok, errors[], warnings[], …)` that deliberately
   carries **no** `trusted` or `safe` field. `ok == True` means well-formed and
   nothing else. The invariant is stated in code, in `docs/OKF.md`, and asserted in
   the acceptance suite (a valid result's dict contains neither `trusted` nor
   `safe`). Import (Stage 3) remains the only path by which content becomes PENDING
   candidates through the normal safety + human-promotion pipeline.

2. **Never claim `ok` on a malformed or unsafe-structure bundle; never partially
   import.** Any error makes the whole result invalid. There is no partial success:
   validation is a gate, not a repair step. The checks cover the handoff list —
   marker, supported version (reject unsupported major, warn on newer minor),
   required + well-formed frontmatter, malformed YAML, unique document identity /
   duplicate ids, relative-link validity, absolute-path and `../` traversal, unsafe
   filenames, unknown extension fields (warn + preserve), broken supersession /
   contradiction relationships, per-file and total-bundle size, symlink behavior,
   and encoding.

3. **The validator is hardened against a hostile bundle.** This is the load-bearing
   security decision:
   - **Symlinks are classified lexically and never followed.** `os.readlink` reads
     the link text; the target is resolved *lexically* against the bundle-relative
     parent (never through the filesystem). A target that escapes the root is a
     rejected error and is never opened.
   - **No unbounded read.** Every file's size comes from its stat entry first;
     anything over the per-file cap is flagged and skipped, and a running total
     bounds the whole bundle — oversize fails closed.
   - **No execute, no import of content.** Frontmatter is parsed by a small,
     bounded, stdlib subset parser (`_Frontmatter`) that only ever constructs plain
     `dict`/`list`/scalar values — no object construction, no `eval`, no YAML loader
     that can instantiate arbitrary types.
   - **Every path is classified against the real root.** Traversal and absolute
     links are detected lexically, so a malicious link can never make the validator
     touch the host.
   - **No hang.** The walk is depth- and count-bounded, YAML nesting is bounded, and
     cycle detection is an *iterative* finite-graph DFS — a long supersession chain
     cannot exhaust the recursion stack, and a cycle is reported, not spun on.

4. **A stdlib-only frontmatter parser, matching the exporter's stance.** A clean
   `pip install brainconnect-ai` carries no PyYAML, and a real YAML loader is both a
   dependency we do not want in the deterministic core and a construction surface we
   do not want pointed at hostile input. So validation ships its own bounded parser
   over exactly the shapes the exporter emits, and rejects anything else as
   `malformed_yaml`.

5. **Cycle detection excludes contradictions.** A contradiction is symmetric
   (A↔B) and would trivially register as a two-node cycle; only the supersession
   graph is expected to be acyclic, so cycles are detected there and reported as a
   **warning** (a cycle is anomalous but not, by itself, a structural error).

6. **Harden export redaction: port the PEM private-key MARKER rule.** Stage-1
   verification flagged that BrainConnect's baseline secret scanner would let a
   *bare* `-----BEGIN … PRIVATE KEY-----` (or `-----END …-----`) delimiter through
   when its base64 body was absent. The marker rule from AgentConnect's baseline
   scanner (same owner) is ported into `safety/baseline/secrets.py`: it detects any
   lone private-key delimiter (RSA / EC / OPENSSH / ENCRYPTED / generic / PGP, and
   the PGP `BLOCK` variant, both `BEGIN` and `END`) and redacts it, while remaining
   scoped to `PRIVATE KEY` so a `CERTIFICATE` or `PUBLIC KEY` delimiter is never
   false-flagged. Because export runs bodies through the recall safety surface, this
   strengthens what an exported bundle can leak. It overlaps the existing block rule
   on a whole key and is merged by the redactor. No contract fixture changed (none
   contained a private key); positive and negative regressions are in the gate.

## Consequences

- `OKFAdapter.validate_bundle` now returns a `ValidationResult` instead of raising
  `NotImplementedError`; the Stage-1 acceptance check that pinned the deferral was
  updated to pin the new behavior (a missing bundle is *reported* `not_found`, not
  raised). `import_bundle` is still deferred to Stage 3.
- `brainconnect okf validate DIR [--json]` and `brainconnect okf inspect DIR
  [--json]` exit non-zero on an invalid bundle, so the validator drops into a shell
  pipeline as a gate.
- A Stage-1 export validates clean — the export → validate round trip is asserted in
  the acceptance suite and demonstrated by `scripts/okf_validate_demo.py`, which
  also rejects a battery of hostile bundles (traversal, symlink escape, oversize,
  cycle, bad encoding, …) with specific structured errors and no escape or hang.
- Size and count caps are configurable via `ValidationLimits`; the defaults
  (2 MiB/file, 64 MiB/bundle, 10 000 files, depth 32) are policy, not a spec
  requirement, and an operator can raise them for an unusually large but legitimate
  bundle.
