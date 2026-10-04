"""Before/after Gantt charts for a replan (spec P6.1).

    python -m mvp.viz.gantt runs/plans/plan_001.json runs/plans/plan_002.json \
        -o docs/replan_r2.png

Rows are resources, bars are tasks coloured by zone. In the second panel a task
whose resource changed against the first plan gets a red outline, so the picture
of a replan is the picture of the churn the solver chose to accept. A dashed
vertical line marks `t_now`, the instant the plan was made: everything left of
it is history the solver froze.

Reads the plan files `bridge_node.py` writes (`runs/plans/plan_NNN.json`) and
nothing else - no solver, no LLM, no `rclpy`.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import matplotlib
matplotlib.use("Agg")           # no display in CI or over SSH
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

# Zones come from tasks.json (§3.1). Fixed colours, so two panels - and two
# runs of this script - always paint the same zone the same way.
ZONE_COLOURS = {
    "A": "#4c72b0",
    "B": "#dd8452",
    "HALL": "#55a868",
    "LAYDOWN": "#937860",
}
OTHER_ZONE = "#8c8c8c"
REASSIGNED = "#c44e52"

BAR_HEIGHT = 0.6


def assignments(plans: list[dict]) -> list[dict]:
    """Every assignment in every plan given, in file order."""
    return [a for plan in plans for a in plan["assignments"]]


def resource_order(plans: list[dict]) -> list[str]:
    """Resource rows over every plan given: robots (R*) first, then crew, by ID.

    The plan files carry no `kind`, so the ID prefix is the only signal here.
    Row order is otherwise arbitrary, and R-first matches tasks.json (§3.1).
    """
    seen = {a["resource"] for a in assignments(plans)}
    return sorted(seen, key=lambda r: (not r.startswith("R"), r))


def reassigned(before: dict, after: dict) -> set[str]:
    """Tasks that changed resource between the two plans.

    A task missing from the earlier plan is not a reassignment: it was never
    assigned anywhere, so there is no churn to show.
    """
    old = {a["task"]: a["resource"] for a in before["assignments"]}
    return {a["task"] for a in after["assignments"]
            if a["task"] in old and old[a["task"]] != a["resource"]}


def _title(plan: dict) -> str:
    event = plan.get("event") or {}
    source = event.get("type") or event.get("source") or "plan"
    resource = event.get("resource")
    if resource:
        source = f"{source} {resource}"
    return (f"plan {plan['plan_id']}: {source}  "
            f"(t_now={int(plan['world']['t_now'])}, makespan={plan['makespan']})")


def draw_panel(ax, plan: dict, rows: list[str], outlined: set[str]) -> None:
    for assignment in plan["assignments"]:
        y = rows.index(assignment["resource"])
        start, end = assignment["start"], assignment["end"]
        is_reassigned = assignment["task"] in outlined
        ax.broken_barh(
            [(start, end - start)], (y - BAR_HEIGHT / 2, BAR_HEIGHT),
            facecolors=ZONE_COLOURS.get(assignment["zone"], OTHER_ZONE),
            edgecolors=REASSIGNED if is_reassigned else "white",
            linewidth=2.0 if is_reassigned else 0.8,
        )
        ax.text((start + end) / 2, y, assignment["task"],
                ha="center", va="center", color="white", fontsize=8, fontweight="bold")

    t_now = int(plan["world"]["t_now"])
    ax.axvline(t_now, color="black", linestyle="--", linewidth=1.0)
    ax.annotate("t_now", (t_now, 1.0), xycoords=("data", "axes fraction"), fontsize=7,
                xytext=(3, -10), textcoords="offset points")

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.invert_yaxis()
    ax.set_title(_title(plan), fontsize=10, loc="left")
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    ax.set_axisbelow(True)


def render(plans: list[dict], out: pathlib.Path) -> pathlib.Path:
    """Draw one stacked panel per plan and write the PNG."""
    rows = resource_order(plans)
    horizon = max(a["end"] for a in assignments(plans))

    fig, axes = plt.subplots(len(plans), 1, sharex=True,
                             figsize=(10, 1.1 + 1.0 * len(rows) * len(plans)))
    if len(plans) == 1:
        axes = [axes]

    previous: dict | None = None
    for ax, plan in zip(axes, plans):
        draw_panel(ax, plan, rows, reassigned(previous, plan) if previous else set())
        previous = plan

    axes[-1].set_xlim(0, horizon + 5)
    axes[-1].set_xlabel("sim minutes")

    drawn = {a["zone"] for a in assignments(plans)}
    handles = [mpatches.Patch(facecolor=colour, label=f"zone {zone}")
               for zone, colour in ZONE_COLOURS.items() if zone in drawn]
    handles.append(mpatches.Patch(facecolor="white", edgecolor=REASSIGNED,
                                  linewidth=2.0, label="reassigned"))
    fig.legend(handles=handles, loc="lower center", ncol=len(handles),
               frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0.06, 1, 1))

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stacked before/after Gantt charts (free, offline).")
    ap.add_argument("plans", nargs="+", help="plan JSON files, oldest first")
    ap.add_argument("-o", "--out", default="docs/replan_r2.png")
    args = ap.parse_args(argv)

    plans = []
    for name in args.plans:
        path = pathlib.Path(name)
        if not path.exists():
            print(f"{path} not found. Run: make demo-headless")
            return 1
        plans.append(json.loads(path.read_text()))

    out = render(plans, pathlib.Path(args.out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
