# Relative paths only: the project path contains a space (see CLAUDE.md).
.PHONY: test test-live cases eval eval-probe report demo-headless demo-escalate sync-unity

test:
	pytest -m "not live" -q

# PAID: calls the real API.
test-live:
	pytest -m live -q

cases:
	python -m mvp.eval.make_cases --seed 7 -o mvp/data/cases.jsonl

# PAID: 20 runs, to price the full matrix before committing to it.
eval-probe:
	python -m mvp.eval.run_eval --limit 5

# PAID: the full 24 x 2 x 2 matrix.
eval:
	python -m mvp.eval.run_eval --arms single_shot,validated --repeats 2 --workers 4

report:
	python -m mvp.eval.report

demo-headless:
	scripts/headless_demo.sh --fault R2@20

demo-escalate:
	scripts/headless_demo.sh --fault R3@25 --timeout 30 --expect-escalated --expect-dispatched 1

sync-unity:
	mkdir -p DataHallTwin/Assets/StreamingAssets
	cp mvp/data/tasks.json DataHallTwin/Assets/StreamingAssets/tasks.json
