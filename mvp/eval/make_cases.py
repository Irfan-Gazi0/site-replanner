"""Generate `mvp/data/cases.jsonl` from the Appendix B templates (spec P5.1).

24 cases: 6 easy, 8 medium, 10 hard. The gold constraints are known **by
construction** - every template fills its own slots, so nothing here has to
parse the text back. `expect` is then computed, not declared: the gold is run
through the deterministic verifier (`validate`, checks 2-7) against
`tasks.json` and `snapshot_t20.json`, and a gold the verifier rejects means the
right answer is an escalation.

Easy cases name IDs; medium and hard use the aliases and the hour phrasings, so
the benchmark measures resolution against the catalogue and not just copying.
No LLM call and no `rclpy` import: generating the cases is free and offline.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random

from mvp.agents.schema import ParseResult, make_constraint
from mvp.agents.validate import validate as validate_parse

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"

# Appendix B. The flag is True when the phrase takes a plural verb, which keeps
# "the sparkies is down" out of the generated text.
#
# Every alias names exactly one task. T7 and T8 say "rack delivery" and not "the
# zone A/B racks": the first benchmark run used the plural phrase for T7 and the
# model answered T9 (*Set racks and run structured cabling in zone A*) in all six
# affected runs, which is a parse the verifier cannot reject - a valid ID, a
# well-formed constraint and a feasible plan. "the zone B racks" is also inside
# an Appendix A few-shot example, the same reason T6 has one alias only.
ALIASES: dict[str, list[tuple[str, bool]]] = {
    "T4": [("zone B anchor drilling", False), ("the drilling in zone B", False)],
    "T5": [("cable tray install in zone A", False), ("the zone A tray installation", False)],
    # The few-shot uses "cable tray install in zone B", so T6 has one alias only.
    "T6": [("the zone B tray installation", False)],
    "T7": [("rack delivery to zone A", False), ("the zone A rack delivery", False)],
    "T8": [("rack delivery to zone B", False), ("the zone B rack delivery", False)],
    "T9": [("rack setting and cabling in zone A", False)],
    "T10": [("rack setting and cabling in zone B", False)],
    "T11": [("the final inspection", False), ("the hall inspection", False)],
    "R1": [("cargo robot 1", False), ("the first AMR", False)],
    "R2": [("cargo robot 2", False), ("the second AMR", False)],
    "R3": [("the drilling robot", False), ("the drill bot", False)],
    "C1": [("the electrical crew", False), ("the sparkies", True)],
}

MINUTES = (15, 20, 30, 45, 60, 90)
HOUR_PHRASES = ((60, "an hour"), (120, "two hours"), (30, "half an hour"),
                (90, "an hour and a half"))

TARGETS = ("T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11")
# R3 is left out of the easy and medium sets on purpose: it is mid-task at t=20
# (T3 is ongoing on it), so an outage there is a hard, infeasible case.
OUTAGE_RESOURCES = ("R1", "R2", "C1")
DEP_PAIRS = (("T8", "T9"), ("T6", "T7"), ("T4", "T5"), ("T10", "T9"), ("T9", "T10"))

# (template, needs a singular subject)
START_AFTER_TEMPLATES = (
    ("{task} is delayed by {time}.", True),
    ("{task} can't start for another {time}.", False),
    ("Supplier just called: {task} will be {time} late.", False),
)
DURATION_TOTAL_TEMPLATES = (
    ("{task} will take {time} in total.", False),
    ("New estimate for {task}: {time}.", False),
)
DURATION_RELATIVE_TEMPLATE = ("{task} will take {time} longer than planned.", False)
OUTAGE_TEMPLATES = (
    ("{res} is down for {time}.", True),
    ("{res} needs a {time} battery swap starting now.", True),
)
DEP_TEMPLATES = (
    ("Don't start {after} until {before} is finished.", False),
    ("{before} has to be done before {after}.", True),
)

# Appendix A, verbatim. None of these may end up in the cases (spec P5.1).
FEWSHOT_PHRASES = (
    "Cable tray install in zone B will take 50 minutes.",
    "Cargo robot 1 needs a 30 minute battery swap now",
    "Bring in a scissor lift",
)


# --------------------------------------------------------------------------- #
# slot fillers
# --------------------------------------------------------------------------- #

def _name(rng: random.Random, ident: str, alias: bool) -> tuple[str, bool]:
    """(phrase, is_plural) for a task or resource: the ID, or one of its aliases."""
    if not alias:
        return ident, False
    return rng.choice(ALIASES[ident])


def _time(rng: random.Random, hours: bool) -> tuple[int, str]:
    """(minutes, phrase). The hour phrasings are medium and hard only."""
    if hours:
        return rng.choice(HOUR_PHRASES)
    n = rng.choice(MINUTES)
    return n, f"{n} minutes"


def _fill(template: tuple[str, bool], **slots: tuple[str, bool]) -> str | None:
    """Render a template, or None if it needs a singular subject and got a plural.

    The subject is the first slot - the task or resource in every template here -
    and only its number can make a template unusable. A caller that gets None
    draws again rather than filtering the templates up front: filtering would be
    tidier but consumes the RNG differently, and `cases.jsonl` is committed with
    its SHA-256 recorded in `runs/eval/results.json`.
    """
    text, needs_singular = template
    subject_is_plural = next(iter(slots.values()))[1]
    if needs_singular and subject_is_plural:
        return None
    rendered = text.format(**{k: v[0] for k, v in slots.items()})
    return rendered[0].upper() + rendered[1:]


# --------------------------------------------------------------------------- #
# one constraint of each type
# --------------------------------------------------------------------------- #

def _start_after(rng: random.Random, alias: bool, used: set[str]) -> tuple[str, dict]:
    task = _pick(rng, TARGETS, used)
    minutes, phrase = _time(rng, hours=alias)
    while True:
        text = _fill(rng.choice(START_AFTER_TEMPLATES),
                     task=_name(rng, task, alias), time=(phrase, False))
        if text:
            return text, {"type": "start_after", "task": task, "delay_min": minutes}


def _duration_change(rng: random.Random, alias: bool, used: set[str],
                     durations: dict[str, int]) -> tuple[str, dict]:
    task = _pick(rng, TARGETS, used)
    if alias and rng.random() < 0.5:
        # Relative form: the gold is the current duration plus the stated delta.
        delta = rng.choice((15, 20, 30))
        text = _fill(DURATION_RELATIVE_TEMPLATE, task=_name(rng, task, alias),
                     time=(f"{delta} minutes", False))
        total = durations[task] + delta
    else:
        total = rng.randrange(10, 95, 5)
        text = _fill(rng.choice(DURATION_TOTAL_TEMPLATES),
                     task=_name(rng, task, alias), time=(f"{total} minutes", False))
    return text, {"type": "duration_change", "task": task, "new_duration_min": total}


def _resource_unavailable(rng: random.Random, alias: bool,
                          used: set[str], seen: set[str]) -> tuple[str, dict]:
    # `seen` spans the whole band, not the case: three resources and eight
    # medium cases otherwise give the same one four times over.
    res = _pick(rng, OUTAGE_RESOURCES, seen)
    used.add(res)
    minutes = rng.randrange(15, 121, 15)
    phrase = f"{minutes} minutes"
    if alias:
        minutes, phrase = rng.choice(HOUR_PHRASES)
    templates = OUTAGE_TEMPLATES if res != "C1" else OUTAGE_TEMPLATES[:1]
    while True:
        text = _fill(rng.choice(templates), res=_name(rng, res, alias),
                     time=(phrase, False))
        if text:
            return text, {"type": "resource_unavailable", "resource": res,
                          "outage_min": minutes}


def _add_dependency(rng: random.Random, alias: bool, used: set[str]) -> tuple[str, dict]:
    before, after = rng.choice(DEP_PAIRS)
    used.update((before, after))
    while True:
        text = _fill(rng.choice(DEP_TEMPLATES), before=_name(rng, before, alias),
                     after=_name(rng, after, alias))
        if text:
            return text, {"type": "add_dependency", "before": before, "after": after}


def _pick(rng: random.Random, pool: tuple[str, ...], used: set[str]) -> str:
    """A member of `pool` that this case has not targeted yet."""
    free = [p for p in pool if p not in used] or list(pool)
    choice = rng.choice(free)
    used.add(choice)
    return choice


def _one(rng: random.Random, kind: str, alias: bool, used: set[str],
         durations: dict[str, int], seen_res: set[str]) -> tuple[str, dict]:
    if kind == "start_after":
        return _start_after(rng, alias, used)
    if kind == "duration_change":
        return _duration_change(rng, alias, used, durations)
    if kind == "resource_unavailable":
        return _resource_unavailable(rng, alias, used, seen_res)
    if kind == "add_dependency":
        return _add_dependency(rng, alias, used)
    raise ValueError(f"unknown constraint kind {kind!r}")


# --------------------------------------------------------------------------- #
# the three difficulty bands
# --------------------------------------------------------------------------- #

def _easy(rng: random.Random, durations: dict[str, int]) -> list[tuple[str, list[dict]]]:
    """6 cases: one constraint, IDs only."""
    kinds = ["start_after", "duration_change", "resource_unavailable",
             "add_dependency", "start_after", "duration_change"]
    out = []
    seen_res: set[str] = set()
    for kind in kinds:
        text, gold = _one(rng, kind, alias=False, used=set(), durations=durations,
                          seen_res=seen_res)
        out.append((text, [gold]))
    return out


def _medium(rng: random.Random, durations: dict[str, int]) -> list[tuple[str, list[dict]]]:
    """8 cases: two constraints of different types, aliases and hour phrasings."""
    pairs = [
        ("start_after", "duration_change"),
        ("resource_unavailable", "start_after"),
        ("duration_change", "add_dependency"),
        ("add_dependency", "resource_unavailable"),
        ("duration_change", "resource_unavailable"),
        ("start_after", "add_dependency"),
        ("resource_unavailable", "duration_change"),
        ("add_dependency", "start_after"),
    ]
    out = []
    seen_res: set[str] = set()
    for first, second in pairs:
        used: set[str] = set()
        t1, g1 = _one(rng, first, alias=True, used=used, durations=durations,
                      seen_res=seen_res)
        t2, g2 = _one(rng, second, alias=True, used=used, durations=durations,
                      seen_res=seen_res)
        second_clause = t2[0].lower() + t2[1:]
        out.append((f"{t1} Also, {second_clause}", [g1, g2]))
    return out


def _hard(durations: dict[str, int]) -> list[tuple[str, list[dict]]]:
    """10 fixed cases, two of each of the five kinds in Appendix B."""
    return [
        # 1. Unknown ID.
        ("R5 is down for an hour.", []),
        ("T14 is delayed 30 minutes.", []),
        # 2. Infeasible: the only drill-capable resource, and the only install crew.
        ("The drilling robot is out of service until further notice.",
         [{"type": "resource_unavailable", "resource": "R3", "outage_min": None}]),
        ("The electrical crew is off site until further notice.",
         [{"type": "resource_unavailable", "resource": "C1", "outage_min": None}]),
        # 3. Cycle.
        ("Don't start rack delivery to zone A until the final inspection is finished.",
         [{"type": "add_dependency", "before": "T11", "after": "T7"}]),
        ("Rack setting and cabling in zone A has to be done before cable tray "
         "install in zone A.",
         [{"type": "add_dependency", "before": "T9", "after": "T5"}]),
        # 4. Out of scope: a new task is not one of the four types.
        ("Add a new task: fire suppression piping in zone B, 40 minutes.", []),
        ("Add a new task: fire suppression piping in zone A, 25 minutes.", []),
        # 5. Relative duration with a distractor clause.
        ("It's raining but that doesn't affect the hall; the zone A rack delivery "
         "will take 15 minutes longer than planned.",
         [{"type": "duration_change", "task": "T7",
           "new_duration_min": durations["T7"] + 15}]),
        ("The client called about invoicing, which doesn't affect the schedule; "
         "the zone B tray installation will take 20 minutes longer than planned.",
         [{"type": "duration_change", "task": "T6",
           "new_duration_min": durations["T6"] + 20}]),
    ]


# --------------------------------------------------------------------------- #
# expect
# --------------------------------------------------------------------------- #

def gold_constraint(gold: dict):
    """One gold slot dict -> a full `Constraint`, every other field null.

    The gold is written with the fields its type uses and nothing else, which is
    also the shape the benchmark compares a parse against.
    """
    return make_constraint(gold["type"], **{k: v for k, v in gold.items() if k != "type"})


def expected_outcome(gold: list[dict], tasks_doc: dict, world: dict) -> str:
    """"plan" or "escalate", from the verifier's own checks on the gold.

    An empty gold means the request cannot be expressed at all (unknown ID, out
    of scope), which is an escalation by definition. Otherwise the gold goes
    through `validate`, so a request that is well-formed but impossible - the
    only drilling robot going down - is an escalation for the same reason the
    live system would escalate it.
    """
    if not gold:
        return "escalate"
    parse = ParseResult(constraints=[gold_constraint(g) for g in gold],
                        unresolvable=False, reason="gold")
    errors = validate_parse(parse, tasks_doc, world, [])
    return "plan" if not errors else "escalate"


def build_cases(seed: int) -> list[dict]:
    tasks_doc = json.loads((DATA / "tasks.json").read_text())
    world = json.loads((DATA / "snapshot_t20.json").read_text())
    durations = {t["id"]: t["duration"] for t in tasks_doc["tasks"]}

    rng = random.Random(seed)
    bands = [("easy", "E", _easy(rng, durations)),
             ("medium", "M", _medium(rng, durations)),
             ("hard", "H", _hard(durations))]

    cases = []
    for difficulty, prefix, generated in bands:
        for i, (text, gold) in enumerate(generated, start=1):
            cases.append({
                "id": f"{prefix}{i:02d}",
                "difficulty": difficulty,
                "text": text,
                "expect": expected_outcome(gold, tasks_doc, world),
                "gold": gold,
            })
    return cases


def check_no_fewshot(cases: list[dict]) -> list[str]:
    """The Appendix A examples must not be in the benchmark (spec P5.1)."""
    return [
        f"{c['id']}: contains the few-shot phrase {phrase!r}"
        for c in cases for phrase in FEWSHOT_PHRASES
        if phrase.lower() in c["text"].lower()
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the P5 benchmark cases (free, offline).")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-o", "--out", default="mvp/data/cases.jsonl")
    args = ap.parse_args(argv)

    cases = build_cases(args.seed)
    leaks = check_no_fewshot(cases)
    if leaks:
        for leak in leaks:
            print(leak)
        return 1

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(c) + "\n" for c in cases))

    counts = collections.Counter(c["difficulty"] for c in cases)
    expects = collections.Counter(c["expect"] for c in cases)
    print(f"{len(cases)} cases -> {out}")
    print("by difficulty: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("by expect: " + ", ".join(f"{k}={v}" for k, v in sorted(expects.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
