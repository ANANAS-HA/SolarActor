"""Raqeeb with the LLM router (Milestone 3): when it asks, how it tests proposals, what reaches a prompt.

A fake router stands in for the model, so these tests are deterministic and need no server. One test
runs the real Router on a fake gateway to check the prompts themselves.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from raqeeb.agent import RaqeebAgent
from raqeeb.agent.consult import MAX_HYPOTHESIS_CALLS
from raqeeb.bench.harness import run_scenario
from raqeeb.bench.score import score_run
from raqeeb.contracts import (
    FaultSpec,
    FaultType,
    Hypothesis,
    LlmCall,
    LlmRole,
    Scenario,
)
from raqeeb.config import load_settings
from raqeeb.faults import catalogue
from raqeeb.llm.gateway import Gateway
from raqeeb.llm.router import TOP2_MARGIN, Router
from raqeeb.telemetry.store import TelemetryStore
from tests.leakcheck import assert_no_answer_leak

DAY = datetime(2025, 1, 15, 3, tzinfo=timezone.utc)


def scenario(*faults: dict, seed: int = 21) -> Scenario:
    s = Scenario(id="fx_llm", seed=seed, start=DAY, duration_hours=12, faults=[FaultSpec(**f) for f in faults])
    return s.model_copy(update={"answer_key": s.expected_answer_key()})


def call(purpose: str) -> LlmCall:
    return LlmCall(role=LlmRole.LOCAL, model="fake", purpose=purpose, prompt_tokens=100, completion_tokens=20)


class FakeRouter:
    """Proposes a fixed list (citing whatever is unexplained), records what it was given."""

    margin = TOP2_MARGIN

    def __init__(self, proposals: list[tuple[str, str]]):
        self.proposals = proposals
        self.asked: list[dict] = []
        self.explained = self.reported = 0

    def propose_hypotheses(self, results, symptoms=(), ranked=(), unexplained=(), *, notes=(), manuals=None,
                           layout=None, force=False):
        self.asked.append({"results": results, "symptoms": symptoms, "ranked": ranked, "unexplained": unexplained,
                           "notes": notes, "manuals": manuals})
        hyps = [Hypothesis(id=f"llm-{k}", fault_type=f, targets=[t], explains=list(unexplained), evidence=["fake"])
                for k, (f, t) in enumerate(self.proposals, start=1)]
        return hyps, [call("hypotheses")]

    def explain_diagnosis(self, diagnosis, symptoms=(), results=()):
        self.explained += 1
        return "fake explanation", [call("explain")]

    def write_shift_report(self, draft, notes=()):
        self.reported += 1
        return draft.model_copy(update={"summary_en": draft.summary_en + "\n(model)", "llm_calls": draft.llm_calls + 1}), \
            [call("report")]


def run(s: Scenario, router):
    agent, store = RaqeebAgent(router=router, name="raqeeb_test"), TelemetryStore()
    record = run_scenario(s, agent, telemetry=store)
    return record, agent, store


TRIP = {"type": "TRANSFORMER_TRIP", "target": "T2", "start": "06:00"}
FIRMWARE = {"type": "FIRMWARE_ALARM", "target": "INV-11", "start": "05:00", "severity": 0.2}


def test_a_tested_proposal_is_accepted_and_untested_or_wrong_ones_are_not():
    s = scenario(TRIP)
    router = FakeRouter([("TRANSFORMER_TRIP", "T2"), ("STRING_OPEN", "INV-05"), ("TRANSFORMER_TRIP", "T3"),
                         ("SOILING", "ALL")])
    record, agent, _ = run(s, router)
    assert record.predicted == ["TRANSFORMER_TRIP@T2"]
    assert record.actions == []  # no fix in the allowlist for it: escalated, not acted on
    row = score_run(record)
    assert row["unknown_named"] == 1 and row["unknown_escalated"] == 1
    assert any(e.subject == "TRANSFORMER_TRIP@T2" and set(e.devices) == {"INV-05", "INV-06", "INV-07", "INV-08"}
               for e in record.escalations)
    rejected = [e.summary for e in agent.memory.timeline if e.summary.startswith("rejected model proposal")]
    assert len(rejected) >= 3  # the wrong area, the open string and soiling all fail their tests
    assert "proposed by a language model and tested on tool data" in record.report.summary_en
    trip_devices = {"INV-05", "INV-06", "INV-07", "INV-08", "PPC-01"}
    assert not any(agent.memory.symptoms[sid].device_id in trip_devices for sid in agent.memory.diagnosis.unexplained)


def test_an_accepted_unknown_device_fault_gets_only_a_ticket_and_is_verified():
    s = scenario(FIRMWARE)
    record, agent, _ = run(s, FakeRouter([("FIRMWARE_ALARM", "INV-11")]))
    assert record.predicted == ["FIRMWARE_ALARM@INV-11"]
    assert [(a.request.action.value, a.request.target) for a in record.actions] == [("dispatch_technician", "INV-11")]
    assert all(v.recovered for _, v in record.verifications)
    row = score_run(record)
    assert row["unknown_named"] == 1 and row["mwh_recovered"] > 0 and row["false_actions"] == 0


def test_no_answer_from_the_model_keeps_the_rule_result():
    s = scenario(TRIP)
    record, _, _ = run(s, FakeRouter([]))
    assert record.predicted == [] and record.actions == []
    assert score_run(record)["unknown_escalated"] == 1


def test_the_model_is_asked_only_when_the_rules_are_stuck_and_every_call_is_reported():
    derate = scenario({"type": "INVERTER_DERATE", "target": "INV-07", "start": "05:30", "severity": 0.35})
    router = FakeRouter([("STRING_OPEN", "INV-07")])
    record, agent, _ = run(derate, router)
    assert router.asked == []  # the rules explain everything: no hypothesis call
    assert (router.explained, router.reported) == (1, 1)  # the end-of-incident write-up
    assert [c.purpose for c in record.llm_calls] == ["explain", "report"]
    assert record.predicted == ["INVERTER_DERATE@INV-07"]

    s = scenario(TRIP)
    router = FakeRouter([])
    record, agent, _ = run(s, router)
    assert 1 <= len(router.asked) <= MAX_HYPOTHESIS_CALLS
    assert len(record.llm_calls) == len(router.asked) + 2 == len(agent.memory.llm_calls)


def test_everything_the_model_is_given_passes_the_leak_check():
    s = scenario(TRIP, {"type": "STRING_OPEN", "target": "INV-14", "start": "05:00", "severity": 0.25})
    router = FakeRouter([("TRANSFORMER_TRIP", "T2")])
    run(s, router)
    assert router.asked
    for question in router.asked:  # `ranked` holds the agent's own candidates, which name causes by design
        assert_no_answer_leak({k: v for k, v in question.items() if k != "ranked"}, s)
    assert any(q["notes"] for q in router.asked) and all(q["manuals"] for q in router.asked)


def test_injected_log_text_never_reaches_a_prompt_and_notes_stay_inside_data_blocks(monkeypatch):
    monkeypatch.setattr(catalogue, "INJECTION_TEMPLATES", catalogue.INJECTION_TEMPLATES[1:2])
    s = scenario(TRIP, {"type": "PROMPT_INJECTION", "target": "INV-02", "start": "06:30"})
    prompts: list[str] = []

    def completion(**kw):
        prompts.append("\n".join(m["content"] for m in kw["messages"]))

        class R:
            usage = None
            choices = [type("C", (), {"message": type("M", (), {"content": json.dumps({"hypotheses": []})})()})()]
        return R()

    settings = load_settings().model_copy(update={"local_llm_base_url": "http://fake", "local_llm_model": "fake"})
    router = Router(Gateway(settings, completion=completion), api=False)
    record, agent, store = run(s, router)
    assert prompts
    injected = catalogue.INJECTION_TEMPLATES[0].text[:30]
    assert not any(injected in p for p in prompts)
    notes = list(store.read("technician_notes")["text"])
    for p in prompts:
        outside = re.sub(r'<untrusted_data name="[^"]+" id="(\w+)">.*?</untrusted_data id="\1">', "", p, flags=re.S)
        assert not any(n[:30] in outside for n in notes)
    assert any(n[:30] in p for p in prompts for n in notes)  # the notes did reach the model, as data
    assert all(c.role == LlmRole.LOCAL for c in record.llm_calls)
    assert FaultType.PROMPT_INJECTION.value not in record.predicted


# -- fully local: no API key, and a local server that is down -----------------------------------------


def _local_only_settings():
    return load_settings().model_copy(update={"local_llm_base_url": "http://fake", "local_llm_model": "fake",
                                              "anthropic_api_key": "", "api_llm_model": ""})


def test_without_an_api_key_the_router_never_tries_the_api():
    asked: list[str] = []

    def completion(**kw):
        asked.append(kw["model"])

        class R:
            usage = None
            choices = [type("C", (), {"message": type("M", (), {"content": "not json"})()})()]  # invalid every time
        return R()

    router = Router(Gateway(_local_only_settings(), completion=completion))  # api=True asked, but no key in .env
    assert router.api is False
    s = scenario(TRIP)  # many unexplained symptoms: the hard case that would go to the API
    record, agent, _ = run(s, router)
    assert asked and all(model.startswith("openai/") for model in asked)  # local model only
    assert all(c.role == LlmRole.LOCAL for c in record.llm_calls)
    text = (record.report.summary_en + record.report.summary_ar).lower()
    assert "api" not in text and "(api)" not in text


def test_an_unreachable_local_server_falls_back_to_rules_and_escalates():
    import litellm

    def completion(**kw):
        raise litellm.exceptions.APIConnectionError(message="down", llm_provider="openai", model=kw["model"])

    router = Router(Gateway(_local_only_settings(), completion=completion))
    s = scenario(TRIP, {"type": "STRING_OPEN", "target": "INV-14", "start": "05:00", "severity": 0.25})
    record, agent, _ = run(s, router)
    rules, _, _ = run(s, None)
    assert record.predicted == rules.predicted == ["STRING_OPEN@INV-14"]  # the rule result stands
    assert [a.request.action for a in record.actions] == [a.request.action for a in rules.actions]
    assert score_run(record)["unknown_escalated"] == 1  # the unexplained symptoms go to a human
    assert record.llm_calls and not any(c.ok for c in record.llm_calls)
    assert all(c.role == LlmRole.LOCAL for c in record.llm_calls)
    assert record.report.summary_en.startswith(rules.report.summary_en.split("\n")[0])  # the template report


def test_live_llm_tests_are_skipped_while_a_bench_run_holds_the_lock(pytester=None):
    from raqeeb.config import BENCH_LOCK
    from tests.conftest import pytest_collection_modifyitems

    class Item:
        def __init__(self, live):
            self.keywords = {"llm": True} if live else {}
            self.markers = []

        def add_marker(self, marker):
            self.markers.append(marker)

    live, other = Item(True), Item(False)
    BENCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    BENCH_LOCK.touch()  # this test process's own lock name
    try:
        pytest_collection_modifyitems(None, [live, other])
    finally:
        BENCH_LOCK.unlink()
    assert live.markers and not other.markers


# -- re-rank after an accepted proposal ------------------------------------------------------------------


def test_an_accepted_cause_makes_a_redundant_rule_guess_drop_out_and_takes_its_ticket():
    """Stuck-tracker rules switched off: the rules call the unit an open string and send a technician. The
    (fake) model proposes the stuck tracker citing all the unit's symptoms; after testing, the ranker runs
    again with it pinned and drops the open-string guess, whose ticket moves to the accepted cause."""
    s = scenario({"type": "TRACKER_STUCK", "target": "INV-03", "start": "05:00"})

    class CitingRouter(FakeRouter):
        def propose_hypotheses(self, results, symptoms=(), ranked=(), unexplained=(), **kw):
            on_unit = [x.id for x in symptoms if x.device_id == "INV-03"]
            hyps = [Hypothesis(id="llm-1", fault_type=FaultType.TRACKER_STUCK, targets=["INV-03"], explains=on_unit,
                               evidence=["fake"])]
            return hyps, [call("hypotheses")]

    rules = run_scenario(s, RaqeebAgent(name="r", disabled=frozenset({FaultType.TRACKER_STUCK})))
    agent = RaqeebAgent(router=CitingRouter([]), name="l", disabled=frozenset({FaultType.TRACKER_STUCK}))
    record = run_scenario(s, agent)
    assert "STRING_OPEN@INV-03" in rules.predicted  # the rule guess without the model
    assert record.predicted == ["TRACKER_STUCK@INV-03"]
    assert any(e.summary.startswith(("diagnosis revised: STRING_OPEN@INV-03", "dropped STRING_OPEN@INV-03"))
               for e in agent.memory.timeline)


def test_a_cause_outside_the_rules_is_not_stacked_on_a_unit_a_rule_cause_explains():
    """practice2 pr2_0008: the model proposed FIRMWARE_ALARM on a unit the rules had right as a stuck tracker."""
    s = scenario({"type": "TRACKER_STUCK", "target": "INV-03", "start": "05:00"},
                 {"type": "TRANSFORMER_TRIP", "target": "T3", "start": "06:00"})
    record, agent, _ = run(s, FakeRouter([("FIRMWARE_ALARM", "INV-03"), ("TRANSFORMER_TRIP", "T3")]))
    assert "FIRMWARE_ALARM@INV-03" not in record.predicted
    assert {"TRACKER_STUCK@INV-03", "TRANSFORMER_TRIP@T3"} <= set(record.predicted)
