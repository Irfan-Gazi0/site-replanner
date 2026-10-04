# Relative paths only: the project path contains a space (see CLAUDE.md).
.PHONY: test test-live demo-headless demo-escalate sync-unity

test:
	pytest -m "not live" -q

# PAID: calls the real API.
test-live:
	pytest -m live -q

demo-headless:
	scripts/headless_demo.sh --fault R2@20

demo-escalate:
	scripts/headless_demo.sh --fault R3@25 --timeout 30 --expect-escalated --expect-dispatched 1

sync-unity:
	mkdir -p DataHallTwin/Assets/StreamingAssets
	cp mvp/data/tasks.json DataHallTwin/Assets/StreamingAssets/tasks.json
