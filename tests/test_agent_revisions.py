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


# -- unknown-fault guards tuned on scenarios/practice_unknown ------------------------------------------


def test_an_alarm_code_no_cause_covers_blocks_a_single_unit_guess():
    plant = load_plant()
    weak = [sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-04", ratio_to_peers=0.85),
            sym(SymptomKind.ALARM, "INV-04", code="LOW_PR")]
    assert [h.labels[0] for h in rank_causes(weak, plant)[0]] == ["STRING_OPEN@INV-04"]
    hypotheses, unexplained, _ = rank_causes([*weak, sym(SymptomKind.ALARM, "INV-04", code="CTRL_PARAM_LIMIT")], plant)
    assert hypotheses == [] and "ALARM:CTRL_PARAM_LIMIT:INV-04" in unexplained


def test_two_units_weak_together_are_held_until_a_third_joins_or_the_hold_ends(memory):
    from raqeeb.agent.investigate import ONSET_HOLD, mark_common_onset

    def weak(device, onset):
        s = sym(SymptomKind.DEVICE_UNDERPERFORMANCE, device, ratio_to_peers=0.8)
        return s.model_copy(update={"start": onset})

    t = T0 + timedelta(hours=6)
    two = {s.id: s for s in (weak("INV-05", t), weak("INV-09", t))}
    for sid, s in two.items():
        memory.symptoms[sid] = s.model_copy(deep=True)
        memory.seen_at[sid] = t + timedelta(minutes=30)
    mark_common_onset(memory, two, t + timedelta(minutes=30))
    assert all(memory.symptoms[sid].evidence.get("onset_hold") for sid in two)  # no single-unit guess yet
    assert rank_causes([memory.symptoms[sid] for sid in two], load_plant())[0] == []

    three = {**two, **{s.id: s for s in (weak("INV-01", t + timedelta(minutes=5)),)}}
    for sid, s in three.items():
        memory.symptoms[sid] = s.model_copy(deep=True)
        memory.seen_at.setdefault(sid, t + timedelta(minutes=45))
    mark_common_onset(memory, three, t + timedelta(minutes=45))
    assert all(memory.symptoms[sid].evidence.get("common_onset") for sid in three)  # a shared cause

    for sid, s in two.items():  # only two, and the hold is over: single-unit guesses are allowed again
        memory.symptoms[sid] = s.model_copy(deep=True)
    later = t + timedelta(minutes=30) + ONSET_HOLD
    mark_common_onset(memory, two, later)
    assert not any(memory.symptoms[sid].evidence.get("onset_hold") for sid in two)
    assert len(rank_causes([memory.symptoms[sid] for sid in two], load_plant())[0]) == 2


def test_an_open_string_never_explains_a_unit_at_zero_output():
    """Physics: an open string leaves the other strings producing. A unit flagged only by the reference-ratio
    tool (whole-window ratio still well above zero) but at zero output now must not get an open-string guess."""
    plant = load_plant()
    weak = sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-04", ratio_vs_reference=0.6)
    assert [h.labels[0] for h in rank_causes([weak], plant)[0]] == ["STRING_OPEN@INV-04"]
    stopped = sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-04", ratio_vs_reference=0.6, zero_output=True)
    assert rank_causes([stopped], plant)[0] == []

    # End to end: a stopped unit with the unauthorized-command rules switched off is left unexplained
    from raqeeb.agent import RaqeebAgent
    from raqeeb.bench.harness import run_scenario
    from raqeeb.contracts import FaultType

    s = scenario({"type": "UNAUTHORIZED_CMD", "target": "INV-04", "start": "05:00"})
    agent = RaqeebAgent(name="raqeeb_rules", disabled=frozenset({FaultType.UNAUTHORIZED_CMD}))
    record = run_scenario(s, agent)
    assert "STRING_OPEN@INV-04" not in record.predicted and record.actions == []
    assert record.escalations


def test_a_unit_stopped_until_dusk_is_still_not_an_open_string():
    """Zero output is judged at the latest step where peers produce: at dusk (peers below the daylight floor)
    the earlier verdict stands, so the stopped unit never gets an open-string guess or a ticket. (Hand-made,
    like ablation abl_0011's fault, which is not used here: the ablation set is never tuned on.)"""
    from raqeeb.agent import RaqeebAgent
    from raqeeb.bench.harness import run_scenario
    from raqeeb.contracts import FaultType

    s = scenario({"type": "UNAUTHORIZED_CMD", "target": "INV-04", "start": "06:35"}, hours=12, seed=11)
    agent = RaqeebAgent(name="raqeeb_rules", disabled=frozenset({FaultType.UNAUTHORIZED_CMD}))
    record = run_scenario(s, agent)
    assert "STRING_OPEN@INV-04" not in record.predicted and record.actions == []
    assert record.escalations


# -- zero output: never left unreported -----------------------------------------------------------------


@pytest.mark.parametrize("evidence", [{"ratio_to_peers": 0.0, "zero_output": True},  # peer tool: exactly zero
                                      {"ratio_vs_reference": 0.4, "zero_output": True}])  # no peer ratio at all
def test_soiling_never_explains_a_unit_at_zero_output(evidence):
    """Soiling scales output; it never stops a unit. Units at zero while their peers produce stay unexplained
    (and go to a human) unless a cause that can stop a unit covers them."""
    units = ["INV-01", "INV-02", "INV-03", "INV-04"]
    storm = [sym(SymptomKind.PLANT_UNDERPERFORMANCE, PLANT_TARGET, vs_reference=0.8),
             sym(SymptomKind.HIGH_WIND, "WS-01", stow=True),
             sym(SymptomKind.ALARM, PLANT_TARGET, code="LOW_PR")]
    zero = [sym(SymptomKind.DEVICE_UNDERPERFORMANCE, u, common_onset=units, **evidence) for u in units]
    hypotheses, unexplained, _, _ = rank_detail(storm + zero, load_plant())
    assert "SOILING@ALL" in {h.labels[0] for h in hypotheses}
    assert sorted(unexplained) == sorted(s.id for s in zero)
    assert not any(s.id in h.explains for h in hypotheses for s in zero)


def test_a_remote_stop_still_explains_its_unit_at_zero_output():
    stopped = sym(SymptomKind.DEVICE_UNDERPERFORMANCE, "INV-05", ratio_to_peers=0.0, zero_output=True)
    hypotheses, unexplained, _, _ = rank_detail([stopped, sym(SymptomKind.UNEXPECTED_COMMAND, "INV-05")], load_plant())
    assert [h.labels[0] for h in hypotheses] == ["UNAUTHORIZED_CMD@INV-05"] and unexplained == []
    assert stopped.id in hypotheses[0].explains


def test_units_at_zero_during_a_dust_storm_are_escalated_not_put_down_to_soiling():
    """End to end, hand-made: a dust storm, then a quiet trip of transformer T1 (no alarm of its own). Rules-only
    Raqeeb must name the soiling and escalate the four units at zero, not let the soiling explain them."""
    from raqeeb.bench.harness import run_scenario

    s = scenario({"type": "SOILING", "target": "ALL", "start": "04:30", "end": "05:40", "severity": 0.2},
                 {"type": "TRANSFORMER_TRIP", "target": "T1", "start": "06:30", "variant": "quiet"}, seed=7)
    agent = RaqeebAgent(name="raqeeb_rules")
    record = run_scenario(s, agent)
    t1 = set(agent.memory.plant.layout.area_of("T1"))
    assert record.predicted == ["SOILING@ALL"] and record.actions and all(
        a.request.action == ActionType.CREATE_CLEANING_WORK_ORDER for a in record.actions)
    assert any(e.subject == "unexplained" and t1 <= set(e.devices) for e in record.escalations)
    soiling = agent.memory.causes["SOILING@ALL"].hypothesis
    assert not any(agent.memory.symptoms[sid].device_id in t1 for sid in soiling.explains)
