"""wiki-brain librarian: the event-driven judgment agent.

This package is the OTHER side of the project's core boundary. The `wiki` CLI
is pure code with ZERO model calls (BUILD_SPEC §1); the librarian is where
model judgment lives when you don't want to run it inside an interactive agent
session. It is a separate console script (`brainconnect-librarian`) and a separate
process — `wiki` never imports it.

Design:
- Triggered by events, not schedules. Ingest (`brainconnect add/drop/capture/transcribe`)
  can spawn `brainconnect-librarian extract --source N` the moment a source lands
  (opt-in via `[librarian] auto_extract`), and `brainconnect-librarian catch-up`
  idempotently processes any backlog. No Task Scheduler, no cron required.
- Provider-agnostic. One thin OpenAI-compatible chat client covers Ollama /
  LM Studio (local, key-free) and OpenRouter / Anthropic / OpenAI compat
  endpoints. API keys live in environment variables named by config — never
  in the repo (`brainconnect lint` still enforces that).
- Same doors, same gate. The librarian files extractions through the exact
  `file-claims` contract a session would use; everything it writes is pending
  material behind the human gate. It can never promote or edit truth.
"""
