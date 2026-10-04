"""LangGraph replanning loop (spec P2).

Two arms, built from the same nodes so the P5 benchmark compares like with like:

- `validated=True`: intake -> (translate | parse) -> validate -> solve -> approve
  -> dispatch, with up to 3 parse attempts fed by the verifier's errors.
- `validated=False`: intake -> parse -> solve -> approve -> dispatch. No verifier,
  no retry. This is the baseline the benchmark measures against.

Two invariants this file must keep:

- The LLM never writes the schedule. `parse` only produces constraints; `solve`
  is the only place a plan comes from.
- A twin event never goes through the LLM. `translate` turns it into a
  constraint in plain code, and a twin event that fails validation escalates
  immediately - there is nobody to re-prompt.

The graph never touches ROS; the bridge (P3) publishes what it returns.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Annotated, Any, Callable, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from mvp.agents.llm import llm_parse
from mvp.agents.schema import ParseResult, make_constraint
from mvp.agents.solver import solve as solve_plan
from mvp.agents.validate import resolve, validate as validate_parse

MAX_ATTEMPTS = 3
SOLVE_TIME_LIMIT_S = 5.0

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"


def _accumulate(existing: list | None, new: list | None) -> list:
    """Reducer for `trace`: nodes append to it instead of overwriting."""
    return (existing or []) + (new or [])


class ReplanState(TypedDict, total=False):
    """Spec P2.4, plus three fields the spec's list leaves implicit.

    `resolved` carries the absolute-time constraints from `solve` to `dispatch`
    so `dispatch` stays trivial; `auto_approve` is read by the `approve` node;
    `tasks_doc` lets a test pin its own catalogue. `proposed` is typed `Any`
    because LangGraph resolves these annotations at build time and a pydantic
    model in a channel buys nothing here.
    """

    event: dict                      # {"source": "twin"|"narrative", ...}
    world: dict
    tasks_doc: dict
    active_constraints: list[dict]
    resolved: list[dict]
    proposed: Any                    # ParseResult | None
    errors: list[str]
    attempts: int
    plan: dict | None
    diff: dict | None
    status: str
    reason: str
    tokens: int
    auto_approve: bool
    trace: Annotated[list[str], _accumulate]


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #

def _intake(state: ReplanState) -> dict:
    source = (state.get("event") or {}).get("source", "narrative")
    return {
        "trace": ["intake"],
        "status": "replanning",
        "reason": f"intake: {source} event",
        "errors": [],
    }


def _translate(state: ReplanState) -> dict:
    """Twin event -> constraint, in code. No LLM on this path, ever."""
    event = state["event"]
    kind = event.get("type")
    if kind != "resource_fault":
        return {
            "trace": ["translate"],
            "errors": [f"unsupported twin event type '{kind}'"],
            "reason": f"unsupported twin event type '{kind}'",
        }

    resource = event["resource"]
    parse = ParseResult(
        constraints=[make_constraint("resource_unavailable", resource=resource)],
        unresolvable=False,
        reason=f"{resource} faulted in the twin: unavailable until further notice",
    )
    return {
        "trace": ["translate"],
        "proposed": parse,
        "reason": parse.reason,
    }


def _make_parse_node(parser: Callable[..., tuple[ParseResult | None, dict]]):
    def _parse(state: ReplanState) -> dict:
        attempts = state.get("attempts", 0) + 1
        errors = state.get("errors") or []
        feedback = "\n".join(f"- {e}" for e in errors) if errors else None

        parse, usage = parser(
            state["event"].get("text", ""),
            _catalogue(state),
            state["world"],
            feedback,
            state.get("proposed"),
        )
        tokens = state.get("tokens", 0) + int(usage.get("input", 0)) + int(usage.get("output", 0))

        if parse is None:
            why = usage.get("error", "parser returned nothing")
            return {
                "trace": ["parse"], "attempts": attempts, "tokens": tokens,
                "proposed": None, "errors": [f"Parser failed: {why}"],
                "reason": f"parser failed: {why}",
            }
        return {
            "trace": ["parse"], "attempts": attempts, "tokens": tokens,
            "proposed": parse, "errors": [], "reason": parse.reason,
        }
    return _parse


def _validate(state: ReplanState) -> dict:
    parse = state.get("proposed")
    if parse is None:
        return {"trace": ["validate"], "errors": state.get("errors") or ["no parse to validate"]}

    errors = validate_parse(parse, _catalogue(state), state["world"],
                            state.get("active_constraints") or [])
    reason = parse.reason if not errors else f"{len(errors)} validation error(s)"
    return {"trace": ["validate"], "errors": errors, "reason": reason}


def _solve(state: ReplanState) -> dict:
    """The only source of a plan. Also computes the diff against `prev_plan`."""
    parse = state["proposed"]
    world = state["world"]
    active = state.get("active_constraints") or []
    t_now = int(world.get("t_now", 0))

    try:
        resolved = [resolve(c, t_now) for c in parse.constraints]
        result = solve_plan(_catalogue(state), world, active + resolved,
                            time_limit_s=SOLVE_TIME_LIMIT_S)
    except Exception as exc:
        return {
            "trace": ["solve"], "status": "error", "plan": None, "diff": None,
            "reason": f"solve failed: {type(exc).__name__}: {exc}",
        }

    if result["status"] not in ("OPTIMAL", "FEASIBLE"):
        # Reachable in the single-shot arm, which has no feasibility check.
        return {
            "trace": ["solve"], "status": "error", "plan": None, "diff": None,
            "reason": f"no plan: {result['reason']}",
        }

    return {
        "trace": ["solve"], "plan": result, "diff": _diff(world, result),
        "resolved": resolved, "reason": result["reason"],
    }


def _make_approve_node(auto_approve: bool):
    """Own node, nothing expensive in it: LangGraph re-runs it on resume.

    Either the compiled graph or the state may switch approval off, so the
    bridge can set it once and a single run can still override it.
    """
    def _approve(state: ReplanState) -> dict:
        if auto_approve or state.get("auto_approve"):
            return {"trace": ["approve"], "status": "approved"}

        answer = interrupt({"diff": state.get("diff"), "reason": state.get("reason", "")})
        if str(answer).strip().lower() in ("y", "yes"):
            return {"trace": ["approve"], "status": "approved"}
        return {"trace": ["approve"], "status": "rejected",
                "reason": "rejected by the operator"}
    return _approve


def _dispatch(state: ReplanState) -> dict:
    """Accept the plan and make its constraints part of the world's standing set."""
    active = list(state.get("active_constraints") or [])
    active += state.get("resolved") or []
    return {
        "trace": ["dispatch"], "status": "dispatched", "active_constraints": active,
        "reason": state.get("reason", "dispatched"),
    }


def _rejected(state: ReplanState) -> dict:
    return {"trace": ["rejected"], "status": "rejected"}


def _escalate(state: ReplanState) -> dict:
    """Terminal hand-off to a human.

    A solve that failed outright keeps `status="error"` (spec P2.4): the run
    reached a human either way, but the benchmark has to tell "the model could
    not be built" apart from "the request was understood and refused".
    """
    if state.get("status") == "error":
        return {"trace": ["escalate"]}

    parse = state.get("proposed")
    errors = state.get("errors") or []
    if parse is not None and parse.unresolvable:
        reason = f"unresolvable: {parse.reason}"
    elif errors:
        reason = "; ".join(errors)
    else:
        reason = state.get("reason", "escalated")
    return {"trace": ["escalate"], "status": "escalated", "reason": reason}


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #

def _after_intake(state: ReplanState) -> Literal["translate", "parse"]:
    return "translate" if (state.get("event") or {}).get("source") == "twin" else "parse"


def _after_translate(state: ReplanState) -> Literal["validate", "escalate"]:
    return "escalate" if state.get("errors") else "validate"


def _after_parse(state: ReplanState) -> Literal["validate", "escalate"]:
    parse = state.get("proposed")
    if parse is None:
        # A failed call is still an attempt; retry while there are any left.
        return "validate" if state.get("attempts", 0) < MAX_ATTEMPTS else "escalate"
    return "escalate" if parse.unresolvable else "validate"


def _after_parse_single_shot(state: ReplanState) -> Literal["solve", "escalate"]:
    parse = state.get("proposed")
    if parse is None:
        return "escalate"
    return "escalate" if parse.unresolvable else "solve"


def _after_validate(state: ReplanState) -> Literal["solve", "parse", "escalate"]:
    if not state.get("errors"):
        return "solve"
    if (state.get("event") or {}).get("source") == "twin":
        return "escalate"                       # no retries for twin events
    return "parse" if state.get("attempts", 0) < MAX_ATTEMPTS else "escalate"


def _after_solve(state: ReplanState) -> Literal["approve", "escalate"]:
    return "approve" if state.get("plan") else "escalate"


def _after_approve(state: ReplanState) -> Literal["dispatch", "rejected"]:
    return "dispatch" if state.get("status") == "approved" else "rejected"


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #

def _diff(world: dict, result: dict) -> dict:
    """Which tasks moved, and what the makespan did (spec P2.4)."""
    prev = {p["task"]: p for p in world.get("prev_plan", [])}
    changes = []
    for a in result["assignments"]:
        old = prev.get(a["task"])
        if old is None:
            changes.append(f"{a['task']}: new -> {a['resource']}, start {a['start']}")
            continue
        if old["resource"] != a["resource"] or int(old["start"]) != a["start"]:
            changes.append(
                f"{a['task']}: {old['resource']}->{a['resource']}, "
                f"{old['start']}->{a['start']}"
            )
    return {
        "changes": changes,
        "makespan_old": max((int(p["end"]) for p in prev.values()), default=None),
        "makespan_new": result["makespan"],
        "n_reassigned": result["n_reassigned"],
    }


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

# The catalogue is the same for every run; held module-level so a node does not
# re-read it from disk on every LangGraph step (and on every interrupt resume).
_TASKS_DOC: dict | None = None


def tasks_doc() -> dict:
    global _TASKS_DOC
    if _TASKS_DOC is None:
        _TASKS_DOC = json.loads((DATA / "tasks.json").read_text())
    return _TASKS_DOC


def _catalogue(state: ReplanState) -> dict:
    """A test may pin its own catalogue in the state; otherwise use tasks.json."""
    return state.get("tasks_doc") or tasks_doc()


def build_graph(parser: Callable[..., tuple[ParseResult | None, dict]] = llm_parse,
                validated: bool = True,
                auto_approve: bool = False):
    """Compile the replanning graph. `validated=False` is the single-shot baseline."""
    builder = StateGraph(ReplanState)
    builder.add_node("intake", _intake)
    builder.add_node("parse", _make_parse_node(parser))
    builder.add_node("solve", _solve)
    builder.add_node("approve", _make_approve_node(auto_approve))
    builder.add_node("dispatch", _dispatch)
    builder.add_node("rejected", _rejected)
    builder.add_node("escalate", _escalate)

    # Shared by both arms: a twin event is translated in code either way, so the
    # baseline cannot accidentally send one to the LLM.
    builder.add_node("translate", _translate)
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", _after_intake,
                                  {"translate": "translate", "parse": "parse"})

    # The arms differ in one thing only: whether `validate` sits between the
    # proposal and the solver. Without it there is also nothing to retry against.
    if validated:
        builder.add_node("validate", _validate)
        builder.add_conditional_edges("translate", _after_translate,
                                      {"validate": "validate", "escalate": "escalate"})
        builder.add_conditional_edges("parse", _after_parse,
                                      {"validate": "validate", "escalate": "escalate"})
        builder.add_conditional_edges("validate", _after_validate,
                                      {"solve": "solve", "parse": "parse",
                                       "escalate": "escalate"})
    else:
        builder.add_conditional_edges("translate", _after_translate,
                                      {"validate": "solve", "escalate": "escalate"})
        builder.add_conditional_edges("parse", _after_parse_single_shot,
                                      {"solve": "solve", "escalate": "escalate"})

    builder.add_conditional_edges("solve", _after_solve,
                                  {"approve": "approve", "escalate": "escalate"})
    builder.add_conditional_edges("approve", _after_approve,
                                  {"dispatch": "dispatch", "rejected": "rejected"})
    builder.add_edge("dispatch", END)
    builder.add_edge("rejected", END)
    builder.add_edge("escalate", END)

    return builder.compile(checkpointer=InMemorySaver())


def initial_state(event: dict, world: dict, active_constraints: list[dict] | None = None,
                  auto_approve: bool = False) -> dict:
    return {
        "event": event, "world": world,
        "active_constraints": list(active_constraints or []),
        "proposed": None, "errors": [], "attempts": 0,
        "plan": None, "diff": None, "status": "replanning", "reason": "",
        "tokens": 0, "trace": [], "auto_approve": auto_approve,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run one replan on snapshot_t20.json. --narrative costs an API call."
    )
    ap.add_argument("--narrative", help="manager message to parse with the LLM")
    ap.add_argument("--fault", help="simulate a twin resource fault instead, e.g. R2")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--single-shot", action="store_true",
                    help="no verifier, no retry (the P5 baseline)")
    args = ap.parse_args(argv)

    if not args.narrative and not args.fault:
        ap.error("give --narrative or --fault")

    world = json.loads((DATA / "snapshot_t20.json").read_text())
    if args.fault:
        event = {"source": "twin", "type": "resource_fault",
                 "resource": args.fault, "t": world["t_now"]}
    else:
        event = {"source": "narrative", "text": args.narrative}

    graph = build_graph(validated=not args.single_shot, auto_approve=args.auto_approve)
    cfg = {"configurable": {"thread_id": "cli"}}
    out = graph.invoke(initial_state(event, world, auto_approve=args.auto_approve), cfg)

    while out.get("__interrupt__"):
        payload = out["__interrupt__"][0].value
        print(json.dumps(payload, indent=2))
        answer = input("dispatch this plan? [y/N] ")
        out = graph.invoke(Command(resume=answer), cfg)

    parse = out.get("proposed")
    print(json.dumps({
        "constraints": [] if parse is None else [c.model_dump() for c in parse.constraints],
        "diff": out.get("diff"),
        "status": out.get("status"),
        "reason": out.get("reason"),
        "attempts": out.get("attempts"),
        "tokens": out.get("tokens"),
        "trace": out.get("trace"),
    }, indent=2, default=str))
    return 0 if out.get("status") == "dispatched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
