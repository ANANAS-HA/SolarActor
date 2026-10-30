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
