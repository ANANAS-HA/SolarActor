"""Grid-operator notices (a plant cap with no grid setpoint, explained only by a note), fake notices, and
"quiet" unknown faults (no telltale alarm: a site note decides between the causes the numbers allow).

Limits: a note-based CURTAILMENT may only reduce actions, never trigger one; a cause outside the catalogue
(not a FaultType) may only lead to escalation.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from raqeeb.agent import RaqeebAgent
from raqeeb.bench.harness import run_scenario
from raqeeb.config import load_settings
from raqeeb.contracts import (
    PLANT_TARGET,
    UNKNOWN_FAULTS,
    FaultSpec,
    FaultType,
    Hypothesis,
    LlmCall,
    LlmRole,
    NoticeClaim,
    Scenario,
)
from raqeeb.faults.injector import FaultInjector
from raqeeb.faults.notes import GRID_OPERATOR, local_hhmm
from raqeeb.faults.unknown import FIRMWARE_CODE, TRIP_CODE
from raqeeb.llm.gateway import Gateway
from raqeeb.llm.router import TOP2_MARGIN, Router
from raqeeb.telemetry.store import TelemetryStore
from tests.leakcheck import assert_no_answer_leak

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)
CAP = {"type": "CURTAILMENT", "target": "ALL", "start": "07:30", "end": "09:30", "severity": 0.4}
STRING = {"type": "STRING_OPEN", "target": "INV-02", "start": "05:00", "severity": 0.25}


def scenario(*faults: dict, seed: int = 21) -> Scenario:
    s = Scenario(id="fx_notice", seed=seed, start=DAY, duration_hours=12, faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


@lru_cache
def planted(variant: str, seed: int = 21):
    s = scenario(CAP | {"variant": variant}, seed=seed)
    inj = FaultInjector.for_scenario(s)
    store = TelemetryStore()
    store.write_batch(inj.run_to_end())
    return s, inj, store


def call(purpose: str) -> LlmCall:
    return LlmCall(role=LlmRole.LOCAL, model="fake", purpose=purpose, prompt_tokens=100, completion_tokens=20)


class NoticeRouter:
    """Fake model that reads the notice 'as written' (its window and limit are passed in by the test)."""

    margin = TOP2_MARGIN

    def __init__(self, claim: NoticeClaim | None):
        self.claim = claim

    def propose_hypotheses(self, results, symptoms=(), ranked=(), unexplained=(), **kw):
        if self.claim is None:
            return [], [call("hypotheses")]
        return [Hypothesis(id="llm-1", fault_type=FaultType.CURTAILMENT, targets=[PLANT_TARGET],
                           explains=list(unexplained), evidence=["grid operator notice"], notice=self.claim)], \
            [call("hypotheses")]

    def explain_diagnosis(self, diagnosis, symptoms=(), results=()):
        return None, [call("explain")]

    def write_shift_report(self, draft, notes=()):
        return draft, [call("report")]


def true_claim(s: Scenario, limit_kw: float, shift=timedelta(0), scale: float = 1.0) -> NoticeClaim:
    start, end = s.resolve_time(CAP["start"]) + shift, s.resolve_time(CAP["end"]) + shift
    return NoticeClaim(start_local=local_hhmm(start), end_local=local_hhmm(end), limit_kw=limit_kw * scale)


# -- the twin ----------------------------------------------------------------------------------------


def test_a_notice_cap_writes_no_setpoint_and_its_note_matches():
    s, inj, store = planted("notice")
    assert store.read("grid_setpoint").empty and "GRID_LIMIT_ACTIVE" not in set(store.read("events")["code"])
    meter = store.meter()["plant_ac_kw"]
    window = meter.loc[s.resolve_time("07:45"):s.resolve_time("09:15")]
    limit = 0.6 * 40000
    assert window.max() <= limit * 1.01 and window.min() > 0.9 * limit  # the cap binds
    (notice,) = [n for n in inj.notes if n.author == GRID_OPERATOR]
    assert notice.ts < s.resolve_time(CAP["start"]) and "24.0" in notice.text
    assert local_hhmm(s.resolve_time(CAP["start"])) in notice.text and local_hhmm(s.resolve_time(CAP["end"])) in notice.text
    assert_no_answer_leak(store.read("technician_notes"), s)


def test_a_fake_notice_does_not_match_the_cap():
    for seed in range(1, 7):
        s, inj, store = planted("fake_notice", seed)
        assert store.read("grid_setpoint").empty
        (notice,) = [n for n in inj.notes if n.author == GRID_OPERATOR]
        right_times = (local_hhmm(s.resolve_time(CAP["start"])) in notice.text
                       and local_hhmm(s.resolve_time(CAP["end"])) in notice.text)
        assert not (right_times and "24.0" in notice.text), notice.text  # times or level are wrong


def test_notices_come_in_english_and_arabic():
    langs = Counter(bool(re.search(r"[؀-ۿ]", n.text)) for seed in range(1, 9)
                    for n in planted("notice", seed)[1].notes if n.author == GRID_OPERATOR)
    assert langs[True] and langs[False]


@lru_cache
def quiet(fault: str):
    faults = {"trip": {"type": "TRANSFORMER_TRIP", "target": "T2", "start": "06:00", "variant": "quiet"},
              "firmware": {"type": "FIRMWARE_ALARM", "target": "INV-11", "start": "05:00", "severity": 0.2,
                           "variant": "quiet"}}
    s = scenario(faults[fault])
    inj = FaultInjector.for_scenario(s)
    store = TelemetryStore()
    store.write_batch(inj.run_to_end())
    return s, inj, store


def test_quiet_unknown_faults_have_no_telltale_alarm_but_a_deciding_and_a_misleading_note():
    s, inj, store = quiet("trip")
    assert TRIP_CODE not in set(store.read("events")["code"])
    texts = [n.text for n in inj.notes]
    assert any("T2" in t and ("smell" in t or "bang" in t or "waiting for grid" in t) for t in texts)
    assert any("rebooted the network switch" in t for t in texts)
    s, inj, store = quiet("firmware")
    assert FIRMWARE_CODE not in set(store.read("events")["code"])
    texts = [n.text for n in inj.notes]
    assert any("controller package" in t for t in texts) and any("cooling fan" in t for t in texts)
    for _, inj, store in (quiet("trip"), quiet("firmware")):
        for t in store.read("technician_notes")["text"]:
            assert not any(f.value in t or f.value.replace("_", " ").lower() in t.lower() for f in FaultType), t


# -- the agent ---------------------------------------------------------------------------------------


def test_rules_only_escalate_an_unsignalled_cap_and_do_not_act_on_it():
    s = scenario(CAP | {"variant": "notice"}, STRING)
    record = run_scenario(s, RaqeebAgent(name="raqeeb_rules"))
    assert "CURTAILMENT@ALL" not in record.predicted
    assert all(a.request.target == "INV-02" for a in record.actions)
    assert any(e.subject == "unexplained" for e in record.escalations)


def test_a_matching_notice_is_marked_expected_and_flagged_and_reduces_actions_only():
    s = scenario(CAP | {"variant": "notice"}, STRING)
    rules = run_scenario(s, RaqeebAgent(name="r"))
    agent = RaqeebAgent(router=NoticeRouter(true_claim(s, 24000.0)), name="l")
    record = run_scenario(s, agent)
    cause = agent.memory.causes.get("CURTAILMENT@ALL")
    assert cause is not None and cause.status.value == "expected"
    assert "CURTAILMENT@ALL" not in record.predicted  # expected behaviour, not a root cause
    assert any(e.subject == "CURTAILMENT@ALL" and "confirm" in e.reason for e in record.escalations)
    mine = Counter((a.request.action, a.request.target) for a in record.actions)
    theirs = Counter((a.request.action, a.request.target) for a in rules.actions)
    assert not mine - theirs  # the notice triggers no action of its own: it can only take actions away


def test_a_notice_that_does_not_match_the_cap_is_rejected_and_the_cap_escalated():
    s = scenario(CAP | {"variant": "fake_notice"}, STRING)
    for claim in (true_claim(s, 24000.0, shift=timedelta(hours=2)), true_claim(s, 24000.0, scale=0.7)):
        agent = RaqeebAgent(router=NoticeRouter(claim), name="l")
        record = run_scenario(s, agent)
        assert "CURTAILMENT@ALL" not in agent.memory.causes
        assert any(e.summary.startswith("rejected model proposal CURTAILMENT@ALL") and "does not match" in e.summary
                   for e in agent.memory.timeline)
        assert any(e.subject == "unexplained" for e in record.escalations)
        assert not any(e.subject == "CURTAILMENT@ALL" for e in record.escalations)


def test_a_cause_outside_the_catalogue_only_leads_to_escalation():
    """The model names a fault type that does not exist: the reply is invalid, nothing is accepted or acted on,
    and the unexplained symptoms go to a human."""
    def completion(**kw):
        reply = {"hypotheses": [{"fault_type": "BIRD_NEST", "targets": ["ALL"], "explains": [], "evidence": ["x"]}]}

        class R:
            usage = None
            choices = [type("C", (), {"message": type("M", (), {"content": json.dumps(reply)})()})()]
        return R()

    settings = load_settings().model_copy(update={"local_llm_base_url": "http://fake", "local_llm_model": "fake",
                                                  "anthropic_api_key": "", "api_llm_model": ""})
    s = scenario(CAP | {"variant": "notice"})
    agent = RaqeebAgent(router=Router(Gateway(settings, completion=completion)), name="l")
    record = run_scenario(s, agent)
    assert record.actions == [] and record.predicted == []
    assert not any(c.source == "llm" for c in agent.memory.causes.values())
    assert any(e.subject == "unexplained" for e in record.escalations)


# -- the practice set ---------------------------------------------------------------------------------


def test_practice_unknown_has_the_notes_decide_and_notice_scenarios():
    from raqeeb.config import PRACTICE_UNKNOWN_SCENARIO_DIR
    from raqeeb.faults.generator import generate_set, load_scenarios

    scenarios = load_scenarios(PRACTICE_UNKNOWN_SCENARIO_DIR, "pru")
    assert scenarios == generate_set("practice_unknown") and len(scenarios) == 25
    unknown = [f for s in scenarios for f in s.faults if f.type in UNKNOWN_FAULTS]
    quiet_n = sum(f.variant == "quiet" for f in unknown)
    assert len(unknown) == 20 and quiet_n == 10 and quiet_n * 2 >= len(unknown)  # at least half: notes decide
    variants = Counter(f.variant for s in scenarios for f in s.faults if f.type == FaultType.CURTAILMENT)
    assert variants == {"notice": 3, "fake_notice": 2}
    for s in scenarios[20:]:
        assert any(f.type not in (FaultType.CURTAILMENT,) for f in s.faults) and s.answer_key.root_causes


def test_final_set_spec_has_the_llm_categories():
    """The final set's spec (generated once, by the integration session): 12 unknown-fault scenarios, 6 of them
    notes decide; 4 grid notices (2 Arabic, 2 English) and 2 fake notices (1 of each). Nothing is written here."""
    from raqeeb.faults.generator import HELDOUT_SLICES, generate_set

    final = generate_set("final")  # 50 regular scenarios, then the held-out slice (tests/test_heldout_generator.py)
    unknown = [f for s in final for f in s.faults if f.type in UNKNOWN_FAULTS]
    assert len(final) == 50 + HELDOUT_SLICES["final"] == 58
    assert len(unknown) == 12 and sum(f.variant == "quiet" for f in unknown) == 6
    notices = Counter((f.variant, f.language) for s in final for f in s.faults
                      if f.type == FaultType.CURTAILMENT and f.variant)
    assert notices == {("notice", "en"): 2, ("notice", "ar"): 2, ("fake_notice", "en"): 1, ("fake_notice", "ar"): 1}
    for s in final:
        assert s.answer_key == s.expected_answer_key()
        assert sum(f.type in UNKNOWN_FAULTS for f in s.faults) <= 1


def test_an_unsignalled_cap_is_still_flagged_while_one_unit_is_stopped():
    """Hand-made: a grid-notice cap while one unit is stopped by a remote command. Rules-only escalate the cap
    (never act on it); with the matching notice it is marked expected and flagged for operator confirmation."""
    stop = {"type": "UNAUTHORIZED_CMD", "target": "INV-05", "start": "06:00"}
    s = scenario(CAP | {"variant": "notice"}, stop)
    agent = RaqeebAgent(name="r")
    record = run_scenario(s, agent)
    assert "FLAT_CAP_PLANT:ALL" in agent.memory.symptoms
    assert any(e.subject == "unexplained" and e.devices == [PLANT_TARGET] for e in record.escalations)
    assert all(a.request.target == "INV-05" for a in record.actions)
    agent = RaqeebAgent(router=NoticeRouter(true_claim(s, 24000.0)), name="l")
    record = run_scenario(s, agent)
    assert agent.memory.causes["CURTAILMENT@ALL"].status.value == "expected"
    assert any(e.subject == "CURTAILMENT@ALL" for e in record.escalations)
    assert all(a.request.target == "INV-05" for a in record.actions)
