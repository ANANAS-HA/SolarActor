"""Ask Raqeeb: questions about the loaded run, answered from the record first.

    answer = ask("why did it reset INV-12", log, now, power, gateway=None)
    answer.facts      # what was retrieved from the trajectory (shown under the reply as "Based on:")
    answer.text       # the reply: the facts, or the model's phrasing of them when a model is reachable

Every question is matched to a deterministic lookup over the RunLog (actions and their rationale, model
proposals, gated actions, the unexplained symptoms, the energy shortfall, causes, escalations). The model,
when a gateway is given, only rewords those facts; it never sees telemetry or the question alone, and its
reply is shown as data (escaped) above the facts it was given. Without a model the facts are shown as they
are. Nothing here can request, approve or reject an action: the module has no path to the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from raqeeb.contracts import LlmRole
from raqeeb.ui import summary, trace
from raqeeb.ui.runlog import RunLog
from raqeeb.ui.source import local_hhmm

PROMPT_FILE = Path(__file__).with_name("prompts") / "ask_raqeeb.md"
PURPOSE = "ask_raqeeb"
MAX_TOKENS = 300
DEVICE = re.compile(r"\b([A-Z]{2,4}-\d{1,3})\b")
EXAMPLES = ("why did it act on INV-12", "what did the model propose", "which actions needed approval",
            "what is still unexplained", "how much energy was lost", "what went wrong", "what was escalated")

DEVICE_Q, PROPOSALS, APPROVALS, UNEXPLAINED, ENERGY, CAUSES, ESCALATIONS, ACTIONS, OVERVIEW = (
    "device", "proposals", "approvals", "unexplained", "energy", "causes", "escalations", "actions", "overview")


@dataclass
class Answer:
    question: str
    topic: str
    facts: list[str]  # retrieved from the record, in order
    text: str  # the reply shown to the operator
    phrased_by: str = ""  # "" = the facts themselves; else the model that reworded them
    llm_s: float = 0.0
    note: str = ""  # why the model was not used, when it was expected


# -- lookups --------------------------------------------------------------------------------------------


def _hm(ts: datetime) -> str:
    return local_hhmm(ts)


def _actions(log: RunLog, now: datetime, device: str | None = None, gated_only: bool = False) -> list[str]:
    recovered = {v.action_id: v for _, v in log.verifications_until(now)}
    out = []
    for a in log.actions_until(now):
        if device and a.request.target != device:
            continue
        if gated_only and not a.gated:
            continue
        status = a.status.value.lower() if a.status else "waiting for a decision"
        who = trace.DECIDED_BY.get(a.decided_by, a.decided_by) if a.status else ""
        v = recovered.get(a.request.id)
        line = (f"{_hm(a.requested_at)} {a.request.action.value} on {a.request.target}"
                f" for {a.cause or 'no named cause'}: {'needed approval' if a.gated else 'auto'}, {status}"
                + (f" {who}" if who else "") + f". Rationale: {a.request.rationale} Expected: {a.request.expected_effect}")
        if v is not None:
            line += (f" Verified: {'recovered' if v.recovered else 'not recovered'} "
                     f"({v.before_kw:.0f} → {v.after_kw:.0f} kW, expected {v.expected_kw:.0f}).")
        out.append(line)
    return out


def _device(log: RunLog, now: datetime, device: str) -> list[str]:
    facts = _actions(log, now, device=device)
    for p in log.polls_until(now)[-1:]:
        for c in p.candidates:
            if device in c.targets or c.label.endswith(f"@{device}"):
                facts.append(f"candidate cause {c.label}: score {c.score:.2f}, "
                             f"{'selected' if c.selected else 'not selected'}")
    for e in log.escalations_until(now):
        if device in e.devices or e.subject.endswith(f"@{device}"):
            facts.append(f"{_hm(e.ts)} escalated {e.subject} ({', '.join(e.devices)}): {e.reason}")
    for pr in log.proposals_until(now):
        if pr.target == device:
            facts.append(f"{_hm(pr.ts)} model proposed {pr.label}: {pr.decision}" + (f" ({pr.reason})" if pr.reason else ""))
    return facts or [f"The record has no action, candidate cause, proposal or escalation about {device}."]


def _proposals(log: RunLog, now: datetime) -> list[str]:
    calls = log.llm_calls_until(now)
    facts = [f"{_hm(p.ts)} {p.label} ({p.model or 'model'}): {p.decision}" + (f", {p.reason}" if p.reason else "")
             + (f"; explains {', '.join(p.explains)}" if p.explains else "") for p in log.proposals_until(now)]
    if not facts:
        facts.append("No model proposal in this run." if not calls else
                     "The model was called but proposed no cause the agent recorded.")
    facts.append(f"Model calls: {len(calls)}, {sum(c.latency_s for c in calls):.1f} s in total."
                 if calls else "Model calls: none (rules only).")
    return facts


def _unexplained(log: RunLog, now: datetime) -> list[str]:
    d = log.diagnosis_until(now)
    statuses = trace.cause_statuses(log, now)
    facts = []
    if d is None:
        facts.append("No diagnosis yet.")
    elif d.unexplained:
        facts.append("Unexplained symptoms: " + ", ".join(d.unexplained) + ".")
    else:
        facts.append("Every symptom is explained by a named cause.")
    for status in (trace.OPEN, trace.ACTING, trace.ESCALATED):
        if statuses[status]:
            facts.append(f"Causes {status}: " + ", ".join(statuses[status]) + ".")
    for e in log.escalations_until(now):
        facts.append(f"{_hm(e.ts)} escalated to a human: {e.subject}"
                     + (f" ({', '.join(e.devices)})" if e.devices else "") + f": {e.reason}")
    return facts


def _energy(power: pd.DataFrame, log: RunLog, now: datetime) -> list[str]:
    e = summary.energy(power, log.actions_until(now), now)
    facts = [f"Energy below the expected-from-reference line up to {_hm(now)}: {e.lost_mwh:.2f} MWh "
             f"(expected {e.expected_mwh:.2f} MWh, metered {e.actual_mwh:.2f} MWh)."]
    if e.first_effective is not None:
        facts.append(f"Before the first applied action took effect ({_hm(e.first_effective)}): "
                     f"{e.lost_before_mwh:.2f} MWh; after: {e.lost_after_mwh:.2f} MWh.")
    if e.gap_worst_pct is not None:
        facts.append(f"Shortfall: worst hour {e.gap_worst_pct:.0f}%, last hour "
                     + ("—" if e.gap_last_pct is None else f"{e.gap_last_pct:.0f}%") + ".")
    facts.append("The reference itself carries a few percent of error; this is the shortfall, not a proven loss.")
    return facts


def _causes(log: RunLog, now: datetime) -> list[str]:
    rows = summary.cause_rows(log, now)
    return [f"{r.cause}: {r.status}" + (f", first named at turn {r.turn}" if r.turn else "")
            + f"; actions: {r.actions}; verified: {r.verified}" for r in rows] or ["No cause named yet."]


def _escalations(log: RunLog, now: datetime) -> list[str]:
    return [f"{_hm(e.ts)} {e.subject}" + (f" ({', '.join(e.devices)})" if e.devices else "") + f": {e.reason}"
            for e in log.escalations_until(now)] or ["Nothing was escalated."]


def _overview(log: RunLog, now: datetime) -> list[str]:
    head = trace.header(log, now)
    named = sum(len(v) for v in head.causes.values())
    return [f"Turn {head.turn}" + (f" of {head.turns_total}" if head.turns_total else "") + f" at {_hm(now)}; "
            f"{named} cause(s) named, {len(head.causes[trace.RESOLVED])} resolved, "
            f"{len(head.causes[trace.ESCALATED])} escalated; {len(log.actions_until(now))} action(s); "
            f"{head.llm_calls} model call(s).",
            "Questions this panel answers: " + "; ".join(EXAMPLES) + "."]


def _named_target(question: str, log: RunLog, now: datetime) -> str | None:
    """A device or target the question names: one the record knows (actions, candidates, escalations,
    proposals; longest match first), else anything shaped like a device id (INV-12, MET-01)."""
    known = {a.request.target for a in log.actions_until(now)}
    known |= {t for p in log.polls_until(now) for c in p.candidates for t in c.targets}
    known |= {d for e in log.escalations_until(now) for d in e.devices} | {p.target for p in log.proposals_until(now)}
    words = re.findall(r"[A-Za-z0-9_-]+", question.upper())
    for target in sorted((k for k in known if k), key=len, reverse=True):
        if target.upper() in words:
            return target
    match = DEVICE.search(question.upper())
    return match.group(1) if match else None


def lookup(question: str, log: RunLog, now: datetime, power: pd.DataFrame) -> tuple[str, list[str]]:
    """(topic, facts) for a question: a deterministic match on words and device ids."""
    q = question.lower()
    device = _named_target(question, log, now)
    if device:
        return DEVICE_Q, _device(log, now, device)
    if any(w in q for w in ("propos", "model", "llm", "suggest")):
        return PROPOSALS, _proposals(log, now)
    if any(w in q for w in ("approv", "gated", "permission", "human")):
        return APPROVALS, _actions(log, now, gated_only=True) or ["No action needed approval."]
    if any(w in q for w in ("unexplained", "still open", "open", "explain", "remaining")):
        return UNEXPLAINED, _unexplained(log, now)
    if any(w in q for w in ("energy", "lost", "loss", "mwh", "kwh", "recover")):
        return ENERGY, _energy(power, log, now)
    if any(w in q for w in ("escalat", "hand", "ticket")):
        return ESCALATIONS, _escalations(log, now)
    if any(w in q for w in ("cause", "root", "diagnos", "wrong", "fault", "happen")):
        return CAUSES, _causes(log, now)
    if any(w in q for w in ("action", "did it do", "what did", "reset", "clean", "derate", "dispatch")):
        return ACTIONS, _actions(log, now) or ["No action requested yet."]
    return OVERVIEW, _overview(log, now)


# -- phrasing ---------------------------------------------------------------------------------------------


def _prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def phrase(question: str, facts: list[str], gateway) -> tuple[str, str, float] | None:
    """The model's wording of `facts` for `question`: (text, model label, seconds), or None on any failure.
    The facts are delimited data; the model adds nothing of its own (the prompt says so, and the facts are
    shown next to the reply either way)."""
    try:
        messages = [{"role": "system", "content": _prompt()},
                    {"role": "user", "content": "Question: " + question.replace("\n", " ") + "\n\nFacts:\n<<<\n"
                                                + "\n".join(f"- {f}" for f in facts) + "\n>>>"}]
        text, call = gateway.complete(LlmRole.LOCAL, PURPOSE, messages, max_tokens=MAX_TOKENS)
        text = text.strip()
        if not text:
            return None
        return text, gateway.model_label(LlmRole.LOCAL), float(call.latency_s)
    except Exception:  # noqa: BLE001 - the facts are the fallback
        return None


def ask(question: str, log: RunLog, now: datetime, power: pd.DataFrame, gateway=None) -> Answer:
    topic, facts = lookup(question, log, now, power)
    if gateway is None:
        return Answer(question, topic, facts, "\n\n".join(facts))
    phrased = phrase(question, facts, gateway)
    if phrased is None:
        return Answer(question, topic, facts, "\n\n".join(facts), note="the model did not answer; facts shown directly")
    text, model, seconds = phrased
    return Answer(question, topic, facts, text, phrased_by=model, llm_s=seconds)
