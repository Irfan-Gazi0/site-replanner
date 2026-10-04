"""Constraint schema for the LLM parser (spec §3.2).

Flat on purpose: every field is required but can be null, which is what
structured outputs handle reliably. Per-type rules are enforced by the
verifier in `validate.py`, not here.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Constraint(BaseModel):
    type: Literal["duration_change", "start_after", "resource_unavailable", "add_dependency"]
    task: str | None = Field(description="duration_change/start_after: task ID like 'T8'; otherwise null")
    new_duration_min: int | None = Field(description="duration_change only: NEW TOTAL duration in minutes; otherwise null")
    delay_min: int | None = Field(description="start_after only: task may not start until this many minutes from now; otherwise null")
    resource: str | None = Field(description="resource_unavailable only: resource ID like 'R2'; otherwise null")
    outage_min: int | None = Field(description="resource_unavailable only: minutes until the resource is back; null = until further notice")
    before: str | None = Field(description="add_dependency only: task that must finish first; otherwise null")
    after: str | None = Field(description="add_dependency only: task that must wait; otherwise null")


class ParseResult(BaseModel):
    constraints: list[Constraint]
    unresolvable: bool = Field(description="true if the message cannot be expressed with these types, names unknown IDs, or concerns a completed task")
    reason: str = Field(description="one sentence: what you understood, or why it is unresolvable")


# The two models above are §3.2 verbatim and frozen. The helper below adds no
# field and changes no shape; it only spares every caller from spelling out the
# six or seven fields that must be null for a given type.
def make_constraint(type_: str, **fields) -> Constraint:
    """A `Constraint` of `type_` with `fields` set and every other field null."""
    blank: dict[str, object] = {
        "task": None, "new_duration_min": None, "delay_min": None,
        "resource": None, "outage_min": None, "before": None, "after": None,
    }
    unknown = set(fields) - set(blank)
    if unknown:
        raise TypeError(f"unknown constraint field(s): {', '.join(sorted(unknown))}")
    return Constraint(type=type_, **{**blank, **fields})
