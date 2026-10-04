"""Graph tests (spec P2.5).

The parser is faked everywhere except the one `live` test, so the retry loop,
the escalation paths and the approval interrupt are all exercised for free. The
fake also counts its calls, which is how the twin-event tests prove the LLM is
never reached.
"""

import json
import pathlib

import pytest
from langgraph.types import Command

from mvp.agents.graph import build_graph, initial_state
from mvp.agents.schema import ParseResult, make_constraint as constraint

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
TASKS = json.loads((DATA / "tasks.json").read_text())
SNAPSHOT = json.loads((DATA / "snapshot_t20.json").read_text())


GOOD = ParseResult(
    constraints=[constraint("start_after", task="T8", delay_min=120)],
    unresolvable=False, reason="T8 waits two hours",
)
BAD_ID = ParseResult(
    constraints=[constraint("start_after", task="T99", delay_min=120)],
    unresolvable=False, reason="T99 waits two hours",
)
UNRESOLVABLE = ParseResult(constraints=[], unresolvable=True,
                           reason="asks for a task that does not exist")


class FakeParser:
    """Returns scripted parses in order, and remembers how it was called."""

    def __init__(self, *results, usage=None):
        self.results = list(results)
        self.calls = 0
        self.feedbacks: list[str | None] = []
        self.usage = usage or {"input": 100, "output": 20}

    def __call__(self, text, tasks_doc, world, feedback=None, previous=None):
        self.calls += 1
        self.feedbacks.append(feedback)
        # The last scripted result repeats, so "always bad" needs one entry.
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        return result, dict(self.usage)


def run(graph, event, world=SNAPSHOT, active=None, auto_approve=True, tid="t"):
    state = initial_state(event, world, active, auto_approve=auto_approve)
    state["tasks_doc"] = TASKS
    return graph.invoke(state, {"configurable": {"thread_id": tid}})


def narrative(text="Rack delivery to zone B is delayed by 2 hours"):
    return {"source": "narrative", "text": text}


def fault(resource):
    return {"source": "twin", "type": "resource_fault", "resource": resource, "t": 20}


# --------------------------------------------------------------------------- #
# 1-3: the retry loop
# --------------------------------------------------------------------------- #

def test_correct_parse_dispatches_on_the_first_attempt():
    parser = FakeParser(GOOD)
    out = run(build_graph(parser=parser, auto_approve=True), narrative())

    assert out["status"] == "dispatched"
    assert out["attempts"] == 1
    assert parser.calls == 1
    assert out["plan"]["makespan"] == 205
    assert out["diff"]["makespan_old"] == 175
    assert out["diff"]["makespan_new"] == 205
    assert out["tokens"] == 120


def test_bad_id_then_correct_parse_dispatches_on_the_second_attempt():
    parser = FakeParser(BAD_ID, GOOD)
    out = run(build_graph(parser=parser, auto_approve=True), narrative())

    assert out["status"] == "dispatched"
    assert out["attempts"] == 2
    assert parser.calls == 2
    # The retry has to carry the verifier's complaint, or it is just a re-roll.
    assert parser.feedbacks[0] is None
    assert "T99" in parser.feedbacks[1]


def test_always_bad_escalates_after_three_attempts():
    parser = FakeParser(BAD_ID)
    out = run(build_graph(parser=parser, auto_approve=True), narrative())

    assert out["status"] == "escalated"
    assert out["attempts"] == 3
    assert parser.calls == 3
    assert out["plan"] is None
    assert "T99" in out["reason"]


def test_unresolvable_escalates_without_a_retry():
    parser = FakeParser(UNRESOLVABLE)
    out = run(build_graph(parser=parser, auto_approve=True), narrative())

    assert out["status"] == "escalated"
    assert parser.calls == 1
    assert "unresolvable" in out["reason"]


# --------------------------------------------------------------------------- #
# 4-5: twin events never reach the LLM
# --------------------------------------------------------------------------- #

def test_twin_r2_fault_dispatches_without_calling_the_parser():
    parser = FakeParser(GOOD)
    out = run(build_graph(parser=parser, auto_approve=True), fault("R2"))

    assert out["status"] == "dispatched"
    assert parser.calls == 0, "a twin event must never go through the LLM"
    assert out["tokens"] == 0
    assert out["plan"]["makespan"] == 175
    assert all(a["resource"] != "R2" for a in out["plan"]["assignments"])


def test_twin_r3_fault_escalates_without_calling_the_parser():
    parser = FakeParser(GOOD)
    out = run(build_graph(parser=parser, auto_approve=True), fault("R3"))

    assert out["status"] == "escalated"
    assert parser.calls == 0
    assert out["attempts"] == 0, "a twin event gets no retries"
    assert "drill" in out["reason"]


# --------------------------------------------------------------------------- #
# 6: the approval interrupt
# --------------------------------------------------------------------------- #

def test_approval_interrupt_then_yes_dispatches():
    graph = build_graph(parser=FakeParser(GOOD), auto_approve=False)
    cfg = {"configurable": {"thread_id": "approve-yes"}}
    state = initial_state(narrative(), SNAPSHOT, auto_approve=False)
    state["tasks_doc"] = TASKS

    out = graph.invoke(state, cfg)
    assert out.get("__interrupt__"), "should pause for approval"
    payload = out["__interrupt__"][0].value
    assert payload["diff"]["makespan_new"] == 205

    out = graph.invoke(Command(resume="y"), cfg)
    assert out["status"] == "dispatched"


def test_approval_interrupt_then_no_rejects():
    graph = build_graph(parser=FakeParser(GOOD), auto_approve=False)
    cfg = {"configurable": {"thread_id": "approve-no"}}
    state = initial_state(narrative(), SNAPSHOT, auto_approve=False)
    state["tasks_doc"] = TASKS

    out = graph.invoke(state, cfg)
    assert out.get("__interrupt__")

    out = graph.invoke(Command(resume="n"), cfg)
    assert out["status"] == "rejected"
    assert out["plan"] is not None, "the plan was built, the operator declined it"


def test_approve_node_does_not_re_solve_on_resume():
    """The LLM and the solver must stay out of the node LangGraph re-runs."""
    parser = FakeParser(GOOD)
    graph = build_graph(parser=parser, auto_approve=False)
    cfg = {"configurable": {"thread_id": "approve-once"}}
    state = initial_state(narrative(), SNAPSHOT, auto_approve=False)
    state["tasks_doc"] = TASKS

    graph.invoke(state, cfg)
    graph.invoke(Command(resume="y"), cfg)
    assert parser.calls == 1, "resuming must not re-run the parser"


# --------------------------------------------------------------------------- #
# dispatch side effects and the single-shot arm
# --------------------------------------------------------------------------- #

def test_dispatch_appends_the_resolved_constraints():
    out = run(build_graph(parser=FakeParser(GOOD), auto_approve=True), narrative())
    assert out["active_constraints"] == [
        {"type": "start_after", "task": "T8", "earliest": 140}
    ]


def test_single_shot_dispatches_a_bad_parse_that_still_solves():
    """The baseline has no verifier, so a wrong-but-solvable parse goes through."""
    wrong = ParseResult(
        constraints=[constraint("start_after", task="T8", delay_min=10)],
        unresolvable=False, reason="T8 waits ten minutes",
    )
    parser = FakeParser(wrong)
    out = run(build_graph(parser=parser, validated=False, auto_approve=True), narrative())

    assert out["status"] == "dispatched"
    assert parser.calls == 1
    assert out["plan"]["makespan"] == 175      # the delay was swallowed


def test_single_shot_dispatches_a_parse_naming_an_unknown_task():
    """The unsafe dispatch the benchmark is built to count.

    `start_after T99` cannot bind to anything, so the solver silently ignores it
    and returns the unchanged plan. The baseline dispatches that as a success;
    the validated arm rejects the same parse on the unknown ID.
    """
    parser = FakeParser(BAD_ID)
    out = run(build_graph(parser=parser, validated=False, auto_approve=True), narrative())

    assert out["status"] == "dispatched"
    assert parser.calls == 1, "the baseline has no retry"
    assert out["plan"]["makespan"] == 175, "the manager's delay never took effect"


def test_single_shot_errors_on_a_malformed_parse():
    """No verifier means `resolve` is where a missing field finally bites."""
    malformed = ParseResult(
        constraints=[constraint("start_after", task="T8")],      # no delay_min
        unresolvable=False, reason="T8 is delayed",
    )
    parser = FakeParser(malformed)
    out = run(build_graph(parser=parser, validated=False, auto_approve=True), narrative())

    assert out["status"] == "error"
    assert parser.calls == 1, "the baseline has no retry"
    assert out["plan"] is None
    assert "solve failed" in out["reason"]


def test_validated_arm_catches_the_parse_the_baseline_swallows():
    """Same bad parse, both arms: this is the benchmark's whole claim."""
    parser = FakeParser(BAD_ID)
    out = run(build_graph(parser=parser, validated=True, auto_approve=True), narrative())

    assert out["status"] == "escalated"
    assert "T99" in out["reason"]


def test_single_shot_still_keeps_twin_events_away_from_the_llm():
    parser = FakeParser(GOOD)
    out = run(build_graph(parser=parser, validated=False, auto_approve=True), fault("R2"))

    assert out["status"] == "dispatched"
    assert parser.calls == 0


def test_parser_failure_is_retried_then_escalates():
    """A refusal or an API error returns None; that is an attempt, not a crash."""
    class Failing(FakeParser):
        def __call__(self, *a, **kw):
            self.calls += 1
            return None, {"input": 0, "output": 0, "error": "refused"}

    parser = Failing()
    out = run(build_graph(parser=parser, auto_approve=True), narrative())
    assert out["status"] == "escalated"
    assert parser.calls == 3


# --------------------------------------------------------------------------- #
# live: the real parser, one paid call
# --------------------------------------------------------------------------- #

@pytest.mark.live
def test_live_narrative_parses_to_start_after_t8_120():
    from mvp.agents.llm import llm_parse

    graph = build_graph(parser=llm_parse, auto_approve=True)
    out = run(graph, narrative("Rack delivery to zone B is delayed by 2 hours"),
              auto_approve=True, tid="live")

    assert out["status"] == "dispatched", out["reason"]
    normalised = {
        (c.type, c.task, c.delay_min) for c in out["proposed"].constraints
    }
    assert normalised == {("start_after", "T8", 120)}
    assert out["plan"]["makespan"] == 205
    assert out["tokens"] > 0
