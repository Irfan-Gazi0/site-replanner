"""P5 benchmark: cases, grading and aggregation. No API call, no ROS."""

from __future__ import annotations

import json
import pathlib

import pytest

from mvp.agents.schema import ParseResult, make_constraint
from mvp.eval import make_cases, report, run_eval

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return make_cases.build_cases(seed=7)


@pytest.fixture(scope="module")
def world() -> dict:
    return json.loads((DATA / "snapshot_t20.json").read_text())


def test_mix_is_6_8_10(cases):
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["difficulty"]] = counts.get(c["difficulty"], 0) + 1
    assert counts == {"easy": 6, "medium": 8, "hard": 10}
    assert len(cases) == 24
    assert len({c["id"] for c in cases}) == 24


def test_same_seed_same_cases(cases):
    assert make_cases.build_cases(seed=7) == cases


def test_no_fewshot_phrase_leaks(cases):
    assert make_cases.check_no_fewshot(cases) == []


def test_shipped_file_matches_the_generator(cases):
    """`mvp/data/cases.jsonl` is in git; it must be the seed-7 output."""
    shipped = [json.loads(line)
               for line in (DATA / "cases.jsonl").read_text().splitlines() if line.strip()]
    assert shipped == cases


def test_easy_cases_use_ids_only(cases):
    for c in [c for c in cases if c["difficulty"] == "easy"]:
        for gold in c["gold"]:
            for field in ("task", "before", "after", "resource"):
                ident = gold.get(field)
                if ident:
                    assert ident in c["text"], f"{c['id']}: {ident} not named in the text"


def test_hard_band_has_two_of_each_kind(cases):
    hard = [c for c in cases if c["difficulty"] == "hard"]
    assert len(hard) == 10
    assert sum(c["gold"] == [] for c in hard) == 4          # unknown ID + out of scope
    assert sum(c["expect"] == "escalate" for c in hard) == 8


def test_expect_comes_from_the_verifier(cases, world):
    """Recomputing `expect` from the gold must reproduce the file."""
    tasks_doc = json.loads((DATA / "tasks.json").read_text())
    for c in cases:
        assert c["expect"] == make_cases.expected_outcome(c["gold"], tasks_doc, world)


def test_plan_cases_are_feasible_and_escalate_cases_are_not(cases):
    assert all(c["expect"] in ("plan", "escalate") for c in cases)
    # The two infeasible hard cases are well-formed, so only the verifier's
    # feasibility check can reject them: a non-empty gold with expect=escalate.
    infeasible = [c for c in cases
                  if c["expect"] == "escalate" and c["gold"]
                  and c["gold"][0]["type"] == "resource_unavailable"]
    assert {c["gold"][0]["resource"] for c in infeasible} == {"R3", "C1"}


# --------------------------------------------------------------------------- #
# grading one run
# --------------------------------------------------------------------------- #

def _stub(constraints: list[dict], unresolvable: bool = False):
    """A parser that answers with fixed constraints, so grading can be tested free."""
    def parser(text, tasks_doc, world, feedback=None, previous=None):
        parse = ParseResult(
            constraints=[make_cases.gold_constraint(c) for c in constraints],
            unresolvable=unresolvable,
            reason="stub",
        )
        return parse, {"input": 10, "output": 5}
    return parser


def _case(cid: str, cases: list[dict]) -> dict:
    return next(c for c in cases if c["id"] == cid)


def test_gold_parse_is_graded_correct(cases, world, monkeypatch):
    case = _case("E01", cases)
    monkeypatch.setattr(run_eval.llm, "llm_parse", _stub(case["gold"]))
    row = run_eval.run_one(case, "validated", 1, world)
    assert row["outcome"] == "dispatched_correct"
    assert report.is_correct(row) and not report.is_unsafe(row)
    assert row["attempts"] == 1 and row["tokens"] == 15
    assert row["parses"] == [{"constraints": [dict(row["constraints"][0])],
                              "unresolvable": False, "reason": "stub"}]


def test_wrong_value_is_graded_wrong_not_correct(cases, world, monkeypatch):
    case = _case("E01", cases)
    wrong = [{**case["gold"][0], "delay_min": case["gold"][0]["delay_min"] + 5}]
    monkeypatch.setattr(run_eval.llm, "llm_parse", _stub(wrong))
    row = run_eval.run_one(case, "validated", 1, world)
    assert row["outcome"] == "dispatched_wrong"
    assert not report.is_correct(row) and report.is_unsafe(row)


def test_weakening_an_impossible_request_counts_as_unsafe(cases, world, monkeypatch):
    """H03 is infeasible. A parse that softens it into a short outage is a dispatch
    that should never have happened - the headline failure mode."""
    case = _case("H03", cases)
    softened = [{"type": "resource_unavailable", "resource": "R3", "outage_min": 30}]
    monkeypatch.setattr(run_eval.llm, "llm_parse", _stub(softened))
    row = run_eval.run_one(case, "single_shot", 1, world)
    assert row["status"] == "dispatched"               # it did dispatch
    assert row["outcome"] == "dispatched_wrong"        # on constraints that are not the gold
    assert not report.is_correct(row)                  # the case expected an escalation
    assert report.is_unsafe(row)


def test_unresolvable_escalates_and_is_correct_for_a_hard_case(cases, world, monkeypatch):
    case = _case("H01", cases)
    monkeypatch.setattr(run_eval.llm, "llm_parse", _stub([], unresolvable=True))
    row = run_eval.run_one(case, "validated", 1, world)
    assert row["outcome"] == "escalated"
    assert report.is_correct(row) and not report.is_unsafe(row)


def test_validated_arm_retries_against_the_verifier(cases, world, monkeypatch):
    """First answer names an unknown task, second is the gold: correct, 2 calls."""
    case = _case("E01", cases)
    calls = {"n": 0}

    def parser(text, tasks_doc, world_, feedback=None, previous=None):
        calls["n"] += 1
        if calls["n"] == 1:
            assert feedback is None
            bad = make_constraint("start_after", task="T99", delay_min=20)
            return ParseResult(constraints=[bad], unresolvable=False, reason="bad"), {}
        assert feedback and "T99" in feedback
        return _stub(case["gold"])(text, tasks_doc, world_, feedback, previous)

    monkeypatch.setattr(run_eval.llm, "llm_parse", parser)
    row = run_eval.run_one(case, "validated", 1, world)
    assert row["outcome"] == "dispatched_correct" and row["attempts"] == 2
    assert len(row["parses"]) == 2
    assert report.is_correct(row)


def test_single_shot_arm_never_validates(cases, world, monkeypatch):
    """The same bad parse the validated arm retries is dispatched by the baseline.

    Nothing checks it, and the solver silently ignores a constraint on a task ID
    that is not in the catalogue, so the run dispatches a plan that does not
    honour the manager's message at all. That is an unsafe dispatch, not an
    error - the distinction the benchmark exists to show.
    """
    case = _case("E01", cases)

    def parser(text, tasks_doc, world_, feedback=None, previous=None):
        bad = make_constraint("start_after", task="T99", delay_min=20)
        return ParseResult(constraints=[bad], unresolvable=False, reason="bad"), {}

    monkeypatch.setattr(run_eval.llm, "llm_parse", parser)
    row = run_eval.run_one(case, "single_shot", 1, world)
    assert row["attempts"] == 1
    assert row["outcome"] == "dispatched_wrong"
    assert report.is_unsafe(row)
    assert "validate" not in row["trace"]


def test_constraint_order_does_not_change_the_grade(cases, world, monkeypatch):
    case = _case("M01", cases)
    monkeypatch.setattr(run_eval.llm, "llm_parse", _stub(list(reversed(case["gold"]))))
    row = run_eval.run_one(case, "validated", 1, world)
    assert row["outcome"] == "dispatched_correct"


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #

def _row(**kw) -> dict:
    base = {"id": "E01", "difficulty": "easy", "expect": "plan", "arm": "validated",
            "repeat": 1, "outcome": "dispatched_correct", "attempts": 1,
            "latency_s": 1.0, "tokens": 100}
    return {**base, **kw}


def test_aggregate_percentages_and_repeat_range():
    rows = [
        _row(id="A", repeat=1), _row(id="B", repeat=1, outcome="escalated"),
        _row(id="A", repeat=2), _row(id="B", repeat=2),
    ]
    agg = report.aggregate(rows)
    arm = agg["overall"]["validated"]
    assert arm["n"] == 2 and arm["repeats"] == 2
    assert arm["correct_pct"] == {"mean": 75.0, "min": 50.0, "max": 100.0}
    assert arm["unsafe_pct"]["mean"] == 0.0
    assert agg["n_runs"] == 4


def test_recovered_by_retry_needs_a_correct_outcome():
    retried_right = _row(attempts=2)
    retried_wrong = _row(id="B", attempts=3, outcome="dispatched_wrong")
    agg = report.aggregate([retried_right, retried_wrong])
    arm = agg["overall"]["validated"]
    assert arm["recovered_pct"]["mean"] == 50.0
    assert arm["unsafe_pct"]["mean"] == 50.0
    assert arm["llm_calls_mean"]["mean"] == 2.5


def test_tables_render_both_arms():
    rows = [_row(arm="validated"), _row(id="B", arm="single_shot", outcome="escalated")]
    out = report.tables(report.aggregate(rows))
    assert "single-shot" in out and "validated loop" in out
    assert "unsafe dispatch" in out and "easy" in out


# --------------------------------------------------------------------------- #
# the two CLIs, end to end with a stubbed parser (still free)
# --------------------------------------------------------------------------- #

def test_run_eval_and_report_clis(tmp_path, cases, monkeypatch, capsys):
    """Both arms, both repeats, two cases: the files and the tables."""
    def parser(text, tasks_doc, world_, feedback=None, previous=None):
        gold = next(c["gold"] for c in cases if c["text"] == text)
        return _stub(gold)(text, tasks_doc, world_, feedback, previous)

    monkeypatch.setattr(run_eval.llm, "llm_parse", parser)
    out_dir = tmp_path / "eval"
    assert run_eval.main(["--limit", "2", "--repeats", "2", "--workers", "2",
                          "--out", str(out_dir)]) == 0

    rows = report.load_rows(out_dir / "raw.jsonl")
    assert len(rows) == 8                                    # 2 cases x 2 arms x 2 repeats
    assert all(r["outcome"] == "dispatched_correct" for r in rows)
    assert [r["id"] for r in rows] == ["E01"] * 4 + ["E02"] * 4   # file order is stable

    results = json.loads((out_dir / "results.json").read_text())
    assert results["model"] == run_eval.llm.MODEL
    assert results["n_cases"] == 2 and results["n_cases_in_file"] == 24
    assert len(results["cases_sha256"]) == 64
    assert results["overall"]["validated"]["correct_pct"]["mean"] == 100.0

    capsys.readouterr()
    assert report.main(["--raw", str(out_dir / "raw.jsonl"),
                        "--results", str(out_dir / "results.json")]) == 0
    printed = capsys.readouterr().out
    assert "unsafe dispatch" in printed and run_eval.llm.MODEL in printed


def test_report_cli_without_a_run_is_an_error(tmp_path, capsys):
    assert report.main(["--raw", str(tmp_path / "nope.jsonl")]) == 1
    assert "run_eval" in capsys.readouterr().out
