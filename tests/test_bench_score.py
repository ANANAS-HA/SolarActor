"""Scorer on hand-made run records for the fixture answer key (no twin runs)."""

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from raqeeb.bench.harness import ActionRecord, ReferenceEnergy, RunRecord, TurnRecord
from raqeeb.bench.score import (
    addresses,
    failures,
    losses,
    results_frame,
    score_run,
    summarize,
    summary_markdown,
    time_to_verified_recovery,
)
from raqeeb.contracts import ActionRequest, ActionStatus, LlmCall, Scenario, Verification

FIXTURE = Path(__file__).parent / "fixtures" / "scn_example.yaml"
T0 = datetime(2025, 4, 12, 7, tzinfo=timezone.utc)  # first root cause starts at 07:00
KEY = ["SOILING@ALL", "SENSOR_SOILING@WS-01", "INVERTER_DERATE@INV-07", "COMMS_DROPOUT@INV-12"]


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def request(action_id: str, action: str, target: str) -> ActionRequest:
    return ActionRequest(id=action_id, action=action, target=target, reversible=True, needs_approval=False,
                         rationale="test", expected_effect="test")


def applied(action_id: str, action: str, target: str, minute: int, status=ActionStatus.APPLIED,
            gated=False) -> ActionRecord:
    return ActionRecord(ts=at(minute), request=request(action_id, action, target), status=status, gated=gated)


def verified(action_id: str, minute: int, ok: bool = True) -> tuple[datetime, Verification]:
    return at(minute), Verification(action_id=action_id, recovered=ok, before_kw=0, after_kw=0, expected_kw=0)


def turns(*mitigated_from: int, end: int = 300) -> list[TurnRecord]:
    """One turn every 15 min; cause k is mitigated from minute mitigated_from[k] on."""
    return [TurnRecord(ts=at(m), mitigated={label: m >= start for label, start in zip(KEY, mitigated_from)})
            for m in range(15, end + 1, 15)]


def record(agent="a", scenario_id="scn_0007", **kw) -> RunRecord:
    defaults = dict(root_causes=list(KEY), cause_starts={label: T0 + timedelta(hours=k) for k, label in enumerate(KEY)},
                    reference=ReferenceEnergy(no_action_mwh=100.0, decoy_only_mwh=160.0), energy_mwh=100.0)
    return RunRecord(scenario_id=scenario_id, agent=agent, **{**defaults, **kw})


def test_fixture_answer_key_matches():
    scenario = Scenario.from_yaml(FIXTURE)
    assert sorted(scenario.answer_key.root_causes) == sorted(KEY)


def test_perfect_diagnosis():
    row = score_run(record(predicted=sorted(KEY)))
    assert (row["tp"], row["fp"], row["fn"]) == (4, 0, 0)
    assert row["precision"] == row["recall"] == row["type_recall"] == 1.0
    assert row["all_found"]


def test_empty_diagnosis_has_undefined_precision():
    row = score_run(record())
    assert math.isnan(row["precision"])
    assert row["recall"] == 0.0 and not row["all_found"]
    assert row["n_actions"] == 0 and math.isnan(row["ttvr_min"])


def test_symptoms_and_decoys_count_against_precision():
    predicted = ["SOILING@ALL", "COMMS_DROPOUT@INV-12", "SOILING@INV-03", "CURTAILMENT@ALL"]
    row = score_run(record(predicted=predicted))
    assert (row["tp"], row["fp"], row["fn"]) == (2, 2, 2)
    assert row["precision"] == 0.5 and row["recall"] == 0.5
    assert row["type_recall"] == 0.5  # SOILING and COMMS_DROPOUT named; SENSOR_SOILING, DERATE not
    assert not row["all_found"]


def test_false_actions():
    actions = [
        applied("1", "create_cleaning_work_order", "INV-03", 60),  # plant-wide soiling covers one inverter
        applied("2", "reset_comms_gateway", "INV-12", 60),
        applied("3", "set_power_limit", "INV-03", 60, gated=True),  # healthy inverter
        applied("4", "reset_comms_gateway", "INV-07", 60),  # INV-07 is derated, not offline
        applied("5", "flag_sensor_use_reference", "WS-01", 60, status=ActionStatus.REJECTED),  # still a right call
        applied("6", "isolate_device", "INV-12", 60, status=ActionStatus.FAILED, gated=True),
    ]
    row = score_run(record(actions=actions))
    assert row["n_actions"] == 6 and row["false_actions"] == 3
    assert row["false_action_list"] == "set_power_limit@INV-03;reset_comms_gateway@INV-07;isolate_device@INV-12"
    assert row["approvals"] == 2 and row["failed_actions"] == 1


def test_addresses():
    assert addresses(request("x", "dispatch_technician", "INV-07"), "INVERTER_DERATE@INV-07")
    assert addresses(request("x", "dispatch_technician", "WS-01"), "SENSOR_SOILING@WS-01")
    assert not addresses(request("x", "create_cleaning_work_order", "ALL"), "INVERTER_DERATE@INV-07")
    assert not addresses(request("x", "set_power_limit", "ALL"), "INVERTER_DERATE@INV-07")


ACTIONS = [applied("c", "create_cleaning_work_order", "ALL", 30), applied("s", "flag_sensor_use_reference", "WS-01", 30)]


def test_time_to_verified_recovery_needs_truth_and_verification():
    # every cause mitigated from 120 min; both actions verified at 90 -> recovery at the 120 poll
    r = record(actions=ACTIONS, turns=turns(120, 60, 90, 105), verifications=[verified("c", 90), verified("s", 90)])
    assert time_to_verified_recovery(r) == 120.0
    assert score_run(r)["mitigated_at_end"]
    # verified only after truth: the later of the two
    r = record(actions=ACTIONS, turns=turns(60, 60, 60, 60), verifications=[verified("c", 150), verified("s", 45)])
    assert time_to_verified_recovery(r) == 150.0


def test_unverified_action_or_failed_verification_blocks_recovery():
    r = record(actions=ACTIONS, turns=turns(60, 60, 60, 60), verifications=[verified("c", 90)])
    assert math.isnan(time_to_verified_recovery(r))  # "s" was applied but never verified
    r = record(actions=ACTIONS, turns=turns(60, 60, 60, 60),
               verifications=[verified("c", 90), verified("s", 90, ok=False), verified("s", 180)])
    assert time_to_verified_recovery(r) == 180.0  # the latest verification of each action counts


def test_no_recovery_without_truth_or_without_verifications():
    r = record(actions=ACTIONS, turns=turns(60, 60, 60, 10**6), verifications=[verified("c", 30), verified("s", 30)])
    assert math.isnan(time_to_verified_recovery(r))
    assert score_run(r)["unmitigated_at_end"] == "COMMS_DROPOUT@INV-12"
    r = record(turns=turns(60, 60, 60, 60))  # faults went away by themselves, the agent verified nothing
    assert math.isnan(time_to_verified_recovery(r))


def test_energy_cost_and_local_share():
    calls = [LlmCall(role="local", model="q", purpose="p"), LlmCall(role="local", model="q", purpose="p"),
             LlmCall(role="api", model="s", purpose="p", cost_usd=0.03)]
    row = score_run(record(energy_mwh=130.0, llm_calls=calls))
    assert row["mwh_recovered"] == pytest.approx(30.0) and row["mwh_lost"] == pytest.approx(60.0)
    assert row["cost_usd"] == pytest.approx(0.03)
    assert (row["llm_calls"], row["local_calls"]) == (3, 2)


def _bench() -> list[RunRecord]:
    calls = [LlmCall(role="local", model="q", purpose="p"), LlmCall(role="api", model="s", purpose="p", cost_usd=0.02)]
    return [
        record("good", "scn_a", predicted=sorted(KEY), actions=ACTIONS, energy_mwh=140.0, llm_calls=calls,
               turns=turns(60, 60, 60, 60), verifications=[verified("c", 60), verified("s", 60)]),
        record("good", "scn_b", predicted=["SOILING@ALL"], energy_mwh=100.0,
               actions=[applied("p", "set_power_limit", "INV-01", 30, gated=True)]),
        record("bad", "scn_a", predicted=["SOILING@INV-01", "SOILING@INV-02"], energy_mwh=150.0),
        record("bad", "scn_b"),
    ]


def test_summary_is_micro_averaged():
    summary = summarize(results_frame(_bench()), {"bad": "reference"}).set_index("agent")
    good, bad = summary.loc["good"], summary.loc["bad"]
    assert good["precision"] == 1.0  # 5 of 5 predictions right
    assert good["recall"] == pytest.approx(5 / 8)
    assert good["all_causes_found_rate"] == 0.5
    assert good["false_action_rate"] == pytest.approx(1 / 3)
    assert good["approvals_per_incident"] == 0.5
    assert good["verified_recovery_rate"] == 0.5 and good["ttvr_median_min"] == 60.0
    assert good["mwh_recovered"] == pytest.approx(40.0) and good["mwh_lost"] == pytest.approx(120.0)
    assert good["cost_per_incident_usd"] == pytest.approx(0.01)
    assert good["local_call_share"] == 0.5
    assert bad["precision"] == 0.0 and bad["recall"] == 0.0 and bad["note"] == "reference"
    assert math.isnan(bad["false_action_rate"]) and math.isnan(bad["ttvr_median_min"])
    assert math.isnan(bad["local_call_share"])


def test_losses_and_failures_are_reported():
    results = results_frame(_bench())
    lost = losses(results, "good", "bad")
    assert lost == ["scn_a: MWh recovered 40.00 vs 50.00; cost $0.0200 vs $0.0000",
                    "scn_b: false actions 1 vs 0"]
    assert losses(results, "bad", "good")[0].startswith("scn_a: recall 0.00 vs 1.00; precision 0.00 vs 1.00")
    wrong = failures(results, "good")
    assert wrong[0] == ("scn_b: missed COMMS_DROPOUT@INV-12, INVERTER_DERATE@INV-07, SENSOR_SOILING@WS-01; "
                        "1 false action(s)")


def test_summary_markdown():
    results = results_frame(_bench())
    text = summary_markdown(results, summarize(results, {"bad": "reference bound"}))
    assert "| Agent | Scenarios | Root-cause precision |" in text
    assert "| good | 2 | 1.00 | 0.62 | 50% | 33% |" in text
    assert "synthetic" in text and "n/a" in text
    assert "Generated" not in text and text == summary_markdown(results, summarize(results, {"bad": "reference bound"}))
    assert "## Where good gets it wrong" in text and "## Where bad gets it wrong" in text
    assert "loses to" not in text  # "bad" is a reference bound, so no head-to-head
    text = summary_markdown(results, summarize(results))
    assert "## Where good loses to bad" in text and "## Where bad loses to good" in text
