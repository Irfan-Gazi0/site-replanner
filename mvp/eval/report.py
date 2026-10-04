"""P5 tables: single-shot vs. validated loop (spec P5.3, P5.4).

Every number here comes from `runs/eval/raw.jsonl` alone - no API call - so a
finished run can be regraded offline after a metric definition changes.
`aggregate` is the one place the metrics are defined; `run_eval.py` calls it to
write `results.json`, so the file and these tables can never disagree.

Metric definitions (spec P5.3):

- **correct**: `expect="plan"` and the arm dispatched the gold constraints, or
  `expect="escalate"` and the arm escalated.
- **unsafe**: a plan was dispatched that should not have been - either the case
  expected an escalation, or the constraints differ from the gold. This is the
  headline number: it counts the runs where the retry loop "fixed" an
  impossible request by quietly weakening it.
- **recovered**: a correct outcome that needed more than one LLM call. Only the
  validated arm can score here; the baseline has nothing to retry against.

Means are over the repeats, with the min-max range across repeats in brackets.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics

ARM_LABELS = {"single_shot": "single-shot", "validated": "validated loop"}
DIFFICULTIES = ("easy", "medium", "hard")


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

def is_correct(row: dict) -> bool:
    if row["expect"] == "plan":
        return row["outcome"] == "dispatched_correct"
    return row["outcome"] == "escalated"


def is_unsafe(row: dict) -> bool:
    """A dispatch that should not have happened, or happened with wrong constraints."""
    if row["outcome"] == "dispatched_wrong":
        return True
    return row["outcome"] == "dispatched_correct" and row["expect"] == "escalate"


def _pct(hits: int, total: int) -> float:
    return 100.0 * hits / total if total else 0.0


def _repeat_metrics(rows: list[dict]) -> dict:
    """The metrics for one arm within one repeat."""
    n = len(rows)
    correct = [r for r in rows if is_correct(r)]
    return {
        "n": n,
        "correct_pct": _pct(len(correct), n),
        "unsafe_pct": _pct(sum(is_unsafe(r) for r in rows), n),
        "escalated_pct": _pct(sum(r["outcome"] == "escalated" for r in rows), n),
        "error_pct": _pct(sum(r["outcome"] == "error" for r in rows), n),
        "recovered_pct": _pct(sum(r["attempts"] > 1 for r in correct), n),
        "llm_calls_mean": statistics.fmean([r["attempts"] for r in rows]) if n else 0.0,
        "latency_median_s": statistics.median([r["latency_s"] for r in rows]) if n else 0.0,
        "tokens_mean": statistics.fmean([r["tokens"] for r in rows]) if n else 0.0,
    }


def _over_repeats(per_repeat: list[dict]) -> dict:
    """{metric: {"mean": m, "min": lo, "max": hi}} across the repeats."""
    keys = [k for k in per_repeat[0] if k != "n"]
    return {
        "n": per_repeat[0]["n"],
        "repeats": len(per_repeat),
        **{k: {"mean": statistics.fmean([m[k] for m in per_repeat]),
               "min": min(m[k] for m in per_repeat),
               "max": max(m[k] for m in per_repeat)}
           for k in keys},
    }


def aggregate(rows: list[dict]) -> dict:
    """Raw rows -> the numbers behind both tables. The single definition of the metrics."""
    arms = sorted({r["arm"] for r in rows})
    repeats = sorted({r["repeat"] for r in rows})

    overall: dict[str, dict] = {}
    by_difficulty: dict[str, dict] = {}
    for arm in arms:
        arm_rows = [r for r in rows if r["arm"] == arm]
        overall[arm] = _over_repeats(
            [_repeat_metrics([r for r in arm_rows if r["repeat"] == rep]) for rep in repeats]
        )
        by_difficulty[arm] = {
            d: _over_repeats([
                _repeat_metrics([r for r in arm_rows
                                 if r["repeat"] == rep and r["difficulty"] == d])
                for rep in repeats
            ])
            for d in DIFFICULTIES
            if any(r["difficulty"] == d for r in arm_rows)
        }

    return {
        "arms": arms,
        "repeats": len(repeats),
        "n_runs": len(rows),
        "overall": overall,
        "by_difficulty": by_difficulty,
    }


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #

def _cell(stat: dict, unit: str = "%", decimals: int = 1) -> str:
    mean, lo, hi = stat["mean"], stat["min"], stat["max"]
    body = f"{mean:.{decimals}f}{unit}"
    if abs(hi - lo) < 10 ** -decimals / 2:
        return body
    return f"{body} ({lo:.{decimals}f}-{hi:.{decimals}f})"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def tables(agg: dict) -> str:
    """The two Markdown tables of P5.4, as one string."""
    out: list[str] = []

    header = ["arm", "correct", "unsafe dispatch", "escalated", "error",
              "recovered by retry", "LLM calls", "median latency", "tokens"]
    rows = []
    for arm in agg["arms"]:
        m = agg["overall"][arm]
        rows.append([
            ARM_LABELS.get(arm, arm),
            _cell(m["correct_pct"]),
            _cell(m["unsafe_pct"]),
            _cell(m["escalated_pct"]),
            _cell(m["error_pct"]),
            _cell(m["recovered_pct"]),
            _cell(m["llm_calls_mean"], unit="", decimals=2),
            _cell(m["latency_median_s"], unit=" s", decimals=2),
            _cell(m["tokens_mean"], unit="", decimals=0),
        ])
    first = agg["overall"][agg["arms"][0]]
    out.append(f"**{first['n']} cases x {agg['repeats']} repeats x "
               f"{len(agg['arms'])} arms = {agg['n_runs']} runs.** "
               "Mean over the repeats, min-max in brackets.")
    out.append("")
    out.append(_table(header, rows))

    out.append("")
    out.append("Correct outcome by difficulty:")
    out.append("")
    diffs = [d for d in DIFFICULTIES if d in agg["by_difficulty"][agg["arms"][0]]]
    rows = [[ARM_LABELS.get(arm, arm)]
            + [_cell(agg["by_difficulty"][arm][d]["correct_pct"]) for d in diffs]
            for arm in agg["arms"]]
    out.append(_table(["arm"] + list(diffs), rows))
    return "\n".join(out)


def load_rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the P5 tables from raw.jsonl (free, offline).")
    ap.add_argument("--raw", default="runs/eval/raw.jsonl")
    ap.add_argument("--results", default="runs/eval/results.json",
                    help="read the run's provenance (cases SHA, model) from here if present")
    args = ap.parse_args(argv)

    raw = pathlib.Path(args.raw)
    if not raw.exists():
        print(f"{raw} not found. Run: python -m mvp.eval.run_eval")
        return 1

    agg = aggregate(load_rows(raw))
    results = pathlib.Path(args.results)
    if results.exists():
        meta = json.loads(results.read_text())
        print(f"model: {meta.get('model')}  cases: {meta.get('cases')} "
              f"(sha256 {str(meta.get('cases_sha256'))[:12]})")
        print()
    print(tables(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
