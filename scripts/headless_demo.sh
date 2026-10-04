#!/usr/bin/env bash
# Headless closed loop: bridge + fake twin, no Unity, no API call (spec P3.4).
#
#   scripts/headless_demo.sh [--fault R2@20] [--speed 5] [--timeout 180]
#                            [--expect-escalated] [--expect-dispatched N]
#
# --expect-escalated inverts the check: the run passes when the bridge escalates
# and dispatches nothing afterwards (the R3 case: the drill capability is gone,
# so the twin is *meant* to stall). How many plans go out *before* the
# escalation depends on the fault, not on the bridge - R3@25 escalates after the
# initial plan, C1@0 kills the only `install` resource before that plan is even
# solved - so pin it with --expect-dispatched N when the demo must show a plan.
#
# C1@0 is also the race probe: the twin publishes the fault event before its
# first state (`fake_twin._fault`), so the bridge has to hold it. Which topic
# arrives first is not guaranteed, so a pass does not prove the hold ran.
#
# Starts the bridge with --auto-approve, then the twin. Exits non-zero unless
# the twin reports ALL TASKS COMPLETED. The project path contains a space, so
# every path here is quoted.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAULT="R2@20"
SPEED="5"
TIMEOUT="180"
EXPECT_ESCALATED=0
EXPECT_DISPATCHED=""     # empty: any count before the escalation is accepted
while [ $# -gt 0 ]; do
  case "$1" in
    --fault) FAULT="$2"; shift 2 ;;
    --speed) SPEED="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --expect-escalated) EXPECT_ESCALATED=1; shift ;;
    --expect-dispatched)
      # A non-numeric count makes the `-ne` test below error out, which would
      # leave the check passing silently. Reject it at parse time instead.
      case "$2" in ''|*[!0-9]*)
        echo "--expect-dispatched wants a count, got '$2'" >&2; exit 2 ;;
      esac
      EXPECT_DISPATCHED="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Only the --expect-escalated check reads a pinned count, so on its own the flag
# would be silently ignored and the run would report whatever it liked.
if [ -n "$EXPECT_DISPATCHED" ] && [ "$EXPECT_ESCALATED" -eq 0 ]; then
  echo "--expect-dispatched needs --expect-escalated" >&2
  exit 2
fi

LOGS="$ROOT/runs/logs"
mkdir -p "$LOGS"
BRIDGE_LOG="$LOGS/bridge.log"
TWIN_LOG="$LOGS/twin.log"
: > "$BRIDGE_LOG"
: > "$TWIN_LOG"

BRIDGE_PID=""
TWIN_PID=""
cleanup() {
  for pid in "$TWIN_PID" "$BRIDGE_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
}
trap cleanup EXIT

echo "== bridge (--auto-approve), log: $BRIDGE_LOG"
python -m mvp.ros.bridge_node --auto-approve > "$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!

# Wait for the bridge to be subscribed before the twin publishes its first
# state: that state is what triggers the initial plan.
for _ in $(seq 1 50); do
  grep -q "waiting for /twin/state" "$BRIDGE_LOG" && break
  sleep 0.2
done

echo "== twin (--speed $SPEED --fault $FAULT), log: $TWIN_LOG"
python -m mvp.ros.fake_twin --speed "$SPEED" --fault "$FAULT" --timeout "$TIMEOUT" \
  > "$TWIN_LOG" 2>&1 &
TWIN_PID=$!
wait "$TWIN_PID"
TWIN_RC=$?
TWIN_PID=""

echo
echo "== twin result"
grep -E "ALL TASKS COMPLETED|TIMEOUT|FAULT" "$TWIN_LOG" || echo "(nothing)"
echo "== bridge plans and statuses"
grep -E "/plan/status|trace:" "$BRIDGE_LOG" || echo "(nothing)"

if [ "$EXPECT_ESCALATED" -eq 1 ]; then
  if ! grep -q "/plan/status escalated" "$BRIDGE_LOG"; then
    echo "FAILED: the bridge never published escalated" >&2
    exit 1
  fi
  # Escalating hands the problem to a human, so the bridge must stop planning
  # there: nothing may be dispatched from the first escalation on. That is the
  # invariant. How many plans went out before it depends on the fault time, so
  # it is only checked when the caller pins a count.
  DISPATCHED_AFTER=$(sed -n '/\/plan\/status escalated/,$p' "$BRIDGE_LOG" \
    | grep -c "/plan/status dispatched")
  if [ "$DISPATCHED_AFTER" -ne 0 ]; then
    echo "FAILED: $DISPATCHED_AFTER plan(s) dispatched after escalating" >&2
    exit 1
  fi
  # Nothing came after the escalation, so the total is the count before it.
  DISPATCHED=$(grep -c "/plan/status dispatched" "$BRIDGE_LOG")
  if [ -n "$EXPECT_DISPATCHED" ] && [ "$DISPATCHED" -ne "$EXPECT_DISPATCHED" ]; then
    echo "FAILED: wanted $EXPECT_DISPATCHED plan(s) before escalating," \
         "got $DISPATCHED" >&2
    exit 1
  fi
  echo "OK (escalated after $DISPATCHED dispatched plan(s), none after)"
  exit 0
fi

if [ "$TWIN_RC" -ne 0 ]; then
  echo "FAILED: the twin did not complete every task (rc=$TWIN_RC)" >&2
  exit 1
fi
echo "OK"
