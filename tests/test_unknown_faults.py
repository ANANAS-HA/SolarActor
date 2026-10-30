"""Unknown faults (TRANSFORMER_TRIP, NEW_SHADING, FIRMWARE_ALARM) and technician notes.

The faults are deliberately absent from the agent's causal graph and planner; rules-only Raqeeb must
report them as unexplained and escalate, never guess or act. Notes are untrusted free text: they never
name a fault type, and the agent's rules never read them.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache

import pytest

from raqeeb.agent import RaqeebAgent
from raqeeb.agent.plan import LADDERS
from raqeeb.agent.rank import CAUSAL_GRAPH
from raqeeb.bench.harness import run_scenario
from raqeeb.bench.score import score_run
from raqeeb.config import load_plant
from raqeeb.contracts import (
    TABLE_MODELS,
    UNKNOWN_FAULTS,
    ActionType,
    FaultSpec,
    FaultType,
    InverterStatus,
    Scenario,
)
from raqeeb.faults.generator import generate, has_unknown
from raqeeb.faults.injector import FaultInjector
from raqeeb.faults.unknown import FIRMWARE_CODE, TRIP_CODE
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin.controls import apply_action
from tests.leakcheck import assert_no_answer_leak

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)  # clear, cool day, 06:00-18:00 Riyadh
CASES = {
    "trip": {"type": "TRANSFORMER_TRIP", "target": "T2", "start": "06:00"},
    "shade_west": {"type": "NEW_SHADING", "target": "EDGE-W", "start": "04:00", "severity": 0.45},
    "shade_east": {"type": "NEW_SHADING", "target": "EDGE-E", "start": "03:00", "severity": 0.45},
    "firmware": {"type": "FIRMWARE_ALARM", "target": "INV-11", "start": "05:00", "severity": 0.2},
}


def scenario(*faults: dict, seed: int = 21) -> Scenario:
    s = Scenario(id="fx_unknown", seed=seed, start=DAY, duration_hours=12, faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


@lru_cache
def planted(name: str):
    s = scenario(CASES[name])
    inj = FaultInjector.for_scenario(s)
    store = TelemetryStore()
    store.write_batch(inj.run_to_end())
    return s, inj, store


# -- the faults --------------------------------------------------------------------------------------


def test_unknown_faults_are_not_in_the_causal_graph_or_planner():
    assert UNKNOWN_FAULTS == {FaultType.TRANSFORMER_TRIP, FaultType.NEW_SHADING, FaultType.FIRMWARE_ALARM}
    assert not UNKNOWN_FAULTS & set(CAUSAL_GRAPH.nodes)
    assert not UNKNOWN_FAULTS & set(LADDERS)
    codes = {c for _, _, link in CAUSAL_GRAPH.edges(data="link") for c in link.codes}
    assert TRIP_CODE not in codes and FIRMWARE_CODE not in codes


def test_transformer_trip_drops_its_group_together_without_inverter_alarms():
    s, inj, store = planted("trip")
    group = load_plant().layout.transformers["T2"]
    after = store.inverter_readings(start=s.resolve_time("06:05"))
    tripped = after[after["inverter_id"].isin(group)]
    assert (tripped["ac_kw"] == 0).all() and set(tripped["status"]) == {InverterStatus.FAULT.value}
    assert after[~after["inverter_id"].isin(group)]["ac_kw"].max() > 1000
    events = store.read("events")
    assert list(events.loc[events["code"] == TRIP_CODE, "device_id"]) == [load_plant().plant_controller_id]
    assert not set(events.loc[events["device_id"].isin(group), "code"]) - {"LOW_PR", "LOW_STRING_CURRENT", "DC_INPUT_LOW"}
    apply_action(inj.twin, ActionType.DISPATCH_TECHNICIAN, group[0])  # a technician cannot re-energise it
    inj.twin.advance(150)
    assert inj.twin.state.inverters[group[0]].grid_lost


@pytest.mark.parametrize("name, side_hours", [("shade_west", (13, 14)), ("shade_east", (3, 4))])
def test_new_shading_costs_output_only_with_the_sun_low_on_that_side(name, side_hours):
    s, _, store = planted(name)
    edge = load_plant().layout.edges[CASES[name]["target"]]
    wide = store.inverter_wide("ac_kw")
    others = wide.drop(columns=edge).median(axis=1)
    ratio = wide[edge].mean(axis=1) / others
    noon = ratio.between_time("08:30", "09:30").mean()
    low_sun = ratio.between_time(f"{side_hours[0]:02d}:00", f"{side_hours[1]:02d}:00").mean()
    assert noon > 0.97 and low_sun < 0.9


def test_firmware_alarm_holds_the_unit_without_heat_and_a_technician_clears_it():
    s, inj, store = planted("firmware")
    events = store.read("events")
    raised = events[events["device_id"] == "INV-11"]["code"]
    assert Counter(raised)[FIRMWARE_CODE] >= 5 and "DERATE_TEMP" not in set(raised)
    wide = store.inverter_wide("ac_kw").between_time("07:00", "09:00")
    assert wide["INV-11"].max() < 0.81 * load_plant().inverters.ac_kw < wide.drop(columns="INV-11").max().max()
    fresh = FaultInjector.for_scenario(s)
    fresh.advance(4 * 60)
    apply_action(fresh.twin, ActionType.DISPATCH_TECHNICIAN, "INV-11")
    fresh.advance(150)
    assert fresh.twin.state.inverters["INV-11"].firmware_cap_kw is None


# -- rules-only Raqeeb: unexplained and escalated, never guessed ------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_rules_only_raqeeb_escalates_unknown_faults_without_guessing(name):
    s = scenario(CASES[name])
    agent, store = RaqeebAgent(), TelemetryStore()
    record = run_scenario(s, agent, telemetry=store)
    row = score_run(record)
    assert record.predicted == [] and record.actions == []
    assert row["n_unknown"] == 1 and row["unknown_named"] == 0 and row["unknown_escalated"] == 1
    assert "Escalated to a human: symptoms no known cause explains" in record.report.summary_en
    for table in TABLE_MODELS:
        assert_no_answer_leak(store.read(table).rename(columns={"severity": "alarm_severity"}), s)
    assert_no_answer_leak(agent.memory.results, s)
    assert_no_answer_leak(record.escalations, s)


def test_known_faults_next_to_an_unknown_one_are_still_found_and_fixed():
    s = scenario(CASES["trip"] | {"target": "T3", "start": "07:00"},
                 {"type": "STRING_OPEN", "target": "INV-02", "start": "05:00", "severity": 0.25})
    record = run_scenario(s, RaqeebAgent(), telemetry=TelemetryStore())
    assert record.predicted == ["STRING_OPEN@INV-02"]
    assert [(a.request.action, a.request.target) for a in record.actions] == [(ActionType.DISPATCH_TECHNICIAN, "INV-02")]
    assert score_run(record)["unknown_escalated"] == 1


# -- technician notes ---------------------------------------------------------------------------------


def _forbidden_words() -> list[str]:
    words = []
    for f in FaultType:
        words += [f.value, f.value.replace("_", " ").lower()]
    return words


@pytest.mark.parametrize("name", sorted(CASES))
def test_notes_arrive_over_time_and_never_name_the_fault(name):
    s, inj, store = planted(name)
    notes = store.read("technician_notes")
    relevant = [n for n in inj.notes if n.ts >= s.resolve_time(CASES[name]["start"])]
    assert len(notes) == len(inj.notes) >= 3 and len(relevant) >= 2
    assert notes.index.is_monotonic_increasing and notes.index.nunique() > 1
    for text in [*notes["text"], *notes["area_or_device"]]:
        for word in _forbidden_words():
            assert not re.search(re.escape(word), text, re.IGNORECASE), (word, text)
    assert_no_answer_leak(notes, s)


def test_background_notes_avoid_planted_devices_and_appear_in_every_scenario():
    plant = load_plant()
    for s in generate(n=12, seed=5, full_scope=True):
        inj = FaultInjector.for_scenario(s, plant)
        assert 1 <= len(inj.notes) <= 3  # no unknown faults here: background notes only
        busy = {f.target for f in s.faults if f.target.startswith("INV-")}
        for n in inj.notes:
            assert n.area_or_device not in busy and not any(device in n.text for device in busy), n


def test_the_agent_rules_never_read_note_text():
    s, _, _ = planted("trip")
    agent, store = RaqeebAgent(), TelemetryStore()
    run_scenario(s, agent, telemetry=store)
    texts = list(store.read("technician_notes")["text"])
    seen = repr(agent.memory.results) + repr(agent.memory.symptoms) + repr(agent.memory.timeline)
    assert texts and not any(t[:40] in seen for t in texts)


def test_generator_adds_unknown_faults_on_request_only():
    plain = generate(n=30, seed=11, full_scope=True)
    mixed = generate(n=30, seed=11, full_scope=True, n_unknown=6)
    assert not any(map(has_unknown, plain)) and sum(map(has_unknown, mixed)) == 6
    used = Counter(f.type for s in mixed for f in s.faults if f.type in UNKNOWN_FAULTS)
    assert set(used) == UNKNOWN_FAULTS and min(used.values()) >= 2
    for a, b in zip(plain, mixed):  # the other faults are untouched
        assert [f for f in b.faults if f.type not in UNKNOWN_FAULTS] == a.faults
        assert b.answer_key == b.expected_answer_key()
    plant = load_plant()
    for s in filter(has_unknown, mixed):
        FaultInjector.for_scenario(s, plant).run_to_end()


def test_only_the_consult_step_reads_technician_notes_and_nothing_reads_alarm_text():
    from raqeeb.config import REPO_ROOT

    for path in sorted((REPO_ROOT / "raqeeb" / "agent").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.name != "consult.py":
            assert "technician_notes" not in source, path.name
        assert not re.search(r"\[\s*[\"']message[\"']\s*\]|\.message\b", source), path.name
