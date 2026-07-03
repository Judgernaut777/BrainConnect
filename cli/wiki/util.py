"""Pure helpers: hashing, slugs, timestamps, FTS query building."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def now_iso() -> str:
    """UTC timestamp, second precision, ISO-8601 with Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_local_compact() -> str:
    """Local 'YYYY-MM-DD HH:MM' for the operations log header."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_slug_strip = re.compile(r"[^a-z0-9]+")


def slug(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _slug_strip.sub("-", text).strip("-")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip("-")
    return text or "untitled"


_word = re.compile(r"[A-Za-z0-9]+")

# Negation tokens used by the contradiction polarity heuristic. Apostrophe-free
# contraction forms are enumerated explicitly rather than matched by a "*nt"
# suffix, which mis-flagged ordinary words like "important", "different",
# "deployment", "count", "current", "environment". (The tokenizer splits on the
# apostrophe, so "isn't" becomes "isn"+"t" and is not detected either way — a
# known coarseness of this polarity check, not a regression.)
NEGATION_TOKENS = {
    "not", "no", "never", "cannot", "without", "fails", "failed",
    "unsupported", "incompatible", "false", "lacks", "missing",
    "none", "neither", "nor", "aint",
    # negative contractions, apostrophe-stripped
    "cant", "wont", "dont", "doesnt", "isnt", "arent", "wasnt", "werent",
    "didnt", "hasnt", "havent", "hadnt", "wouldnt", "couldnt", "shouldnt",
}


def tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _word.finditer(text)]


STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "for", "and", "or", "with", "as", "at", "by",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def fts_query(terms: str) -> str:
    """Build a safe FTS5 MATCH expression (AND) from free user input.

    Each alphanumeric token becomes a quoted term ANDed together. This avoids
    accidental FTS operator injection. Used for `wiki search` (high precision).
    """
    toks = _word.findall(terms)
    if not toks:
        # Match nothing rather than erroring on punctuation-only input.
        return '"' + terms.replace('"', "") + '"' if terms.strip() else '""'
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def significant_tokens(text: str) -> list[str]:
    """Content tokens with stopwords AND negation tokens dropped."""
    return [t for t in tokens(text)
            if len(t) > 1 and t not in STOPWORDS and t not in NEGATION_TOKENS]


def fts_or_query(text: str) -> str:
    """OR of significant tokens — high recall, for similarity/contradiction
    candidate retrieval. Negation tokens are dropped so 'X is not Y' still
    retrieves 'X is Y' as a candidate (polarity is then checked separately).
    Returns '""' (matches nothing) when there are no significant tokens.
    """
    toks = significant_tokens(text)
    if not toks:
        return '""'
    return " OR ".join('"' + t + '"' for t in toks)


def has_negation(text: str) -> bool:
    return any(t in NEGATION_TOKENS for t in tokens(text))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Shared "polarity conflict" heuristic: two texts are similar enough (by token
# Jaccard) to be about the same fact, but assert opposite polarity (one negated,
# one not). Used by both ingest._detect_contradictions (new claim vs promoted)
# and gate._conflicts_with_promoted (pending claim vs promoted) — one definition,
# one threshold, so the two call sites can't silently drift apart.
CONTRADICTION_JACCARD = 0.4


def polarity_conflict(a: str, b: str) -> bool:
    return jaccard(a, b) >= CONTRADICTION_JACCARD and has_negation(a) != has_negation(b)
