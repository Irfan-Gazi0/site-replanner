"""Verifier: the gate between the LLM and a dispatch (spec §3.2, P2).

`resolve` turns a relative `Constraint` into the absolute-time dict the solver
reads. `validate` returns a list of human-readable errors; an empty list means
the parse is safe to solve and dispatch.

The errors are written to be fed straight back to the LLM as retry feedback, so
each one names the offending value and, where there is a closed set, the valid
alternatives. Nothing here calls an LLM and nothing imports `rclpy`.
"""

from __future__ import annotations

from mvp.agents.schema import Constraint, ParseResult
from mvp.agents.solver import solve

# Per type: which fields must be non-null. Everything else is expected to be null.
REQUIRED: dict[str, tuple[str, ...]] = {
    "duration_change": ("task", "new_duration_min"),
    "start_after": ("task", "delay_min"),
    "resource_unavailable": ("resource",),      # outage_min null = until further notice
    "add_dependency": ("before", "after"),
}

# (field, low, high) ranges, inclusive. `outage_min` may also be null.
RANGES: tuple[tuple[str, int, int], ...] = (
    ("new_duration_min", 1, 600),
    ("delay_min", 0, 1440),
    ("outage_min", 1, 1440),
)

NO_CONSTRAINTS = (
    "No constraints extracted. If the message needs no schedule change or cannot "
    "be expressed, set unresolvable=true and explain."
)

FEASIBILITY_TIME_LIMIT_S = 2.0


def resolve(c: Constraint, t_now: int) -> dict:
    """Relative constraint -> absolute-time form used by the solver (§3.2)."""
    t_now = int(t_now)
    if c.type == "duration_change":
        return {"type": "duration_change", "task": c.task, "duration": int(c.new_duration_min)}
    if c.type == "start_after":
        return {"type": "start_after", "task": c.task, "earliest": t_now + int(c.delay_min)}
    if c.type == "resource_unavailable":
        until = None if c.outage_min is None else t_now + int(c.outage_min)
        return {"type": "resource_unavailable", "resource": c.resource, "until": until}
    if c.type == "add_dependency":
        return {"type": "add_dependency", "before": c.before, "after": c.after}
    raise ValueError(f"unknown constraint type {c.type!r}")


def _task_ids(c: Constraint) -> list[tuple[str, str]]:
    """(field name, value) pairs on this constraint that must be task IDs."""
    return [(f, getattr(c, f)) for f in ("task", "before", "after") if getattr(c, f) is not None]


def _cycle(edges: list[tuple[str, str]], nodes: set[str]) -> list[str] | None:
    """Return one cycle as a node list (first node repeated at the end), or None.

    Depth-first, returning the path rather than a bool: the error message has to
    show the cycle itself, because "there is a cycle" is useless as retry
    feedback. Recursion is safe here - the graph is the task list, 11 nodes deep
    at the very most.
    """
    succ: dict[str, list[str]] = {}
    for before, after in edges:
        succ.setdefault(before, []).append(after)

    exhausted: set[str] = set()      # fully explored, known to start no cycle

    def walk(node: str, path: list[str]) -> list[str] | None:
        if node in path:
            return path[path.index(node):] + [node]
        if node in exhausted or node not in nodes:   # unknown ID: check 3 reports it
            return None
        path.append(node)
        for nxt in succ.get(node, ()):
            found = walk(nxt, path)
            if found:
                return found
        path.pop()
        exhausted.add(node)
        return None

    for root in sorted(nodes):
        found = walk(root, [])
        if found:
            return found
    return None


def validate(parse: ParseResult, tasks_doc: dict, world: dict,
             active_constraints: list[dict] | None = None) -> list[str]:
    """Check a parse against the catalogue, the world and the solver. [] = valid.

    Every check runs and every error is collected, so one retry can fix all of
    them instead of one per round trip.
    """
    errors: list[str] = []
    tasks = {t["id"]: t for t in tasks_doc["tasks"]}
    resources = {r["id"]: r for r in tasks_doc["resources"]}
    completed = set(world.get("completed", []))
    ongoing = {o["task"] for o in world.get("ongoing", []) if o["task"] not in completed}
    t_now = int(world.get("t_now", 0))
    active = list(active_constraints or [])

    # 1. Nothing extracted, and not flagged as unresolvable.
    if not parse.constraints and not parse.unresolvable:
        return [NO_CONSTRAINTS]

    for i, c in enumerate(parse.constraints, start=1):
        where = f"constraint {i} ({c.type})"

        # 2. Required fields per type.
        missing = [f for f in REQUIRED.get(c.type, ()) if getattr(c, f) is None]
        if missing:
            errors.append(f"{where}: missing required field(s) {', '.join(missing)}.")

        # 3. Every ID exists.
        for field, value in _task_ids(c):
            if value not in tasks:
                errors.append(
                    f"{where}: unknown task '{value}' in '{field}'. "
                    f"Valid: {', '.join(tasks)}."
                )
        if c.resource is not None and c.resource not in resources:
            errors.append(
                f"{where}: unknown resource '{c.resource}'. Valid: {', '.join(resources)}."
            )

        # 4. Ranges.
        for field, low, high in RANGES:
            value = getattr(c, field)
            if value is not None and not (low <= value <= high):
                errors.append(f"{where}: {field}={value} is outside {low}..{high}.")

        # 5. Not about the past.
        if c.type in ("duration_change", "start_after") and c.task in completed:
            errors.append(
                f"{where}: {c.task} is already completed, so its schedule cannot change. "
                f"Set unresolvable=true if the message is about a completed task."
            )
        # The `after` task must still be pending: a task that is finished or
        # already running cannot be made to wait for anything.
        if c.type == "add_dependency" and c.after in completed | ongoing:
            state = "completed" if c.after in completed else "already running"
            errors.append(
                f"{where}: {c.after} is {state}, so it cannot be made to wait "
                f"for {c.before}."
            )

    # 6. No cycle, counting the declared preds and the constraints already active.
    new_deps = [
        (c.before, c.after) for c in parse.constraints
        if c.type == "add_dependency" and c.before and c.after
    ]
    if new_deps:
        edges = [(p, tid) for tid, t in tasks.items() for p in t["preds"]]
        edges += [(c["before"], c["after"]) for c in active if c["type"] == "add_dependency"]
        edges += new_deps
        cycle = _cycle(edges, set(tasks))
        if cycle:
            errors.append(
                f"add_dependency creates a dependency cycle: {' -> '.join(cycle)}."
            )

    # Later checks would crash or mislead on a parse that is already broken.
    if errors:
        return errors

    # 7. Feasibility, with the constraints that are already in force.
    try:
        resolved = [resolve(c, t_now) for c in parse.constraints]
        result = solve(tasks_doc, world, active + resolved,
                       time_limit_s=FEASIBILITY_TIME_LIMIT_S)
    except Exception as exc:                      # a malformed-but-typed parse
        return [f"Infeasible: solver failed ({exc})."]
    if result["status"] not in ("OPTIMAL", "FEASIBLE"):
        return [f"Infeasible: {result['reason']}."]

    return []
