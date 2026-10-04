# Model structure adapted from google/or-tools examples/python/flexible_job_shop_sat.py (Apache-2.0)
"""CP-SAT replanner (spec §3.1, P1).

Two entry points:

- `solve(tasks_doc, world, constraints)` builds and solves the scheduling model.
- `check_plan(tasks_doc, world, constraints, assignments)` independently audits a
  plan and returns the violations it finds. Nothing in here calls an LLM, and
  nothing imports `rclpy`.

The OR-Tools example gives every (task, machine) alternative its own start and
end because durations are machine-dependent. Ours are not, so each task keeps a
single `start`/`end` pair and the alternatives are optional intervals that share
them.
"""

from __future__ import annotations

import time

from ortools.sat.python import cp_model

# Objective weights: makespan first, then churn against the previous plan, then
# a nudge away from spending the (scarce, multi-skilled) crew on haulage.
W_MAKESPAN = 1000
W_REASSIGN = 10
W_CREW_TRANSPORT = 1

TRANSPORT = "transport"


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def _index(tasks_doc: dict, world: dict, constraints: list[dict]) -> dict:
    """Pull the pieces every function here needs out of the three inputs."""
    tasks = {t["id"]: t for t in tasks_doc["tasks"]}
    resources = {r["id"]: r for r in tasks_doc["resources"]}

    completed = set(world.get("completed", []))
    faulted = set(world.get("faulted", []))
    ongoing = {o["task"]: o for o in world.get("ongoing", []) if o["task"] not in completed}
    prev = {p["task"]: p for p in world.get("prev_plan", [])}
    t_now = int(world.get("t_now", 0))

    # Resolved constraints (§3.2), bucketed by type.
    durations = {t: tasks[t]["duration"] for t in tasks}
    earliest: dict[str, int] = {}
    unavailable: dict[str, int | None] = {}   # resource -> until (None = forever)
    extra_deps: list[tuple[str, str]] = []    # (before, after)

    for c in constraints or []:
        kind = c["type"]
        if kind == "duration_change":
            durations[c["task"]] = int(c["duration"])
        elif kind == "start_after":
            earliest[c["task"]] = max(earliest.get(c["task"], 0), int(c["earliest"]))
        elif kind == "resource_unavailable":
            res, until = c["resource"], c.get("until")
            # Once a resource is out "forever" (None) nothing shortens it;
            # otherwise the latest outage for that resource wins.
            if not (res in unavailable and unavailable[res] is None):
                unavailable[res] = None if until is None else int(until)
        elif kind == "add_dependency":
            extra_deps.append((c["before"], c["after"]))

    # A resource is off the table only for an outage with no end; a timed outage
    # just pushes starts later. Ongoing tasks are exempt (see `eligible`).
    blocked = {r for r, until in unavailable.items() if until is None} | faulted

    open_tasks = [t["id"] for t in tasks_doc["tasks"] if t["id"] not in completed]

    def eligible(tid: str) -> list[str]:
        if tid in ongoing:
            # Frozen: a resource_unavailable constraint never moves an ongoing
            # task (§3.1). Only a twin fault does, and that reverts it first.
            return [ongoing[tid]["resource"]]
        cap = tasks[tid]["capability"]
        return [
            rid for rid, r in resources.items()
            if cap in r["capabilities"] and rid not in blocked
        ]

    return {
        "tasks": tasks, "resources": resources, "completed": completed,
        "faulted": faulted, "ongoing": ongoing, "prev": prev, "t_now": t_now,
        "durations": durations, "earliest": earliest, "unavailable": unavailable,
        "extra_deps": extra_deps, "open_tasks": open_tasks, "eligible": eligible,
    }


def _preds(ix: dict, tid: str) -> list[str]:
    """Declared predecessors plus any add_dependency edges, completed ones dropped."""
    out = list(ix["tasks"][tid]["preds"])
    out += [b for b, a in ix["extra_deps"] if a == tid]
    return [p for p in out if p not in ix["completed"]]


# --------------------------------------------------------------------------- #
# solve
# --------------------------------------------------------------------------- #

def solve(tasks_doc: dict, world: dict, constraints: list[dict],
          time_limit_s: float = 5.0) -> dict:
    t0 = time.perf_counter()
    ix = _index(tasks_doc, world, constraints)
    tasks, t_now = ix["tasks"], ix["t_now"]
    dur, ongoing, prev = ix["durations"], ix["ongoing"], ix["prev"]

    def done(status: str, reason: str, makespan=None, assignments=None, n_reassigned=0) -> dict:
        return {
            "status": status, "makespan": makespan,
            "assignments": assignments or [], "n_reassigned": n_reassigned,
            "solve_s": round(time.perf_counter() - t0, 4), "reason": reason,
        }

    # Fail fast, before building a model, when a capability has gone missing.
    stranded: dict[str, list[str]] = {}
    for tid in ix["open_tasks"]:
        if not ix["eligible"](tid):
            stranded.setdefault(tasks[tid]["capability"], []).append(tid)
    if stranded:
        cap, ids = next(iter(stranded.items()))
        return done("INFEASIBLE",
                    f"no available resource has capability '{cap}' "
                    f"(needed by {', '.join(ids)})")

    # Horizon: now, plus the latest hard date any constraint names, plus every
    # remaining minute of work laid end to end.
    bounds = list(ix["earliest"].values())
    bounds += [u for u in ix["unavailable"].values() if u is not None]
    horizon = t_now + max(bounds, default=0) + sum(dur[t] for t in ix["open_tasks"])

    m = cp_model.CpModel()
    start, end, x, lower = {}, {}, {}, {}

    for tid in ix["open_tasks"]:
        if tid in ongoing:
            s = int(ongoing[tid]["start"])
            # An ongoing task keeps running; give it at least one more minute so
            # the twin and the dependents never see it finish in the past.
            e = max(s + dur[tid], t_now + 1)
            start[tid] = m.new_int_var(s, s, f"start_{tid}")
            end[tid] = m.new_int_var(e, e, f"end_{tid}")
        else:
            lo = max(t_now, ix["earliest"].get(tid, t_now))
            lower[tid] = lo
            start[tid] = m.new_int_var(lo, horizon, f"start_{tid}")
            end[tid] = m.new_int_var(lo + dur[tid], horizon, f"end_{tid}")
            m.add(end[tid] == start[tid] + dur[tid])

    per_resource: dict[str, list] = {rid: [] for rid in ix["resources"]}
    for tid in ix["open_tasks"]:
        lits = []
        for rid in ix["eligible"](tid):
            lit = m.new_bool_var(f"x_{tid}_{rid}")
            x[tid, rid] = lit
            lits.append(lit)
            per_resource[rid].append(
                m.new_optional_interval_var(start[tid], dur[tid], end[tid], lit,
                                            f"iv_{tid}_{rid}")
            )
        m.add_exactly_one(lits)

    for rid, ivs in per_resource.items():
        if len(ivs) > 1:
            m.add_no_overlap(ivs)

    for tid in ix["open_tasks"]:
        for p in _preds(ix, tid):
            m.add(start[tid] >= end[p])

    # start_after is already in the variable's lower bound; state it anyway so a
    # frozen task that violates it shows up as INFEASIBLE rather than silently.
    for tid, earliest in ix["earliest"].items():
        if tid in start:
            m.add(start[tid] >= earliest)

    # A timed outage delays whatever is scheduled on that resource.
    for rid, until in ix["unavailable"].items():
        if until is None:
            continue
        for tid in ix["open_tasks"]:
            if tid in ongoing:
                continue
            if (tid, rid) in x:
                m.add(start[tid] >= until).only_enforce_if(x[tid, rid])

    makespan = m.new_int_var(0, horizon, "makespan")
    m.add_max_equality(makespan, [end[t] for t in ix["open_tasks"]])

    # Churn penalty: a task that was in the previous plan and is not on its old
    # resource any more. A task whose old resource is gone has no term at all -
    # the move is forced, so penalising it would only bias the other choices.
    reassign = [
        1 - x[tid, prev[tid]["resource"]]
        for tid in ix["open_tasks"]
        if tid in prev and tid not in ongoing and (tid, prev[tid]["resource"]) in x
    ]

    crew_haulage = [
        lit for (tid, rid), lit in x.items()
        if tasks[tid]["capability"] == TRANSPORT
        and ix["resources"][rid]["kind"] == "crew"
    ]

    m.minimize(W_MAKESPAN * makespan
               + W_REASSIGN * sum(reassign)
               + W_CREW_TRANSPORT * sum(crew_haulage))

    # Warm start from the previous plan, so a small disruption stays small.
    for tid in ix["open_tasks"]:
        if tid in prev and tid not in ongoing:
            old = prev[tid]["resource"]
            if (tid, old) in x:
                m.add_hint(x[tid, old], 1)
            if prev[tid]["start"] >= lower[tid]:
                m.add_hint(start[tid], prev[tid]["start"])

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = 8
    status = solver.solve(m)
    name = solver.status_name(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return done(name, f"solver returned {name} after {time_limit_s}s")

    assignments = []
    for tid in ix["open_tasks"]:
        rid = next(r for r in ix["eligible"](tid) if solver.boolean_value(x[tid, r]))
        assignments.append({
            "task": tid, "resource": rid,
            "start": solver.value(start[tid]), "end": solver.value(end[tid]),
            "zone": tasks[tid]["zone"], "preds": list(tasks[tid]["preds"]),
        })
    assignments.sort(key=lambda a: (a["start"], a["task"]))

    # Reported churn counts every actual move, forced or not.
    moved = sum(
        1 for a in assignments
        if a["task"] in prev and a["task"] not in ongoing
        and a["resource"] != prev[a["task"]]["resource"]
    )
    mk = solver.value(makespan)
    return done(name, f"{name.lower()} plan, makespan {mk}, {moved} task(s) reassigned",
                makespan=mk, assignments=assignments, n_reassigned=moved)


# --------------------------------------------------------------------------- #
# check_plan
# --------------------------------------------------------------------------- #

def check_plan(tasks_doc: dict, world: dict, constraints: list[dict],
               assignments: list[dict]) -> list[str]:
    """Audit a plan against the world and the constraints. [] means valid.

    Deliberately independent of the model above: this is what the graph trusts
    before dispatching, so it re-derives everything from the inputs.
    """
    ix = _index(tasks_doc, world, constraints)
    errors: list[str] = []
    plan = {a["task"]: a for a in assignments}

    missing = [t for t in ix["open_tasks"] if t not in plan]
    if missing:
        errors.append(f"plan is missing non-completed task(s): {', '.join(missing)}")
    extra = [t for t in plan if t in ix["completed"]]
    if extra:
        errors.append(f"plan schedules completed task(s): {', '.join(extra)}")

    for tid, a in plan.items():
        if tid not in ix["tasks"]:
            errors.append(f"{tid}: unknown task ID")
            continue
        rid = a["resource"]
        res = ix["resources"].get(rid)
        if res is None:
            errors.append(f"{tid}: unknown resource '{rid}'")
            continue

        want = ix["durations"][tid]
        got = a["end"] - a["start"]
        frozen = tid in ix["ongoing"]
        if got != want and not frozen:
            errors.append(f"{tid}: duration is {got} min, expected {want}")

        cap = ix["tasks"][tid]["capability"]
        if cap not in res["capabilities"]:
            errors.append(f"{tid}: {rid} has no '{cap}' capability")

        if frozen:
            o = ix["ongoing"][tid]
            if rid != o["resource"] or a["start"] != int(o["start"]):
                errors.append(
                    f"{tid}: ongoing task moved - plan has {rid}@{a['start']}, "
                    f"world has {o['resource']}@{o['start']}"
                )
        else:
            if a["start"] < ix["t_now"]:
                errors.append(f"{tid}: starts at {a['start']}, before t_now={ix['t_now']}")
            until = ix["unavailable"].get(rid, 0)
            if rid in ix["faulted"]:
                errors.append(f"{tid}: assigned to faulted resource {rid}")
            elif until is None:
                errors.append(f"{tid}: assigned to {rid}, unavailable until further notice")
            elif until and a["start"] < until:
                errors.append(f"{tid}: starts at {a['start']} on {rid}, unavailable until {until}")

        earliest = ix["earliest"].get(tid)
        if earliest is not None and a["start"] < earliest:
            errors.append(f"{tid}: starts at {a['start']}, earliest allowed is {earliest}")

        for p in _preds(ix, tid):
            if p not in plan:
                errors.append(f"{tid}: predecessor {p} is neither completed nor planned")
            elif plan[p]["end"] > a["start"]:
                errors.append(
                    f"{tid}: starts at {a['start']} but predecessor {p} ends at {plan[p]['end']}"
                )

    by_resource: dict[str, list[dict]] = {}
    for a in plan.values():
        by_resource.setdefault(a["resource"], []).append(a)
    for rid, items in by_resource.items():
        items.sort(key=lambda a: (a["start"], a["task"]))
        for prev_a, next_a in zip(items, items[1:]):
            if next_a["start"] < prev_a["end"]:
                errors.append(
                    f"{rid}: {prev_a['task']} ({prev_a['start']}-{prev_a['end']}) overlaps "
                    f"{next_a['task']} ({next_a['start']}-{next_a['end']})"
                )

    return errors
