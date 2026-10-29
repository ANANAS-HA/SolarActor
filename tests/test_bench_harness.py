"""Harness, fake agents, threshold baseline and the bench CLI on the fixture scenario."""

import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pytest

from raqeeb.baseline.threshold import ThresholdBaseline
from raqeeb.bench import run as bench_run
from raqeeb.bench.fakes import EmptyAgent, OracleAgent
from raqeeb.bench.harness import ReferenceEnergy, reference_energy, run_scenario
from raqeeb.bench.memory import MemoryTelemetry
from raqeeb.bench.score import score_run
from raqeeb.config import load_plant
from raqeeb.contracts import (
    TABLE_MODELS,
    ActionRequest,
    ActionStatus,
    AgentTurn,
    Event,
    InverterReading,
    LlmCall,
    Scenario,
    Severity,
    TelemetryBatch,
)
from tests.leakcheck import assert_no_answer_leak

FIXTURE = Path(__file__).parent / "fixtures" / "scn_example.yaml"


@lru_cache
def scenario() -> Scenario:
    return Scenario.from_yaml(FIXTURE)


@lru_cache
def reference() -> ReferenceEnergy:
    return reference_energy(scenario())


def run(agent, **kw):
    return run_scenario(scenario(), agent, reference=reference(), **kw)


# -- fake agents bound the scores ------------------------------------------------------------


def test_oracle_scores_perfectly():
    calls = [LlmCall(role="local", model="q", purpose="hypotheses"),
             LlmCall(role="api", model="s", purpose="hypotheses", cost_usd=0.02)]
    rec = run(OracleAgent(scenario(), llm_calls=calls))
    row = score_run(rec)
    assert row["precision"] == row["recall"] == 1.0 and row["all_found"]
    assert row["n_actions"] == 4 and row["false_actions"] == 0
    assert all(a.status == ActionStatus.APPLIED for a in rec.actions)
    assert row["approvals"] == 1  # set_power_limit, gated by configs/actions.yaml
    assert row["mitigated_at_end"] and row["unmitigated_at_end"] == ""
    assert 0 < row["ttvr_min"] <= 60 * scenario().duration_hours
    assert 0 < row["mwh_recovered"] < row["mwh_lost"]  # cleaning helps, the storm itself is lost
    assert (row["llm_calls"], row["local_calls"], row["cost_usd"]) == (2, 1, 0.02)


def test_empty_agent_changes_nothing():
    row = score_run(run(EmptyAgent()))
    assert row["recall"] == 0.0 and math.isnan(row["precision"]) and math.isnan(row["ttvr_min"])
    assert row["mwh_recovered"] == 0.0  # same seed, no actions: identical to the no-action run
    assert row["mwh_lost"] == pytest.approx(reference().decoy_only_mwh - reference().no_action_mwh)
    # the comms dropout ends at 13:00 by itself; soiling, sensor and derate stay
    assert row["unmitigated_at_end"] == "INVERTER_DERATE@INV-07;SENSOR_SOILING@WS-01;SOILING@ALL"


# -- threshold baseline ------------------------------------------------------------------------


def test_threshold_baseline_falls_into_the_trap():
    rec = run(ThresholdBaseline())
    row = score_run(rec)
    # the dusty sensor hides the soiling alarms; LOW_PR on the derated inverter becomes "soiling"
    assert rec.predicted == ["COMMS_DROPOUT@INV-12", "INVERTER_DERATE@INV-07", "SOILING@INV-07"]
    assert (row["tp"], row["fp"], row["fn"]) == (2, 1, 2)
    assert [(a.request.action.value, a.request.target) for a in rec.actions] == [
        ("set_power_limit", "INV-07"), ("create_cleaning_work_order", "INV-07"), ("reset_comms_gateway", "INV-12")]
    assert rec.actions[0].request.params == {"kw": load_plant().inverters.ac_kw * 0.5}
    assert math.isnan(row["ttvr_min"])  # it never verifies
    assert row["mwh_recovered"] < 0  # the fixed 50% limit is below the derate cap


def _event(minute: int, device: str, code: str) -> Event:
    return Event(ts=datetime(2025, 4, 12, 8, minute, tzinfo=timezone.utc), device_id=device, code=code,
                 severity=Severity.WARNING, message="m")


def test_threshold_baseline_one_ticket_per_alarm():
    telemetry = MemoryTelemetry()
    view = scenario().agent_view()
    agent = ThresholdBaseline()
    agent.start(view, telemetry)
    telemetry.write_batch(TelemetryBatch(events=[
        _event(0, "WS-01", "HIGH_WIND"),  # no related fault: no ticket
        _event(0, "PPC-01", "GRID_LIMIT_ACTIVE"),  # decoy: no ticket
        _event(5, "INV-03", "LOW_PR"),  # -> SOILING (first related fault)
        _event(5, "INV-03", "LOW_STRING_CURRENT"),  # second ticket, same cleaning order
        _event(10, "INV-03", "LOW_PR"),  # repeat: no new ticket
        _event(10, "INV-05", "COMM_LOSS"),
    ]))
    turn = agent.step(datetime(2025, 4, 12, 8, 15, tzinfo=timezone.utc), [])
    labels = [label for h in turn.diagnosis.root_causes for label in h.labels]
    assert labels == ["SOILING@INV-03", "SOILING@INV-03", "COMMS_DROPOUT@INV-05"]
    assert [(a.action.value, a.target, a.needs_approval) for a in turn.actions] == [
        ("create_cleaning_work_order", "INV-03", False), ("reset_comms_gateway", "INV-05", False)]
    later = agent.step(datetime(2025, 4, 12, 8, 30, tzinfo=timezone.utc), [])
    assert later.diagnosis is None and later.actions == []  # nothing new


# -- the harness is the environment ------------------------------------------------------------


class Scripted:
    """Sends fixed requests on its first turn and records what it is given."""

    name = "scripted"

    def __init__(self, requests: list[ActionRequest]):
        self.requests = requests
        self.outcomes = []

    def start(self, view, telemetry):
        self.sent = False

    def step(self, now, outcomes, final=False):
        self.outcomes += outcomes
        actions = [] if self.sent else self.requests
        self.sent = True
        return AgentTurn(actions=actions)


def _req(action_id, action, target, needs_approval=False, **params):
    return ActionRequest(id=action_id, action=action, target=target, params=params, reversible=True,
                         needs_approval=needs_approval, rationale="r", expected_effect="e")


def test_harness_gates_with_the_config_not_the_agent():
    agent = Scripted([_req("p", "set_power_limit", "INV-07", kw=1000.0),  # claims no approval needed
                      _req("r", "reset_comms_gateway", "INV-12"),
                      _req("x", "reset_comms_gateway", "INV-99"),
                      _req("r", "reset_comms_gateway", "INV-12")])
    asked = []
    rec = run(agent, approve=lambda request: asked.append(request.id) or False)
    assert asked == ["p"]
    assert [(a.request.id, a.status, a.gated) for a in rec.actions] == [
        ("p", ActionStatus.REJECTED, True), ("r", ActionStatus.APPLIED, False),
        ("x", ActionStatus.FAILED, False), ("r", ActionStatus.FAILED, False)]
    assert "duplicate" in rec.actions[3].note and "INV-99" in rec.actions[2].note
    assert [o.status for o in agent.outcomes] == [a.status for a in rec.actions]  # fed back next turn
    assert agent.outcomes[0].ts == rec.actions[0].ts == scenario().start + timedelta(minutes=15)


def test_poll_interval_must_fit_the_step():
    with pytest.raises(ValueError):
        run(EmptyAgent(), poll_min=7)


class Spy:
    """Reads every table at every poll so the test can check what the agent can see."""

    name = "spy"

    def start(self, view, telemetry):
        self.view, self.telemetry, self.seen, self.polls = view, telemetry, [], []

    def step(self, now, outcomes, final=False):
        self.polls.append(now)
        self.seen.append({table: self.telemetry.read(table) for table in TABLE_MODELS})
        return AgentTurn()


def test_agent_sees_only_the_view_and_past_telemetry():
    spy = Spy()
    run(spy, poll_min=60)
    s = scenario()
    assert_no_answer_leak(spy.view, s, strict=True)
    assert spy.polls[0] == s.start + timedelta(hours=1) and spy.polls[-1] == s.end
    for now, tables in zip(spy.polls, spy.seen):
        # events.severity is the alarm's severity (contracts table), not the planted fault's
        assert_no_answer_leak({**tables, "events": tables["events"].drop(columns="severity")}, s)
        for table, df in tables.items():
            assert df.empty or df.index.max() < now, table  # nothing from the future
    readings = spy.seen[-1]["inverter_readings"]
    gap = readings[(readings["inverter_id"] == "INV-12")].loc["2025-04-12 11:15":"2025-04-12 12:55"]
    assert gap.empty  # comms dropout: rows missing, never zeros


# -- telemetry stand-in --------------------------------------------------------------------------


def test_memory_telemetry_reads_like_the_store():
    t0 = datetime(2025, 4, 12, 8, tzinfo=timezone.utc)
    readings = [InverterReading(ts=t0 + timedelta(minutes=5 * k), inverter_id=i, ac_kw=1.0, dc_kw=1.0, temp_c=30,
                                status="RUNNING") for k in range(3) for i in ("INV-01", "INV-02") if (k, i) != (1, "INV-02")]
    telemetry = MemoryTelemetry()
    telemetry.write_batch(TelemetryBatch(inverter_readings=readings, events=[_event(5, "INV-01", "LOW_PR")]))
    df = telemetry.read("inverter_readings", t0, t0 + timedelta(minutes=10))
    assert len(df) == 3 and str(df.index.tz) == "UTC" and df.index.name == "ts"
    assert list(df.columns) == [f for f in InverterReading.model_fields if f != "ts"]
    assert df["status"].iloc[0] == "RUNNING" and type(df["status"].iloc[0]) is str
    assert len(telemetry.read("inverter_readings", inverter_id="INV-02")) == 2
    events = telemetry.read("events")
    assert list(events.columns[:2]) == ["id", "device_id"] and events["id"].tolist() == [1]
    empty = telemetry.read("meter")
    assert empty.empty and str(empty.index.tz) == "UTC"
    with pytest.raises(ValueError):
        telemetry.read("nope")
    with pytest.raises(ValueError):
        telemetry.read("meter", plant_ac_kw="1")


# -- CLI -----------------------------------------------------------------------------------------


def test_bench_run_writes_results(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench_run, "load_scenarios", lambda ids: [scenario()])
    bench_run.main(["--agents", "threshold,oracle,empty", "--out", str(tmp_path)])
    results = pd.read_csv(tmp_path / "results.csv")
    summary = pd.read_csv(tmp_path / "summary.csv")
    assert len(results) == 3 and list(summary["agent"]) == ["threshold", "oracle", "empty"]
    assert summary.set_index("agent").loc["oracle", "recall"] == 1.0
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "## Where threshold gets it wrong" in text and "missed SENSOR_SOILING@WS-01, SOILING@ALL" in text
    assert "wrote" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        bench_run.main(["--agents", "raqeeb"])
