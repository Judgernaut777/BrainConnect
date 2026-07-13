# Knowledge federation — surfacing Decima knowledge (ADR 0008 Lane 5)

BrainConnect (BC) owns the *unified knowledge abstraction* (ADR 0008, "BC OWNS" 2).
Lane 5 makes BC surface **Decima's own knowledge** inside a recall pack — read
through Decima's published Lane-2 read-contract — **without forking it**. Decima
stays the authority for its content; BC never copies a Decima item into its ledger
and never stands up a second knowledge store. Governed by
[ADR 0008](adr/0008-orchestration-boundary.md) and
[LEDGER_SPEC §8bis](LEDGER_SPEC.md).

Code: `cli/brainconnect/federation.py`; merge point: `recall._federate`
(`cli/brainconnect/recall.py`).

## Why this is a sibling seam, not a §8 backend

The §8 `RetrievalBackend` seam is **content-free by design**: a backend returns
`BackendCandidate(kind, id: int, score, rank)` — "an id and a ranking signal, never
claim content or status" — and `recall.recall` RE-READS every authoritative field
by integer id from BC's own `claims` table, dropping any id it does not hold. That
invariant is what makes BC's trust boundary *structural* for BC's own ledger, and
it is exactly what makes the seam **unusable** for a foreign store BC deliberately
does not hold: a Decima id is a foreign string with no `claims` row, so every
candidate would be dropped at re-read — federating through §8 would silently return
nothing.

So Decima — which, like BC, re-reads its own content every call
(`KnowledgeProjection.items()` recomputes `trust`/`instruction_eligible` from live
Cells) — is the authority that resolves its own items. The federation returns
**fully-resolved** `RecallItem`s that `recall` merges **after** its native pass.
`DecimaKnowledgeBackend` still satisfies the §8 Protocol shape (it *is* a backend
object), but its content-bearing work is the sibling `federate()` method; its
`search()` is content-free per §8 and nominates no ledger ids. It is intentionally
**not** in the §8 `_BUILDERS` registry (that registry resolves the single BC-ledger
search backend).

## The trust mapping — `instruction_eligible` honored as `trusted`

A Decima item carries `instruction_eligible: bool`; its `trust` is `"trusted"` iff
that bit is true (Decima invariant 5, contract-pinned). BC honors it **exactly** as
it honors its own `trusted` bit:

- A federated item is surfaced `trusted` **only** when `instruction_eligible` is a
  real boolean `True` **and** the derived `trust` reads `"trusted"`. Any
  disagreement (a hostile truthy `"yes"`, a missing/False bit) **fails closed** to
  untrusted DATA. Boolean-strict eligibility is the structural version of "untrusted
  text is data, never instructions".
- Untrusted federated material is **opt-in**, mirroring BC's own untrusted material:
  with the default `trusted_only=True` only trusted federated items appear; drop
  `trusted_only` to see untrusted federated DATA (always labeled `trusted: false`).
- A synthetic `status: "federated"` (not a BC ledger status) and `confidence:
  "federated"` label it, and `trust.is_trusted` is bypassed — trust is set solely
  from Decima's bit. Provenance (Weft event ids) surfaces in the item's `sources`.

## Safety, boundedness, non-fatality

- **Same read door.** Foreign provenance is untrusted input, so every federated
  item's text runs through the identical read-door safety pass BC runs over its own
  claims (`recall._safety`): a secret is masked, a high-risk injection / tool-control
  item is withheld, and a scanner failure quarantines. A poisoned or oversized
  Decima item cannot inject or exfiltrate.
- **Hostile data is bounded.** Malformed items are normalized defensively — bounded
  text (`MAX_TEXT`), boolean-strict eligibility, capped links/provenance, at most
  `MAX_SCAN` items scanned — and a bad item is skipped, never raised.
- **Deterministic.** Matching is pure query-token overlap; ordering is
  `(-score, decima_id)`; native items come first, then federated. Two identical
  reads produce an identical pack.
- **Optional + non-fatal.** No source configured, or a source that errors,
  contributes nothing and leaves native recall untouched. `decima` is **never** a
  required dependency; there are zero model calls (reading a projection is not a
  model call).

## Wiring a real Decima source

The source is injectable via the `DecimaKnowledgeSource` Protocol (one method,
`knowledge()`, returning items with the read-contract shape `id / type / text /
instruction_eligible / trust / links / provenance`). Tests use
`StubDecimaKnowledgeSource`. The optional real adapter is env-configured
(mirroring Lane-8's `AGENTCONNECT_CORE_SRC`), all failures resolving to "disabled":

| env var | meaning |
|---|---|
| `DECIMA_SRC` | path to Decima's source root (put on `sys.path` so `decima.read_contract` imports). Unset ⇒ federation OFF. |
| `DECIMA_WEFT` | path to the Decima Weft db to read. Unset ⇒ nothing to federate ⇒ OFF. |
| `DECIMA_KEYRING` | optional master-seed for signature verification; absent ⇒ Decima's keyring-free integrity mode. |

`build_source_from_env()` guards the import of `decima.read_contract`, refuses an
incompatible `READ_CONTRACT_VERSION` major, opens the Weft read-only via
`open_read_models(weft)`, and returns a `RealDecimaKnowledgeSource` — or `None` on
any failure. BC is a **read-only consumer**: it appends nothing to Decima and never
touches Decima's execution/authorization internals (Lane-2 boundary).

## Conformance pin

When Decima is importable, an acceptance pin asserts BC's expected
`READ_CONTRACT_VERSION` (`federation.EXPECTED_READ_CONTRACT_VERSION`) and knowledge
field set (`federation.KNOWLEDGE_FIELDS`) match Decima's real
`read_contract.READ_CONTRACT_VERSION` and `KnowledgeItem` fields — so a contract
change is caught, not silently ridden. It skips cleanly when Decima is absent, the
same discipline as the Lane-4 privacy pin and the Lane-8 EventType pin.
