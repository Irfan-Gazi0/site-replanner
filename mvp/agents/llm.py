"""LLM intake: manager message -> `ParseResult` (spec P2, prompt in Appendix A).

This is the only file in the project that talks to OpenAI. It extracts
constraints and nothing else: the schedule is the solver's job, and the verifier
decides whether a parse is usable. A retry is a fresh single-turn call carrying
the previous answer and the verifier's errors, not a continued conversation -
the model never gets to see its own rejected attempt as "context" it may defend.

No `rclpy` import, and no ROS concept anywhere in here.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib

from openai import OpenAI

from mvp.agents.schema import ParseResult

MODEL = os.environ.get("PARSER_MODEL", "gpt-6-luna")
MAX_OUTPUT_TOKENS = 4000

# Static, so the provider can cache it (Appendix A, verbatim).
SYSTEM_PROMPT = """\
You convert a construction site manager's message into schedule constraints for a data center fit-out.
A separate optimizer builds the schedule. You never write the schedule yourself.

Constraint types. Fill only the fields listed for the type and set every other field to null.
- duration_change: task, new_duration_min = the task's NEW TOTAL duration in minutes.
  "takes 20 minutes longer" means current duration + 20.
- start_after: task, delay_min = minutes from now before the task may start. "delayed 2 hours" = 120.
- resource_unavailable: resource, outage_min = minutes until it is back; null if "until further notice".
- add_dependency: before, after. The `after` task may not start until the `before` task is finished.

Rules:
1. Use only IDs from the catalogue. Map descriptions ("the zone B racks", "the drilling robot") to IDs using it.
2. Set unresolvable=true and constraints=[] if the message names a task or resource not in the catalogue,
   asks for something these four types cannot express (for example a new task), or concerns a completed task.
3. Do not add constraints the message does not state.
4. If validator errors are included, fix exactly what they point to. Never drop or weaken a constraint the
   message clearly states just to make the plan feasible. If the request is impossible, set unresolvable=true.

Examples:
- "Cable tray install in zone B will take 50 minutes." ->
  [{"type":"duration_change","task":"T6","new_duration_min":50, all other fields null}]
- "Cargo robot 1 needs a 30 minute battery swap now, and zone A rack setting has to wait for the zone B racks to arrive." ->
  [{"type":"resource_unavailable","resource":"R1","outage_min":30,...}, {"type":"add_dependency","before":"T8","after":"T9",...}]
- "Bring in a scissor lift and add a lighting install task in zone A." -> unresolvable=true, constraints=[]\
"""

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Built on first use, so importing this module needs no API key."""
    global _client
    if _client is None:
        _client = OpenAI()                      # reads OPENAI_API_KEY
    return _client


def _task_status(tasks_doc: dict, world: dict) -> dict[str, tuple[str, str]]:
    """task ID -> (status, assigned resource or '-'), derived from the world."""
    completed = set(world.get("completed", []))
    ongoing = {o["task"]: o["resource"] for o in world.get("ongoing", [])}
    prev = {p["task"]: p["resource"] for p in world.get("prev_plan", [])}

    out: dict[str, tuple[str, str]] = {}
    for t in tasks_doc["tasks"]:
        tid = t["id"]
        if tid in completed:
            status = "completed"
        elif tid in ongoing:
            status = "ongoing"
        else:
            status = "pending"
        out[tid] = (status, ongoing.get(tid) or prev.get(tid) or "-")
    return out


def _resource_status(tasks_doc: dict, world: dict) -> dict[str, str]:
    faulted = set(world.get("faulted", []))
    busy = {o["resource"] for o in world.get("ongoing", [])}
    return {
        r["id"]: "fault" if r["id"] in faulted else "busy" if r["id"] in busy else "idle"
        for r in tasks_doc["resources"]
    }


def build_user_msg(text: str, tasks_doc: dict, world: dict,
                   feedback: str | None = None,
                   previous: ParseResult | None = None) -> str:
    """The catalogue plus the message, formatted as in Appendix A.

    The catalogue is rendered from `tasks.json` rather than hard-coded, so the
    prompt cannot drift away from the data the solver uses.
    """
    statuses = _task_status(tasks_doc, world)
    res_status = _resource_status(tasks_doc, world)

    lines = [f"Current time: t={int(world.get('t_now', 0))} min", ""]
    lines.append("Tasks (id | name | zone | capability | duration_min | status | assigned):")
    for t in tasks_doc["tasks"]:
        status, assigned = statuses[t["id"]]
        lines.append(
            f"{t['id']} | {t['name']} | {t['zone']} | {t['capability']} | "
            f"{t['duration']} | {status} | {assigned}"
        )
    lines.append("")
    lines.append("Resources (id | name | capabilities | status):")
    for r in tasks_doc["resources"]:
        lines.append(
            f"{r['id']} | {r['name']} | {', '.join(r['capabilities'])} | {res_status[r['id']]}"
        )
    lines.append("")
    lines.append(f'Manager message: "{text}"')

    if feedback:
        lines.append("")
        if previous is not None:
            lines.append(f"Your previous answer: {previous.model_dump_json()}")
        lines.append("The validator rejected it:")
        lines.append(feedback)

    return "\n".join(lines)


def llm_parse(text: str, tasks_doc: dict, world: dict,
              feedback: str | None = None,
              previous: ParseResult | None = None) -> tuple[ParseResult | None, dict]:
    """Parse one message. Returns (result or None, usage).

    `None` means the call did not produce a usable answer - a refusal, a length
    cut-off, or an API error. The caller treats that as a failed attempt; it is
    never confused with a successful parse that happens to be empty.
    """
    usage = {"input": 0, "output": 0}
    user_msg = build_user_msg(text, tasks_doc, world, feedback, previous)

    try:
        resp = _get_client().responses.parse(
            model=MODEL,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            instructions=SYSTEM_PROMPT,
            input=[{"role": "user", "content": user_msg}],
            text_format=ParseResult,
        )
    except Exception as exc:
        usage["error"] = f"{type(exc).__name__}: {exc}"
        return None, usage

    if getattr(resp, "usage", None) is not None:
        # `output_tokens` includes reasoning tokens.
        usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}

    # Structured outputs guarantee the shape, not that a call succeeded: check
    # the status before reading the parse, because a refusal leaves it None.
    if resp.status != "completed":
        usage["error"] = f"response status {resp.status}"
        return None, usage
    if resp.output_parsed is None:
        usage["error"] = "model refused or returned no parsable output"
        return None, usage

    return resp.output_parsed, usage


def main(argv: list[str] | None = None) -> int:
    """`python -m mvp.agents.llm "message"` - one paid call, prints the parse."""
    ap = argparse.ArgumentParser(description="Parse one manager message (PAID: calls the API).")
    ap.add_argument("text")
    args = ap.parse_args(argv)

    data = pathlib.Path(__file__).resolve().parents[1] / "data"
    tasks_doc = json.loads((data / "tasks.json").read_text())
    world = json.loads((data / "snapshot_t20.json").read_text())

    parse, usage = llm_parse(args.text, tasks_doc, world)
    print(json.dumps({
        "parse": None if parse is None else parse.model_dump(),
        "usage": usage,
    }, indent=2))
    return 0 if parse is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
