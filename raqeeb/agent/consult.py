"""Consult: ask the LLM router for hypotheses when the rules are stuck, and test every proposal.

The router is called only when the causal ranker leaves symptoms unexplained for
CONSULT_AFTER_POLLS polls, or when its top two competing candidates score within the router's
margin; the same question is asked again only after new technician notes, and at most
MAX_HYPOTHESIS_CALLS hypothesis questions are asked per incident. It
gets the tool results, the symptoms, the ranker's candidates, the technician notes, the alarm
manual text and the site layout, all as untrusted data. This module is the only place on the agent
side that reads the technician_notes table; the rules never read note or alarm text.

No proposal is acted on untested. A proposal is accepted only if:
- its fault type is one the causal graph knows and the graph's own candidate check passes for it
  (key symptom present, nothing contradicting it, support >= ACCEPT_MIN_SUPPORT), and it explains a
  symptom the rules left unexplained; or
- its fault type is one the rules do not know, and its footprint matches the data: every symptom
  it cites exists, the unexplained weak units are inside its target (a device, or a transformer
  group or field edge from the plant layout) and cover at least AREA_MIN_SHARE of that area, and
  no unexplained weak unit lies outside it.
Accepted causes from the model are re-tested every poll and dropped when the test fails (unless
something was already done about them). A cause the planner has no ladder for gets only safe
actions: a technician ticket for a single inverter, otherwise escalation to a human.

At the end of the incident the router writes the plain-language explanation and the English and
Arabic shift report from the agent's own records. If the router returns nothing, the rule-based
result stands. Every LlmCall goes into the turn.
"""

from __future__ import annotations

from datetime import datetime

from raqeeb.agent.rank import CAUSAL_GRAPH, candidates, near_tie_candidates
from raqeeb.agent.state import PASSIVE, Cause, CauseStatus, Memory, PollState
from raqeeb.contracts import (
    DECOY_FAULTS,
    PLANT_TARGET,
    Diagnosis,
    FaultType,
    Hypothesis,
    SymptomKind,
    TechnicianNote,
)

CONSULT_AFTER_POLLS = 2  # a symptom unexplained this many polls in a row is worth a question
MAX_HYPOTHESIS_CALLS = 3  # per incident
ACCEPT_MIN_SUPPORT = 0.5  # a known fault type proposed by the model must have this much support in the graph
AREA_MIN_SHARE = 0.75  # an area cause must cover at least this share of the area's inverters
WEAK_KINDS = {SymptomKind.DEVICE_UNDERPERFORMANCE, SymptomKind.FLAT_CAP_DEVICE, SymptomKind.ALARM,
              SymptomKind.DATA_GAP}
SOURCE_LLM = "llm"


def notes_until(memory: Memory, now: datetime) -> list[TechnicianNote]:
    """Technician notes posted so far (untrusted free text: for the model only, never for rules)."""
    rows = memory.telemetry.read("technician_notes", memory.view.start, now)
    return [TechnicianNote(ts=ts.to_pydatetime(), area_or_device=r.area_or_device, author=r.author, text=r.text)
            for ts, r in zip(rows.index, rows.itertuples())]


def manuals(memory: Memory, symptom_ids: set[str]) -> dict[str, str]:
    """Manual text of the alarm codes behind these symptoms (configs/alarm_codes.yaml, reference text)."""
    codes = {memory.symptoms[sid].evidence.get("code") for sid in symptom_ids if sid in memory.symptoms}
    return {c: memory.alarm_codes[c].manual for c in sorted(c for c in codes if c in memory.alarm_codes)}


def _devices(memory: Memory, target: str) -> list[str]:
    return memory.plant.layout.area_of(target) or [target]


# -- testing a proposal ------------------------------------------------------------------------------


def test_hypothesis(memory: Memory, h: Hypothesis, unexplained: set[str]) -> tuple[list[str], str]:
    """(symptom ids it is accepted to explain, "") or ([], why it was rejected)."""
    if h.fault_type in DECOY_FAULTS or len(h.targets) != 1:
        return [], "decoys and multi-target proposals are left to the rules"
    target = h.targets[0]
    active = [memory.symptoms[sid] for sid in sorted(memory.active)]
    if h.fault_type in CAUSAL_GRAPH:
        match = next((c for c in candidates(active, memory.plant) if (c.fault, c.target) == (h.fault_type, target)),
                     None)
        if match is None:
            return [], "the causal graph finds no key symptom for it, or a symptom rules it out"
        explains = sorted({s.id for s in match.explains} & unexplained)
        if match.support < ACCEPT_MIN_SUPPORT or not explains:
            return [], f"support {match.support:.2f}, explains {len(explains)} unexplained symptoms"
        return explains, ""
    devices = set(_devices(memory, target))
    plant = memory.plant
    if target == PLANT_TARGET or not devices <= {*plant.inverters.ids, plant.weather_station_id}:
        return [], f"{target} is not a device or a site area"
    cited = set(h.explains)
    if not cited or not cited <= set(memory.active):
        return [], "it cites symptoms that are not active"
    weak = {memory.symptoms[sid].device_id: sid for sid in unexplained
            if memory.symptoms[sid].kind in WEAK_KINDS and memory.symptoms[sid].device_id in plant.inverters.ids}
    inside = {d for d in weak if d in devices}
    outside = set(weak) - devices
    inverters = devices & set(plant.inverters.ids)
    if not inverters or len(inside) < AREA_MIN_SHARE * len(inverters) or outside:
        return [], (f"footprint does not match: {len(inside)} of {len(inverters)} units in {target} weak, "
                    f"{len(outside)} weak units outside it")
    explains = sorted(sid for sid in unexplained
                      if memory.symptoms[sid].device_id in devices
                      or (sid in cited and memory.symptoms[sid].device_id in (plant.plant_controller_id, PLANT_TARGET)))
    return explains, ""


# -- the node --------------------------------------------------------------------------------------


def _accepted(memory: Memory) -> list[Cause]:
    return [c for c in memory.causes.values() if c.source == SOURCE_LLM and c.status not in PASSIVE]


def _rebuild_diagnosis(memory: Memory, unexplained: set[str]) -> None:
    roots = [c.hypothesis for c in memory.causes.values() if c.status not in PASSIVE]
    old = memory.diagnosis
    explained_now = sum(1 for sid in old.unexplained if sid not in unexplained) if old else 0
    total = len(memory.active) or 1
    confidence = min(1.0, (old.confidence if old else 0.0) + explained_now / total)
    memory.diagnosis = Diagnosis(root_causes=roots, unexplained=sorted(unexplained), confidence=round(confidence, 3))


def consult(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    router = memory.router
    if router is None or memory.diagnosis is None or state.get("final"):
        return {}
    unexplained = set(memory.diagnosis.unexplained)
    changed = False

    # Re-test what the model proposed before; keep what still fits, drop what no longer does
    for cause in _accepted(memory):
        explains, why = test_hypothesis(memory, cause.hypothesis, unexplained | set(cause.hypothesis.explains))
        if explains:
            cause.hypothesis = cause.hypothesis.model_copy(update={"explains": explains})
            unexplained -= set(explains)
        elif not cause.action_ids and cause.status in (CauseStatus.OPEN, CauseStatus.WAITING):
            del memory.causes[cause.label]
            memory.log(now, "consult", f"dropped model-proposed {cause.label}: {why}")
            changed = True

    stuck = {sid for sid in unexplained if memory.unexplained_polls.get(sid, 0) + 1 >= CONSULT_AFTER_POLLS}
    tie = near_tie_candidates(memory, router.margin)
    notes = notes_until(memory, now)
    # The same question again only if site staff have written something new since
    question = (frozenset(stuck) or frozenset(h.labels[0] for h in tie)) | {f"notes:{len(notes)}"}
    if len(question) > 1 and question not in memory.consulted and memory.hypothesis_calls < MAX_HYPOTHESIS_CALLS:
        memory.consulted.add(question)
        memory.hypothesis_calls += 1
        ranked = sorted([*memory.hypotheses, *memory.alternatives], key=lambda h: h.score, reverse=True)
        symptoms = [memory.symptoms[sid] for sid in sorted(memory.active)]
        proposals, calls = router.propose_hypotheses(
            list(memory.results.values()), symptoms, ranked, sorted(stuck), notes=notes,
            manuals=manuals(memory, set(memory.active)), force=bool(stuck))
        memory.llm_calls += calls
        memory.log(now, "consult", f"asked the model about {len(stuck)} unexplained symptoms"
                   + (" and a near tie" if tie else "") + f": {len(proposals)} proposals, {len(calls)} calls")
        for h in proposals:
            label = h.labels[0] if h.targets else h.id
            if label in memory.causes:
                continue
            explains, why = test_hypothesis(memory, h, unexplained)
            if not explains:
                memory.log(now, "consult", f"rejected model proposal {label}: {why}")
                continue
            tested = h.model_copy(update={"id": f"H-{label}", "explains": explains, "score": 0.0,
                                          "evidence": [*h.evidence, "proposed by a language model, tested on tool data"]})
            memory.causes[label] = Cause(hypothesis=tested, first_seen=now, last_selected=now, source=SOURCE_LLM,
                                         confidence=0.0)
            unexplained -= set(explains)
            changed = True
            memory.log(now, "consult", f"accepted model proposal {label} after testing: explains {', '.join(explains)}")
    if changed or unexplained != set(memory.diagnosis.unexplained):
        _rebuild_diagnosis(memory, unexplained)
        return {"diagnosis_changed": True}
    return {}


# -- end of incident: explanation and shift report --------------------------------------------------


def write_up(memory: Memory, draft):
    """The router's explanation and shift report for a template draft; the draft if the router gives nothing."""
    router = memory.router
    if router is None:
        return draft
    symptoms = [memory.symptoms[sid] for sid in sorted(memory.active)]
    explanation, calls = router.explain_diagnosis(memory.diagnosis or Diagnosis(root_causes=[], confidence=0.0),
                                                  symptoms, list(memory.results.values()))
    memory.llm_calls += calls
    notes = [f"Explanation: {explanation}"] if explanation else []
    notes += [f"Escalated to a human: {e.subject} on {', '.join(e.devices)}" for e in memory.escalations]
    draft = draft.model_copy(update={"cost_usd": sum(c.cost_usd for c in memory.llm_calls),
                                     "llm_calls": len(memory.llm_calls)})
    report, calls = router.write_shift_report(draft, notes)
    memory.llm_calls += calls
    if explanation and report.summary_en == draft.summary_en:  # the report call failed: keep the template
        report = report.model_copy(update={"summary_en": f"{draft.summary_en}\nExplanation (language model): "
                                                         f"{explanation}"})
    return report
