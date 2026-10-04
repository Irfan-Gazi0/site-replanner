#!/usr/bin/env bash
# Headless closed loop: bridge + fake twin, no Unity, no API call (spec P3.4).
#
#   scripts/headless_demo.sh [--fault R2@20] [--speed 5] [--timeout 180]
#                            [--expect-escalated]
#
# --expect-escalated inverts the check: the run passes when the bridge escalates
# and dispatches nothing after the initial plan (the R3 case: the drill
# capability is gone, so the twin is *meant* to stall).
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
while [ $# -gt 0 ]; do
  case "$1" in
    --fault) FAULT="$2"; shift 2 ;;
    --speed) SPEED="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --expect-escalated) EXPECT_ESCALATED=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

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
  DISPATCHED=$(grep -c "/plan/status dispatched" "$BRIDGE_LOG")
  if ! grep -q "/plan/status escalated" "$BRIDGE_LOG"; then
    echo "FAILED: the bridge never published escalated" >&2
    exit 1
  fi
  if [ "$DISPATCHED" -ne 1 ]; then
    echo "FAILED: expected only the initial plan, got $DISPATCHED dispatched" >&2
    exit 1
  fi
  echo "OK (escalated, no plan dispatched after the initial one)"
  exit 0
fi

if [ "$TWIN_RC" -ne 0 ]; then
  echo "FAILED: the twin did not complete every task (rc=$TWIN_RC)" >&2
  exit 1
fi
echo "OK"
