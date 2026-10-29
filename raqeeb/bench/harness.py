"""Run one scenario with one agent and record what the scorer needs.

    record = run_scenario(scenario, agent)          # RunRecord
    row = score_run(record)                         # bench/score.py

The harness is the agent's environment. It drives the plant through the FaultInjector, writes
each new batch to the telemetry the agent reads, and polls the agent every `poll_min` sim
minutes with only scenario.agent_view(). Returned ActionRequests are gated with
configs/actions.yaml (not the agent's own needs_approval flag), approved by `approve` (bench
default: a simulated operator approves everything; the count is reported) and run with
twin.controls.apply_action. Outcomes go back to the agent on the next poll.

The harness (not the agent) also reads the twin: after every poll it checks in twin.state
whether each answer-key cause is mitigated, and it takes energy from twin.truth. Energy is
compared with a no-action run (same faults, no fixes) and a decoy-only run (only the
curtailment decoys, i.e. no real fault at all) of the same seed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from raqeeb.config import load_actions, load_plant
from raqeeb.contracts import (
    DECOY_FAULTS,
    STEP_MINUTES,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    Agent,
    FaultType,
    IncidentReport,
    LlmCall,
    PlantConfig,
    PlantState,
    Scenario,
    Verification,
)
from raqeeb.bench.memory import MemoryTelemetry
from raqeeb.faults.injector import FaultInjector
from raqeeb.twin.controls import apply_action
from raqeeb.twin.simulator import StepTruth
from raqeeb.twin.weather import weather_5min

POLL_MIN = 15
STEP = timedelta(minutes=STEP_MINUTES)


def approve_all(request: ActionRequest) -> bool:
    return True


# -- mitigation in the twin state (bench only: the agent never sees the state) ------------


def _soiling_mitigated(state: PlantState, target: str) -> bool:
    calm = state.atmosphere.wind_add_ms == 0.0 and state.atmosphere.irradiance_factor == 1.0
    return calm and all(inv.soiling_loss == 0.0 for inv in state.inverters.values())


def _sensor_mitigated(state: PlantState, target: str) -> bool:
    return state.station.flagged or state.station.sensor_soiling == 0.0


def _derate_mitigated(state: PlantState, target: str) -> bool:
    """Repaired, or held by a power limit at or below the derate cap (section 8: stabilised)."""
    inv = state.inverters[target]
    return inv.derate_cap_kw is None or (inv.power_limit_kw is not None
                                         and inv.power_limit_kw <= inv.derate_cap_kw + 1e-6)


def _comms_mitigated(state: PlantState, target: str) -> bool:
    device = state.inverters.get(target, state.station)
    return device.comms_up


MITIGATED: dict[FaultType, Callable[[PlantState, str], bool]] = {
    FaultType.SOILING: _soiling_mitigated,
    FaultType.SENSOR_SOILING: _sensor_mitigated,
    FaultType.INVERTER_DERATE: _derate_mitigated,
    FaultType.COMMS_DROPOUT: _comms_mitigated,
    FaultType.TRACKER_STUCK: lambda s, t: s.inverters[t].tracker_override_deg is None,
    FaultType.STRING_OPEN: lambda s, t: s.inverters[t].string_loss == 0.0,
    FaultType.UNAUTHORIZED_CMD: lambda s, t: s.inverters[t].isolated or not s.inverters[t].stopped,
}


# -- records ---------------------------------------------------------------------------------


@dataclass
class ActionRecord:
    ts: datetime  # sim time the harness handled it
    request: ActionRequest
    status: ActionStatus
    gated: bool  # needed approval per configs/actions.yaml
    note: str = ""


@dataclass
class TurnRecord:
    ts: datetime
    mitigated: dict[str, bool]  # answer-key label -> mitigated in the twin state after this poll


@dataclass
class ReferenceEnergy:
    """Plant energy (MWh, from twin.truth) of the two agent-free runs of a scenario."""

    no_action_mwh: float  # all planted faults, nobody fixes anything
    decoy_only_mwh: float  # only the decoys (curtailment): output with no real fault at all


@dataclass
class RunRecord:
    scenario_id: str
    agent: str
    root_causes: list[str]  # answer key
    cause_starts: dict[str, datetime]
    predicted: list[str] = field(default_factory=list)  # labels of the final diagnosis
    confidence: float | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    verifications: list[tuple[datetime, Verification]] = field(default_factory=list)
    llm_calls: list[LlmCall] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    report: IncidentReport | None = None
    energy_mwh: float = 0.0
    reference: ReferenceEnergy | None = None

    @property
    def incident_start(self) -> datetime | None:
        return min(self.cause_starts.values(), default=None)


# -- runs ------------------------------------------------------------------------------------


def energy_mwh(truth: list[StepTruth]) -> float:
    return sum(t.plant_ac_kw for t in truth) * STEP_MINUTES / 60.0 / 1000.0


def reference_energy(scenario: Scenario, plant: PlantConfig | None = None,
                     weather: pd.DataFrame | None = None) -> ReferenceEnergy:
    plant = plant or load_plant()
    weather = weather if weather is not None else weather_5min(scenario.start, scenario.end, plant)
    no_action = FaultInjector.for_scenario(scenario, plant, weather)
    no_action.run_to_end()
    decoys = scenario.model_copy(update={"faults": [f for f in scenario.faults if f.type in DECOY_FAULTS],
                                         "answer_key": None})
    decoy_only = FaultInjector.for_scenario(decoys, plant, weather)
    decoy_only.run_to_end()
    return ReferenceEnergy(no_action_mwh=energy_mwh(no_action.twin.truth),
                           decoy_only_mwh=energy_mwh(decoy_only.twin.truth))


def _answer_key(scenario: Scenario) -> list[str]:
    return list((scenario.answer_key or scenario.expected_answer_key()).root_causes)


def run_scenario(scenario: Scenario, agent: Agent, *, poll_min: int = POLL_MIN,
                 approve: Callable[[ActionRequest], bool] = approve_all, telemetry=None,
                 reference: ReferenceEnergy | None = None, plant: PlantConfig | None = None) -> RunRecord:
    """Run `agent` on `scenario` to the end of its window. `telemetry` needs write_batch and read."""
    if poll_min < STEP_MINUTES or poll_min % STEP_MINUTES:
        raise ValueError(f"poll_min must be a multiple of {STEP_MINUTES}")
    plant = plant or load_plant()
    weather = weather_5min(scenario.start, scenario.end, plant)
    reference = reference or reference_energy(scenario, plant, weather)
    specs = load_actions()
    inj = FaultInjector.for_scenario(scenario, plant, weather)
    twin = inj.twin
    telemetry = telemetry if telemetry is not None else MemoryTelemetry()

    labels = _answer_key(scenario)
    by_label = {f.label: f for f in scenario.faults}
    starts = {label: scenario.resolve_time(by_label[label].start) for label in labels}
    record = RunRecord(scenario_id=scenario.id, agent=agent.name, root_causes=labels, cause_starts=starts,
                       reference=reference)
    seen_ids: set[str] = set()

    def execute(request: ActionRequest) -> ActionOutcome:
        gated = specs[request.action].needs_approval
        status, note = ActionStatus.APPLIED, ""
        if request.id in seen_ids:
            status, note = ActionStatus.FAILED, f"duplicate action id {request.id!r}"
        elif gated and not approve(request):
            status, note = ActionStatus.REJECTED, "approval refused"
        else:
            try:
                apply_action(twin, request.action, request.target, request.params)
            except (ValueError, TypeError, KeyError) as exc:
                status, note = ActionStatus.FAILED, str(exc)
        seen_ids.add(request.id)
        record.actions.append(ActionRecord(ts=twin.clock, request=request, status=status, gated=gated, note=note))
        return ActionOutcome(action_id=request.id, status=status, ts=twin.clock, note=note)

    def mitigated(now: datetime) -> dict[str, bool]:
        """A cause counts only once its first faulty step has run."""
        out = {}
        for label in labels:
            fault = by_label[label]
            out[label] = now >= starts[label] + STEP and MITIGATED[fault.type](twin.state, fault.target)
        return out

    agent.start(scenario.agent_view(), telemetry)
    outcomes: list[ActionOutcome] = []
    while True:
        telemetry.write_batch(inj.advance(poll_min))
        now, final = twin.clock, inj.finished
        turn = agent.step(now, outcomes, final=final)
        if turn.diagnosis is not None:
            record.predicted = sorted({label for h in turn.diagnosis.root_causes for label in h.labels})
            record.confidence = turn.diagnosis.confidence
        record.verifications += [(now, v) for v in turn.verifications]
        record.llm_calls += turn.llm_calls
        record.report = turn.report or record.report
        outcomes = [execute(request) for request in turn.actions]
        record.turns.append(TurnRecord(ts=now, mitigated=mitigated(now)))
        if final:
            break
    record.energy_mwh = energy_mwh(twin.truth)
    return record
