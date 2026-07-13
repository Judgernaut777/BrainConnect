# ROLES.md — agent-role assignment (ADR 0008 Lane 6)

Status: **active** (ADR 0008 Lane 6, 2026-07-13). Governed by
[ADR 0008](adr/0008-orchestration-boundary.md) and
[ORCHESTRATION.md](ORCHESTRATION.md). Consumes the Lane-1 registry tiers
([REGISTRY.md](REGISTRY.md)), composes with the Lane-4 delegation trigger
([DELEGATION.md](DELEGATION.md)), and binds to [LEDGER_SPEC.md](LEDGER_SPEC.md)
§2 (promotion is human-only).

Code: `cli/brainconnect/roles.py`. CLI: `brainconnect roles`.

---

## 1. What this is (and is not)

BrainConnect's Lane-6 seam onto AgentConnect's multi-model collaboration roles. It
does exactly two things:

1. **MAPS** a plan's requested agent roles to existing AgentConnect
   model-manager profiles via a deterministic DATA table.
2. **RECORDS** the resulting role-assignment (plus a reviewer-independence
   recommendation) as ordinary BrainConnect PENDING decision-provenance.

**BC recommends + records. AgentConnect executes and — with Decima — enforces.**
Per ADR 0008 Lane 6, BC does **not**:

- run AgentConnect's `RouterService` decompose → execute → synthesize,
- spawn workers, execute, authorize, or assign ownership,
- keep reviewer and implementer apart at runtime (that is AC/Decima's job),
- make **any** model call.

It contains **no role engine and no verifier**. The mapping is a table
(`ROLE_TABLE`); nothing branches on a role name. Re-pointing a role at a
different AC profile is an edit to DATA — the ADR-0008 provider-portability rule
made structural for roles, exactly as the Lane-1 registry made it structural for
models.

---

## 2. The role → profile map (data-driven)

The seven roles the brief names, each mapped to one of the four shipped
AgentConnect model-manager profiles (`model_manager/backends.py`:
`general_coder`, `coding_specialist`, `review_worker`, `critic`) and to a Lane-1
capability tier (which is what makes an assignment **compose** with the Lane-4
delegation trigger — feed the tier to `brainconnect delegate`).

| role | AC profile | capability tier | kind | reviews implementer |
|---|---|---|---|---|
| implementer | `coding_specialist` | high-capability-local | producer | — (is the implementer) |
| test_reviewer | `review_worker` | high-capability-local | reviewer | yes |
| security_reviewer | `critic` | high-capability-local | reviewer | yes |
| documentation_reviewer | `review_worker` | general-doc | reviewer | yes |
| verifier | `critic` | high-capability-local | verifier | yes |
| research_agent | `general_coder` | general-doc | producer | — |
| integration_agent | `general_coder` | high-capability-local | producer | — |

The table is **independence-clean by construction**: no reviewer or verifier
shares the implementer's `coding_specialist` profile, so the default assignment
already preserves independent review. Resolution is a pure lookup — no `if role
== …` dispatch anywhere. The table is validated at import against the real AC
profile set and the real Lane-1 tier names, so a bad data edit fails at import,
never silently downstream.

Read it with `brainconnect roles list` (or `roles list --json`).

### Unknown roles fail closed

A requested role that is not in the table is **refused** — recorded in
`refused_roles` with a reason, never given a profile — and the result's `ok`
becomes `False`. An unknown role is never silently mapped. An override targeting
an unknown AC profile (or an unknown role) is a hard `RoleError`, also fail-closed.

---

## 3. Reviewer independence — a recommendation, not enforcement

For every reviewer/verifier role assigned alongside the implementer, BC emits an
**independence recommendation**: AgentConnect/Decima should ensure a *distinct
agent* executes the review so it stays independent of the work it reviews. When
the reviewer's profile **equals** the implementer's profile (e.g. after an
operator override re-points a reviewer onto `coding_specialist`), the
recommendation escalates to a **collision flag** (`same_profile: true`) — an
elevated same-agent risk that AC/Decima *must* resolve.

This is a **recommendation recorded in provenance**, not a refusal and not
enforcement. BC still maps the roles and records the assignment. BC does not
itself keep reviewer and implementer apart, spawn anything, or assign an agent —
ownership, concurrency, and independence are enforced by AgentConnect and Decima
at execution time. The independence logic is data-driven: it reads the
`reviews` / `primary_producer` flags on each assignment, never a role name.

---

## 4. Provenance — recorded, never promoted

The assignment (mapped profiles, refusals, and independence findings) is filed
via `api.capture_candidate` as an ordinary **PENDING** memory candidate, scoped
`task:<task_id>`, tagged `orchestration-decision` / `role-assignment`, with the
full assignment in `metadata` (`provenance_only: true`, `trusted: false`) for
later explainability. A deterministic fingerprint keeps two distinct assignments
from colliding on the ingest content hash while an exact re-assignment dedups.

It is **never** auto-trusted and **never** self-promoted: promotion is
human/librarian-only (LEDGER_SPEC §2), and no agent/worker/tool proposer can
confer trust — a worker or agent attempting to promote the recorded assignment is
refused with `ReviewerNotPermitted`. Recording is best-effort and
degrade-never-crash: a duplicate, a safety refusal, or any capture fault is
recorded as a note, never propagated — the computed assignment is always returned.

---

## 5. CLI

```
brainconnect roles list [--json]

brainconnect roles assign <task_id> \
    [--role R ...] [--profile-override role=profile ...] \
    [--no-record] [--by WHO] [--by-type T] [--json]
```

`roles list` (or bare `roles`) prints the deterministic role→profile table.
`roles assign` maps the requested roles, flags reviewer/implementer collisions,
and records the assignment as PENDING provenance. `--profile-override
role=profile` re-points a role at a different AC profile (data-driven provider
portability); an unknown target profile is refused. No model is ever called; the
command stays key-free and model-free — BC recommends and records, AgentConnect
executes and enforces.
