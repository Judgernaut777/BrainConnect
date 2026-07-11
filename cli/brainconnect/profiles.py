"""Recall profiles (LEDGER_SPEC.md §7) — what a given consumer should see.

A profile is a *deterministic filter*, not a prompt: minimum confidence, which
claim tags qualify, which scope types are excluded, how many items come back. Tags
are the classification substrate — they flow from a candidate's tags onto the claim
at promotion, and drive both these profiles and the Obsidian ledger sections. No
model call is involved in deciding what a manager versus a worker gets to see.

`require_tags` is an ANY-OF filter. An empty tuple means "no tag filter" — that is
why `manager_brief` still returns useful context over a ledger whose claims were
never tagged (every pre-ledger claim), while `known_failures` correctly returns
nothing rather than everything.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import confidence as conf

MANAGER_BRIEF = "manager_brief"
WORKER_BRIEF = "worker_brief"
REVIEWER_BRIEF = "reviewer_brief"
IMPLEMENTATION_CONSTRAINTS = "implementation_constraints"
USER_PREFERENCES = "user_preferences"
KNOWN_FAILURES = "known_failures"
MODEL_PERFORMANCE = "model_performance"

DEFAULT = MANAGER_BRIEF


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    min_confidence: str = conf.MEDIUM
    #: ANY-OF over claim tags; empty = no tag filter.
    require_tags: tuple[str, ...] = ()
    #: Scope types this profile never surfaces (noise for that consumer).
    exclude_scope_types: tuple[str, ...] = ()
    #: Scope types this profile restricts to; empty = no restriction.
    only_scope_types: tuple[str, ...] = ()
    max_items: int = 8
    include_sources: bool = True

    def accepts(self, *, tags: list[str], confidence_label: str,
                scope_type: str) -> bool:
        if not conf.at_least(confidence_label, self.min_confidence):
            return False
        if scope_type in self.exclude_scope_types:
            return False
        if self.only_scope_types and scope_type not in self.only_scope_types:
            return False
        if self.require_tags and not (set(tags) & set(self.require_tags)):
            return False
        return True


PROFILES: dict[str, Profile] = {
    MANAGER_BRIEF: Profile(
        name=MANAGER_BRIEF,
        description="Durable context for high-level planning: promoted architectural "
                    "decisions, current constraints, important project facts, recently "
                    "verified preferences. Excludes worker-specific noise.",
        min_confidence=conf.MEDIUM,
        require_tags=(),
        exclude_scope_types=("worker",),
    ),
    WORKER_BRIEF: Profile(
        name=WORKER_BRIEF,
        description="Only task-relevant facts needed for execution: constraints, "
                    "file/module facts, known gotchas, output requirements. Excludes "
                    "strategy debate and manager preferences.",
        min_confidence=conf.MEDIUM,
        require_tags=("constraint", "known-failure", "gotcha", "interface",
                      "output-requirement"),
        exclude_scope_types=("manager",),
    ),
    REVIEWER_BRIEF: Profile(
        name=REVIEWER_BRIEF,
        description="Review criteria, prior decisions, and known risks.",
        min_confidence=conf.MEDIUM,
        require_tags=("decision", "constraint", "known-failure", "risk", "criteria"),
    ),
    IMPLEMENTATION_CONSTRAINTS: Profile(
        name=IMPLEMENTATION_CONSTRAINTS,
        description="Hard constraints and locked decisions only.",
        min_confidence=conf.HIGH,
        require_tags=("constraint", "decision"),
    ),
    USER_PREFERENCES: Profile(
        name=USER_PREFERENCES,
        description="How the user wants things done.",
        min_confidence=conf.MEDIUM,
        require_tags=("preference",),
        only_scope_types=("global", "user"),
    ),
    KNOWN_FAILURES: Profile(
        name=KNOWN_FAILURES,
        description="Repeated failures and lessons learned.",
        min_confidence=conf.LOW,
        require_tags=("known-failure", "failure", "gotcha"),
    ),
    MODEL_PERFORMANCE: Profile(
        name=MODEL_PERFORMANCE,
        description="Model and worker performance facts.",
        min_confidence=conf.LOW,
        require_tags=("model-performance",),
        only_scope_types=("global", "model", "worker"),
    ),
}

NAMES = tuple(PROFILES)


def get(name: str | None) -> Profile:
    name = name or DEFAULT
    try:
        return PROFILES[name]
    except KeyError:
        raise ProfileError(
            f"unknown recall profile {name!r}; expected one of {', '.join(NAMES)}")
