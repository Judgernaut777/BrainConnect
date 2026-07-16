"""Lane 5 — Decima knowledge FEDERATION (ADR 0008; LEDGER_SPEC §8bis).

BrainConnect (BC) owns the *unified knowledge abstraction* (ADR 0008 §BC OWNS 2).
Lane 5's job is to surface Decima's own knowledge — read through Decima's published
Lane-2 read-contract (``decima.read_contract``, ``READ_CONTRACT_VERSION`` 0.1) —
inside a BC recall pack, **without forking it**. "Federate, do not fork": Decima
stays the authority for its own content; BC never copies a Decima item into its
ledger and never creates a second knowledge store. The item is surfaced at READ
TIME and vanishes the moment Decima retracts it.

Why this is a SIBLING seam, not a §8 ``RetrievalBackend``
--------------------------------------------------------
The §8 seam is content-free BY DESIGN: a backend returns only
``BackendCandidate(kind, id: int, score, rank)`` — "an id and a ranking signal,
never claim content or status" (``backends/base.py``). ``recall.recall`` then
RE-READS every authoritative field by integer id from BC's OWN ``claims`` table
and DROPS any id it does not hold. That invariant is exactly what makes BC's trust
boundary structural for BC's own ledger — and exactly what makes the seam unusable
for a FOREIGN store whose content BC deliberately does not hold: a Decima id is a
foreign string with no matching ``claims`` row, so every candidate would be
dropped at re-read and the pack would silently gain nothing.

So Decima — which, like BC, re-reads its own content every call
(``KnowledgeProjection.items()`` recomputes ``trust``/``instruction_eligible`` from
live Cells) — is the authority that resolves its OWN items. This module returns
FULLY-RESOLVED :class:`~brainconnect.recall.RecallItem`s that ``recall`` merges
AFTER its native pass. :class:`DecimaKnowledgeBackend` still satisfies the §8
``RetrievalBackend`` Protocol shape (so it is a first-class backend object), but
its content-bearing work happens through :meth:`DecimaKnowledgeBackend.federate`,
not through the content-free ``search``. See LEDGER_SPEC §8bis.

The trust mapping (the core semantics)
--------------------------------------
A Decima item carries ``instruction_eligible: bool``; its ``trust`` is
``"trusted"`` iff that bit is true (Decima invariant 5, contract-pinned). BC honors
it EXACTLY as BC honors its own ``trusted`` bit: a federated item is surfaced
``trusted`` **only** when ``instruction_eligible`` is truly ``True`` (a real
boolean, and ``trust == "trusted"`` — fail-closed on any disagreement). Everything
else is DATA, never an instruction — never surfaced ``trusted``. Untrusted
federated material is opt-in exactly like BC's own untrusted material: it appears
only when the caller drops ``trusted_only`` (mirroring ``include_pending``).

NOTE (docs/adr/0009-http-trust-boundary-honor-system.md, Finding B): this
extends BC's trust boundary to a foreign system's own self-assertion — a
federated item is surfaced ``trusted`` because Decima's own
``instruction_eligible`` bit says so, without passing BC's human-promotion
gate. Fail-closed on parsing/eligibility and still safety-scanned on recall,
but not the same trust root as a locally-promoted claim. Documented, not
enforced further, in this pass.

Safety, boundedness, non-fatality
---------------------------------
Foreign provenance is untrusted input. Every federated item's text runs through
the SAME read-door safety pass BC runs over its own claims (masking / withholding /
quarantine-on-scanner-failure), so a poisoned or oversized Decima item cannot
inject or exfiltrate. Malformed/hostile source data is normalized defensively
(bounded text, boolean-strict eligibility, capped counts) and bad items are
skipped, never raised. Federation is OPTIONAL and NON-FATAL end to end: no source
configured, or a source that errors, contributes nothing and BC retrieval is
unaffected. ``decima`` is NEVER a required dependency of BC.

Zero model calls. Reading a projection is not a model call; no provider, no key.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

_log = logging.getLogger("brainconnect.federation")

#: Stable federation source label (provenance / synthetic scope / warnings).
DECIMA = "decima"

#: The synthetic scope TYPE a federated item carries (``external:decima``). It is a
#: FOREIGN scope: registered in ``scopes.SCOPE_TYPES`` only so a caller can construct
#: and request it, but it names knowledge BC does not own (never a BC ledger claim). A
#: scoped recall must ask for it explicitly to admit foreign knowledge (see
#: ``recall._external_scope_requested``); otherwise federation is fail-closed to the
#: caller's scope.
EXTERNAL_SCOPE_TYPE = "external"

#: The Decima read-contract version BC is written against. The conformance pin
#: (tests) asserts this equals Decima's real ``READ_CONTRACT_VERSION`` when Decima
#: is importable, so a contract bump cannot drift silently.
EXPECTED_READ_CONTRACT_VERSION = "0.1"

#: The Decima ``KnowledgeItem`` field set BC federates over. The conformance pin
#: asserts this equals Decima's real dataclass fields — a removed/renamed field is
#: caught, not silently ignored.
KNOWLEDGE_FIELDS = (
    "id", "type", "text", "instruction_eligible", "trust", "links", "provenance",
)

# --- defensive bounds: a hostile/oversized source can never crash or flood -----
#: Max Decima items scanned per call (a runaway source cannot pin the read door).
MAX_SCAN = 10_000
#: Max chars of a federated item's text kept before the safety pass (a huge blob
#: is truncated, never buffered whole into a pack).
MAX_TEXT = 8_000
#: Max links / provenance pointers surfaced per item.
MAX_LINKS = 64
MAX_PROVENANCE = 64
#: Max chars of any single scalar (id/type/pointer) copied out of a source item.
MAX_SCALAR = 256

#: Synthetic status for a federated item. NOT one of BC's ledger statuses, so it
#: can never be confused with a promoted/pending/superseded claim, and
#: ``trust.is_trusted`` is bypassed entirely (trust is set explicitly, below).
FEDERATED_STATUS = "federated"
#: Neutral confidence label (not a BC confidence band).
FEDERATED_CONFIDENCE = "federated"


# ---------------------------------------------------------------------------
# The injectable source contract (matches Decima's Lane-2 read-contract shape).
# ---------------------------------------------------------------------------
@runtime_checkable
class DecimaKnowledgeSource(Protocol):
    """A read-only source of Decima knowledge items.

    ``knowledge()`` returns an iterable of items, each exposing the read-contract
    knowledge shape ``id / type / text / instruction_eligible / trust / links /
    provenance`` — as attributes (a Decima ``KnowledgeItem``) or as a mapping.
    Decima's own ``decima.read_contract.ReadModels`` satisfies this by duck-typing;
    :class:`StubDecimaKnowledgeSource` satisfies it for tests. Implementations MUST
    NOT mutate anything: this is a read seam.
    """

    def knowledge(self) -> Iterable[Any]: ...


@dataclass(frozen=True)
class FederatedItem:
    """One normalized, bounded Decima knowledge item — the safe internal shape.

    Produced by :func:`normalize` from arbitrary (possibly hostile) source data,
    so downstream code never touches a raw foreign object.
    """
    id: str
    type: str
    text: str
    instruction_eligible: bool
    trust: str
    links: tuple[dict, ...] = field(default_factory=tuple)
    provenance: tuple[str, ...] = field(default_factory=tuple)


class StubDecimaKnowledgeSource:
    """An in-memory :class:`DecimaKnowledgeSource` for tests and offline use.

    Constructed with a list of item dicts (or objects); ``knowledge()`` returns
    them verbatim. Pass ``raises=True`` to simulate an erroring source (the
    federation must treat it as "contributes nothing", never crash)."""

    def __init__(self, items: Iterable[Any] | None = None, *, raises: bool = False):
        self._items = list(items or [])
        self._raises = raises

    def knowledge(self) -> Iterable[Any]:
        if self._raises:
            raise RuntimeError("decima source is unavailable (simulated)")
        return list(self._items)


# ---------------------------------------------------------------------------
# Normalization — the hostile-data boundary.
# ---------------------------------------------------------------------------
def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _scalar(v: Any, *, bound: int = MAX_SCALAR) -> str:
    """Coerce to a bounded ``str``. Never raises; a non-string becomes its repr."""
    try:
        s = v if isinstance(v, str) else str(v)
    except Exception:  # noqa: BLE001 — a pathological __str__ must not escape.
        return ""
    return s[:bound]


def _links(v: Any) -> tuple[dict, ...]:
    """Keep at most ``MAX_LINKS`` well-shaped ``{"rel","dst"}`` links. Anything
    unexpected is dropped, not raised."""
    out: list[dict] = []
    if not isinstance(v, (list, tuple)):
        return ()
    for link in v:
        if len(out) >= MAX_LINKS:
            break
        if isinstance(link, dict):
            out.append({"rel": _scalar(link.get("rel")), "dst": _scalar(link.get("dst"))})
    return tuple(out)


def _provenance(v: Any) -> tuple[str, ...]:
    if not isinstance(v, (list, tuple)):
        return ()
    return tuple(_scalar(p) for p in list(v)[:MAX_PROVENANCE])


def normalize(item: Any) -> Optional[FederatedItem]:
    """Turn one raw source item into a bounded :class:`FederatedItem`, or ``None``
    if it is unusable (no id, or normalization itself failed). NEVER raises.

    Eligibility is boolean-STRICT: ``instruction_eligible`` grants trust only when
    it is a real ``True`` (a hostile ``"yes"`` / ``1`` / truthy object does NOT),
    and only when ``trust`` also reads ``"trusted"`` — any disagreement fails
    closed to DATA. This is the structural version of "untrusted text is data".
    """
    try:
        ident = _scalar(_get(item, "id"))
        if not ident:
            return None
        raw_text = _get(item, "text")
        text = raw_text[:MAX_TEXT] if isinstance(raw_text, str) else _scalar(raw_text, bound=MAX_TEXT)
        eligible = _get(item, "instruction_eligible") is True
        # Derive trust from eligibility only when the item OMITS a trust label.
        # `_scalar(None)` returns the truthy string "None", so a bare `or` fallback
        # would never fire (dead branch) and a trust-omitted item could never derive
        # its trust — guard on presence explicitly. Still fail-closed: the derived
        # label is "trusted" only when the eligibility bit is a real ``True``.
        raw_trust = _get(item, "trust")
        trust = _scalar(raw_trust) if raw_trust is not None \
            else ("trusted" if eligible else "untrusted")
        # Fail-closed: trusted iff the boolean is true AND the derived label agrees.
        trusted = eligible and trust == "trusted"
        return FederatedItem(
            id=ident,
            type=_scalar(_get(item, "type")),
            text=text,
            instruction_eligible=trusted,
            trust="trusted" if trusted else "untrusted",
            links=_links(_get(item, "links")),
            provenance=_provenance(_get(item, "provenance")),
        )
    except Exception as e:  # noqa: BLE001 — one bad item never fails the batch.
        _log.warning("federation: skipped a malformed Decima item (%s)", type(e).__name__)
        return None


# ---------------------------------------------------------------------------
# Deterministic query matching (pure, model-free).
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _score(query_tokens: set[str], item: FederatedItem) -> int:
    """Distinct query tokens present in the item's text/type. Pure token overlap —
    deterministic, no ranking model. Zero means "no match" (excluded)."""
    if not query_tokens:
        return 0
    hay = _tokens(item.text) | _tokens(item.type)
    return len(query_tokens & hay)


# ---------------------------------------------------------------------------
# The backend — §8 Protocol shape + the content-bearing federate() sibling seam.
# ---------------------------------------------------------------------------
class DecimaKnowledgeBackend:
    """Federates Decima knowledge into BC recall at read time.

    Satisfies the §8 :class:`~brainconnect.backends.base.RetrievalBackend` Protocol
    (``backend_name`` + indexing/search/health), so it is a first-class backend
    object — but its ``search`` is content-free by the §8 contract and nominates NO
    BC ledger ids (Decima ids are foreign; BC holds none of them). The real work is
    :meth:`federate`, which returns FULLY-RESOLVED items ``recall`` merges after its
    native pass. Indexing is a no-op: BC never copies Decima content (federate, do
    not fork).
    """

    #: Distinct name so it can never be mistaken for a BC ledger backend.
    NAME = "decima_federation"

    def __init__(self, source: DecimaKnowledgeSource | None):
        self.source = source

    # --- §8 RetrievalBackend Protocol shape ----------------------------------
    @property
    def backend_name(self) -> str:
        return self.NAME

    def index_source(self, source_id: int) -> None:
        """No-op: federation never ingests Decima content into BC (do not fork)."""

    def index_claim(self, claim_id: int) -> None:
        """No-op: BC claims are Decima's concern to know nothing about."""

    def delete_or_deindex(self, entity_id: str) -> None:
        """No-op: nothing is stored, so nothing is deleted."""

    def search(self, request):  # type: ignore[no-untyped-def]
        """Content-free by the §8 contract: this backend nominates NO BC claim ids
        (it holds none), so it returns an empty candidate set. Federation content
        flows through :meth:`federate`, merged after native recall — see the module
        docstring on why the content-free seam structurally cannot carry it."""
        from .backends.base import BackendSearchResult
        return BackendSearchResult(backend=self.NAME, candidates=[], mode="federation")

    def health(self) -> dict:
        return {
            "backend": self.NAME,
            "ok": True,
            "configured": self.source is not None,
            "note": ("federates Decima knowledge at read time via the Lane-2 "
                     "read-contract; never forks it into the BC ledger"),
        }

    # --- the content-bearing sibling seam ------------------------------------
    def federate(self, query: str, *, limit: int) -> list["RecallItem"]:
        """Return deterministically-ranked, fully-resolved federated items for
        ``query``. NEVER raises: a missing/erroring source yields ``[]`` (the
        non-fatal contract). Items are normalized (hostile data bounded), scored by
        pure token overlap, and ordered by ``(-score, id)`` for a stable pack. They
        are NOT yet safety-scanned — ``recall`` runs the read-door safety pass over
        them before returning (foreign provenance is untrusted input)."""
        from .recall import RecallItem  # local import: avoid an import cycle

        if self.source is None or limit <= 0:
            return []
        try:
            raw = self.source.knowledge()
        except Exception as e:  # noqa: BLE001 — a source error is NON-FATAL.
            _log.warning("federation: Decima source errored, contributing nothing (%s)",
                         type(e).__name__)
            return []

        qtokens = _tokens(query or "")
        scored: list[tuple[int, str, FederatedItem]] = []
        try:
            for i, raw_item in enumerate(raw):
                if i >= MAX_SCAN:
                    _log.warning("federation: Decima source exceeded MAX_SCAN=%d; "
                                 "remaining items ignored", MAX_SCAN)
                    break
                item = normalize(raw_item)
                if item is None:
                    continue
                s = _score(qtokens, item)
                if s > 0:
                    scored.append((s, item.id, item))
        except Exception as e:  # noqa: BLE001 — a source that errors mid-iteration.
            _log.warning("federation: Decima source errored mid-scan, using what was "
                         "read so far (%s)", type(e).__name__)

        # Deterministic: strongest match first, ties broken by stable Decima id.
        scored.sort(key=lambda t: (-t[0], t[1]))

        out: list[RecallItem] = []
        for _s, _id, item in scored[:limit]:
            out.append(RecallItem(
                id=f"{DECIMA}:{item.id}",
                text=item.text,
                status=FEDERATED_STATUS,
                confidence=FEDERATED_CONFIDENCE,
                scope={"type": EXTERNAL_SCOPE_TYPE, "id": DECIMA},
                validity="current",
                trusted=item.instruction_eligible,  # normalize() made this fail-closed
                tags=[f"federated:{DECIMA}"] + ([f"type:{item.type}"] if item.type else []),
                sources=[{"id": p, "origin": DECIMA} for p in item.provenance],
            ))
        return out


# ---------------------------------------------------------------------------
# Optional real adapter — guarded import of decima.read_contract via DECIMA_SRC.
# ---------------------------------------------------------------------------
class RealDecimaKnowledgeSource:
    """A :class:`DecimaKnowledgeSource` backed by Decima's real Lane-2 read-contract.

    Holds an already-built ``decima.read_contract.ReadModels`` facade (a read-only
    view over an opened Weft) and returns ``read_models.knowledge()``. Constructing
    the facade / opening the Weft is Decima's concern; this class is a read-only
    consumer that appends nothing (Lane-2 hard boundary). Prefer :func:`build_source_from_env`
    to construct it — that path is fully guarded and non-fatal.
    """

    def __init__(self, read_models: Any):
        self._read_models = read_models

    def knowledge(self) -> Iterable[Any]:
        # ReadModels.knowledge() re-reads live Cells every call; keep it current if
        # the facade supports an incremental refresh (best-effort, non-fatal).
        refresh = getattr(self._read_models, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001 — a refresh failure must not crash reads.
                pass
        return self._read_models.knowledge()


def build_source_from_env(env: Optional[dict] = None) -> Optional[DecimaKnowledgeSource]:
    """Build a real Decima source from the environment, or ``None``.

    Reads (mirroring Lane-8's ``AGENTCONNECT_CORE_SRC`` pattern):

    * ``DECIMA_SRC``   — path to Decima's source root (put on ``sys.path`` so
      ``decima.read_contract`` imports). If unset/absent, federation is OFF.
    * ``DECIMA_WEFT``  — path to the Decima Weft db to read. Without it there is no
      content to federate, so the source is OFF (import-only presence is not enough).
    * ``DECIMA_KEYRING`` — optional master-seed path for signature verification;
      absent means Decima's keyring-free integrity mode.

    EVERY failure — unset var, unimportable module, unopenable Weft, contract-version
    mismatch — resolves to ``None`` (federation disabled), never an exception. Decima
    is optional and must never break BC startup or retrieval.
    """
    env = os.environ if env is None else env
    src = (env.get("DECIMA_SRC") or "").strip()
    if not src:
        return None
    try:
        if Path(src).is_dir() and src not in sys.path:
            sys.path.insert(0, src)
        import decima.read_contract as rc  # type: ignore
    except Exception as e:  # noqa: BLE001 — Decima absent is not an error.
        _log.warning("federation: DECIMA_SRC set but decima.read_contract not "
                     "importable; federation disabled (%s)", type(e).__name__)
        return None

    # Contract-version guard: refuse to federate over an incompatible major.
    version = getattr(rc, "READ_CONTRACT_VERSION", None)
    if not _version_compatible(version):
        _log.warning("federation: Decima READ_CONTRACT_VERSION=%r is incompatible "
                     "with expected %r; federation disabled", version,
                     EXPECTED_READ_CONTRACT_VERSION)
        return None

    weft_path = (env.get("DECIMA_WEFT") or "").strip()
    if not weft_path:
        _log.info("federation: DECIMA_SRC importable but DECIMA_WEFT unset; nothing "
                  "to federate (disabled)")
        return None
    try:
        keyring = _load_keyring(env.get("DECIMA_KEYRING"))
        from decima.kernel.weft import Weft  # type: ignore
        weft = Weft(weft_path, keyring)
        read_models = rc.open_read_models(weft)
        return RealDecimaKnowledgeSource(read_models)
    except Exception as e:  # noqa: BLE001 — opening the Weft is best-effort.
        _log.warning("federation: could not open Decima Weft at %s; federation "
                     "disabled (%s)", weft_path, type(e).__name__)
        return None


def _load_keyring(seed_path: Optional[str]):
    """Load a Decima verifying keyring from a master-seed file, or ``None`` for the
    keyring-free integrity mode. Never raises."""
    if not seed_path:
        return None
    try:
        from decima.kernel.crypto import Keyring  # type: ignore
        with open(seed_path, "rb") as fh:
            return Keyring(seed=fh.read())
    except Exception as e:  # noqa: BLE001
        _log.warning("federation: could not load DECIMA_KEYRING (%s); using "
                     "keyring-free mode", type(e).__name__)
        return None


def _version_compatible(version: Any) -> bool:
    """Same MAJOR as ``EXPECTED_READ_CONTRACT_VERSION`` (semver: additive minors are
    compatible; a major bump is a breaking change BC must not silently ride)."""
    if not isinstance(version, str):
        return False
    try:
        return version.split(".")[0] == EXPECTED_READ_CONTRACT_VERSION.split(".")[0]
    except Exception:  # noqa: BLE001
        return False


#: env vars that fully determine the env-built federation source. The default
#: backend is memoized on their values so the Decima Weft is opened ONCE per
#: process per config — not reopened on every omitted-``federation`` recall (which
#: would leak Weft handles). A config change (any of these vars) re-resolves.
_ENV_KEYS = ("DECIMA_SRC", "DECIMA_WEFT", "DECIMA_KEYRING")
_backend_cache: dict[tuple, Optional["DecimaKnowledgeBackend"]] = {}


def _env_key(env: dict) -> tuple:
    return tuple((env.get(k) or "").strip() for k in _ENV_KEYS)


def reset_default_backend_cache() -> None:
    """Drop the memoized env-built backend (test/reload hook). Non-fatal."""
    _backend_cache.clear()


def default_backend(env: Optional[dict] = None) -> Optional[DecimaKnowledgeBackend]:
    """The env-configured federation backend, or ``None`` when federation is off.

    Never raises: any failure resolving a source disables federation. MEMOIZED on
    the federation env config (:data:`_ENV_KEYS`) so the Decima Weft is opened once
    per process per config — recall with the ``federation`` arg omitted no longer
    reopens the Weft (nor leaks its handle) on every call. A config change
    re-resolves; :func:`reset_default_backend_cache` clears the memo."""
    env = os.environ if env is None else env
    key = _env_key(env)
    if key in _backend_cache:
        return _backend_cache[key]
    try:
        source = build_source_from_env(env)
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders non-fatal boundary.
        _log.warning("federation: source resolution failed; disabled (%s)",
                     type(e).__name__)
        source = None
    backend = DecimaKnowledgeBackend(source) if source is not None else None
    _backend_cache[key] = backend
    return backend


# Re-exported for type hints in docstrings without a hard import cycle.
if False:  # pragma: no cover — typing only
    from .recall import RecallItem
