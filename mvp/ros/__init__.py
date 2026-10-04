"""ROS 2 side of the MVP (spec P3).

This is the only package that may import `rclpy`. Nothing under
`mvp/agents/`, `mvp/eval/` or `mvp/tests/` imports from here, so CI runs
without ROS.
"""
