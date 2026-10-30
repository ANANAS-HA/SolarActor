"""Rank causes: a NetworkX causal graph (fault type -> symptom kinds it can cause) and a greedy,
simplest-explanation selection.

Each link says which symptoms a fault explains and at what scope (the whole plant, one device,
the weather station, or the weather). A `key` link can propose a cause on its own; a `predicted`
link is a symptom the fault should produce, so its absence counts against it. A candidate cause
scores

    explained symptom weight - ABSENT_PENALTY x predicted-but-absent links - CAUSE_PENALTY

and causes are picked greedily by marginal score (symptoms already explained by a chosen cause
add nothing), while the score stays positive. Symptoms (e.g. LOW_PR alarms) are thereby left to
the root cause that explains them, and an extra cause must earn its place.

CURTAILMENT is ranked like any cause (it explains a plant-wide cap under a grid limit), then
reported as expected behaviour: never a root cause, never an action. PROMPT_INJECTION explains a
device log line (known only by its code and device; the text is never read) and is reported as
flagged for security review: never a root cause, never an action. Log lines count neither for
nor against the confidence, so no log line can push an irreversible action over its threshold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import networkx as nx

from raqeeb.agent.state import PASSIVE, Cause, CauseStatus, PollState
from raqeeb.contracts import (
    DECOY_FAULTS,
    PLANT_TARGET,
    Diagnosis,
    FaultType,
    Hypothesis,
    PlantConfig,
    Symptom,
    SymptomKind,
)

ABSENT_PENALTY = 0.25
CAUSE_PENALTY = 0.4
DROP_AFTER_MISSES = 2  # a cause not selected for this many polls, with no action on it, is dropped
WEIGHTS = {SymptomKind.ALARM: 0.5, SymptomKind.HIGH_WIND: 0.3}  # alarms are noisy; wind is weather
BENIGN = {SymptomKind.HIGH_WIND}  # unexplained, these are just weather
RECORD_ONLY = {SymptomKind.DEVICE_LOG}  # kept out of the confidence and the unexplained list
PERFORMANCE_CODES = frozenset({"LOW_PR", "LOW_STRING_CURRENT", "DC_INPUT_LOW"})

F, K = FaultType, SymptomKind


@dataclass(frozen=True)
class Link:
    fault: FaultType
    kind: SymptomKind
    scope: str  # "plant", "device" (same device as the cause), "any_inverter", "station", "weather"
    key: bool = False
    predicted: bool = False
    codes: frozenset[str] = frozenset()  # for ALARM symptoms: the codes this link covers
    when: Callable[[Symptom], bool] | None = None


def _alarm(fault: FaultType, codes: set[str], scope: str, **kw) -> Link:
    return Link(fault, K.ALARM, scope, codes=frozenset(codes), **kw)


LINKS: list[Link] = [
    # Dust storm soiling: the plant loses output against the trusted reference; storm wind before it
    Link(F.SOILING, K.PLANT_UNDERPERFORMANCE, "plant", key=True, predicted=True),
    Link(F.SOILING, K.HIGH_WIND, "weather", predicted=True),
    Link(F.SOILING, K.DEVICE_UNDERPERFORMANCE, "any_inverter"),  # uneven dust
    _alarm(F.SOILING, PERFORMANCE_CODES, "plant", key=True),  # the whole fleet below its sensor-based expected
    _alarm(F.SOILING, PERFORMANCE_CODES, "any_inverter"),
    # Dusty irradiance sensor
    Link(F.SENSOR_SOILING, K.SENSOR_DEVIATION, "station", key=True, predicted=True),
    # Thermal derate: flat cap below peers, hot, its own alarm, and the LOW_PR family on that unit
    Link(F.INVERTER_DERATE, K.FLAT_CAP_DEVICE, "device", key=True, predicted=True),
    Link(F.INVERTER_DERATE, K.HIGH_TEMPERATURE, "device", key=True, predicted=True),
    Link(F.INVERTER_DERATE, K.DEVICE_UNDERPERFORMANCE, "device"),
    _alarm(F.INVERTER_DERATE, {"DERATE_TEMP"}, "device", key=True, predicted=True),
    _alarm(F.INVERTER_DERATE, PERFORMANCE_CODES, "device"),
    # Comms dropout: rows missing while the meter shows the device still producing
    Link(F.COMMS_DROPOUT, K.DATA_GAP, "device", key=True, predicted=True,
         when=lambda s: s.evidence.get("still_producing") is not False),
    _alarm(F.COMMS_DROPOUT, {"COMM_LOSS"}, "device", key=True, predicted=True),
    # Grid curtailment (decoy): all inverters capped together under a grid limit
    Link(F.CURTAILMENT, K.FLAT_CAP_PLANT, "plant", key=True, predicted=True,
         when=lambda s: bool(s.evidence.get("grid_limit"))),
    _alarm(F.CURTAILMENT, {"GRID_LIMIT_ACTIVE"}, "plant", key=True),
    # Full scope
    Link(F.TRACKER_STUCK, K.TRACKER_ANGLE_FLAT, "device", key=True, predicted=True),
    Link(F.TRACKER_STUCK, K.DEVICE_UNDERPERFORMANCE, "device"),
    _alarm(F.TRACKER_STUCK, {"TRACKER_FAULT"}, "device", key=True),
    Link(F.STRING_OPEN, K.DEVICE_UNDERPERFORMANCE, "device", key=True, predicted=True),
    _alarm(F.STRING_OPEN, PERFORMANCE_CODES, "device"),
    Link(F.UNAUTHORIZED_CMD, K.UNEXPECTED_COMMAND, "device", key=True, predicted=True),
    _alarm(F.UNAUTHORIZED_CMD, {"REMOTE_STOP"}, "device", key=True),
    Link(F.UNAUTHORIZED_CMD, K.DEVICE_UNDERPERFORMANCE, "device"),
    Link(F.UNAUTHORIZED_CMD, K.DATA_GAP, "device", when=lambda s: s.evidence.get("still_producing") is False),
    # Injected instruction (decoy): a free-text log line from a device. Flagged, never acted on
    Link(F.PROMPT_INJECTION, K.DEVICE_LOG, "device", key=True, predicted=True),
]


def build_graph(links: list[Link] = LINKS) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for link in links:
        graph.add_edge(link.fault, link.kind, link=link)
    return graph


CAUSAL_GRAPH = build_graph()


def links_of(fault: FaultType) -> list[Link]:
    return [data["link"] for _, _, data in CAUSAL_GRAPH.out_edges(fault, data=True)]


def links_to(kind: SymptomKind) -> list[Link]:
    return [data["link"] for _, _, data in CAUSAL_GRAPH.in_edges(kind, data=True)] if kind in CAUSAL_GRAPH else []


def valid_target(fault: FaultType, target: str, plant: PlantConfig) -> bool:
    if fault in (F.SOILING, F.CURTAILMENT):
        return target == PLANT_TARGET
    if fault == F.SENSOR_SOILING:
        return target == plant.weather_station_id
    if fault == F.COMMS_DROPOUT:
        return target in plant.inverters.ids or target == plant.weather_station_id
    if fault == F.PROMPT_INJECTION:
        return target in {*plant.inverters.ids, plant.weather_station_id, plant.meter_id, plant.plant_controller_id}
    return target in plant.inverters.ids


def matches(link: Link, symptom: Symptom, target: str, plant: PlantConfig) -> bool:
    if link.kind != symptom.kind:
        return False
    if link.codes and symptom.evidence.get("code") not in link.codes:
        return False
    device = symptom.device_id
    scope_ok = {
        "plant": device in (PLANT_TARGET, plant.plant_controller_id),
        "device": device == target,
        "any_inverter": device in plant.inverters.ids,
        "station": device == plant.weather_station_id == target,
        "weather": device == plant.weather_station_id,
    }[link.scope]
    return scope_ok and (link.when is None or link.when(symptom))


def _proposed_target(link: Link, symptom: Symptom) -> str | None:
    if link.scope == "plant":
        return PLANT_TARGET
    if link.scope in ("device", "station"):
        return symptom.device_id
    return None


def weight(symptom: Symptom) -> float:
    return WEIGHTS.get(symptom.kind, 1.0)


@dataclass
class Candidate:
    fault: FaultType
    target: str
    explains: list[Symptom]
    absent: list[Link]

    @property
    def label(self) -> str:
        return f"{self.fault.value}@{self.target}"


def candidates(symptoms: list[Symptom], plant: PlantConfig) -> list[Candidate]:
    proposed: dict[tuple[FaultType, str], None] = {}
    for symptom in symptoms:
        for link in links_to(symptom.kind):
            target = _proposed_target(link, symptom)
            if (link.key and target is not None and valid_target(link.fault, target, plant)
                    and matches(link, symptom, target, plant)):
                proposed[(link.fault, target)] = None
    out = []
    for fault, target in proposed:
        links = links_of(fault)
        explains = [s for s in symptoms if any(matches(link, s, target, plant) for link in links)]
        absent = [link for link in links if link.predicted
                  and not any(matches(link, s, target, plant) for s in symptoms)]
        out.append(Candidate(fault, target, explains, absent))
    return out


def select(cands: list[Candidate]) -> list[tuple[Candidate, float]]:
    """Greedy simplest explanation: add the cause with the best marginal score while it is positive."""
    chosen: list[tuple[Candidate, float]] = []
    explained: set[str] = set()
    pool = list(cands)
    while pool:
        def gain(c: Candidate) -> float:
            new = sum(weight(s) for s in c.explains if s.id not in explained)
            return new - ABSENT_PENALTY * len(c.absent) - CAUSE_PENALTY

        best = max(pool, key=lambda c: (gain(c), c.label))
        score = gain(best)
        if score <= 0:
            break
        chosen.append((best, score))
        explained |= {s.id for s in best.explains}
        pool.remove(best)
    return chosen


def rank_causes(symptoms: list[Symptom], plant: PlantConfig) -> tuple[list[Hypothesis], list[str], float]:
    """Selected hypotheses (best first), unexplained symptom ids and a confidence in [0, 1]."""
    chosen = select(candidates(symptoms, plant))
    hypotheses = [
        Hypothesis(id=f"H-{c.label}", fault_type=c.fault, targets=[c.target],
                   explains=sorted(s.id for s in c.explains),
                   evidence=[s.evidence.get("summary", s.id) for s in c.explains]
                   + [f"expected but not seen: {link.kind.value}" for link in c.absent],
                   score=round(score, 3))
        for c, score in chosen
    ]
    explained = {sid for h in hypotheses for sid in h.explains}
    symptoms = [s for s in symptoms if s.kind not in RECORD_ONLY]
    unexplained = sorted(s.id for s in symptoms if s.id not in explained and s.kind not in BENIGN)
    total = sum(weight(s) for s in symptoms if s.id in explained or s.kind not in BENIGN)
    confidence = sum(weight(s) for s in symptoms if s.id in explained) / total if total else 1.0
    return hypotheses, unexplained, round(min(max(confidence, 0.0), 1.0), 3)


def rank(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    symptoms = [memory.symptoms[sid] for sid in sorted(memory.active)]
    hypotheses, unexplained, confidence = rank_causes(symptoms, memory.plant)
    memory.hypotheses = hypotheses
    selected = {h.labels[0]: h for h in hypotheses}

    for label, h in selected.items():
        cause = memory.causes.get(label)
        if cause is None:
            status = (CauseStatus.FLAGGED if h.fault_type == FaultType.PROMPT_INJECTION
                      else CauseStatus.EXPECTED if h.fault_type in DECOY_FAULTS else CauseStatus.OPEN)
            memory.causes[label] = Cause(hypothesis=h, first_seen=now, last_selected=now, status=status)
            what = {CauseStatus.FLAGGED: "flagged for security review", CauseStatus.EXPECTED: "expected behaviour"}
            memory.log(now, "rank", f"{what.get(status, 'root cause')}: {label} (score {h.score})", evidence=h.evidence)
        else:
            cause.hypothesis, cause.last_selected, cause.misses = h, now, 0
    for label, cause in list(memory.causes.items()):
        if label in selected:
            continue
        cause.misses += 1
        if cause.misses >= DROP_AFTER_MISSES and cause.status in (CauseStatus.OPEN, CauseStatus.WAITING) \
                and not cause.action_ids:
            del memory.causes[label]
            memory.log(now, "rank", f"dropped {label}: no longer supported by the evidence")

    roots = [c.hypothesis for c in memory.causes.values() if c.status not in PASSIVE]
    diagnosis = Diagnosis(root_causes=roots, unexplained=unexplained, confidence=confidence)
    old = memory.diagnosis
    changed = old is None or sorted(h.labels[0] for h in old.root_causes) != sorted(h.labels[0] for h in roots)
    memory.diagnosis = diagnosis
    return {"diagnosis_changed": changed}
