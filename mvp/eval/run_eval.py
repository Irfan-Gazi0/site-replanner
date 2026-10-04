"""P5 benchmark runner: single-shot vs. validated loop (spec P5.2).

One run is (case, arm, repeat). It goes through the same `build_graph` the live
bridge uses, on `snapshot_t20.json`, with `auto_approve=True` - so the only
difference between the arms is the verifier, which is the thing being measured.
No ROS, no Unity, no `rclpy`.

Every raw field needed to regrade offline is written to `runs/eval/raw.jsonl`:
the outcome, the final constraints, the LLM calls, the wall clock, the tokens,
and the raw parse of each attempt in order. `results.json` holds the same
aggregation the report prints, plus the SHA-256 of `cases.jsonl` and the model
ID, so a table can always be traced back to the exact cases and model it came
from.

**PAID.** Each run makes at least one API call. Use `--limit 5` first: the
summary extrapolates the tokens for the full matrix (spec: stop and ask the
human if the full run would exceed $1).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import pathlib
import time

from mvp.agents import llm
from mvp.agents.graph import build_graph, initial_state, tasks_doc
from mvp.agents.schema import Constraint
from mvp.eval.make_cases import gold_constraint
from mvp.eval.report import aggregate, tables

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
ARMS = ("single_shot", "validated")


def _normalise(constraints: list[Constraint]) -> list[dict]:
    """Constraints as a sorted list of full field dicts, for comparison.

    Sorted, so the gold and the parse compare equal regardless of the order the
    model happened to emit them in. Every field is present (null where the type
    does not use it), which is what makes a gold slot dict and a parsed
    constraint directly comparable.
    """
    return sorted((c.model_dump() for c in constraints),
                  key=lambda d: json.dumps(d, sort_keys=True))


def _recording_parser(sink: list[dict]):
    """`llm_parse` with every attempt appended to `sink`, in order.

    A wrapper rather than a new state field: the graph's state is a frozen
    contract shared with the bridge, and the benchmark has no business adding to
    it. A failed call records its error instead of a parse, so a run that ends
    in `error` still shows what the model said.
    """
    def parser(*args, **kwargs):
        parse, usage = llm.llm_parse(*args, **kwargs)
        sink.append(parse.model_dump() if parse is not None
                    else {"error": usage.get("error", "no parse")})
        return parse, usage
    return parser


def run_one(case: dict, arm: str, repeat: int, world: dict) -> dict:
    """One (case, arm, repeat). Never raises: a crash is recorded as an error row."""
    parses: list[dict] = []
    graph = build_graph(parser=_recording_parser(parses),
                        validated=(arm == "validated"), auto_approve=True)
    event = {"source": "narrative", "text": case["text"]}
    cfg = {"configurable": {"thread_id": f"{case['id']}-{arm}-{repeat}"}}

    started = time.perf_counter()
    try:
        out = graph.invoke(initial_state(event, world, auto_approve=True), cfg)
    except Exception as exc:                     # pragma: no cover - defensive
        out = {"status": "error", "reason": f"{type(exc).__name__}: {exc}",
               "attempts": len(parses), "tokens": 0, "trace": []}
    latency = time.perf_counter() - started

    parse = out.get("proposed")
    got = _normalise(parse.constraints) if parse is not None else []
    gold = _normalise([gold_constraint(g) for g in case["gold"]])

    status = out.get("status")
    if status == "dispatched":
        outcome = "dispatched_correct" if got == gold else "dispatched_wrong"
    elif status == "escalated":
        outcome = "escalated"
    else:
        outcome = "error"                        # "error", and "rejected" cannot happen here

    return {
        "id": case["id"], "difficulty": case["difficulty"], "expect": case["expect"],
        "arm": arm, "repeat": repeat,
        "outcome": outcome, "status": status,
        "constraints": got, "gold": gold,
        "attempts": int(out.get("attempts", 0)),
        "latency_s": round(latency, 3),
        "tokens": int(out.get("tokens", 0)),
        "reason": out.get("reason", ""),
        "trace": out.get("trace", []),
        "parses": parses,
    }


def load_cases(path: pathlib.Path, limit: int | None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return cases[:limit] if limit else cases


def _cost_summary(rows: list[dict], n_cases_total: int, n_arms: int, repeats: int,
                  price_per_mtok: float | None) -> str:
    tokens = sum(r["tokens"] for r in rows)
    calls = sum(r["attempts"] for r in rows)
    full_runs = n_cases_total * n_arms * repeats
    scale = full_runs / len(rows) if rows else 0
    lines = [f"{len(rows)} runs, {calls} LLM calls, {tokens} tokens."]
    if len(rows) != full_runs:
        lines.append(f"Full matrix is {full_runs} runs: about {int(tokens * scale)} "
                     f"tokens and {int(calls * scale)} calls.")
    if price_per_mtok:
        lines.append(f"At ${price_per_mtok:.2f}/Mtok blended: this run "
                     f"${tokens / 1e6 * price_per_mtok:.4f}, "
                     f"full matrix about ${tokens * scale / 1e6 * price_per_mtok:.4f}.")
    else:
        lines.append("No price given (--price-per-mtok), so no dollar estimate.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the P5 benchmark (PAID: one or more API calls per run)."
    )
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, help="first N cases only; use 5 to price the full run")
    ap.add_argument("--cases", default="mvp/data/cases.jsonl")
    ap.add_argument("--out", default="runs/eval")
    ap.add_argument("--price-per-mtok", type=float,
                    help="blended $ per million tokens, for the cost line")
    args = ap.parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        ap.error(f"unknown arm(s) {', '.join(unknown)}; valid: {', '.join(ARMS)}")

    cases_path = pathlib.Path(args.cases)
    if not cases_path.exists():
        print(f"{cases_path} not found. Run: python -m mvp.eval.make_cases")
        return 1
    all_cases = load_cases(cases_path, None)
    cases = all_cases[:args.limit] if args.limit else all_cases
    world = json.loads((DATA / "snapshot_t20.json").read_text())
    tasks_doc()                                  # read the catalogue once, before the threads

    jobs = [(c, arm, rep) for c in cases for arm in arms
            for rep in range(1, args.repeats + 1)]
    print(f"{len(jobs)} runs: {len(cases)} cases x {len(arms)} arms x "
          f"{args.repeats} repeats, {args.workers} workers, model {llm.MODEL}")

    rows: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, c, arm, rep, world): (c["id"], arm, rep)
                   for c, arm, rep in jobs}
        for done in concurrent.futures.as_completed(futures):
            row = done.result()
            rows.append(row)
            print(f"  {row['id']:>4} {row['arm']:<12} r{row['repeat']} "
                  f"{row['outcome']:<18} {row['attempts']} call(s) "
                  f"{row['latency_s']:.2f}s {row['tokens']} tok")

    # Deterministic file order, whatever order the threads finished in.
    order = {cid: i for i, cid in enumerate(c["id"] for c in all_cases)}
    rows.sort(key=lambda r: (order[r["id"]], r["arm"], r["repeat"]))

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw.jsonl"
    raw_path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    results = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "model": llm.MODEL,
        "cases": str(cases_path),
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "n_cases": len(cases),
        "n_cases_in_file": len(all_cases),
        "arms": arms,
        "repeats": args.repeats,
        "tokens_total": sum(r["tokens"] for r in rows),
        "llm_calls_total": sum(r["attempts"] for r in rows),
        **aggregate(rows),
    }
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    print()
    print(_cost_summary(rows, len(all_cases), len(arms), args.repeats,
                        args.price_per_mtok))
    print()
    print(tables(aggregate(rows)))
    print()
    print(f"wrote {raw_path} and {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
