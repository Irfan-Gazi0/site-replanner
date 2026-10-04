"""Test-wide environment guards.

`env.sh` loads the git-ignored key file, which sets `LANGSMITH_TRACING=true`, so
tracing is on in every shell that can run the MVP. CI has no LangSmith key and
may have no network at all, and the CI job (spec §6) is `pytest -m "not live"`.
So: turn tracing off for every test that is not marked `live`, which keeps a
free test run entirely offline.

`live` tests keep tracing on deliberately - the LangSmith trace is where the
per-run token count and cost of a paid run come from, so there is nothing to
instrument by hand.
"""

import pytest

# Current (LANGSMITH_*) and legacy (LANGCHAIN_*) switches; langsmith and
# langchain-core still honour either one.
TRACING_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


@pytest.fixture(autouse=True)
def langsmith_off_unless_live(request, monkeypatch):
    """Disable LangSmith tracing unless the test carries the `live` marker."""
    if "live" in request.keywords:
        return
    for var in TRACING_VARS:
        monkeypatch.setenv(var, "false")
