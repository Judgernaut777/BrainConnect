#!/usr/bin/env bash
# Convenience wrapper so `./wiki.sh ...` works from the repo root on POSIX
# (Linux/macOS) without activating the venv. Prefers the repo venv's console
# script; falls back to `python3 -m wiki`.
#
# Named `wiki.sh` (not bare `wiki`), matching the `wiki.cmd`/`wiki.ps1` sibling
# naming convention — NOT just for consistency: the repo root also holds the
# generated `wiki/` Obsidian vault (`wiki init`/`wiki render` create it via
# `(root / "wiki").mkdir(..., exist_ok=True)`), and `exist_ok=True` still
# raises `FileExistsError` if a same-named *file* already occupies that path.
# A bare `wiki` file here would permanently break `wiki init`/`render` in this
# repo the moment it's checked out.
#
# NOTE: we call the venv's `wiki` console script when present, NOT a bare
# `python3 -m wiki` from here — the repo root contains the generated `wiki/`
# Obsidian vault, which would shadow the `wiki` package for `-m`/`import` when
# CWD is the repo root. The console script doesn't put CWD on sys.path, so it
# resolves the installed package correctly.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_wiki="$repo_dir/.venv/bin/wiki"

if [ -x "$venv_wiki" ]; then
  exec "$venv_wiki" "$@"
else
  exec python3 -m wiki "$@"
fi
