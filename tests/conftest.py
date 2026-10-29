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
