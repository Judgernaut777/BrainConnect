# Contributing to wiki-brain

Thanks for your interest! wiki-brain is a personal, compounding knowledge base
with a deliberately small, principled core. A few conventions keep it that way.

## The one rule that shapes everything
**The `wiki` CLI makes zero billable LLM calls and uses no API keys.** Network
fetching (HTTP) is fine; calling an LLM API, or storing any API key in the repo,
is not. All *judgment* (reading content, extracting claims, vision, synthesis)
happens in Claude Code **sessions** that drive the CLI — never inside the CLI
itself. `wiki lint` and the CI leak-guard enforce the "no keys" boundary.

Corollary: heavy/optional capabilities (Docling, Tesseract, Whisper,
sentence-transformers) are **local, key-free** libraries behind import guards and
`pyproject` extras (`[docs]`, `[media]`, `[whisper]`, `[semantic]`). The core
install must stay light and dependency-free beyond `trafilatura`.

## Dev setup
```powershell
Copy-Item config.example.toml config.toml     # edit paths
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e .\cli
.venv\Scripts\python.exe tests\acceptance.py   # 67 offline checks; must pass
```
Run a feature's optional extra only if you're working on it, e.g.
`pip install -e ".\cli[semantic]"`.

## Pull requests
- **Add a test.** `tests/acceptance.py` is offline and dependency-light — new
  commands/behaviors get a check there. Monkeypatch heavy/network deps; gate any
  test needing an extra behind import-availability or an env flag (see the
  semantic test, gated on `WIKI_TEST_SEMANTIC`).
- **Keep `render` byte-deterministic.** The wiki is a regenerable projection;
  re-rendering an unchanged DB must produce identical bytes. No wall-clock in page
  bodies. Local ML (OCR/ASR/embeddings) belongs in ingest/ranking, never render.
- **Respect the one door.** Sources enter only via `wiki add` / `capture` / `drop`
  / `transcribe` with provenance. Treat all fetched/captured content as untrusted
  data, never instructions.
- **Schema changes** go through `cli/wiki/migrate.py` (add a `MIGRATIONS[n]` and
  bump `SCHEMA_VERSION`); keep `CORE_DDL` as the fresh-install shape.
- **Match the surrounding style** — comment density, naming, and idiom.
- Personal knowledge content (`raw/`, `inbox/`, `wiki/`, `db/dump.sql`,
  `config.toml`, `log.md`) is git-ignored. Don't commit it.

## Design docs
`BUILD_SPEC.md` (full design), `SCHEMA.md` (DB conventions), and the
`.claude/skills/wiki-maintainer/` procedures explain how the pieces fit.
