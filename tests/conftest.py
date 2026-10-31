from functools import lru_cache

import pytest

from raqeeb.contracts import Scenario, ToolResult
from tests import tool_scenarios
from tests.leakcheck import assert_no_answer_leak


@lru_cache
def _cached(name: str):
    return tool_scenarios.build(name)


@pytest.fixture(scope="session")
def run():
    """run(name) -> (store, scenario, injector), built once per session. Do not write to these stores."""
    return _cached


def checked(result: ToolResult, scenario: Scenario) -> ToolResult:
    """Every agent-facing tool output: a ToolResult that survives JSON and leaks nothing."""
    assert isinstance(result, ToolResult)
    assert ToolResult.model_validate_json(result.model_dump_json()).verdict == result.verdict
    assert result.evidence
    assert_no_answer_leak(result, scenario)
    return result


def call(tool, store, scenario: Scenario, **kwargs) -> ToolResult:
    """Call a tool on the agent's window (agent_view) and check its output."""
    view = scenario.agent_view()
    return checked(tool(store, view.start, view.end, **kwargs), scenario)


@pytest.fixture(autouse=True)
def _no_email_alerts(monkeypatch):
    """Console alerts are off in every test unless a test builds its own Alerter: no email leaves a test run
    even when .env holds a Resend key."""
    monkeypatch.setenv("RAQEEB_ALERTS", "0")


def pytest_collection_modifyitems(config, items):
    """Live LLM tests (marker `llm`) stay off the local model server while a bench run uses it."""
    from raqeeb.config import bench_running

    if bench_running():
        skip = pytest.mark.skip(reason="a bench run is using the local model server (bench/.running-* present)")
        for item in items:
            if "llm" in item.keywords:
                item.add_marker(skip)
