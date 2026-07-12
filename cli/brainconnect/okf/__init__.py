"""OKF (Open Knowledge Format) support — Stage 1 (export) + Stage 2 (validate).

The ledger is canonical; an OKF bundle is a **portable projection** of it. This
package exports that projection and structurally validates a bundle. It never
mutates the ledger, and OKF-valid never implies trusted, promoted, or safe.
Validation is STRUCTURAL ONLY and is hostile-input safe (no symlink follow-out,
no unbounded read, no import/execute of bundle content). Import (Stage 3) is
declared on the adapter Protocol but not implemented here.

Public surface:

    from brainconnect.okf import OKFAdapter, ExportRequest
    result = OKFAdapter().export_bundle(repo, ExportRequest(output_dir="./knowledge"))
    verdict = OKFAdapter().validate_bundle("./knowledge")   # -> ValidationResult
"""
from __future__ import annotations

from .adapter import KnowledgeFormatAdapter, OKFAdapter
from .export import FORMAT_NAME, OKF_VERSION, ExportError, export_bundle
from .model import ExportRequest, ExportResult
from .validate import (ValidationIssue, ValidationLimits, ValidationResult,
                       validate_bundle)

__all__ = [
    "KnowledgeFormatAdapter", "OKFAdapter",
    "ExportRequest", "ExportResult", "ExportError",
    "export_bundle", "OKF_VERSION", "FORMAT_NAME",
    "validate_bundle", "ValidationResult", "ValidationIssue", "ValidationLimits",
]
