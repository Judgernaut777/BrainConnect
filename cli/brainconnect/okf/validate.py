"""OKF bundle validation (Stage 2) — STRUCTURAL ONLY, and hostile-input safe.

This is the second bounded OKF stage. It answers exactly one question: *is this
directory a structurally well-formed OKF bundle?* It answers nothing else. In
particular:

    OKF-valid  !=  trusted
    OKF-valid  !=  promoted
    OKF-valid  !=  safe

Validation confers no authority. A bundle can be perfectly valid and consist
entirely of hostile, false, or unvetted claims; import (Stage 3) is where content
enters the normal candidate + safety pipeline as PENDING, and only a human can
promote. This module never imports, never executes, and never trusts bundle
content — it only inspects structure and reports STRUCTURED errors + warnings.

Security posture (the validator must survive a HOSTILE bundle):

  * **Never follow a symlink out of the bundle.** Symlinks are inspected
    *lexically* (`os.readlink`, never resolved through the filesystem), and one
    whose target escapes the root is a rejected error — the target is never read.
  * **Never read an unbounded file.** Every file's size is taken from its stat
    entry first; anything over the per-file cap is flagged and not read, and the
    running total is bounded so an oversized bundle fails closed.
  * **Never execute or import bundle content.** No `eval`, no `import`, no YAML
    loader that can construct objects — frontmatter is parsed by a tiny, bounded,
    stdlib subset parser that only ever produces `dict`/`list`/`str`/`int`/…​.
  * **Resolve every path against the real root and reject anything outside it.**
    Relative links and traversal (`../`, absolute paths) are classified purely
    lexically, so a malicious link can never make the validator touch the host.
  * **Never hang.** The directory walk is bounded (depth, count), YAML nesting is
    bounded, and relationship-cycle detection is a finite-graph DFS.

The one guarantee we make: **a malformed or unsafe-structure bundle never reports
`ok`.** We never partially import; here there is nothing to import at all.
"""
from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .export import OKF_VERSION

# The version this build understands. A bundle's MAJOR must match; a newer MINOR
# is accepted with a warning (forward-compatible), an unknown MAJOR is rejected.
_SUPPORTED_MAJOR, _SUPPORTED_MINOR = (int(x) for x in OKF_VERSION.split(".")[:2])

# Markdown inline link: `](target)`. We only look at the target.
_LINK_RE = re.compile(r"\]\(([^)]+)\)")

# A source anchor in sources/source-index.md, e.g. `{#source_7}`.
_SOURCE_ANCHOR_RE = re.compile(r"\{#(source_[0-9]+)\}")

# Bidirectional / zero-width / format control code points that make a filename
# lie about what it is. Rejected in any path component.
_UNSAFE_NAME_CHARS = frozenset(
    "​‌‍‎‏‪‫‬‭‮"
    "⁦⁧⁨⁩﻿ ")

# Top-level frontmatter keys the format defines. Anything else is an unknown
# extension field: preserved (we never rewrite a bundle) and warned about.
_KNOWN_TOP_KEYS = frozenset({"title", "okf_version", "brainconnect", "tags"})
# Keys inside the `brainconnect:` extension block that the mapping defines.
_KNOWN_BC_KEYS = frozenset({
    "id", "status", "trusted", "scope", "confidence", "sources",
    "valid_from", "valid_until", "learned_at", "last_verified_at",
    "superseded_by", "contradictions", "provenance", "safety",
})

# A bare private-key delimiter never belongs in a shareable bundle. Structural,
# not a full secret scan (that is the safety surface's job) — a cheap belt to
# catch the obvious leak on the way in.
_PEM_MARKER_RE = re.compile(
    r"-----(?:BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")


@dataclass
class ValidationLimits:
    """Bounds that make the validator fail closed on a hostile or huge bundle.

    All configurable so tests can trip them cheaply and an operator can tune them
    for an unusually large but legitimate bundle.
    """
    #: A single document larger than this is rejected and never read into memory.
    max_file_bytes: int = 2 * 1024 * 1024
    #: The whole bundle larger than this is rejected (fail closed on oversize).
    max_bundle_bytes: int = 64 * 1024 * 1024
    #: More entries than this and the walk stops with an error.
    max_files: int = 10_000
    #: Directory nesting deeper than this is refused (walk guard).
    max_dir_depth: int = 32
    #: Frontmatter nesting deeper than this is malformed (parser guard).
    max_yaml_depth: int = 64


@dataclass
class ValidationIssue:
    """One structured finding. `code` is machine-stable; `path` is bundle-relative."""
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass
class ValidationResult:
    """The structured verdict. `ok` is true iff there are zero errors.

    Warnings never affect `ok`: an unknown-but-safe extension field, a newer
    compatible minor version, or a relationship cycle is reported, not fatal.
    """
    ok: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    okf_version: str = ""
    document_count: int = 0
    claim_count: int = 0
    source_count: int = 0
    ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "okf_version": self.okf_version,
            "document_count": self.document_count,
            "claim_count": self.claim_count,
            "source_count": self.source_count,
            "ids": self.ids,
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
        }


class _Issues:
    """Mutable collector; keeps error/warning separation and a running `ok`."""

    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def error(self, code: str, message: str, path: str = "") -> None:
        self.errors.append(ValidationIssue(code, message, path))

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.warnings.append(ValidationIssue(code, message, path))


# --- a tiny, bounded, non-constructing YAML-subset parser --------------------
class _YamlError(Exception):
    """Raised on anything the frontmatter subset parser cannot accept."""


def _parse_scalar(s: str) -> object:
    s = s.strip()
    if not s:
        return None
    if s[0] == '"':
        return _parse_dquoted(s)
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise _YamlError("unterminated single-quoted scalar")
        return s[1:-1].replace("''", "'")
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", "Null"):
        return None
    if re.fullmatch(r"-?[0-9]+", s):
        return int(s)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", s):
        return float(s)
    return s  # a bare (unquoted) string


def _parse_dquoted(s: str) -> str:
    if len(s) < 2 or s[-1] != '"':
        raise _YamlError("unterminated double-quoted scalar")
    out: list[str] = []
    i = 1
    end = len(s) - 1
    while i < end:
        ch = s[i]
        if ch == "\\":
            i += 1
            if i >= end:
                raise _YamlError("dangling escape in scalar")
            e = s[i]
            if e == "n":
                out.append("\n")
            elif e == "t":
                out.append("\t")
            elif e == "r":
                out.append("\r")
            elif e in ('"', "\\", "/"):
                out.append(e)
            elif e == "x":
                if i + 2 >= end:
                    raise _YamlError("truncated \\x escape")
                out.append(chr(int(s[i + 1:i + 3], 16)))
                i += 2
            else:
                raise _YamlError(f"unknown escape \\{e}")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_MAP_ENTRY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*\s*:(\s|$)")


class _Frontmatter:
    """Indentation-based recursive parser for the OKF frontmatter subset.

    It accepts exactly the shapes the exporter emits — nested maps, sequences of
    scalars, and sequences of maps — and *rejects* anything else as malformed
    rather than guessing. It constructs only plain containers and scalars; it can
    never instantiate an arbitrary object the way a full YAML loader can.
    """

    def __init__(self, text: str, max_depth: int) -> None:
        self.toks: list[tuple[int, str]] = []
        for raw in text.split("\n"):
            stripped = raw.lstrip(" ")
            indent = len(raw) - len(stripped)
            # Leading whitespace must be spaces only; a tab in indentation is
            # invalid YAML and a classic ambiguity source — reject it.
            if raw[:len(raw) - len(raw.lstrip())].find("\t") != -1:
                raise _YamlError("tab in indentation")
            content = stripped.rstrip()
            if not content or content.startswith("#"):
                continue
            self.toks.append((indent, content))
        self.i = 0
        self.max_depth = max_depth

    def parse(self) -> object:
        if not self.toks:
            return {}
        node = self._block(self.toks[0][0], 0)
        if self.i != len(self.toks):
            raise _YamlError("inconsistent indentation")
        return node

    def _block(self, indent: int, depth: int) -> object:
        if depth > self.max_depth:
            raise _YamlError("frontmatter nested too deep")
        if self.toks[self.i][1].startswith("- "):
            return self._list(indent, depth)
        return self._map(indent, depth)

    def _map(self, indent: int, depth: int) -> dict:
        out: dict = {}
        while self.i < len(self.toks):
            ind, content = self.toks[self.i]
            if ind != indent or content.startswith("- "):
                break
            if ":" not in content:
                raise _YamlError(f"map entry without a colon: {content!r}")
            key, _, rest = content.partition(":")
            key = key.strip()
            if not key or key[0] in "\"'":
                raise _YamlError(f"bad map key: {content!r}")
            rest = rest.strip()
            self.i += 1
            if rest == "":
                nxt = self.toks[self.i] if self.i < len(self.toks) else None
                if nxt and nxt[1].startswith("- ") and nxt[0] == indent:
                    out[key] = self._list(indent, depth + 1)
                elif nxt and nxt[0] > indent:
                    out[key] = self._block(nxt[0], depth + 1)
                else:
                    out[key] = None
            elif rest == "[]":
                out[key] = []
            elif rest == "{}":
                out[key] = {}
            else:
                out[key] = _parse_scalar(rest)
        return out

    def _list(self, indent: int, depth: int) -> list:
        out: list = []
        while self.i < len(self.toks):
            ind, content = self.toks[self.i]
            if ind != indent or not content.startswith("- "):
                break
            item = content[2:].strip()
            if item and item[0] not in "\"'" and _MAP_ENTRY_RE.match(item):
                # A map item: splice the inline first entry as a token at indent+2
                # and let _map consume it plus any continuation lines.
                self.toks[self.i] = (indent + 2, item)
                out.append(self._map(indent + 2, depth + 1))
            else:
                out.append(_parse_scalar(item))
                self.i += 1
        return out


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return `(yaml, body)` if `text` opens with a `---` block, else `None`.

    `None` means *no frontmatter at all*; a `_YamlError` means *the block opened
    but is malformed* (never terminated). The bundle is written with `\\n`
    newlines, so a `\\r\\n` document is normalized before splitting.
    """
    norm = text.replace("\r\n", "\n")
    if not norm.startswith("---\n"):
        return None
    rest = norm[4:]
    end = rest.find("\n---\n")
    if end != -1:
        return rest[:end], rest[end + len("\n---\n"):]
    # tolerate a file that ends exactly on the closing delimiter
    if rest.endswith("\n---"):
        return rest[: -len("\n---")], ""
    raise _YamlError("frontmatter block is not terminated")


# --- filename / path safety --------------------------------------------------
def _unsafe_name(name: str) -> str | None:
    """Return a reason string if `name` is an unsafe path component, else None."""
    if name in ("", ".", ".."):
        return "reserved or empty name"
    if len(name.encode("utf-8", "surrogatepass")) > 255:
        return "name exceeds 255 bytes"
    for ch in name:
        o = ord(ch)
        if o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            return "contains a control character"
        if ch in _UNSAFE_NAME_CHARS:
            return "contains a bidirectional or zero-width control character"
        if ch in "/\\":
            return "contains a path separator"
        if ch == "\x00":
            return "contains a null byte"
    if name[-1] in " .":
        return "ends with a space or dot"
    return None


# --- the directory walk (hostile-safe) ---------------------------------------
def _collect(root: Path, limits: ValidationLimits, iss: _Issues) -> list[tuple[str, int]]:
    """Walk `root` without following symlinks out; return `[(relpath, size)]`.

    Bounded on depth, count, per-file size, and total size. Symlinks are inspected
    lexically and never followed; an escaping symlink is an error and its target
    is never read.
    """
    files: list[tuple[str, int]] = []
    total = 0
    count = 0
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    while stack:
        d, prefix, depth = stack.pop()
        if depth > limits.max_dir_depth:
            iss.error("too_deep",
                      f"directory nesting exceeds {limits.max_dir_depth}",
                      prefix or ".")
            continue
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            iss.error("unreadable_dir", f"cannot read directory: {e.strerror}",
                      prefix or ".")
            continue
        for entry in entries:
            name = entry.name
            rel = posixpath.join(prefix, name) if prefix else name
            reason = _unsafe_name(name)
            if reason:
                iss.error("unsafe_filename",
                          f"unsafe path component ({reason})", rel)
                continue
            count += 1
            if count > limits.max_files:
                iss.error("too_many_files",
                          f"bundle exceeds {limits.max_files} entries", rel)
                return files
            if entry.is_symlink():
                _check_symlink(rel, entry, iss)
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), rel, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    iss.error("unsafe_file_type",
                              "not a regular file (device/fifo/socket)", rel)
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as e:
                iss.error("unreadable_file", f"cannot stat file: {e.strerror}", rel)
                continue
            total += size
            if size > limits.max_file_bytes:
                iss.error("file_too_large",
                          f"file is {size} bytes (cap {limits.max_file_bytes})", rel)
                files.append((rel, -1))  # -1 => oversized, do NOT read it
            else:
                files.append((rel, size))
            if total > limits.max_bundle_bytes:
                iss.error("bundle_too_large",
                          f"bundle exceeds {limits.max_bundle_bytes} bytes", rel)
                return files
    return files


def _check_symlink(rel: str, entry, iss: _Issues) -> None:
    """Lexically classify a symlink. Never resolves it through the filesystem."""
    try:
        target = os.readlink(entry.path)
    except OSError as e:
        iss.error("symlink_unreadable", f"cannot read symlink: {e.strerror}", rel)
        return
    if os.path.isabs(target) or (len(target) > 1 and target[1] == ":"):
        iss.error("symlink_escape",
                  "symlink points to an absolute path outside the bundle", rel)
        return
    posix_target = target.replace("\\", "/")
    parent = posixpath.dirname(rel)
    resolved = posixpath.normpath(posixpath.join(parent, posix_target))
    if resolved == ".." or resolved.startswith("../") or posixpath.isabs(resolved):
        iss.error("symlink_escape", "symlink target escapes the bundle root", rel)
    else:
        iss.warn("symlink_present",
                 "symlink inside the bundle (not followed during validation)", rel)


# --- per-document checks -----------------------------------------------------
def _classify_link(doc_rel: str, link: str) -> tuple[str, str]:
    """Return `(kind, normalized_relpath)` for a markdown link target.

    `kind` is one of: `external` (http/mailto/anchor), `absolute` (rejected),
    `escape` (traversal out of the bundle, rejected), or `internal`.
    """
    target = link.split("#", 1)[0].strip()
    if not target:
        return "external", ""  # pure anchor
    if target.startswith(("http://", "https://", "mailto:", "//")):
        return "external", ""
    if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
        return "absolute", target
    base = posixpath.dirname(doc_rel)
    norm = posixpath.normpath(posixpath.join(base, target))
    if norm == ".." or norm.startswith("../") or posixpath.isabs(norm):
        return "escape", norm
    return "internal", norm


def _check_document(rel: str, text: str, file_set: set[str], limits: ValidationLimits,
                    iss: _Issues, ids: dict[str, str], rels: list[tuple[str, str, str, str]]):
    """Frontmatter + link checks for one markdown document.

    Populates `ids` (brainconnect.id -> doc rel) and `rels` (from_id, target_ref,
    relpath) for later duplicate + relationship resolution.
    """
    is_claim = rel.startswith("claims/") and rel.endswith(".md")

    # -- frontmatter --
    front = None
    try:
        split = _split_frontmatter(text)
    except _YamlError as e:
        iss.error("malformed_frontmatter", f"frontmatter block: {e}", rel)
        split = None
        if is_claim:
            return
    if split is None:
        if is_claim:
            iss.error("missing_frontmatter",
                      "claim document has no YAML frontmatter block", rel)
            return
    else:
        yaml_text, _body = split
        try:
            front = _Frontmatter(yaml_text, limits.max_yaml_depth).parse()
        except _YamlError as e:
            iss.error("malformed_yaml", f"malformed frontmatter: {e}", rel)
            if is_claim:
                return
        if front is not None and not isinstance(front, dict):
            iss.error("malformed_yaml",
                      "frontmatter is not a mapping", rel)
            front = None
            if is_claim:
                return

    if is_claim and isinstance(front, dict):
        _check_claim_front(rel, front, iss, ids, rels)

    # -- links (traversal / absolute / broken) --
    for m in _LINK_RE.finditer(text):
        kind, norm = _classify_link(rel, m.group(1))
        if kind == "external":
            continue
        if kind == "absolute":
            iss.error("absolute_link",
                      f"link uses an absolute path: {m.group(1)!r}", rel)
        elif kind == "escape":
            iss.error("link_traversal",
                      f"link escapes the bundle root: {m.group(1)!r}", rel)
        elif norm not in file_set:
            iss.error("broken_link",
                      f"relative link does not resolve inside the bundle: "
                      f"{m.group(1)!r}", rel)

    # -- a bare private-key delimiter should never ship in a bundle --
    if _PEM_MARKER_RE.search(text):
        iss.warn("private_key_marker",
                 "a PEM private-key delimiter is present in the document body", rel)


def _check_claim_front(rel: str, front: dict, iss: _Issues, ids: dict[str, str],
                       rels: list[tuple[str, str, str, str]]):
    ver = front.get("okf_version")
    if ver is None:
        iss.error("missing_field", "frontmatter lacks okf_version", rel)
    elif not isinstance(ver, str):
        iss.error("bad_field", "okf_version is not a string", rel)
    else:
        _version_issue(ver, rel, iss, key="okf_version")

    if "title" not in front:
        iss.warn("missing_title", "claim document has no title", rel)

    bc = front.get("brainconnect")
    if not isinstance(bc, dict):
        iss.error("missing_field",
                  "frontmatter lacks the brainconnect: extension block", rel)
        return
    cid = bc.get("id")
    if not isinstance(cid, str) or not cid:
        iss.error("missing_field", "brainconnect.id is missing or not a string", rel)
    else:
        if cid in ids:
            iss.error("duplicate_id",
                      f"claim id {cid!r} also appears in {ids[cid]}", rel)
        else:
            ids[cid] = rel
        stem = Path(rel).stem
        if cid != stem:
            iss.warn("id_filename_mismatch",
                     f"brainconnect.id {cid!r} does not match filename {stem!r}", rel)

    # unknown extension fields: warn + preserve (validation never rewrites)
    for k in front:
        if k not in _KNOWN_TOP_KEYS:
            iss.warn("unknown_field",
                     f"unknown top-level frontmatter field {k!r} (preserved)", rel)
    for k in bc:
        if k not in _KNOWN_BC_KEYS:
            iss.warn("unknown_field",
                     f"unknown brainconnect.{k} field (preserved)", rel)

    # relationships → resolved later against the id/file set. The edge KIND is
    # recorded: contradictions are symmetric (A↔B) and must never be read as a
    # "cycle"; only the supersession chain is expected to be acyclic.
    if isinstance(cid, str) and cid:
        sup = bc.get("superseded_by")
        if isinstance(sup, str) and sup:
            rels.append((cid, sup, rel, "superseded_by"))
        con = bc.get("contradictions")
        if isinstance(con, list):
            for target in con:
                if isinstance(target, str) and target:
                    rels.append((cid, target, rel, "contradiction"))


def _version_issue(ver: str, rel: str, iss: _Issues, *, key: str) -> None:
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\..*)?", ver.strip())
    if not m:
        iss.error("bad_version", f"{key}={ver!r} is not a MAJOR.MINOR version", rel)
        return
    major, minor = int(m.group(1)), int(m.group(2))
    if major != _SUPPORTED_MAJOR:
        iss.error("unsupported_version",
                  f"{key}={ver!r} has unsupported major {major} "
                  f"(this build supports {_SUPPORTED_MAJOR}.x)", rel)
    elif minor > _SUPPORTED_MINOR:
        iss.warn("newer_minor_version",
                 f"{key}={ver!r} is a newer compatible minor than "
                 f"{OKF_VERSION}; unknown fields will be preserved", rel)


# --- relationship graph ------------------------------------------------------
def _resolve_relationships(rels, ids: dict[str, str], file_set: set[str], iss: _Issues):
    """Every supersession/contradiction target must resolve; report cycles."""
    for from_id, target, rel, kind in rels:
        if target in ids:
            continue
        if f"claims/{target}.md" in file_set:
            continue
        iss.error("broken_relationship",
                  f"relationship target {target!r} has no document in the bundle",
                  rel)

    # Cycle detection over the SUPERSESSION graph only. Contradictions are
    # symmetric (A↔B) and would trivially register as a two-node cycle, so they
    # are excluded. Iterative DFS so a long supersession chain can never exhaust
    # the recursion stack — a finite graph terminates; a cyclic bundle is
    # reported, never hangs.
    edges: dict[str, set[str]] = {}
    for from_id, target, rel, kind in rels:
        if kind == "superseded_by":
            edges.setdefault(from_id, set()).add(target)
    _WHITE, _GRAY, _BLACK = 0, 1, 2
    color: dict[str, int] = {}
    reported: set[frozenset] = set()

    for start in sorted(edges):
        if color.get(start, _WHITE) != _WHITE:
            continue
        # One shared `path` (the current gray chain) plus per-frame successor
        # iterators — O(nodes) memory, no recursion, no O(depth^2) path copies.
        stack: list[tuple[str, list[str]]] = [(start, sorted(edges.get(start, ())))]
        path: list[str] = [start]
        depth_of = {start: 0}
        color[start] = _GRAY
        while stack:
            node, succ = stack[-1]
            if not succ:
                color[node] = _BLACK
                path.pop()
                stack.pop()
                continue
            nxt = succ.pop(0)
            st = color.get(nxt, _WHITE)
            if st == _GRAY:  # back-edge → cycle
                cyc = path[depth_of.get(nxt, len(path) - 1):] + [nxt]
                key = frozenset(cyc)
                if key not in reported:
                    reported.add(key)
                    iss.warn("relationship_cycle",
                             "relationship cycle: " + " -> ".join(cyc),
                             ids.get(cyc[0], ""))
            elif st == _WHITE and nxt in edges:
                color[nxt] = _GRAY
                depth_of[nxt] = len(path)
                path.append(nxt)
                stack.append((nxt, sorted(edges.get(nxt, ()))))


# --- entry point -------------------------------------------------------------
def validate_bundle(path, limits: ValidationLimits | None = None) -> ValidationResult:
    """Structurally validate an OKF bundle at `path`. STRUCTURAL ONLY.

    Returns a `ValidationResult`; `ok` is true iff there are zero errors. Never
    raises on bundle content — a hostile bundle produces structured errors, not
    an exception, and never an escape/hang/unbounded read.
    """
    limits = limits or ValidationLimits()
    iss = _Issues()
    root = Path(path)
    if not root.exists():
        iss.error("not_found", "bundle path does not exist", str(path))
        return ValidationResult(ok=False, errors=iss.errors, warnings=iss.warnings)
    if not root.is_dir():
        iss.error("not_a_directory", "bundle path is not a directory", str(path))
        return ValidationResult(ok=False, errors=iss.errors, warnings=iss.warnings)
    root = root.resolve()

    # marker: presence + format + version
    marker = root / ".okf-bundle"
    okf_version = ""
    if not marker.is_file() or marker.is_symlink():
        iss.error("missing_marker",
                  "no .okf-bundle marker at the bundle root", ".okf-bundle")
    else:
        okf_version = _check_marker(marker, iss)

    files = _collect(root, limits, iss)
    file_set = {rel for rel, _ in files}

    ids: dict[str, str] = {}
    rels: list[tuple[str, str, str, str]] = []
    doc_count = 0
    claim_count = 0
    source_count = 0

    for rel, size in files:
        if size == -1:
            continue  # oversized: already errored, never read
        p = root / rel
        try:
            data = p.read_bytes()
        except OSError as e:
            iss.error("unreadable_file", f"cannot read file: {e.strerror}", rel)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            iss.error("invalid_encoding",
                      f"file is not valid UTF-8 (byte {e.start})", rel)
            continue
        if rel == "sources/source-index.md":
            source_count = len(set(_SOURCE_ANCHOR_RE.findall(text)))
        if rel.endswith(".md"):
            doc_count += 1
            if rel.startswith("claims/"):
                claim_count += 1
            _check_document(rel, text, file_set, limits, iss, ids, rels)

    _resolve_relationships(rels, ids, file_set, iss)

    return ValidationResult(
        ok=not iss.errors,
        errors=iss.errors,
        warnings=iss.warnings,
        okf_version=okf_version,
        document_count=doc_count,
        claim_count=claim_count,
        source_count=source_count,
        ids=sorted(ids),
    )


def _check_marker(marker: Path, iss: _Issues) -> str:
    try:
        raw = marker.read_bytes()
    except OSError as e:
        iss.error("unreadable_file", f"cannot read marker: {e.strerror}", ".okf-bundle")
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        iss.error("invalid_encoding", ".okf-bundle is not valid UTF-8", ".okf-bundle")
        return ""
    kv: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            iss.error("bad_marker", f"marker line without key=value: {line!r}",
                      ".okf-bundle")
            continue
        k, _, v = line.partition("=")
        kv[k.strip()] = v.strip()
    if kv.get("format") != "okf":
        iss.error("bad_marker",
                  f"marker format is {kv.get('format')!r}, expected 'okf'",
                  ".okf-bundle")
    ver = kv.get("version", "")
    if not ver:
        iss.error("bad_marker", "marker has no version=", ".okf-bundle")
    else:
        _version_issue(ver, ".okf-bundle", iss, key="version")
    return ver
