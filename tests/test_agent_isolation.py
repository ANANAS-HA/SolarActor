"""The agent must never see the planted faults: it reads only the telemetry database.

If a test here fails, the benchmark is measuring an agent that can read the answer.
"""

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from raqeeb.config import REPO_ROOT, SAFETY_SCENARIO_DIR, SCENARIO_DIR, TEST_SCENARIO_DIR
from raqeeb.contracts import Scenario
from raqeeb.twin.simulator import Twin
from tests.leakcheck import AnswerLeak, assert_no_answer_leak

FIXTURE = Path(__file__).parent / "fixtures" / "scn_example.yaml"
SCENARIOS = [FIXTURE, *sorted(SCENARIO_DIR.glob("scn_*.yaml")), *sorted(TEST_SCENARIO_DIR.glob("tst_*.yaml")),
             *sorted(SAFETY_SCENARIO_DIR.glob("saf_*.yaml"))]

# Packages whose code runs on the agent's side of the line (Raqeeb and the baseline it is compared with)
AGENT_SIDE = ["agent", "tools", "llm", "baseline"]
FORBIDDEN_IMPORTS = ("raqeeb.faults", "raqeeb.bench", "raqeeb.twin.simulator")
FORBIDDEN_CODE = {
    r"\.truth\b": "the twin's truth log",
    r"\btwin\.state\b|\bPlantState\b|\bInverterState\b": "twin state",
    r"answer_key": "the answer key",
    r"\bScenario\b|\bFaultSpec\b|from_yaml|expected_answer_key": "full scenarios (use AgentScenario)",
    r"SCENARIO_DIR|[\"'/\\]scenarios\b": "the scenarios/ folder",
}


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return Scenario.from_yaml(FIXTURE)


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_agent_view_leaks_nothing(path):
    scenario = Scenario.from_yaml(path)
    assert_no_answer_leak(scenario.agent_view(), scenario, strict=True)


def test_leak_check_catches_leaks(scenario):
    twin = Twin(scenario.start, scenario.start + timedelta(minutes=10), seed=1)
    twin.run_to_end()
    leaks = [
        scenario,
        scenario.model_dump(mode="json"),
        {"tool": "peers", "hint": scenario.faults[2]},
        twin.truth,
        {"ok": [twin.state]},
        {"severity": 0.2},
        f"likely {scenario.faults[0].label}",
    ]
    for leak in leaks:
        with pytest.raises(AnswerLeak):
            assert_no_answer_leak(leak, scenario)
    for leak in ({"device": "INV-07", "note": "INVERTER_DERATE"}, {"run": scenario.seed}):
        with pytest.raises(AnswerLeak):
            assert_no_answer_leak(leak, scenario, strict=True)


def test_telemetry_naming_devices_is_allowed(scenario):
    reading = {"ts": datetime(2025, 4, 12, 9, tzinfo=timezone.utc), "inverter_id": "INV-07", "ac_kw": 1550.0}
    assert_no_answer_leak({"rows": [reading], "alarm": "DERATE_TEMP"}, scenario)


def _agent_side_files() -> list[Path]:
    return sorted(p for pkg in AGENT_SIDE for p in (REPO_ROOT / "raqeeb" / pkg).rglob("*.py"))


def test_agent_side_code_cannot_reach_the_answer():
    problems = []
    for path in _agent_side_files():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        for node in ast.walk(ast.parse(source)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            problems += [f"{rel}: imports {n}" for n in names if n.startswith(FORBIDDEN_IMPORTS)]
        for pattern, what in FORBIDDEN_CODE.items():
            for match in re.finditer(pattern, source):
                line = source.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line}: touches {what} ({match.group(0)!r})")
    assert not problems, "\n".join(problems)


def test_agent_side_packages_exist():
    for pkg in AGENT_SIDE:
        assert (REPO_ROOT / "raqeeb" / pkg / "__init__.py").exists(), pkg
