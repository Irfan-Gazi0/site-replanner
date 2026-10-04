"""Verifier tests (spec P2.5).

One test per check the verifier is supposed to make, plus `resolve`'s absolute-time
conversion, which the solver and `active_constraints` both depend on.
"""

import json
import pathlib

from mvp.agents.schema import ParseResult, make_constraint as constraint
from mvp.agents.validate import NO_CONSTRAINTS, resolve, validate

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
TASKS = json.loads((DATA / "tasks.json").read_text())
SNAPSHOT = json.loads((DATA / "snapshot_t20.json").read_text())




def parse_of(*constraints, unresolvable=False, reason="test") -> ParseResult:
    return ParseResult(constraints=list(constraints), unresolvable=unresolvable, reason=reason)


def check(*constraints, **kw):
    return validate(parse_of(*constraints, **kw), TASKS, SNAPSHOT, [])


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #

def test_resolve_makes_times_absolute():
    assert resolve(constraint("start_after", task="T8", delay_min=120), 20) == {
        "type": "start_after", "task": "T8", "earliest": 140}
    assert resolve(constraint("resource_unavailable", resource="R3", outage_min=60), 20) == {
        "type": "resource_unavailable", "resource": "R3", "until": 80}
    assert resolve(constraint("resource_unavailable", resource="R2"), 20) == {
        "type": "resource_unavailable", "resource": "R2", "until": None}
    # A duration is already absolute; t_now must not leak into it.
    assert resolve(constraint("duration_change", task="T6", new_duration_min=50), 20) == {
        "type": "duration_change", "task": "T6", "duration": 50}


# --------------------------------------------------------------------------- #
# the checks, in spec order
# --------------------------------------------------------------------------- #

def test_empty_parse_without_unresolvable_is_an_error():
    assert check() == [NO_CONSTRAINTS]


def test_empty_parse_with_unresolvable_is_fine():
    assert check(unresolvable=True, reason="asks for a new task") == []


def test_missing_required_field_is_an_error():
    errors = check(constraint("start_after", task="T8"))
    assert any("delay_min" in e for e in errors)


def test_unknown_resource_lists_the_valid_ones():
    errors = check(constraint("resource_unavailable", resource="R5"))
    assert len(errors) == 1
    assert "R5" in errors[0] and "R1, R2, R3, C1" in errors[0]


def test_unknown_task_is_an_error():
    errors = check(constraint("start_after", task="T99", delay_min=30))
    assert any("T99" in e and "T1" in e for e in errors)


def test_out_of_range_duration_is_an_error():
    errors = check(constraint("duration_change", task="T6", new_duration_min=9000))
    assert any("9000" in e and "1..600" in e for e in errors)


def test_start_after_on_a_completed_task_is_an_error():
    errors = check(constraint("start_after", task="T1", delay_min=30))
    assert len(errors) == 1
    assert "T1" in errors[0] and "completed" in errors[0]


def test_duration_change_on_a_completed_task_is_an_error():
    errors = check(constraint("duration_change", task="T2", new_duration_min=40))
    assert any("completed" in e for e in errors)


def test_add_dependency_cycle_is_reported_with_the_cycle():
    errors = check(constraint("add_dependency", before="T11", after="T7"))
    assert len(errors) == 1
    assert "cycle" in errors[0]
    # T7 -> T9 -> T11 -> T7, however the walk enters it.
    for tid in ("T7", "T9", "T11"):
        assert tid in errors[0]


def test_infeasible_parse_is_rejected_with_the_reason():
    errors = check(constraint("resource_unavailable", resource="R3"))
    assert len(errors) == 1
    assert errors[0].startswith("Infeasible:")
    assert "drill" in errors[0]


def test_a_valid_start_after_passes():
    assert check(constraint("start_after", task="T8", delay_min=120)) == []


def test_a_valid_two_constraint_parse_passes():
    assert check(
        constraint("duration_change", task="T6", new_duration_min=50),
        constraint("resource_unavailable", resource="R1", outage_min=30),
    ) == []


def test_errors_are_collected_not_short_circuited():
    errors = check(
        constraint("start_after", task="T99", delay_min=9999),
        constraint("resource_unavailable", resource="R5"),
    )
    assert len(errors) >= 3                       # unknown task, range, unknown resource


def test_active_constraints_are_counted_for_feasibility():
    """Harmless on its own, infeasible on top of what is already in force."""
    alone = check(constraint("resource_unavailable", resource="R1"))
    assert alone == []

    active = [{"type": "resource_unavailable", "resource": "C1", "until": None}]
    errors = validate(parse_of(constraint("resource_unavailable", resource="R1")),
                      TASKS, SNAPSHOT, active)
    assert errors and errors[0].startswith("Infeasible:")
