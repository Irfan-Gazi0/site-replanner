# Source this in EVERY terminal, from anywhere: source "/home/igazi2/Documents/Multi Agent Robot/env.sh"
# The project path contains spaces, so every path below is quoted. Keep it that way.
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_ROOT
source /opt/ros/humble/setup.bash
source "$PROJECT_ROOT/ros2_ws/install/setup.bash"
export ROS_LOCALHOST_ONLY=1
source "$PROJECT_ROOT/.venv/bin/activate"
# Secrets (OPENAI_API_KEY) live in .env, which is git-ignored.
if [ -f "$PROJECT_ROOT/.env" ]; then set -a; source "$PROJECT_ROOT/.env"; set +a; fi
