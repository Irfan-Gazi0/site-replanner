"""Oracle tests for the CP-SAT replanner.

One test per row of the oracle table in spec §3.1, plus a `check_plan` test on
the hand-written plan in `snapshot_t20.json`. The initial plan has ties, so
nothing here asserts an exact resource unless the table says it is forced -
which tie the solver picks is stable (see `test_repeated_solves_are_identical`)
but it is not part of the contract.
"""

import json
import pathlib

from mvp.agents.solver import check_plan, solve

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
TASKS = json.loads((DATA / "tasks.json").read_text())
SNAPSHOT = json.loads((DATA / "snapshot_t20.json").read_text())

EMPTY_WORLD = {"t_now": 0, "completed": [], "ongoing": [], "faulted": [], "prev_plan": []}

TIME_BUDGET_S = 2.0


def run(world, constraints):
    """Solve, then assert the result is self-consistent and fast enough."""
    result = solve(TASKS, world, constraints)
    assert result["solve_s"] < TIME_BUDGET_S, f"solve took {result['solve_s']}s"
    if result["status"] in ("OPTIMAL", "FEASIBLE"):
        assert check_plan(TASKS, world, constraints, result["assignments"]) == []
    return result


def test_initial_plan_from_empty_world():
    r = run(EMPTY_WORLD, [])
    assert r["status"] == "OPTIMAL"
    assert r["makespan"] == 175
    assert {a["task"] for a in r["assignments"]} == {f"T{i}" for i in range(1, 12)}


def test_r2_unavailable_indefinitely_moves_only_t8():
    cs = [{"type": "resource_unavailable", "resource": "R2", "until": None}]
    r = run(SNAPSHOT, cs)
    assert r["status"] == "OPTIMAL"
    assert r["makespan"] == 175

    by_task = {a["task"]: a["resource"] for a in r["assignments"]}
    assert "R2" not in by_task.values()
    assert by_task["T8"] == "R1"
    assert r["n_reassigned"] == 1, "only T8 should count as reassigned"
    for p in SNAPSHOT["prev_plan"]:
        if p["resource"] != "R2" and p["task"] in by_task:
            assert by_task[p["task"]] == p["resource"], f"{p['task']} was reassigned"


def test_r3_outage_until_80_does_not_move_the_ongoing_task():
    cs = [{"type": "resource_unavailable", "resource": "R3", "until": 80}]
    r = run(SNAPSHOT, cs)
    assert r["status"] == "OPTIMAL"
    assert r["makespan"] == 215

    by_task = {a["task"]: a for a in r["assignments"]}
    assert by_task["T3"]["resource"] == "R3" and by_task["T3"]["start"] == 0
    assert by_task["T4"]["resource"] == "R3" and by_task["T4"]["start"] >= 80


def test_r3_unavailable_indefinitely_is_infeasible():
    cs = [{"type": "resource_unavailable", "resource": "R3", "until": None}]
    r = solve(TASKS, SNAPSHOT, cs)
    assert r["status"] == "INFEASIBLE"
    assert "drill" in r["reason"] and "T4" in r["reason"]


def test_start_after_t8_at_140():
    cs = [{"type": "start_after", "task": "T8", "earliest": 140}]
    r = run(SNAPSHOT, cs)
    assert r["status"] == "OPTIMAL"
    assert r["makespan"] == 205
    assert next(a for a in r["assignments"] if a["task"] == "T8")["start"] >= 140


def test_crew_unavailable_indefinitely_is_infeasible():
    cs = [{"type": "resource_unavailable", "resource": "C1", "until": None}]
    r = solve(TASKS, SNAPSHOT, cs)
    assert r["status"] == "INFEASIBLE"
    assert "install" in r["reason"]


def test_repeated_solves_are_identical():
    """Same input, same plan - not just the same objective.

    The objective has ties (R1 and R2 are interchangeable transport robots), and
    with `num_workers = 8` whichever worker won the race broke the tie, so the
    same input produced different plans run to run. The solver now runs one
    worker; this is the regression test for that. It compares runs against each
    other rather than against a hard-coded resource map, because the tie itself
    is a solver-version detail - the guarantee is stability, not a given answer.
    """
    first = solve(TASKS, EMPTY_WORLD, [])
    for _ in range(4):
        again = solve(TASKS, EMPTY_WORLD, [])
        assert again["assignments"] == first["assignments"]
        assert again["makespan"] == first["makespan"]
        assert again["n_reassigned"] == first["n_reassigned"]


def test_check_plan_accepts_the_hand_written_snapshot_plan():
    assert check_plan(TASKS, EMPTY_WORLD, [], SNAPSHOT["prev_plan"]) == []
