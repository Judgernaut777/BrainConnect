# 0009 — Two HTTP-surfaced trust-boundary gaps: caller-declared reviewer type, and federation's foreign trust bit

Status: **Documented, no enforcement change** (2026-07-17). Findings only — this
ADR records two places where "only a human/librarian promotes" and "only
BrainConnect (BC) promotion confers trust" are weaker than the code's own
docstrings claim, once the caller is across an HTTP boundary instead of local
Python/CLI context. It recommends options; it decides nothing about
implementation, and changes no behavior.

## Context

BC's central invariant, stated repeatedly across LEDGER_SPEC and this repo's
ADRs, is "agents may never promote their own memories" — promotion is
human-gated, structurally, not by convention. Two places where BC accepts
input from *outside* its own process boundary weaken that structural claim
without breaking it outright. Both are pre-existing, both are already
partially defended (see "What already holds", below), and neither is an
active exploit in the current deployment (loopback-only default, bearer
token available) — but both deserve a name and an owner decision.

## Finding A — HTTP promotion's human gate reduces to bearer-token possession

`candidates.promote` (`cli/brainconnect/candidates.py:308-330`) refuses to
promote unless `reviewer_type in REVIEWER_TYPES` (`("human", "librarian")`,
line 35), raising `ReviewerNotPermitted` otherwise (line 324-327). Read in
isolation this looks like a structural gate: an agent cannot promote its own
candidate because the function itself checks the actor type.

That gate is genuinely structural on the **CLI/Python path**: there,
`reviewer_type` is set by trusted local context (which binary/entry point is
calling, not attacker-controlled input) — the CLI only ever calls `promote`
with `"human"`, MCP tools only reachable in `--review` mode supply the
reviewer type, etc.

It is **not** structural on the **HTTP path**. `cli/brainconnect/server.py`
`_promote` (~lines 109-135) takes `reviewer_type` straight out of the request
body (`_PROMOTE_FIELDS` includes `"reviewer_type"`, line 47; used at line
131: `reviewer_type=str(payload.get("reviewer_type", "human"))`) and passes
it through to `api.promote` unchanged. There is no check anywhere in the HTTP
stack that the caller sending `"reviewer_type": "human"` (or `"librarian"`)
*is* a human, a librarian process, or anything other than the same agent
whose candidate is being promoted. Any HTTP caller in possession of a valid
bearer token (or, with `BRAINCONNECT_TOKEN` unset, any caller at all — the
module docstring is explicit that tokenless serve is "UNAUTHENTICATED...
EVERY non-health route is open to any caller that can reach the socket") can
self-promote by declaring itself human.

So, over HTTP, "human-only promotion" reduces to **bearer-token possession**,
not actor identity — and with no token configured, it reduces to **nothing**:
network reachability alone is sufficient to promote a candidate as if a human
had reviewed it.

**What already holds (not undermined by this finding):**
- The safety_override/override_reason fields are explicitly refused over HTTP
  (`_promote`, same function) — that specific escalation is genuinely
  CLI-only.
- Confidence is still required and not guessable/defaulted.
- The finding is about *actor-type provenance*, not about safety policy or
  confidence banding, both of which remain enforced as designed.

**Recommendation (not implemented here):**
1. **Preferred — stop accepting a caller-declared `reviewer_type` over HTTP
   at all.** Derive reviewer identity from an authenticated *operator*
   credential that is distinct from whatever token gates capture/recall
   (e.g. a separate `BRAINCONNECT_OPERATOR_TOKEN`, or a scope/claim on a
   richer credential) so that possessing the ordinary agent-facing bearer
   token is insufficient to promote. The server would set `reviewer_type`
   itself based on which credential authenticated the request, never trust
   the body's claim.
2. **Alternative — require an out-of-band human confirmation step**, e.g.
   promotion becomes two-phase over HTTP (request recorded, a separate
   human-operated confirmation surface — CLI, or a distinct authenticated
   endpoint — completes it), so no single HTTP request can promote alone.
3. **Fail-closed default:** whichever option is chosen, the safer default is
   to *refuse* promotion over HTTP entirely unless an operator-scoped
   credential is presented (a `forbidden`, not a silent `"human"` default),
   rather than the current implicit `payload.get("reviewer_type", "human")`
   default which fails *open* to "human" when the field is simply omitted.
4. Any of the above should be paired with an acceptance-suite case proving
   an ordinary agent-facing token cannot promote, mirroring the existing
   safety-override HTTP-refusal test.

## Finding B — Federation surfaces a foreign system's self-assertion as `trusted`, bypassing `trust.is_trusted`

`cli/brainconnect/trust.py:is_trusted(*, status, contradicted)` is BC's own
authority function for whether a claim is trusted — it looks at BC's ledger
`status` (was this promoted by a human/librarian?) and whether the claim is
contradicted. Every native BC claim's trust is computed by this function.

`cli/brainconnect/federation.py` deliberately does not call it. The module
sets a synthetic `FEDERATED_STATUS = "federated"` specifically so that "it
can never be confused with a promoted/pending/superseded claim, and
`trust.is_trusted` is bypassed entirely (trust is set explicitly, below)"
(module docstring, ~lines 34-38, verbatim comment). Trust for a federated
item is instead derived in `normalize()` (~lines 104-224): `eligible =
_get(item, "instruction_eligible") is True`, and `trusted = eligible and
trust == "trusted"` (boolean-strict, fail-closed on disagreement) — i.e. a
federated Decima item is surfaced as BC-`trusted` purely because **the
foreign system says so**, via a boolean it computed under its own rules,
never having passed through BC's human-promotion gate at all.

The design is fail-closed in every way it claims to be: malformed items are
skipped, text is bounded and still runs through BC's own safety pass on
recall, a truthy-but-non-`True` eligibility value is rejected, and
`instruction_eligible` disagreeing with a supplied `trust` label fails to
`untrusted`. None of that is in question. What is in question is the
**boundary being extended**: BC's promise "only a human/librarian promotion
confers trust" now also silently includes "...or a foreign system's own
self-certified `instruction_eligible` bit, if federation is configured."
That is a materially different trust root, and nothing in a recall pack
currently distinguishes "trusted because BC's ledger says a human promoted
this" from "trusted because Decima says so."

**Recommendation (not implemented here):**
1. **Treat federated trust as a distinct, lower tier** rather than
   equal to locally-promoted `trusted` — e.g. a `federated-trusted` (or
   `trust: "trusted", origin: "federated"`) label that is never conflated
   with BC's own `trusted` in code that makes trust-gated decisions
   (`trusted_only` filtering, safety promotion checks, etc.), even though it
   may still read as usable/citable content.
2. **Require the federating source to be allow-listed/authenticated** — i.e.
   `DecimaKnowledgeSource` should not be trusted as a bare Protocol
   implementation; the operator should have to explicitly configure which
   peer(s) are federation sources (already somewhat true — "no source
   configured... contributes nothing" — but there is no authentication of
   *which* source, only whether one is wired at all).
3. **Surface provenance to the human** — every federated item's `provenance`
   field (already collected, `MAX_PROVENANCE`, `_provenance()`) should be
   presented distinctly enough in recall packs and any promotion/review UI
   that a human can immediately see "this trust claim came from federation,
   not from BC's own promotion history," rather than it reading identically
   to a native promoted claim.
4. **Trade-off to weigh:** federation's entire value is surfacing another
   system's knowledge *without* re-running BC's full human-promotion
   workflow on every item (that would defeat the point of federating at
   all — Decima's projections change live and re-promoting per item is
   unworkable). Any of the above narrows the trust-boundary extension but
   also narrows federation's usefulness/transparency-of-cost; a lower tier
   or provenance surfacing costs little, a full allow-list/authentication
   requirement costs the most operationally but closes the boundary the
   most.

## Decision

**Documented, no enforcement change in this pass; owner to decide between
the options above.** No code in `server.py`, `candidates.py`, or
`federation.py` is modified by this ADR. Both findings remain live risks in
the current deployment, mitigated today only by (a) HTTP's default
loopback-only bind plus optional bearer token, and (b) federation's several
independent fail-closed defenses on parsing/eligibility/safety — neither of
which structurally closes the gap this ADR names.
