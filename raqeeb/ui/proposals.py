"""Model proposals as the console shows them, read from what the agent already exposes.

agent/consult.py logs each proposal it tests as one timeline line (the same lines bench/ablation.py
reads into proposals.csv):

    asked the model about <n> unexplained symptoms[ and a near tie]: <p> proposals, <c> calls
    accepted model proposal <LABEL> after testing: explains <symptom id>, ...
    rejected model proposal <LABEL>: <reason>
    dropped model-proposed <LABEL>: <reason>          (a later re-test failed; nothing had been done)

Each becomes a ProposalRecord with the test as the first check, then the tool results that cover the
proposal's target at that poll, and the time of that poll's hypothesis calls. Newer agent logs also
carry structured details (decision, label, reason, hypothesis, numbers): the test's values become a
check row and a rejected proposal's own evidence is shown. Older files have only the label and reason.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime

from raqeeb.contracts import PLANT_TARGET, LlmCall, LayoutConfig, ToolResult, parse_cause_label
from raqeeb.ui.runlog import ProposalCheck, ProposalRecord, TimelineItem

HYPOTHESES_PURPOSE = "hypotheses"
MAX_TOOL_CHECKS = 6
AGENT_EVIDENCE = "proposed by a language model, tested on tool data"  # added by consult.py, not the model's words

_ACCEPTED = re.compile(r"^accepted model proposal (?P<label>\S+) after testing: explains (?P<explains>.*)$")
_REJECTED = re.compile(r"^rejected model proposal (?P<label>\S+): (?P<reason>.*)$")
_DROPPED = re.compile(r"^dropped model-proposed (?P<label>\S+): (?P<reason>.*)$")
_ASKED = re.compile(r"^asked the model about .*: (?P<proposals>\d+) proposals, (?P<calls>\d+) calls$")

NOTHING_CHANGED = "Changed nothing: no cause added, no action requested, diagnosis unchanged."


def proposals_from_poll(ts: datetime, timeline: Iterable[TimelineItem], llm_calls: Iterable[LlmCall],
                        results: Iterable[ToolResult], evidence: Mapping[str, list[str]] | None = None,
                        layout: LayoutConfig | None = None) -> list[ProposalRecord]:
    """The proposals the agent decided at one poll. `evidence` = label -> the accepted hypothesis's
    evidence (from the agent's causes), `results` = the tool results at that poll."""
    calls = [c for c in llm_calls if c.purpose == HYPOTHESES_PURPOSE]
    llm_s = round(sum(c.latency_s for c in calls), 3)
    model = ", ".join(sorted({c.model for c in calls}))
    results = list(results)
    out = []
    for item in timeline:
        if item.node != "consult":
            continue
        for decision, pattern in (("accepted", _ACCEPTED), ("rejected", _REJECTED), ("dropped", _DROPPED)):
            match = pattern.match(item.summary)
            if match:
                break
        else:
            continue
        label = match["label"]
        try:
            fault, target = parse_cause_label(label)
        except ValueError:
            continue
        explains = [s.strip() for s in match["explains"].split(",")] if decision == "accepted" else []
        test = "Agent's tool test (causal graph)" if _known(fault) else "Agent's tool test (footprint on the site layout)"
        if decision == "accepted":
            first = ProposalCheck(check=test, result=f"passed: explains {len(explains)} symptoms the rules left "
                                                     f"unexplained ({', '.join(explains)})", passed=True)
        elif decision == "rejected":
            first = ProposalCheck(check=test, result=f"failed: {match['reason']}", passed=False)
        else:
            first = ProposalCheck(check=f"{test}, re-run", result=f"failed: {match['reason']}", passed=False)
        checks = [first]
        details = item.details or {}
        if details.get("numbers"):  # the test's own values, when the agent logged them (structured consult logs)
            checks.append(ProposalCheck(check="Test values", result=", ".join(
                f"{k} {v:g}" if isinstance(v, float) else f"{k} {v}" for k, v in details["numbers"].items())))
        checks += _tool_checks(target, results, layout)
        words = [e for e in (evidence or {}).get(label, []) if e != AGENT_EVIDENCE] if decision == "accepted" else []
        if not words and isinstance(details.get("hypothesis"), dict):  # rejected ones: the proposal as the model sent it
            words = [str(e) for e in details["hypothesis"].get("evidence", [])]
        out.append(ProposalRecord(ts=ts, label=label, fault_type=fault.value, target=target, decision=decision,
                                  reason=match.groupdict().get("reason") or "", explains=explains, checks=checks,
                                  model_evidence=words, model=model,
                                  llm_s=llm_s if decision != "dropped" else 0.0))
    return out


def _known(fault) -> bool:
    from raqeeb.agent.rank import CAUSAL_GRAPH  # public table of the causal graph (read only)

    return fault in CAUSAL_GRAPH


def _tool_checks(target: str, results: list[ToolResult], layout: LayoutConfig | None) -> list[ProposalCheck]:
    """Tool results that cover the proposal's target (its devices, or plant-wide for ALL)."""
    if layout is None:
        from raqeeb.config import load_plant
        layout = load_plant().layout
    devices = set(layout.area_of(target) or [target])
    hits = [r for r in results if devices & set(r.devices) or (target == PLANT_TARGET and PLANT_TARGET in r.devices)]
    hits.sort(key=lambda r: (not r.finding, r.tool))
    return [ProposalCheck(check=f"{r.tool} on {', '.join(r.devices)}", result=f"{r.verdict}: {r.evidence}")
            for r in hits[:MAX_TOOL_CHECKS]]


def effect(record: ProposalRecord, actions: Iterable, escalations: Iterable) -> str:
    """What the proposal changed, for the panel."""
    if record.decision == "rejected":
        return NOTHING_CHANGED
    if record.decision == "dropped":
        return "Removed from the diagnosis when its re-test failed; nothing had been done about it."
    acted = [f"{a.request.action.value} on {a.request.target} ({a.status.value if a.status else 'waiting'})"
             for a in actions if a.cause == record.label]
    handed = [f"handed to a human ({e.reason})" for e in escalations if e.subject == record.label]
    done = "; ".join(acted + handed) or "no action yet"
    return f"Added {record.label} to the diagnosis (explains {len(record.explains)} symptoms). Then: {done}."
