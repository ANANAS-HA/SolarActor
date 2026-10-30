"""Milestone 3 rule fixes: known device losses, soiling vs a weak unit, per-cause support for
irreversible actions, tracker alarms, diagnosis revision and bounded repair."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from raqeeb.agent import RaqeebAgent
from raqeeb.agent.plan import MAX_FAILED_FIXES, fixes_for, plan
from raqeeb.agent.rank import rank, rank_causes, rank_detail
from raqeeb.agent.safety import IRREVERSIBLE_MIN_CONFIDENCE, check
from raqeeb.agent.state import Cause, CauseStatus, PendingAction
from raqeeb.config import load_plant
from raqeeb.contracts import (
    PLANT_TARGET,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    ActionType,
    AgentScenario,
    Diagnosis,
    FaultSpec,
    Hypothesis,
    Scenario,
    SymptomKind,
)
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.metrics import plant_performance
from tests.leakcheck import assert_no_answer_leak
from tests.test_agent import T0, _req, run, sym

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)  # clear, cool day, 06:00 Riyadh


def scenario(*faults: dict, seed: int = 4, hours: float = 10) -> Scenario:
    s = Scenario(id="fx_m3", seed=seed, start=DAY, duration_hours=hours, faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


# -- (a) known device losses -------------------------------------------------------------------------


def test_a_stopped_inverter_is_not_a_plant_wide_loss():
    s = scenario({"type": "UNAUTHORIZED_CMD", "target": "INV-05", "start": "05:00"})
    store = TelemetryStore()
    store.write_batch(FaultInjector.for_scenario(s).run_to_end())
    meter, fleet = plant_performance(store), plant_performance(store, exclude=["INV-05"])
    assert fleet.values["vs_reference"]["ratio"] > meter.values["vs_reference"]["ratio"] + 0.03
    assert fleet.verdict == "ok" and "without INV-05" in fleet.evidence
    assert_no_answer_leak(fleet, s)


def test_raqeeb_isolates_a_stopped_inverter_and_orders_no_wash():
    s = scenario({"type": "UNAUTHORIZED_CMD", "target": "INV-05", "start": "05:00"})
    record, spy, store = run(s)
    assert record.predicted == ["UNAUTHORIZED_CMD@INV-05"]
    actions = [(a.request.action, a.request.target) for a in record.actions]
    assert (ActionType.ISOLATE_DEVICE, "INV-05") in actions
    assert not any(a == ActionType.CREATE_CLEANING_WORK_ORDER for a, _ in actions)
    assert "INV-05" in spy.agent.memory.known_losses
    assert_no_answer_leak(spy.agent.memory.results, s)


# -- (b) soiling and a weak unit; (d) tracker alarms --------------------------------------------------


@pytest.mark.parametrize("ratio_to_peers, string_open", [(0.75, True), (0.97, False)])
def test_soiling_does_not_hide_a_unit_well_below_its_peers(ratio_to_peers, string_open):
    symptoms = [sym(SymptomKind.PLANT_UNDERPERFORMANCE, PLANT_TARGET, vs_reference=0.8),
                sym(SymptomKind.HIGH_WIND, "WS-01", stow=True),
                sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-03", ratio_to_peers=ratio_to_peers),
                sym(SymptomKind.ALARM, "INV-03", code="LOW_STRING_CURRENT")]
    labels = {h.labels[0] for h in rank_causes(symptoms, load_plant())[0]}
    assert "SOILING@ALL" in labels
    assert ("STRING_OPEN@INV-03" in labels) is string_open


def test_tracker_alarms_are_explained_by_the_stuck_tracker():
    symptoms = [sym(SymptomKind.TRACKER_ANGLE_FLAT, "INV-04"), sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-04"),
                sym(SymptomKind.ALARM, "INV-04", code="LOW_PR"), sym(SymptomKind.ALARM, "INV-04", code="DC_INPUT_LOW")]
    hypotheses, unexplained, confidence, supports = rank_detail(symptoms, load_plant())
    assert [h.labels[0] for h in hypotheses] == ["TRACKER_STUCK@INV-04"]
    assert unexplained == [] and confidence == 1.0 and supports["TRACKER_STUCK@INV-04"] == 1.0


# -- (c) per-cause support -------------------------------------------------------------------------


@pytest.fixture
def memory():
    agent = RaqeebAgent()
    agent.start(AgentScenario(id="unit", start=T0, duration_hours=4), TelemetryStore())
    return agent.memory


@pytest.mark.parametrize("support, allowed", [(0.95, True), (0.5, False)])
def test_irreversible_action_uses_the_support_of_its_own_cause(memory, support, allowed):
    h = Hypothesis(id="u", fault_type="UNAUTHORIZED_CMD", targets=["INV-05"])
    memory.causes["UNAUTHORIZED_CMD@INV-05"] = Cause(hypothesis=h, first_seen=T0, last_selected=T0, confidence=support)
    memory.diagnosis = Diagnosis(root_causes=[h], confidence=0.3)  # unrelated symptoms keep the whole diagnosis low
    checked, why = check(memory, _req("isolate_device", "INV-05"), "UNAUTHORIZED_CMD@INV-05", T0 + timedelta(hours=1))
    assert (checked is not None) is allowed, why
    assert allowed or "support" in why


def test_isolation_support_needs_the_command_itself():
    plant = load_plant()
    alarm_only = [sym(SymptomKind.ALARM, "INV-05", code="REMOTE_STOP"), sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-05")]
    _, _, _, supports = rank_detail(alarm_only, plant)
    assert supports.get("UNAUTHORIZED_CMD@INV-05", 0) < IRREVERSIBLE_MIN_CONFIDENCE
    _, _, _, supports = rank_detail([*alarm_only, sym(SymptomKind.UNEXPECTED_COMMAND, "INV-05")], plant)
    assert supports["UNAUTHORIZED_CMD@INV-05"] >= IRREVERSIBLE_MIN_CONFIDENCE


# -- (e) revision ----------------------------------------------------------------------------------


def test_a_better_supported_cause_replaces_an_acted_on_one(memory):
    early = [sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-04"), sym(SymptomKind.ALARM, "INV-04", code="LOW_PR")]
    for s in early:
        memory.symptoms[s.id] = s
    memory.active = {s.id for s in early}
    rank({"memory": memory, "now": T0})
    assert set(memory.causes) == {"STRING_OPEN@INV-04"}
    ticket = ActionRequest(id="raqeeb-1", action="dispatch_technician", target="INV-04", reversible=True,
                           needs_approval=False, rationale="", expected_effect="")
    memory.pending["raqeeb-1"] = PendingAction(request=ticket, cause="STRING_OPEN@INV-04", requested_at=T0,
                                               status=ActionStatus.APPLIED)
    memory.causes["STRING_OPEN@INV-04"].action_ids.append("raqeeb-1")
    memory.causes["STRING_OPEN@INV-04"].status = CauseStatus.ACTING

    later = sym(SymptomKind.TRACKER_ANGLE_FLAT, "INV-04")  # the tracker evidence arrives
    memory.symptoms[later.id] = later
    memory.active.add(later.id)
    rank({"memory": memory, "now": T0 + timedelta(minutes=15)})
    assert set(memory.causes) == {"TRACKER_STUCK@INV-04"}
    tracker = memory.causes["TRACKER_STUCK@INV-04"]
    assert tracker.action_ids == ["raqeeb-1"] and memory.pending["raqeeb-1"].cause == tracker.label
    assert tracker.status == CauseStatus.OPEN  # resume_tracking can still go out at once
    assert memory.revised == [("STRING_OPEN@INV-04", "TRACKER_STUCK@INV-04")]
    assert [h.labels[0] for h in memory.diagnosis.root_causes] == ["TRACKER_STUCK@INV-04"]


# -- (f) bounded repair ----------------------------------------------------------------------------


def test_repair_stops_after_the_failed_fixes_limit(memory):
    h = Hypothesis(id="s", fault_type="SOILING", targets=[PLANT_TARGET])
    cause = Cause(hypothesis=h, first_seen=T0, last_selected=T0, status=CauseStatus.REOPENED,
                  attempts=MAX_FAILED_FIXES, note="plant at 85% of expected")
    memory.causes[cause.label] = cause
    requests, reason = fixes_for(memory, cause, T0)
    assert requests == [] and reason.startswith("escalated") and f"{MAX_FAILED_FIXES} fixes failed" in reason
    assert plan({"memory": memory, "now": T0})["proposals"] == []
    assert cause.status == CauseStatus.ESCALATED and "85%" in cause.note
    assert plan({"memory": memory, "now": T0 + timedelta(hours=1)})["proposals"] == []  # never retried


def test_comms_that_never_come_back_end_escalated_not_retried():
    s = Scenario(id="fx_stuck_comms", seed=3, start=datetime(2025, 1, 15, 4, tzinfo=timezone.utc),
                 duration_hours=8, faults=[FaultSpec(type="COMMS_DROPOUT", target="INV-12", start="06:00")])
    inj = FaultInjector.for_scenario(s)
    store, agent = TelemetryStore(), RaqeebAgent()
    agent.start(s.agent_view(), store)
    outcomes, requested = [], []
    while not inj.finished:
        store.write_batch(inj.advance(15))
        turn = agent.step(inj.twin.clock, outcomes, final=inj.finished)
        requested += turn.actions
        # every request "applied", but the twin is never touched: nothing ever fixes the link
        outcomes = [ActionOutcome(action_id=a.id, status=ActionStatus.APPLIED, ts=inj.twin.clock) for a in turn.actions]
    assert [(a.action, a.target) for a in requested] == [(ActionType.RESET_COMMS_GATEWAY, "INV-12"),
                                                          (ActionType.DISPATCH_TECHNICIAN, "INV-12")]
    cause = agent.memory.causes["COMMS_DROPOUT@INV-12"]
    assert cause.status == CauseStatus.ESCALATED and cause.attempts == 2
    assert "Escalated to a human: COMMS_DROPOUT@INV-12" in agent.memory.report.summary_en
