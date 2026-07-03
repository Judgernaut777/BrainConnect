#!/usr/bin/env bash
# mechanical-maintain.sh — POSIX twin of mechanical-maintain.ps1: the
# zero-model half of the morning maintain pass.
#
# Part of the "hybrid" scheduling approach (BUILD_SPEC §7 + the MCP/scheduling
# note): a cron job or systemd timer runs the pure-code steps every day — NO
# model calls, no Claude client involved — while the judgment half of
# maintain.md (claim extraction, synthesis, contradiction adjudication, skill
# drafting) is done interactively via `/maintain` when convenient.
#
# Safe to run unattended: every step here is a zero-model `wiki` command. It
# commits locally but NEVER pushes — you review the morning diff and push.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# Call the console script (NOT `python3 -m wiki`): the repo root holds the
# generated wiki/ vault, which would shadow the `wiki` package for -m.
wiki_bin="$repo/.venv/bin/wiki"
if [ ! -x "$wiki_bin" ]; then
  wiki_bin="wiki"
fi

log_dir="$repo/logs"
mkdir -p "$log_dir"
log="$log_dir/mechanical-maintain.log"
printf '==== %s mechanical maintain ====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log"

step() {
  local desc="$1"; shift
  printf -- '-- %s : wiki %s\n' "$desc" "$*" >> "$log"
  "$wiki_bin" "$@" >> "$log" 2>&1
  printf '   exit=%s\n' "$?" >> "$log"
}

step 'bookmarks sync'                  bookmarks sync
step 'gate (auto-promote boring tier)' gate
step 'render'                          render
step 'lint'                            lint
step 'health'                          health
# Local commit only — never push. `wiki commit` is a no-op-safe if nothing changed.
step 'commit'                          commit "cron: mechanical maintain $(date '+%Y-%m-%d')"

printf '==== done ====\n' >> "$log"
