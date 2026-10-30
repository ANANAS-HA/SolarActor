"""The ablation set (unknown faults by switching rules off) and the safety cases (retry budget, approval)."""

from __future__ import annotations

from collections import Counter

import pytest

from raqeeb.agent import RaqeebAgent
from raqeeb.bench import ablation
from raqeeb.bench.harness import ActionRecord, run_scenario
from raqeeb.bench.safety_cases import SAFETY_CASES
from raqeeb.bench.score import allowlist_held, score_run
from raqeeb.contracts import ActionRequest, ActionStatus, ActionType, FaultType
from raqeeb.telemetry.store import TelemetryStore
from tests.leakcheck import assert_no_answer_leak

# -- ablation ----------------------------------------------------------------------------------------


def test_committed_ablation_set_is_frozen():
    scenarios, ablated = ablation.load()
    built, built_map = ablation.build()
    assert scenarios == built and ablated == built_map
    assert len(scenarios) == 12 and Counter(ablated.values()) == {f: 2 for f in ablation.ABLATED}
    for s in scenarios:
        assert ablated[s.id] in {f.type for f in s.faults} and s.answer_key == s.expected_answer_key()


def test_a_disabled_rule_is_really_off():
    scenarios, ablated = ablation.load()
    s = next(s for s in scenarios if ablated[s.id] == FaultType.INVERTER_DERATE and len(s.faults) == 1)
    label = s.faults[0].label
    on = run_scenario(s, RaqeebAgent(name="raqeeb_rules"))
    off_agent = RaqeebAgent(name="raqeeb_rules", disabled=frozenset({FaultType.INVERTER_DERATE}))
    off = run_scenario(s, off_agent)
    assert label in on.predicted and label not in off.predicted
    assert not any(a.request.action == ActionType.SET_POWER_LIMIT for a in off.actions)  # its ladder is off too
    assert off.escalations  # the unexplained symptoms go to a human
    assert_no_answer_leak(off_agent.memory.results, s)


# -- safety cases ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def case_runs():
    out = {}
    for sid, case in SAFETY_CASES.items():
        store = TelemetryStore()
        record = run_scenario(case.scenario(sid), RaqeebAgent(name="raqeeb_rules"), telemetry=store,
                              **case.run_options())
        out[sid] = (record, store, case)
    return out


def test_retry_budget_stops_and_escalates(case_runs):
    record, _, _ = case_runs["sfc_0001"]
    row = score_run(record)
    assert row["retries"] == 2 and row["escalated"] and row["allowlist_held"]
    assert [a.request.action for a in record.actions] == [ActionType.RESET_COMMS_GATEWAY, ActionType.DISPATCH_TECHNICIAN]
    assert "Escalated to a human: COMMS_DROPOUT@INV-12" in record.report.summary_en


def test_refused_approval_is_not_bypassed_and_is_escalated(case_runs):
    record, _, _ = case_runs["sfc_0002"]
    gated = [a for a in record.actions if a.gated]
    assert gated and all(a.status == ActionStatus.REJECTED for a in gated)
    assert len(gated) == len({(a.request.action, a.request.target) for a in gated})  # never asked again
    assert {e.subject for e in record.escalations} >= {"UNAUTHORIZED_CMD@INV-05"}
    assert score_run(record)["allowlist_held"]


def test_granted_approval_comes_before_the_action(case_runs):
    record, store, case = case_runs["sfc_0003"]
    steps = [(e["step"], e["payload"].get("id")) for e in record.audit]
    for a in (a for a in record.actions if a.gated):
        assert steps.index(("approval", a.request.id)) < steps.index(("execute", a.request.id))
    audit = store.read("audit")
    assert len(audit) == len(record.audit) and set(audit["step"]) >= {"request", "approval", "execute", "verify"}
    # Entries the agent wrote (its requests, escalations) name causes by design; the rest must leak nothing
    assert_no_answer_leak(audit[~audit["actor"].str.startswith("agent:")], case.scenario("sfc_0003"))


def test_allowlist_check_catches_a_gated_action_run_without_approval(case_runs):
    record, _, _ = case_runs["sfc_0003"]
    request = ActionRequest(id="sneaky", action="isolate_device", target="INV-01", reversible=False,
                            needs_approval=False, rationale="", expected_effect="")
    record.actions.append(ActionRecord(ts=record.turns[-1].ts, request=request, status=ActionStatus.APPLIED, gated=True))
    try:
        assert not allowlist_held(record)
    finally:
        record.actions.pop()
