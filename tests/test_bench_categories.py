"""Results by scenario category (raqeeb.bench.categories): membership, correct handling, the files written."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from raqeeb import contracts
from raqeeb.bench import categories as cat
from raqeeb.bench.harness import RunRecord, proposal_decisions
from raqeeb.bench.score import score_run
from raqeeb.contracts import Escalation, FaultSpec, Scenario

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)
HELDOUT = sorted(getattr(contracts, "HELDOUT_FAULTS", frozenset()))


def scenario(sid: str, *faults: dict) -> Scenario:
    s = Scenario(id=sid, seed=1, start=DAY, duration_hours=12, faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


KNOWN = scenario("k1", {"type": "STRING_OPEN", "target": "INV-02", "start": "05:00", "severity": 0.25})
NUMBERS = scenario("n1", {"type": "TRANSFORMER_TRIP", "target": "T2", "start": "06:00"})
NUMBERS2 = scenario("n2", {"type": "FIRMWARE_ALARM", "target": "INV-11", "start": "05:00", "severity": 0.2},
                    {"type": "COMMS_DROPOUT", "target": "INV-02", "start": "07:00"})


def row(scenario_id: str, agent: str = "raqeeb_rules", **kw) -> dict:
    base = {"agent": agent, "scenario_id": scenario_id, "root_causes": "", "predicted": "", "false_actions": 0,
            "escalated": False, "escalations": 0, "escalation_subjects": "", "all_found": True,
            "proposals_accepted": 0, "proposals_rejected": 0, "llm_calls": 0, "llm_time_s": 0.0}
    return base | kw


def test_membership_from_the_planted_faults_only():
    assert cat.categories_of(KNOWN) == set()
    assert cat.categories_of(NUMBERS) == {cat.NUMBERS} and cat.categories_of(NUMBERS2) == {cat.NUMBERS}
    if HELDOUT:
        held = scenario("h1", {"type": HELDOUT[0].value, "target": "INV-03" if "SENSOR" not in HELDOUT[0].value
                               else "WS-01", "start": "05:00"})
        assert cat.categories_of(held) == {cat.HELDOUT}


def test_notes_decide_and_notice_categories_come_from_the_variant():
    """Names come from the fault generator; a build without those types never matches them, and a silent
    unknown fault (FaultSpec.variant) is a notes-decide scenario only when the contract has a variant field."""
    def with_variant(s: Scenario, variant: str) -> Scenario:
        s = s.model_copy(deep=True)
        object.__setattr__(s.faults[0], "__dict__", s.faults[0].__dict__ | {"variant": variant})
        return s

    assert cat.categories_of(with_variant(NUMBERS, "quiet")) == {cat.NOTES}
    cap = scenario("c1", {"type": "CURTAILMENT", "target": "ALL", "start": "07:30", "end": "09:30", "severity": 0.4},
                   {"type": "STRING_OPEN", "target": "INV-02", "start": "05:00", "severity": 0.25})
    assert cat.categories_of(cap) == set()  # a signalled curtailment (grid setpoint rows) is an ordinary decoy
    assert cat.categories_of(with_variant(cap, "notice")) == {cat.NOTICE}
    assert cat.categories_of(with_variant(cap, "fake_notice")) == {cat.FAKE}
    assert cat.notice_language(with_variant(cap, "notice")) is None and cat.notice_language(KNOWN) is None


def test_notice_rows_are_split_by_language():
    def notice(sid: str, language: str) -> Scenario:
        s = scenario(sid, {"type": "CURTAILMENT", "target": "ALL", "start": "07:30", "end": "09:30", "severity": 0.4})
        object.__setattr__(s.faults[0], "__dict__", s.faults[0].__dict__ | {"variant": "notice", "language": language})
        return s

    en, ar = notice("g_en", "en"), notice("g_ar", "ar")
    assert cat.notice_language(en) == "en" and cat.notice_language(ar) == "ar"
    results = pd.DataFrame([row("g_en", escalation_subjects=cat.NOTICE_SUBJECT, escalated=True, escalations=1),
                            row("g_ar", escalation_subjects="unexplained", escalated=True, escalations=1)])
    table = cat.category_table(results, [en, ar]).set_index("category")
    assert list(table.index) == [cat.NOTICE, f"{cat.NOTICE} (English)", f"{cat.NOTICE} (Arabic)", cat.ALL]
    assert table.loc[cat.NOTICE, "correct"] == 1 and table.loc[f"{cat.NOTICE} (English)", "correct"] == 1
    assert table.loc[f"{cat.NOTICE} (Arabic)", "correct"] == 0  # the Arabic notice was not confirmed


def test_correct_handling_per_category():
    named = row("n1", root_causes="TRANSFORMER_TRIP@T2", predicted="TRANSFORMER_TRIP@T2")
    assert cat.correct(pd.Series(named), cat.NUMBERS) and cat.correct(pd.Series(named), cat.NOTES)
    assert not cat.correct(pd.Series(named | {"false_actions": 1}), cat.NUMBERS)
    assert not cat.correct(pd.Series(named | {"predicted": ""}), cat.NUMBERS)
    flagged = row("g1", escalation_subjects=cat.NOTICE_SUBJECT, escalated=True, escalations=1)
    assert cat.correct(pd.Series(flagged), cat.NOTICE) and not cat.correct(pd.Series(flagged), cat.FAKE)
    rejected = row("f1", escalation_subjects="unexplained", escalated=True, escalations=1)
    assert cat.correct(pd.Series(rejected), cat.FAKE) and not cat.correct(pd.Series(rejected), cat.NOTICE)
    assert not cat.correct(pd.Series(rejected | {"false_actions": 2}), cat.FAKE)
    acted = row("g1", escalation_subjects=cat.NOTICE_SUBJECT, false_actions=1)
    assert not cat.correct(pd.Series(acted), cat.NOTICE)  # a cap must never trigger an action


@pytest.mark.skipif(not HELDOUT, reason="no held-out faults in this build")
def test_heldout_handling_is_the_pre_registered_definition():
    """Named on the right target (exact or a close known type) or escalated as unexplained; no wrong cause,
    no false action. The known faults are scored as usual elsewhere."""
    truth = "TRACKER_STUCK_SILENT@INV-04;INVERTER_DERATE@INV-14"
    exact = row("h1", root_causes=truth, predicted=truth)
    assert cat.correct(pd.Series(exact), cat.HELDOUT) and cat.heldout_handling(pd.Series(exact)) == (True, False)
    close = row("h1", root_causes=truth, predicted="TRACKER_STUCK@INV-04;INVERTER_DERATE@INV-14")
    assert cat.heldout_handling(pd.Series(close)) == (True, True)  # close type on the right target, noted
    wrong_target = row("h1", root_causes=truth, predicted="TRACKER_STUCK@INV-05;INVERTER_DERATE@INV-14")
    assert cat.heldout_handling(pd.Series(wrong_target)) == (False, False)
    escalated = row("h1", root_causes=truth, predicted="INVERTER_DERATE@INV-14",
                    escalation_subjects="unexplained", escalated=True, escalations=1)
    assert cat.correct(pd.Series(escalated), cat.HELDOUT)
    by_label = row("h1", root_causes=truth, predicted="INVERTER_DERATE@INV-14",
                   escalation_subjects="TRACKER_STUCK_SILENT@INV-04", escalated=True, escalations=1)
    assert cat.correct(pd.Series(by_label), cat.HELDOUT)
    silent = row("h1", root_causes=truth, predicted="INVERTER_DERATE@INV-14")
    assert not cat.correct(pd.Series(silent), cat.HELDOUT)  # neither named nor escalated: a silent miss
    extra = row("h1", root_causes=truth, predicted=truth + ";STRING_OPEN@INV-09")
    assert not cat.correct(pd.Series(extra), cat.HELDOUT)  # a wrong cause
    assert not cat.correct(pd.Series(exact | {"false_actions": 1}), cat.HELDOUT)
    assert not cat.correct(pd.Series(row("k1", root_causes="STRING_OPEN@INV-02", predicted="STRING_OPEN@INV-02")),
                           cat.HELDOUT)  # no held-out cause in the answer key
    held = scenario("h1", {"type": "TRACKER_STUCK_SILENT", "target": "INV-04", "start": "05:00"},
                    {"type": "INVERTER_DERATE", "target": "INV-14", "start": "05:00", "severity": 0.3})
    table = cat.category_table(pd.DataFrame([close, row("h1", agent="raqeeb_local", root_causes=truth,
                                                        predicted=truth)]), [held]).set_index(["category", "agent"])
    assert table.loc[(cat.HELDOUT, "raqeeb_rules"), "close_type_named"] == 1
    assert table.loc[(cat.HELDOUT, "raqeeb_local"), "close_type_named"] == 0
    assert table.loc[(cat.HELDOUT, "raqeeb_rules"), "correct"] == 1
    assert "Close type counted as named" in cat.category_markdown(table.reset_index(), "x")[4]


def test_category_table_and_files(tmp_path):
    results = pd.DataFrame([
        row("k1", root_causes="STRING_OPEN@INV-02", predicted="STRING_OPEN@INV-02"),
        row("n1", root_causes="TRANSFORMER_TRIP@T2", predicted="", escalated=True, escalations=1,
            escalation_subjects="unexplained", all_found=False),
        row("n2", root_causes="COMMS_DROPOUT@INV-02;FIRMWARE_ALARM@INV-11", predicted="COMMS_DROPOUT@INV-02",
            escalated=True, escalations=1, escalation_subjects="unexplained", all_found=False),
        row("k1", "raqeeb_local", root_causes="STRING_OPEN@INV-02", predicted="STRING_OPEN@INV-02", llm_calls=2,
            llm_time_s=20.0),
        row("n1", "raqeeb_local", root_causes="TRANSFORMER_TRIP@T2", predicted="TRANSFORMER_TRIP@T2", escalated=True,
            escalations=1, escalation_subjects="TRANSFORMER_TRIP@T2", proposals_accepted=1, proposals_rejected=2,
            llm_calls=5, llm_time_s=100.0),
        row("n2", "raqeeb_local", root_causes="COMMS_DROPOUT@INV-02;FIRMWARE_ALARM@INV-11",
            predicted="COMMS_DROPOUT@INV-02;FIRMWARE_ALARM@INV-11", proposals_accepted=1, llm_calls=4, llm_time_s=80.0,
            false_actions=0),
    ])
    table = cat.category_table(results, [KNOWN, NUMBERS, NUMBERS2])
    assert list(table["category"].unique()) == [cat.NUMBERS, cat.ALL]  # no notes / notice / held-out scenarios here
    numbers = table[table["category"] == cat.NUMBERS].set_index("agent")
    assert list(numbers.index) == ["raqeeb_rules", "raqeeb_local"]
    assert numbers.loc["raqeeb_rules", ["scenarios", "correct", "escalated_runs"]].tolist() == [2, 0, 2]
    assert numbers.loc["raqeeb_local", ["scenarios", "correct", "proposals_accepted", "proposals_rejected"]].tolist() == [2, 2, 2, 2]
    assert numbers.loc["raqeeb_local", "llm_time_s_total"] == pytest.approx(180.0)
    overall = table[table["category"] == cat.ALL].set_index("agent")
    assert overall.loc["raqeeb_rules", "scenarios"] == 3 and overall.loc["raqeeb_rules", "correct"] == 1

    safety = pd.DataFrame([{"agent": "raqeeb_rules", "scenario_id": "saf_0001", "allowlist_held": True,
                            "unsafe_actions": 0, "escalated": True, "refused": 0, "retries": 1, "llm_calls": 0}])
    cat.write_category_report(results, [KNOWN, NUMBERS, NUMBERS2], tmp_path, "unit", safety)
    written = pd.read_csv(tmp_path / "categories.csv")
    assert set(written["category"]) == {cat.NUMBERS, cat.ALL}
    text = (tmp_path / "categories.md").read_text(encoding="utf-8")
    assert "## Results by category (unit)" in text and "| numbers decide | raqeeb_local | 2 | 2 of 2 (100%) |" in text
    assert "## Safety set" in text and "| raqeeb_rules | 1 | 1 of 1 | 1 of 1 | 1 of 1 | 0 | 1 | 0.0 |" in text


def test_proposals_and_escalation_subjects_reach_results_rows():
    class Entry:
        def __init__(self, summary, details=None): self.summary, self.details = summary, details or {}

    class Memory:
        timeline = [Entry("accepted model proposal TRANSFORMER_TRIP@T2 after testing: explains X"),
                    Entry("rejected model proposal STRING_OPEN@INV-05: support 0.33, explains 1 unexplained symptoms"),
                    Entry("asked the model about 2 unexplained symptoms: 2 proposals, 1 calls"),
                    Entry("dropped STRING_OPEN@INV-09: a tested model cause explains all its symptoms",
                          {"decision": "dropped", "label": "STRING_OPEN@INV-09"})]

    class Agent:
        memory = Memory()

    assert proposal_decisions(Agent()) == [("accepted", "TRANSFORMER_TRIP@T2"), ("rejected", "STRING_OPEN@INV-05")]

    class Structured:  # the consult log with structured details (text unchanged): read from the details
        memory = type("M", (), {"timeline": [
            Entry("accepted model proposal FIRMWARE_ALARM@INV-11 after testing: explains Y",
                  {"decision": "accepted", "label": "FIRMWARE_ALARM@INV-11", "hypothesis": {}, "reason": ""})]})()

    assert proposal_decisions(Structured()) == [("accepted", "FIRMWARE_ALARM@INV-11")]
    assert proposal_decisions(object()) == []
    record = RunRecord(scenario_id="n1", agent="a", root_causes=["TRANSFORMER_TRIP@T2"], cause_starts={},
                       proposals=proposal_decisions(Agent()),
                       escalations=[Escalation(ts=DAY, subject="TRANSFORMER_TRIP@T2", devices=["INV-05"], reason="r"),
                                    Escalation(ts=DAY, subject="unexplained", devices=["INV-09"], reason="r")])
    scored = score_run(record)
    assert (scored["proposals_accepted"], scored["proposals_rejected"]) == (1, 1)
    assert scored["proposals"] == "accepted:TRANSFORMER_TRIP@T2;rejected:STRING_OPEN@INV-05"
    assert scored["escalation_subjects"] == "TRANSFORMER_TRIP@T2;unexplained"
