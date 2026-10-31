"""Trajectory files (ui/runlog.py v3): the schema loads old and new files, the example fixture holds the
agent's reasoning per poll with nothing from the answer key in the agent-side data, and the fixture
scenario's run shows the node order and the dusty-sensor finding with its numbers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raqeeb.agent.graph import NODES
from raqeeb.contracts import Scenario
from raqeeb.ui import source
from raqeeb.ui.runlog import (
    READABLE_VERSIONS,
    REPLAY_VERSION,
    GateDecision,
    Mode,
    RunLog,
)
from tests.leakcheck import assert_no_answer_leak

FIXTURE_SCENARIO = Path(__file__).parent / "fixtures" / "scn_example.yaml"
FIXTURE_LOG = Path(__file__).parent / "fixtures" / "trajectory_v3_example.json"
V3_POLL_FIELDS = ("checks", "symptoms", "candidates", "gate", "transcripts")
V2_POLL_FIELDS = ("notes", "proposals")
# Agent-side poll fields that are inputs or tool outputs (never the agent's own conclusions, which name
# causes by design): the full leak check applies to them
INPUT_FIELDS = ("outcomes", "checks", "findings", "symptoms", "notes")


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return Scenario.from_yaml(FIXTURE_SCENARIO)


@pytest.fixture(scope="module")
def log() -> RunLog:
    return RunLog.load(FIXTURE_LOG)


def _poll_dump(poll) -> dict:
    return json.loads(poll.model_dump_json())


# -- schema and versions -------------------------------------------------------------------------


def test_fixture_is_a_finished_v3_log(log, scenario):
    assert (log.version, log.mode, log.agent) == (REPLAY_VERSION, Mode.RULES, "raqeeb_rules")
    assert log.finished and log.answer_key is not None and log.bench is None  # a console run: no bench truth
    assert log.incident == scenario.agent_view()
    assert sorted(log.answer_key.root_causes) == sorted(scenario.answer_key.root_causes)
    assert len(log.polls) == int(scenario.duration_hours * 60 / log.poll_min)
    assert log.polls[-1].final and not any(p.final for p in log.polls[:-1])
    assert set(log.telemetry) and all(isinstance(rows, list) for rows in log.telemetry.values())


def test_v3_fields_are_filled_per_poll(log):
    assert all(p.checks for p in log.polls)  # every tool ran at every poll
    tools = {c.tool for p in log.polls for c in p.checks}
    assert {"sensor_check", "plant_performance", "peer_outliers", "inverter_performance", "wind_summary"} <= tools
    for poll in log.polls:
        for check in poll.checks:
            assert check.evidence and check.start is not None and check.end is not None
            assert check.end <= poll.ts  # the window never runs ahead of the poll
    assert any(p.symptoms for p in log.polls) and any(p.candidates for p in log.polls)
    assert any(p.gate for p in log.polls)
    assert not any(p.llm_calls or p.transcripts for p in log.polls)  # rules only: no model, no transcripts
    for poll in log.polls:
        assert [s.id for s in poll.symptoms] == sorted(s.id for s in poll.symptoms)
        selected = [c for c in poll.candidates if c.selected]
        assert all(c.score >= 0 for c in poll.candidates)
        assert [c.label for c in selected] == [c.label for c in poll.candidates[:len(selected)]]  # selection first


def test_gate_records_match_the_sent_requests(log):
    sent = {r.id: r for p in log.polls for r in p.actions}
    gate = {g.request.id: g for p in log.polls for g in p.gate if g.decision != GateDecision.REJECTED}
    assert set(gate) == set(sent)
    for action_id, g in gate.items():
        assert g.request == sent[action_id]
        assert g.decision == (GateDecision.NEEDS_APPROVAL if g.request.needs_approval else GateDecision.AUTO)
        assert g.cause and g.cause.split("@")[0] in g.request.rationale
    entries = {a.request.id: a for a in log.actions}
    assert set(entries) == set(sent)
    for action_id, g in gate.items():
        assert entries[action_id].gated == (g.decision == GateDecision.NEEDS_APPROVAL)
        assert entries[action_id].cause == g.cause
    assert any(g.decision == GateDecision.NEEDS_APPROVAL for g in gate.values())  # set_power_limit


def test_round_trip_and_gzip(log, tmp_path):
    plain = tmp_path / "t.json"
    packed = tmp_path / "t.json.gz"
    log.save(plain)
    log.save(packed)
    assert packed.stat().st_size < plain.stat().st_size / 3
    assert RunLog.load(packed) == RunLog.load(plain) == log
    assert RunLog.from_json(log.to_json()) == log


def test_older_versions_still_load(log):
    data = json.loads(FIXTURE_LOG.read_text(encoding="utf-8"))
    data.pop("bench", None)
    for poll in data["polls"]:
        for field in V3_POLL_FIELDS:
            poll.pop(field, None)
    v2 = json.loads(json.dumps({**data, "version": 2}))
    loaded = RunLog.from_json(json.dumps(v2))
    assert loaded.version == REPLAY_VERSION and len(loaded.polls) == len(log.polls)
    assert all(not getattr(p, f) for p in loaded.polls for f in V3_POLL_FIELDS)
    assert loaded.actions == log.actions and loaded.polls[-1].report == log.polls[-1].report
    for poll in data["polls"]:
        for field in V2_POLL_FIELDS:
            poll.pop(field, None)
    for action in data["actions"]:
        action.pop("cause", None)
    v1 = {**data, "version": 1}
    loaded = RunLog.from_json(json.dumps(v1))
    assert loaded.version == REPLAY_VERSION and all(a.cause == "" for a in loaded.actions)
    assert [p.diagnosis for p in loaded.polls] == [p.diagnosis for p in log.polls]
    with pytest.raises(ValueError):
        RunLog.from_json(json.dumps({**data, "version": max(READABLE_VERSIONS) + 1}))


def test_shipped_replays_load_as_v3():
    for path in source.replay_files():
        loaded = RunLog.load(path)
        assert loaded.version == REPLAY_VERSION and loaded.finished
        assert all(not p.checks and not p.gate for p in loaded.polls)  # v1 recordings: the v3 fields are empty


# -- nothing from the answer key in the agent-side data ------------------------------------------


def test_agent_side_poll_data_leaks_nothing(log, scenario):
    assert_no_answer_leak(log.incident, scenario, strict=True)
    for poll in log.polls:
        dumped = _poll_dump(poll)
        for field in INPUT_FIELDS:
            assert_no_answer_leak(dumped[field], scenario)
        # No answer field anywhere in a poll (the conclusions name causes by design, so only the keys are checked)
        text = json.dumps(dumped)
        for key in ("faults", "answer_key", "severity", "seed", "truth"):
            assert f'"{key}":' not in text, (poll.ts, key)
    # The answer key lives in its own labelled section only
    assert log.answer_key is not None and all("answer_key" not in json.dumps(_poll_dump(p)) for p in log.polls)


# -- the fixture scenario's run ------------------------------------------------------------------


def _node_order_ok(nodes: list[str]) -> bool:
    """Nodes appear in graph order; verify may loop back to plan once (plan, safety, execute, verify again)."""
    order = {n: k for k, n in enumerate(NODES)}
    order["start"] = -1
    loops, last = 0, -1
    for node in nodes:
        k = order[node]
        if k < last:
            if node == "plan" and NODES[last] == "verify" and loops == 0:
                loops += 1
            else:
                return False
        last = k
    return True


def test_timeline_follows_the_graph_order_per_poll(log):
    assert [t.node for t in log.polls[0].timeline][:1] == ["start"]
    for poll in log.polls:
        nodes = [t.node for t in poll.timeline]
        assert _node_order_ok(nodes), (poll.ts, nodes)
        assert all(t.ts == poll.ts for t in poll.timeline if t.node != "start")
    nodes_seen = {t.node for p in log.polls for t in p.timeline}
    assert {"triage", "investigate", "rank", "plan", "safety", "execute", "verify", "report"} <= nodes_seen


def test_trap_turn_shows_the_sensor_check_leading_to_the_finding(log):
    """The dusty sensor (SENSOR_SOILING@WS-01 at 07:00 UTC) is found from the sensor check alone: the check
    turns from ok to a sensor_low finding with its numbers, the symptom appears in the same poll, and the
    ranker selects the sensor cause."""
    first = next(p for p in log.polls if any(c.tool == "sensor_check" and c.verdict.startswith("sensor_low")
                                             for c in p.checks))
    before = log.polls[log.polls.index(first) - 1]
    assert next(c for c in before.checks if c.tool == "sensor_check").verdict == "ok"
    check = next(c for c in first.checks if c.tool == "sensor_check")
    assert check.devices == ["WS-01"]
    assert check.verdict in ("sensor_low", "sensor_low_panels_too")
    assert 0.70 < check.values["sensor_vs_reference"] < 0.85  # about 20% low
    assert check.values["clear_steps"] >= 6 and check.thresholds  # the limits the verdict used
    assert "sensor" in check.evidence.lower()
    symptom = next(s for s in first.symptoms if s.kind == "SENSOR_DEVIATION" and s.device == "WS-01")
    assert symptom.since <= first.ts
    selected = [c for c in first.candidates if c.selected]
    assert "SENSOR_SOILING@WS-01" in [c.label for c in selected]
    assert symptom.id in next(c for c in selected if c.label == "SENSOR_SOILING@WS-01").explains
    assert any(g.request.action.value == "flag_sensor_use_reference" and g.decision == GateDecision.AUTO
               for g in first.gate)
    assert first.diagnosis is not None  # the diagnosis changed at this poll
