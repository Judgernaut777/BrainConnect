"""The narrow knowledge-format adapter seam, and the single OKF implementation.

This is deliberately **not** a plugin framework. It is one Protocol with one
implementation, isolating BrainConnect from a draft external format — not
speculative extensibility. A future stage adds validation and import; their
methods are declared here so the seam is stable, but Stage 1 implements only
export and leaves the rest to raise `NotImplementedError`.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import Repo
from .export import FORMAT_NAME, OKF_VERSION, export_bundle
from .model import ExportRequest, ExportResult


@runtime_checkable
class KnowledgeFormatAdapter(Protocol):
    """Convert between the canonical ledger and a portable knowledge format.

    A projection, never a second source of truth: exporting reads the ledger and
    writes files; importing (a later stage) creates PENDING candidates through the
    normal safety + human-promotion pipeline. Neither direction may confer trust.
    """

    @property
    def format_name(self) -> str: ...

    @property
    def format_version(self) -> str: ...

    def export_bundle(self, repo: Repo, request: ExportRequest) -> ExportResult:
        """Project the ledger into a bundle. Must not mutate the ledger."""
        ...

    def validate_bundle(self, path) -> object:
        """Structurally validate a bundle. (Stage 2.)"""
        ...

    def import_bundle(self, repo: Repo, path, **kw) -> object:
        """Import a bundle as PENDING candidates. (Stage 3.)"""
        ...


class OKFAdapter:
    """OKF (Open Knowledge Format) — Markdown bundle projection of the ledger."""

    @property
    def format_name(self) -> str:
        return FORMAT_NAME

    @property
    def format_version(self) -> str:
        return OKF_VERSION

    def export_bundle(self, repo: Repo, request: ExportRequest) -> ExportResult:
        return export_bundle(repo, request)

    def validate_bundle(self, path):
        raise NotImplementedError(
            "OKF bundle validation is Stage 2; this build ships the exporter only")

    def import_bundle(self, repo: Repo, path, **kw):
        raise NotImplementedError(
            "OKF import is Stage 3; this build ships the exporter only")
