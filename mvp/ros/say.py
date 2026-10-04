"""Publish one `/manager/narrative` message and exit (spec P3.3).

    python -m mvp.ros.say "Rack delivery to zone B is delayed by 2 hours"

It waits for discovery before publishing; without the wait the bridge has not
matched the publisher yet and the message is dropped (gotcha 9).
"""

from __future__ import annotations

import argparse
import json
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

DISCOVERY_WAIT_S = 1.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send a manager narrative to the bridge.")
    ap.add_argument("text", help="what the manager said, in plain English")
    ap.add_argument("--wait", type=float, default=DISCOVERY_WAIT_S,
                    help="seconds to wait for discovery (default 1)")
    args = ap.parse_args(argv)

    rclpy.init()
    node = Node("manager_say")
    pub = node.create_publisher(String, "/manager/narrative", 10)

    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline and pub.get_subscription_count() == 0:
        rclpy.spin_once(node, timeout_sec=0.05)
    if pub.get_subscription_count() == 0:
        node.get_logger().warn("no subscriber on /manager/narrative; sending anyway")

    pub.publish(String(data=json.dumps({"text": args.text})))
    rclpy.spin_once(node, timeout_sec=0.1)
    node.get_logger().info(f"sent: {args.text}")
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
