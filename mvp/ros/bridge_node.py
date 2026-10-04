"""ROS 2 bridge between the twin and the replanning graph (spec P3).

`rclpy.spin` runs in a daemon thread; the main thread pulls events off a
`queue.Queue`, so a blocking `input()` for the approval gate cannot stall the
subscriptions.

The bridge owns no planning logic: it builds `world` from the latest
`/twin/state`, hands the event to `mvp.agents.graph`, and publishes whatever
comes back. It is also the only place that keeps `active_constraints` across
events, so an earlier disruption still applies to every later replan.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import queue
import threading

import rclpy
from langgraph.types import Command
from rclpy.node import Node
from std_msgs.msg import String

from mvp.agents.graph import build_graph, initial_state, plan_diff, tasks_doc
from mvp.agents.solver import solve as solve_plan

RUNS = pathlib.Path(__file__).resolve().parents[2] / "runs" / "plans"
SOLVE_TIME_LIMIT_S = 5.0


class BridgeNode(Node):
    def __init__(self, events: queue.Queue):
        super().__init__("replanner_bridge")
        self.events = events
        self.state: dict | None = None            # latest /twin/state
        self.seen_first_state = False

        self.plan_pub = self.create_publisher(String, "/plan/assignments", 10)
        self.status_pub = self.create_publisher(String, "/plan/status", 10)
        self.create_subscription(String, "/twin/state", self._on_state, 10)
        self.create_subscription(String, "/twin/events", self._on_event, 10)
        self.create_subscription(String, "/manager/narrative", self._on_narrative, 10)

    def _on_state(self, msg: String) -> None:
        self.state = json.loads(msg.data)
        if not self.seen_first_state:
            # Nothing is published before this: a plan sent before the twin
            # subscribes is simply lost (gotcha 9).
            self.seen_first_state = True
            self.events.put({"source": "initial"})

    def _on_event(self, msg: String) -> None:
        event = json.loads(msg.data)
        event["source"] = "twin"
        self.events.put(event)

    def _on_narrative(self, msg: String) -> None:
        self.events.put({"source": "narrative", "text": json.loads(msg.data)["text"]})

    def publish_status(self, plan_id: int, state: str, message: str) -> None:
        self.status_pub.publish(String(data=json.dumps(
            {"plan_id": plan_id, "state": state, "message": message})))
        self.get_logger().info(f"/plan/status {state}: {message}")

    def publish_plan(self, plan_id: int, t_now: int, assignments: list[dict]) -> None:
        self.plan_pub.publish(String(data=json.dumps(
            {"plan_id": plan_id, "t_now": t_now, "assignments": assignments})))


# --------------------------------------------------------------------------- #
# world
# --------------------------------------------------------------------------- #

def world_from_state(state: dict, prev_plan: list[dict], event: dict | None = None) -> dict:
    """`/twin/state` (§3.3) -> the solver's `world` (§3.4b)."""
    tasks = state.get("tasks", [])
    world = {
        "t_now": int(state.get("t", 0)),
        "completed": [t["id"] for t in tasks if t["status"] == "completed"],
        "ongoing": [{"task": t["id"], "resource": t["resource"], "start": int(t["start"])}
                    for t in tasks if t["status"] == "ongoing"],
        "faulted": [r["id"] for r in state.get("resources", []) if r["status"] == "fault"],
        "prev_plan": list(prev_plan),
    }

    if (event or {}).get("type") == "resource_fault":
        # The event may overtake the state that shows the fault. Apply it here
        # too, so the solver never freezes a task onto a dead resource.
        rid = event["resource"]
        if rid not in world["faulted"]:
            world["faulted"].append(rid)
        world["ongoing"] = [o for o in world["ongoing"] if o["resource"] != rid]
    return world


# --------------------------------------------------------------------------- #
# worker loop
# --------------------------------------------------------------------------- #

class Bridge:
    def __init__(self, auto_approve: bool):
        self.auto_approve = auto_approve
        self.events: queue.Queue = queue.Queue()
        self.node = BridgeNode(self.events)
        self.graph = build_graph(auto_approve=auto_approve)
        self.tasks = tasks_doc()
        self.active_constraints: list[dict] = []
        self.prev_plan: list[dict] = []
        self.plan_id = 0
        self.thread_seq = 0
        RUNS.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- plan record #

    def _accept(self, event: dict, world: dict, result: dict, diff: dict,
                trace: list[str]) -> None:
        """Publish a plan, remember it as `prev_plan`, and record it on disk."""
        self.plan_id += 1
        self.prev_plan = [{"task": a["task"], "resource": a["resource"],
                           "start": a["start"], "end": a["end"]}
                          for a in result["assignments"]]
        self.node.publish_plan(self.plan_id, world["t_now"], result["assignments"])
        self.node.publish_status(self.plan_id, "dispatched",
                                 f"plan {self.plan_id}: {result['reason']}")
        (RUNS / f"plan_{self.plan_id:03d}.json").write_text(json.dumps({
            "plan_id": self.plan_id, "event": event, "world": world,
            "assignments": result["assignments"], "makespan": result["makespan"],
            "n_reassigned": result["n_reassigned"], "diff": diff, "trace": trace,
            "active_constraints": self.active_constraints,
        }, indent=2))

    # ---------------------------------------------------------------- handlers #

    def handle_initial(self, event: dict) -> None:
        """The first `/twin/state` triggers the initial plan. Auto-approved."""
        world = world_from_state(self.node.state, self.prev_plan)
        self.node.publish_status(self.plan_id, "replanning", "initial plan")
        result = solve_plan(self.tasks, world, self.active_constraints,
                            time_limit_s=SOLVE_TIME_LIMIT_S)
        if result["status"] not in ("OPTIMAL", "FEASIBLE"):
            self.node.publish_status(self.plan_id, "escalated", result["reason"])
            return
        self._accept(event, world, result, plan_diff(world, result), trace=["initial"])

    def handle_replan(self, event: dict) -> None:
        world = world_from_state(self.node.state, self.prev_plan, event)
        label = event.get("text") or f"{event.get('type')} {event.get('resource', '')}".strip()
        self.node.publish_status(self.plan_id, "replanning", f"{event['source']}: {label}")

        self.thread_seq += 1
        cfg = {"configurable": {"thread_id": f"bridge-{self.thread_seq}"}}
        out = self.graph.invoke(
            initial_state(event, world, self.active_constraints, self.auto_approve), cfg)

        while out.get("__interrupt__"):
            proposal = out["__interrupt__"][0].value
            changes = (proposal.get("diff") or {}).get("changes") or ["no change"]
            self.node.publish_status(self.plan_id, "awaiting_approval", "; ".join(changes))
            print("\n--- proposed plan ---")
            print(json.dumps(proposal, indent=2))
            answer = input("Approve? [y/n] ")
            out = self.graph.invoke(Command(resume=answer), cfg)

        status, reason = out.get("status", "error"), out.get("reason", "")
        trace = out.get("trace") or []
        if status == "dispatched":
            self.active_constraints = list(out.get("active_constraints") or [])
            self._accept(event, world, out["plan"], out.get("diff") or {}, trace)
        elif status == "rejected":
            self.node.publish_status(self.plan_id, "rejected", reason)
        else:
            # `escalated` and `error` both end at a human; §3.3 has one state.
            self.node.publish_status(self.plan_id, "escalated", reason)
        self.node.get_logger().info(f"trace: {' -> '.join(trace)}")

    # -------------------------------------------------------------------- loop #

    def run(self) -> int:
        spin = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True)
        spin.start()
        self.node.get_logger().info(
            f"bridge up (auto_approve={self.auto_approve}); waiting for /twin/state")
        try:
            while rclpy.ok():
                try:
                    event = self.events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event["source"] == "initial":
                    self.handle_initial(event)
                else:
                    self.handle_replan(event)
        except KeyboardInterrupt:
            pass
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ROS bridge for the replanning graph (P3).")
    ap.add_argument("--auto-approve", action="store_true",
                    help="skip the operator gate (used by the headless demo)")
    args = ap.parse_args(argv)

    rclpy.init()
    bridge = Bridge(args.auto_approve)
    try:
        return bridge.run()
    finally:
        bridge.node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
