"""A tiny, deterministic YAML frontmatter emitter (stdlib only).

OKF export must be **byte-identical** for identical ledger state, and it must not
pull a third-party dependency into the deterministic core (a clean wheel install
carries no PyYAML, and PyYAML's emit ordering is not a stable contract across
versions). So the bundle writes its own YAML, over exactly the value shapes it
controls: `str | int | float | bool | None | list | dict`.

Two rules make the output stable and unambiguous:

  * **Every string is double-quoted and escaped.** A scope like `repo:my-app`, a
    value that looks like `true`, an ISO timestamp — all round-trip without the
    reader having to guess a type.
  * **Mapping keys keep insertion order.** The caller builds ordinary dicts whose
    key order is the documented field order; we never sort or reorder them.

The emitted text parses cleanly as YAML (verified in the acceptance suite with a
real parser), but nothing here *depends* on a YAML library at runtime.
"""
from __future__ import annotations


def _quote(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append("\\x%02x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return _quote(str(v))


def _emit_map(m: dict, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    for k, v in m.items():
        if isinstance(v, dict):
            if v:
                lines.append(f"{pad}{k}:")
                _emit_map(v, indent + 1, lines)
            else:
                lines.append(f"{pad}{k}: {{}}")
        elif isinstance(v, list):
            if v:
                lines.append(f"{pad}{k}:")
                _emit_list(v, indent, lines)
            else:
                lines.append(f"{pad}{k}: []")
        else:
            lines.append(f"{pad}{k}: {_scalar(v)}")


def _emit_list(lst: list, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    for item in lst:
        if isinstance(item, dict):
            if not item:
                lines.append(f"{pad}- {{}}")
                continue
            inner: list[str] = []
            _emit_map(item, indent + 1, inner)
            firstpad = "  " * (indent + 1)
            lines.append(f"{pad}- {inner[0][len(firstpad):]}")
            lines.extend(inner[1:])
        elif isinstance(item, list):
            raise ValueError("nested lists are not supported in OKF frontmatter")
        else:
            lines.append(f"{pad}- {_scalar(item)}")


def emit(data: dict) -> str:
    """Serialize `data` to a deterministic YAML block (no document markers)."""
    lines: list[str] = []
    _emit_map(data, 0, lines)
    return "\n".join(lines)


def frontmatter(data: dict) -> str:
    """A full `---`-delimited YAML frontmatter block, newline-terminated."""
    return "---\n" + emit(data) + "\n---\n"


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return `(yaml_block, body)` for a document that starts with frontmatter.

    Used by the export self-check. Raises `ValueError` when the document does not
    open with a `---` line and close the block with a later `---` line — which is
    exactly the malformed-frontmatter case a validator must catch, not paper over.
    """
    if not text.startswith("---\n"):
        raise ValueError("document does not start with a frontmatter block")
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        if rest.endswith("\n---"):
            return rest[: -len("\n---")], ""
        raise ValueError("frontmatter block is not terminated")
    return rest[:end], rest[end + len("\n---\n"):]
