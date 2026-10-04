"""Headless stand-in for the Unity twin (spec §3.4, P3).

Publishes `/twin/state` every 0.5 s of real time and `/twin/events` on a fault,
subscribes to `/plan/assignments` and `/plan/status`.

**This file and the Unity twin must behave identically (§3.4).** Change both or
neither. The rules, in one place, are `advance`:

- the clock advances `speed` sim-minutes per real second;
- a resource runs its queue strictly in order of `start` and never skips ahead;
- a new plan replaces every queue, and an ongoing task keeps running;
- a fault stops the resource, reverts its ongoing task to `pending` (restart
  from scratch), clears its queue and publishes `/twin/events`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
STATE_PERIOD_S = 0.5
TICK_S = 0.05


class FakeTwin(Node):
    def __init__(self, speed: float, faults: list[tuple[str, float]]):
        super().__init__("fake_twin")
        doc = json.loads((DATA / "tasks.json").read_text())
        self.speed = speed
        self.faults = sorted(faults, key=lambda f: f[1])

        self.t = 0.0
        self.plan_id = 0
        # status: pending | ongoing | completed. `resource` is "" while pending
        # (§3.3: no nulls, Unity's JsonUtility cannot read them).
        self.tasks = {
            t["id"]: {"id": t["id"], "status": "pending", "resource": "",
                      "start": 0, "end": 0, "duration": int(t["duration"]),
                      "preds": list(t["preds"])}
            for t in doc["tasks"]
        }
        # status: idle | busy | fault. `queue` holds plan entries sorted by start.
        self.resources = {
            r["id"]: {"id": r["id"], "status": "idle", "task": "", "queue": [], "end_at": 0.0}
            for r in doc["resources"]
        }

        self.state_pub = self.create_publisher(String, "/twin/state", 10)
        self.event_pub = self.create_publisher(String, "/twin/events", 10)
        self.create_subscription(String, "/plan/assignments", self._on_plan, 10)
        self.create_subscription(String, "/plan/status", self._on_status, 10)

    # ----------------------------------------------------------------- inputs #

    def _on_plan(self, msg: String) -> None:
        plan = json.loads(msg.data)
        self.plan_id = int(plan.get("plan_id", self.plan_id))
        assignments = plan.get("assignments", [])

        for res in self.resources.values():
            res["queue"] = []

        for a in sorted(assignments, key=lambda a: (int(a["start"]), a["task"])):
            task = self.tasks.get(a["task"])
            res = self.resources.get(a["resource"])
            if task is None or res is None or task["status"] == "completed":
                continue
            if task["status"] == "ongoing":
                # Already running: it keeps its actual times and is not queued.
                continue
            task["resource"] = a["resource"]
            task["start"] = int(a["start"])
            task["end"] = int(a["end"])
            res["queue"].append({"task": a["task"], "start": int(a["start"]),
                                 "end": int(a["end"]), "preds": list(a.get("preds", []))})

        self.get_logger().info(
            f"plan {self.plan_id} applied at t={self.t:.0f}: {self._queue_summary()}")

    def _on_status(self, msg: String) -> None:
        st = json.loads(msg.data)
        self.get_logger().info(f"status: {st.get('state')} - {st.get('message', '')}")

    def _queue_summary(self) -> str:
        """`C1=[T5,T6], R1=[T7], R2=[]` - how the demo shows what a plan moved."""
        return ", ".join(
            f"{rid}=[{','.join(q['task'] for q in self.resources[rid]['queue'])}]"
            for rid in sorted(self.resources))

    # ------------------------------------------------------------------- world #

    def _fault(self, rid: str) -> None:
        res = self.resources[rid]
        res["status"] = "fault"
        if res["task"]:
            task = self.tasks[res["task"]]
            task["status"] = "pending"          # restart from scratch (§3.4)
            task["resource"] = ""
            task["start"] = task["end"] = 0
        res["task"] = ""
        res["queue"] = []
        self.get_logger().warn(f"FAULT {rid} at t={self.t:.0f}")
        # Event first, then a state that already shows the fault, so the bridge
        # never replans against a world in which the resource is still working.
        self.event_pub.publish(String(data=json.dumps(
            {"type": "resource_fault", "resource": rid, "t": round(self.t, 1)})))
        self.publish_state()

    def advance(self, dt_real: float) -> None:
        """One tick: the clock, then any due fault, then every resource."""
        self.t += self.speed * dt_real

        while self.faults and self.t >= self.faults[0][1]:
            self._fault(self.faults.pop(0)[0])

        for res in self.resources.values():
            self._finish_current(res)
            self._start_next(res)

    def _finish_current(self, res: dict) -> None:
        if not res["task"] or self.t < res["end_at"]:
            return
        task = self.tasks[res["task"]]
        task["status"] = "completed"
        task["end"] = int(round(res["end_at"]))
        res["task"] = ""
        res["status"] = "idle"

    def _start_next(self, res: dict) -> None:
        """Start the head of the queue, or nothing: never skip ahead in it."""
        if res["status"] == "fault" or res["task"] or not res["queue"]:
            return

        head = res["queue"][0]
        task = self.tasks[head["task"]]
        if task["status"] == "completed":
            res["queue"].pop(0)
            return
        ready = all(self.tasks[p]["status"] == "completed" for p in head["preds"])
        if not ready or self.t < head["start"]:
            return

        res["queue"].pop(0)
        res["task"] = head["task"]
        res["status"] = "busy"
        res["end_at"] = self.t + (head["end"] - head["start"])
        task["status"] = "ongoing"
        task["start"] = int(round(self.t))
        task["end"] = int(round(res["end_at"]))
        task["resource"] = res["id"]

    def publish_state(self) -> None:
        state = {
            "t": round(self.t, 1),
            "tasks": [{"id": t["id"], "status": t["status"], "resource": t["resource"],
                       "start": t["start"], "end": t["end"]}
                      for t in self.tasks.values()],
            "resources": [{"id": r["id"], "status": r["status"], "task": r["task"]}
                          for r in self.resources.values()],
        }
        self.state_pub.publish(String(data=json.dumps(state)))

    def all_completed(self) -> bool:
        return all(t["status"] == "completed" for t in self.tasks.values())

    def makespan(self) -> int:
        return max(t["end"] for t in self.tasks.values())


def _parse_fault(spec: str) -> tuple[str, float]:
    rid, _, at = spec.partition("@")
    if not at:
        raise argparse.ArgumentTypeError(f"--fault wants RESOURCE@SIM_TIME, got '{spec}'")
    return rid, float(at)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Headless twin (spec §3.4).")
    ap.add_argument("--speed", type=float, default=5.0,
                    help="sim-minutes per real second (default 5)")
    ap.add_argument("--fault", action="append", default=[], type=_parse_fault,
                    metavar="R2@20", help="fault a resource at a sim time; repeatable")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="real seconds before giving up (default 180)")
    args = ap.parse_args(argv)

    rclpy.init()
    twin = FakeTwin(args.speed, args.fault)
    twin.get_logger().info(f"twin up: speed={args.speed} sim-min/s, faults={args.fault}")

    last_tick = time.monotonic()
    started = last_tick
    last_state = last_tick - STATE_PERIOD_S   # so the first state goes out at once
    rc = 1
    try:
        while rclpy.ok():
            rclpy.spin_once(twin, timeout_sec=TICK_S)
            now = time.monotonic()
            twin.advance(now - last_tick)
            last_tick = now

            if now - last_state >= STATE_PERIOD_S:
                twin.publish_state()
                last_state = now

            if twin.all_completed():
                twin.publish_state()
                print(f"ALL TASKS COMPLETED t={twin.makespan()}", flush=True)
                rc = 0
                break
            if now - started > args.timeout:
                print(f"TIMEOUT after {args.timeout}s at t={twin.t:.0f}", flush=True)
                break
    except KeyboardInterrupt:
        pass
    finally:
        twin.destroy_node()
        rclpy.try_shutdown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
