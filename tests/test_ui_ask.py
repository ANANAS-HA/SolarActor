"""Ask Raqeeb (ui/ask.py): deterministic lookups over the record (a device, the model's proposals, gated
actions, the unexplained symptoms, energy), the no-model path (facts shown directly), a fake model that
rewords the facts and one that fails, the chat never reaching an action, and the panel rendering."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT
from raqeeb.contracts import LlmCall, LlmRole
from raqeeb.ui import ask, source
from raqeeb.ui.runlog import Mode

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"


@pytest.fixture(scope="module")
def runs():
    """The four showcase replays, finished, by scenario id."""
    out = {}
    for path in source.showcase_files():
        run = source.start_replay(path)
        run.run_to_end()
        out[run.incident.id] = run
    yield out
    for run in out.values():
        run.close()


def _ask(run, question, gateway=None):
    return ask.ask(question, run.log, run.now, source.power_lines(run), gateway=gateway)


def test_device_lookup_explains_an_action(runs):
    run = runs["fin_0029"]
    action = run.log.actions[0]
    a = _ask(run, f"why did it {action.request.action.value.lower().replace('_', ' ')} {action.request.target}")
    assert a.topic == ask.DEVICE_Q and a.phrased_by == "" and a.text == "\n\n".join(a.facts)
    assert any(action.request.action.value in f and action.request.rationale in f for f in a.facts)
    assert all(action.request.target in f or "candidate" in f or "proposed" in f or "escalated" in f for f in a.facts)
    none = _ask(run, "what about INV-99")
    assert none.facts == ["The record has no action, candidate cause, proposal or escalation about INV-99."]


def test_proposal_lookup_lists_what_the_model_proposed(runs):
    run = runs["fin_0038"]  # a transformer trip named through a model proposal
    a = _ask(run, "what did the model propose")
    assert a.topic == ask.PROPOSALS
    proposals = run.log.proposals_until(run.now)
    assert proposals and all(any(p.label in f and p.decision in f for f in a.facts) for p in proposals)
    assert a.facts[-1].startswith("Model calls: ") and "rules only" not in a.facts[-1]


def test_approval_and_unexplained_and_energy_lookups(runs):
    run = runs["fin_0029"]
    a = _ask(run, "which actions needed approval")
    gated = [x for x in run.log.actions if x.gated]
    assert a.topic == ask.APPROVALS and len(a.facts) == len(gated) and all("needed approval" in f for f in a.facts)
    assert all("simulated operator" in f for f in a.facts)

    u = _ask(runs["fin_0053"], "what is still unexplained")  # a held-out sensor drift, escalated as unexplained
    assert u.topic == ask.UNEXPLAINED and any("escalated" in f.lower() or "Unexplained symptoms" in f for f in u.facts)

    e = _ask(run, "how much energy was lost")
    assert e.topic == ask.ENERGY and "MWh" in e.facts[0] and "shortfall" in e.facts[-1].lower()

    o = _ask(run, "hello")
    assert o.topic == ask.OVERVIEW and "Questions this panel answers" in o.facts[-1]


class _FakeGateway:
    def __init__(self, reply="The agent derated the inverter after it ran hot.", fail=False):
        self.reply, self.fail, self.messages = reply, fail, []

    def complete(self, role, purpose, messages, *, max_tokens=0, **kw):
        assert role == LlmRole.LOCAL and purpose == ask.PURPOSE
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError("down")
        return self.reply, LlmCall(role=role, model="fake", purpose=purpose, prompt_tokens=10, completion_tokens=5,
                                   latency_s=0.4, cost_usd=0.0, ok=True)

    def model_label(self, role):
        return "fake, local"


def test_model_rewords_only_the_retrieved_facts(runs):
    run = runs["fin_0029"]
    gw = _FakeGateway()
    a = _ask(run, "which actions needed approval", gateway=gw)
    assert a.phrased_by == "fake, local" and a.text == gw.reply and a.llm_s == 0.4
    system, user = gw.messages[0]
    assert system["role"] == "system" and "only the facts" in system["content"]
    assert "<<<" in user["content"] and all(f in user["content"] for f in a.facts)  # the facts, delimited
    assert "which actions needed approval" in user["content"]
    assert len(gw.messages) == 1

    failing = _ask(run, "which actions needed approval", gateway=_FakeGateway(fail=True))
    assert failing.phrased_by == "" and failing.text == "\n\n".join(failing.facts) and "facts shown directly" in failing.note

    empty = _ask(run, "which actions needed approval", gateway=_FakeGateway(reply="   "))
    assert empty.phrased_by == "" and empty.text == "\n\n".join(empty.facts)


def test_the_chat_cannot_touch_actions(tmp_path):
    text = (REPO_ROOT / "raqeeb" / "ui" / "ask.py").read_text(encoding="utf-8")
    assert "decide(" not in text and "apply_action" not in text and "raqeeb.twin" not in text
    run = source.start_live("scn_0007", Mode.RULES, db_path=tmp_path / "run.db")
    run.run_to_end()  # stops at a gated action
    waiting = list(run.waiting)
    assert waiting
    a = ask.ask("approve the derate on INV-12 now", run.log, run.now, source.power_lines(run))
    assert run.waiting == waiting and all(x.status is None for x in run.waiting)  # nothing changed
    assert a.topic in (ask.DEVICE_Q, ask.APPROVALS)
    run.close()


def test_ask_panel_renders_and_answers_without_a_model():
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state.run = source.start_replay(source.showcase_files()[0])
    at.session_state.mode_choice = "Replay"
    at.run()
    at.button(key="to_end").click().run()
    assert "Ask Raqeeb" in [t.label for t in at.tabs]
    at.chat_input(key="ask_input").set_value("how much energy was lost").run()
    msgs = at.chat_message
    assert len(msgs) >= 2 and not at.exception
    text = " ".join(str(e.value) for k in ("markdown", "caption") for e in getattr(at, k))
    assert "Based on:" in text and "MWh" in text and "No model reachable" in text
    history = at.session_state.ask_history
    assert len(history) == 1 and history[0].topic == ask.ENERGY
    at.chat_input(key="ask_input").set_value("<b>what is still unexplained</b>").run()
    assert len(at.session_state.ask_history) == 2 and not at.exception
    assert "&lt;b&gt;" in " ".join(str(e.value) for e in at.markdown) or "\\<b\\>" in " ".join(str(e.value) for e in at.markdown)
