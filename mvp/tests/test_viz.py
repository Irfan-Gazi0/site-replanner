"""Gantt rendering (spec P6.1). No API call, no ROS."""

from __future__ import annotations

from mvp.viz import gantt


def plan(plan_id: int, t_now: int, assignments: list[tuple[str, str, int, int, str]]) -> dict:
    return {
        "plan_id": plan_id,
        "event": {"source": "initial"} if plan_id == 1 else
                 {"type": "resource_fault", "resource": "R2"},
        "world": {"t_now": t_now},
        "makespan": max(a[3] for a in assignments),
        "assignments": [
            {"task": t, "resource": r, "start": s, "end": e, "zone": z, "preds": []}
            for t, r, s, e, z in assignments
        ],
    }


BEFORE = plan(1, 0, [("T1", "R2", 0, 15, "A"), ("T2", "R2", 15, 30, "B"),
                     ("T11", "C1", 30, 45, "HALL")])
AFTER = plan(2, 12, [("T2", "R1", 12, 27, "B"), ("T1", "R1", 27, 42, "A"),
                     ("T11", "C1", 42, 57, "HALL")])


def test_reassigned_lists_only_tasks_that_changed_resource():
    assert gantt.reassigned(BEFORE, AFTER) == {"T1", "T2"}


def test_a_task_absent_from_the_earlier_plan_is_not_a_reassignment():
    added = plan(2, 12, [("T1", "R1", 12, 27, "A"), ("T3", "R3", 0, 30, "B")])
    assert gantt.reassigned(BEFORE, added) == {"T1"}


def test_resource_rows_put_robots_before_crew():
    assert gantt.resource_order([BEFORE, AFTER]) == ["R1", "R2", "C1"]


def test_render_writes_a_png(tmp_path):
    out = gantt.render([BEFORE, AFTER], tmp_path / "sub" / "replan.png")
    assert out.exists() and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_main_reports_a_missing_plan_file(tmp_path, capsys):
    assert gantt.main([str(tmp_path / "nope.json"), "-o", str(tmp_path / "x.png")]) == 1
    assert "not found" in capsys.readouterr().out
