import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from raqeeb.contracts import (
    TABLE_DDL,
    TABLE_MODELS,
    AnswerKey,
    FaultSpec,
    FaultType,
    InverterReading,
    Scenario,
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


def test_agent_view_hides_answer_key(scenario: Scenario):
    assert scenario.without_answer_key().answer_key is None
    assert scenario.answer_key is not None


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
