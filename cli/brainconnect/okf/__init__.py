"""OKF (Open Knowledge Format) support — Stage 1: the exporter.

The ledger is canonical; an OKF bundle is a **portable projection** of it. This
package exports that projection. It never mutates the ledger, and OKF-valid never
implies trusted, promoted, or safe. Validation (Stage 2) and import (Stage 3) are
declared on the adapter Protocol but not implemented here.

Public surface:

    from brainconnect.okf import OKFAdapter, ExportRequest
    result = OKFAdapter().export_bundle(repo, ExportRequest(output_dir="./knowledge"))
"""
from __future__ import annotations

from .adapter import KnowledgeFormatAdapter, OKFAdapter
from .export import FORMAT_NAME, OKF_VERSION, ExportError, export_bundle
from .model import ExportRequest, ExportResult

__all__ = [
    "KnowledgeFormatAdapter", "OKFAdapter",
    "ExportRequest", "ExportResult", "ExportError",
    "export_bundle", "OKF_VERSION", "FORMAT_NAME",
]
