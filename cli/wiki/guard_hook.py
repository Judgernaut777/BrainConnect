"""fascia-guard integration for the brain's write/read doors (memory-poisoning
and secret-leak defense).

Respects this module's invariants:
  * **Soft dependency.** If fascia-guard is not installed, every function is a
    no-op — the offline acceptance harness (no fascia-guard, no `mcp` SDK) imports
    and runs the pure handlers unchanged.
  * **Dormant by default.** Scanning runs only when FASCIA_GUARD (or _ENFORCE) is
    set, so default behavior and the gate are untouched.
  * **Zero model calls / offline.** fascia-guard is deterministic and makes no
    network calls (it scans in a no-network context; secret verification is
    disabled). Importing it pulls no heavy deps — model backends load lazily and
    fall back to a pure-regex heuristic.

Enforcement (FASCIA_GUARD_ENFORCE=1):
  * capture — refuse to store content that carries secret/credential material
    (the write door already instructs "Do not capture secrets"; this enforces it).
  * recall — advisory: annotate the context pack when recalled material trips the
    guard (e.g. an injection payload stored as a pending claim), so the client
    model is warned. Non-destructive.
"""
from __future__ import annotations

import os

try:  # soft dependency
    from fascia_guard.integrations.wikibrain import (
        guard_before_store,
        guard_on_recall,
        has_secret,
        redact_secret_spans,
    )
    _AVAILABLE = True
except Exception:  # pragma: no cover - only when the package is absent
    _AVAILABLE = False


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def available() -> bool:
    return _AVAILABLE


def active() -> bool:
    return _AVAILABLE and (_flag("FASCIA_GUARD") or _flag("FASCIA_GUARD_ENFORCE"))


def enforcing() -> bool:
    return _AVAILABLE and _flag("FASCIA_GUARD_ENFORCE")


def check_capture(text: str):
    """Verdict for a would-be capture, or None if inactive."""
    if not active():
        return None
    return guard_before_store(text, source_id="capture")


def check_recall(text: str):
    """Verdict for assembled recall text, or None if inactive."""
    if not active():
        return None
    return guard_on_recall(text, source_id="recall")


def carries_secret(verdict) -> bool:
    return _AVAILABLE and verdict is not None and has_secret(verdict)


def redact_secrets(text: str) -> str:
    """Mask secret spans in recalled text when enforcing; identity otherwise.

    Secrets must never be returned from memory. Applied per-claim at recall so a
    credential stored before the guard existed (or by another writer) is masked on
    the way out. Cheap (builtin regex only) — safe for the recall hot path.
    """
    if not enforcing() or not text:
        return text
    return redact_secret_spans(text)


def categories(verdict) -> list[str]:
    if not (_AVAILABLE and verdict is not None):
        return []
    return sorted({f.category.value for f in verdict.findings})
