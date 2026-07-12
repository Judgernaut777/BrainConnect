"""Request/result shapes for OKF export (Stage 1).

`ExportRequest` is what a caller asks for; `ExportResult` is the audit-safe record
of what was written. Neither ever carries matched secret text — the `withheld` and
`redacted` records name claim ids and policy reasons only, mirroring the safety
contract used by recall (LEDGER_SPEC.md §14.2).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..scopes import Scope


@dataclass
class ExportRequest:
    #: Directory to (atomically) create/replace with the bundle.
    output_dir: str
    #: Scope filter. Empty means *no scope filter* — a global export of every
    #: scope. When non-empty, a claim is included iff it is `global` or its scope
    #: is among these (the same rule recall uses, so global facts stay visible).
    scopes: list[Scope] = field(default_factory=list)
    #: Only export claims that are `trusted` (promoted AND not party to an open
    #: contradiction). OKF-valid never implies trusted; this narrows what is
    #: projected, it does not change what the projection *means*.
    trusted_only: bool = False
    #: Also export superseded claims and emit `history/log.md`. Off by default:
    #: the bundle is a current-facts projection.
    include_superseded: bool = False


@dataclass
class ExportResult:
    output_dir: str
    format_name: str
    okf_version: str
    claim_count: int
    source_count: int
    files: list[str] = field(default_factory=list)
    #: Claims whose body was WITHHELD by safety policy (id + reason, never text).
    withheld: list[dict] = field(default_factory=list)
    #: Ids of claims whose body was MASKED (secret/PII redaction) on the way out.
    redacted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: sha256 over the sorted (relpath, content) pairs of the bundle. Identical
    #: ledger state + identical request -> identical digest.
    bundle_digest: str = ""

    def as_dict(self) -> dict:
        return asdict(self)
