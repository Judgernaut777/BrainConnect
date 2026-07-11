#!/usr/bin/env bash
# Convenience wrapper so `./brainconnect.sh ...` works from the repo root on
# POSIX (Linux/macOS) without activating the venv. Prefers the repo venv's
# console script; falls back to `python3 -m brainconnect`.
#
# Named `brainconnect.sh` (not bare `brainconnect`), matching the
# `brainconnect.cmd`/`brainconnect.ps1` sibling naming convention.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_bc="$repo_dir/.venv/bin/brainconnect"

if [ -x "$venv_bc" ]; then
  exec "$venv_bc" "$@"
else
  exec python3 -m brainconnect "$@"
fi
