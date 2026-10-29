import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from raqeeb.contracts import (
    TABLE_DDL,
    TABLE_MODELS,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    ActionType,
    Agent,
    AgentTurn,
    AnswerKey,
    Diagnosis,
    FaultSpec,
    FaultType,
    Hypothesis,
    InverterReading,
    LlmCall,
    LlmRole,
    Scenario,
    ToolResult,
    Verification,
    from_db_ts,
    parse_cause_label,
    to_db_ts,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def scenario() -> Scenario:
    return Scenario.from_yaml(FIXTURES / "scn_example.yaml")


def test_example_scenario_loads(scenario: Scenario):
    assert scenario.id == "scn_0007"
    assert scenario.start == datetime(2025, 4, 12, 5, tzinfo=timezone.utc)
    assert [f.type for f in scenario.faults] == [
        FaultType.SOILING,
        FaultType.SENSOR_SOILING,
        FaultType.INVERTER_DERATE,
        FaultType.COMMS_DROPOUT,
    ]
    assert scenario.answer_key == scenario.expected_answer_key()


def test_scenario_yaml_round_trip(scenario: Scenario, tmp_path: Path):
    out = tmp_path / "scn.yaml"
    scenario.to_yaml(out)
    assert Scenario.from_yaml(out) == scenario


def test_fault_times_resolve_to_utc_on_start_date(scenario: Scenario):
    dropout = scenario.faults[3]
    assert scenario.resolve_time(dropout.start) == datetime(2025, 4, 12, 11, 15, tzinfo=timezone.utc)
    assert scenario.resolve_time(dropout.end) == datetime(2025, 4, 12, 13, 0, tzinfo=timezone.utc)
    assert scenario.resolve_time("2025-04-12T09:00:00Z").tzinfo == timezone.utc


def test_agent_view_has_only_the_window(scenario: Scenario):
    view = scenario.agent_view()
    assert set(view.model_dump()) == {"id", "start", "duration_hours"}
    assert (view.start, view.end) == (scenario.start, scenario.end)
    assert scenario.answer_key is not None


def test_to_yaml_quotes_fault_times(scenario: Scenario, tmp_path: Path):
    out = tmp_path / "scn.yaml"
    scenario.to_yaml(out, header="# synthetic\n")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# synthetic\n")
    assert "start: '07:00'" in text and "end: '13:00'" in text
    assert Scenario.from_yaml(out) == scenario


def test_unquoted_yaml_time_is_rejected():
    with pytest.raises(ValidationError, match="quote it"):
        FaultSpec(type="SOILING", target="ALL", start=420)


def test_fault_outside_window_is_rejected(scenario: Scenario):
    data = scenario.model_dump(mode="json")
    data["faults"][0]["start"] = "20:00"
    with pytest.raises(ValidationError, match="outside the scenario window"):
        Scenario.model_validate(data)


def test_answer_key_must_name_planted_faults(scenario: Scenario):
    data = scenario.model_dump(mode="json")
    data["answer_key"]["root_causes"].append("STRING_OPEN@INV-03")
    with pytest.raises(ValidationError, match="not planted"):
        Scenario.model_validate(data)


def test_curtailment_is_not_a_root_cause(scenario: Scenario):
    data = scenario.model_dump(mode="json")
    data["faults"].append({"type": "CURTAILMENT", "target": "ALL", "start": "12:00"})
    labels = Scenario.model_validate(data).expected_answer_key().root_causes
    assert "CURTAILMENT@ALL" not in labels
    assert len(labels) == 4


def test_cause_labels():
    assert parse_cause_label("INVERTER_DERATE@INV-07") == (FaultType.INVERTER_DERATE, "INV-07")
    with pytest.raises(ValueError):
        parse_cause_label("derate on inv 7")
    with pytest.raises(ValidationError):
        AnswerKey(root_causes=["NOT_A_FAULT@INV-01"])


def test_naive_and_non_utc_timestamps_are_rejected():
    row = dict(inverter_id="INV-01", ac_kw=1.0, dc_kw=1.0, temp_c=30.0, status="RUNNING")
    with pytest.raises(ValidationError):
        InverterReading(ts=datetime(2025, 1, 1, 8), **row)
    with pytest.raises(ValidationError):
        InverterReading(ts=datetime(2025, 1, 1, 8, tzinfo=timezone(timedelta(hours=3))), **row)
    assert InverterReading(ts=datetime(2025, 1, 1, 8, tzinfo=timezone.utc), **row)


def test_db_timestamp_format_round_trips():
    ts = datetime(2025, 4, 12, 5, 5, tzinfo=timezone.utc)
    assert to_db_ts(ts) == "2025-04-12T05:05:00Z"
    assert from_db_ts(to_db_ts(ts)) == ts


def test_ddl_creates_every_table_with_model_columns():
    con = sqlite3.connect(":memory:")
    for ddl in TABLE_DDL.values():
        con.execute(ddl)
    assert set(TABLE_DDL) == set(TABLE_MODELS)
    for table, model in TABLE_MODELS.items():
        columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")} - {"id"}
        fields = {"payload_json" if f == "payload" else f for f in model.model_fields}
        assert columns == fields, table


def test_tool_result_shape_and_json_round_trip():
    ts = datetime(2025, 1, 15, 7, tzinfo=timezone.utc)
    result = ToolResult(tool="sensor_check", verdict="sensor_low", devices=["WS-01"], start=ts,
                        end=ts + timedelta(hours=1), values={"sensor_vs_reference": 0.79, "onset": ts},
                        thresholds={"sensor_low_ratio": 0.9}, evidence="sensor reads 79% of the reference")
    assert result.finding
    again = ToolResult.model_validate_json(result.model_dump_json())
    assert again.values["onset"] == "2025-01-15T07:00:00Z"
    assert not ToolResult(tool="t", verdict="ok").finding
    assert not ToolResult(tool="t", verdict="inconclusive").finding
    with pytest.raises(ValidationError):
        ToolResult(tool="t", verdict="ok", start=datetime(2025, 1, 15, 7))
    with pytest.raises(ValidationError):
        ToolResult(tool="t", verdict="ok", extra_field=1)


def test_agent_turn_round_trips_with_actions_and_llm_calls():
    ts = datetime(2025, 1, 15, 7, tzinfo=timezone.utc)
    hyp = Hypothesis(id="h1", fault_type=FaultType.COMMS_DROPOUT, targets=["INV-12"], score=0.9)
    turn = AgentTurn(
        diagnosis=Diagnosis(root_causes=[hyp], confidence=0.8),
        actions=[ActionRequest(id="a1", action=ActionType.RESET_COMMS_GATEWAY, target="INV-12", reversible=True,
                               needs_approval=False, rationale="rows missing", expected_effect="data returns")],
        verifications=[Verification(action_id="a1", recovered=True, before_kw=0, after_kw=2100, expected_kw=2150)],
        llm_calls=[LlmCall(role=LlmRole.LOCAL, model="qwen", purpose="hypotheses", prompt_tokens=900),
                   LlmCall(role="api", model="sonnet", purpose="hypotheses", cost_usd=0.012, escalation="close")],
    )
    again = AgentTurn.model_validate_json(turn.model_dump_json())
    assert again == turn
    assert again.diagnosis.root_causes[0].labels == ["COMMS_DROPOUT@INV-12"]
    assert AgentTurn().diagnosis is None and not AgentTurn().done
    outcome = ActionOutcome(action_id="a1", status=ActionStatus.APPLIED, ts=ts)
    assert ActionOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    with pytest.raises(ValidationError):
        LlmCall(role="cloud", model="x", purpose="p")
    with pytest.raises(ValidationError):
        LlmCall(role="local", model="x", purpose="p", cost_usd=-1)
    with pytest.raises(ValidationError):
        ActionOutcome(action_id="a1", status="APPLIED", ts=datetime(2025, 1, 15, 7))


def test_agent_protocol_is_structural():
    class Minimal:
        name = "minimal"

        def start(self, view, telemetry):
            self.view = view

        def step(self, now, outcomes, final=False):
            return AgentTurn(done=final)

    agent: Agent = Minimal()
    assert agent.step(datetime(2025, 1, 15, 7, tzinfo=timezone.utc), [], final=True).done
