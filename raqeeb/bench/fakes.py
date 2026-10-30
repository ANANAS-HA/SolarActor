"""Fake agents that bound the scores and test the scorer. They are NOT real agents.

- OracleAgent CHEATS: it is built with the full Scenario and returns the answer key, requests
  the right fix for each root cause once it is active, and verifies after the fix's delay.
  It should score perfect precision, recall and false-action rate.
- EmptyAgent does nothing: no diagnosis, no actions, no verification.

Both follow the contracts.Agent protocol, so the harness drives them like any agent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from raqeeb.config import load_actions, load_plant
from raqeeb.contracts import (
    STEP_MINUTES,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    ActionType,
    AgentScenario,
    AgentTurn,
    Diagnosis,
    Escalation,
    FaultSpec,
    FaultType,
    Hypothesis,
    LlmCall,
    PlantConfig,
    Scenario,
    TelemetryReader,
    Verification,
)

STEP = timedelta(minutes=STEP_MINUTES)

FIX: dict[FaultType, ActionType] = {
    FaultType.SOILING: ActionType.CREATE_CLEANING_WORK_ORDER,
    FaultType.SENSOR_SOILING: ActionType.FLAG_SENSOR_USE_REFERENCE,
    FaultType.INVERTER_DERATE: ActionType.SET_POWER_LIMIT,
    FaultType.COMMS_DROPOUT: ActionType.RESET_COMMS_GATEWAY,
    FaultType.TRACKER_STUCK: ActionType.RESUME_TRACKING,
    FaultType.STRING_OPEN: ActionType.DISPATCH_TECHNICIAN,
    FaultType.UNAUTHORIZED_CMD: ActionType.ISOLATE_DEVICE,
    FaultType.FIRMWARE_ALARM: ActionType.DISPATCH_TECHNICIAN,  # TRANSFORMER_TRIP, NEW_SHADING: escalate only
    FaultType.HUNG_INVERTER: ActionType.RESET_COMMS_GATEWAY,  # controlled restart
    FaultType.COMMS_DROPOUT_LOAD: ActionType.DISPATCH_TECHNICIAN,
    FaultType.SENSOR_DRIFT: ActionType.DISPATCH_TECHNICIAN,
    FaultType.TRACKER_STUCK_SILENT: ActionType.RESUME_TRACKING,
}


class EmptyAgent:
    name = "empty"

    def start(self, view: AgentScenario, telemetry: TelemetryReader) -> None:
        pass

    def step(self, now: datetime, outcomes: list[ActionOutcome], final: bool = False) -> AgentTurn:
        return AgentTurn(done=True)


class OracleAgent:
    """Reads the answer key. `llm_calls` are reported on the first turn (to test cost metrics)."""

    name = "oracle"

    def __init__(self, scenario: Scenario, plant: PlantConfig | None = None, llm_calls: list[LlmCall] | None = None):
        self.scenario = scenario
        self.plant = plant or load_plant()
        self.llm_calls = list(llm_calls or [])
        key = scenario.answer_key or scenario.expected_answer_key()
        self.causes: list[FaultSpec] = [f for f in scenario.faults if f.label in key.root_causes]
        self.fixable = [f for f in self.causes if f.type in FIX]

    def start(self, view: AgentScenario, telemetry: TelemetryReader) -> None:
        self.requested: dict[str, FaultSpec] = {}  # action id -> cause
        self.applied: dict[str, datetime] = {}  # action id -> when it was applied
        self.verified: set[str] = set()
        self.escalated: set[str] = set()
        self.first = True

    def _ready(self, fault: FaultSpec, now: datetime) -> bool:
        """Act once the fault has shown up. A dust storm keeps soiling until it ends, so the
        cleaning order is timed to land when the storm is over."""
        ready = self.scenario.resolve_time(fault.start) + STEP
        if fault.type == FaultType.SOILING and fault.end:
            cleaning = timedelta(minutes=self.plant.controls.cleaning_delay_min)
            ready = max(ready, self.scenario.resolve_time(fault.end) - cleaning)
        return now >= ready

    def _delay(self, action: ActionType) -> timedelta:
        controls = self.plant.controls
        minutes = {ActionType.CREATE_CLEANING_WORK_ORDER: controls.cleaning_delay_min,
                   ActionType.RESET_COMMS_GATEWAY: controls.comms_reset_delay_min,
                   ActionType.DISPATCH_TECHNICIAN: controls.technician_delay_min}.get(action, 0)
        return timedelta(minutes=minutes) + STEP  # the change shows after the step at that time

    def _request(self, fault: FaultSpec) -> ActionRequest:
        action = FIX[fault.type]
        params = {}
        if action == ActionType.SET_POWER_LIMIT:
            params["kw"] = self.plant.inverters.ac_kw * (1.0 - (fault.severity or 0.0))
        spec = load_actions()[action]
        return ActionRequest(id=f"oracle-{len(self.requested) + 1}", action=action, target=fault.target, params=params,
                             reversible=spec.reversible, needs_approval=spec.needs_approval,
                             rationale=f"answer key: {fault.label}", expected_effect="fault mitigated")

    def step(self, now: datetime, outcomes: list[ActionOutcome], final: bool = False) -> AgentTurn:
        for outcome in outcomes:
            if outcome.status == ActionStatus.APPLIED:
                self.applied[outcome.action_id] = outcome.ts
        verifications = []
        for action_id, applied_at in self.applied.items():
            fault = self.requested[action_id]
            if action_id not in self.verified and now >= applied_at + self._delay(FIX[fault.type]):
                self.verified.add(action_id)
                verifications.append(Verification(action_id=action_id, recovered=True, before_kw=0.0,
                                                  after_kw=0.0, expected_kw=0.0, note="oracle"))
        actions, escalations = [], []
        for fault in self.causes:
            if fault.type not in FIX and fault.label not in self.escalated and self._ready(fault, now):
                self.escalated.add(fault.label)
                escalations.append(Escalation(ts=now, subject=fault.label, reason="answer key: no allowed fix",
                                              devices=self.plant.layout.area_of(fault.target) or [fault.target]))
        if not final:
            acted = {f.label for f in self.requested.values()}
            for fault in self.fixable:
                if fault.label not in acted and self._ready(fault, now):
                    request = self._request(fault)
                    self.requested[request.id] = fault
                    actions.append(request)
        hypotheses = [Hypothesis(id=f"h{k}", fault_type=f.type, targets=[f.target], score=1.0)
                      for k, f in enumerate(self.causes, start=1)]
        calls, self.llm_calls = (self.llm_calls, []) if self.first else ([], self.llm_calls)
        self.first = False
        return AgentTurn(diagnosis=Diagnosis(root_causes=hypotheses, confidence=1.0), actions=actions,
                         verifications=verifications, llm_calls=calls, escalations=escalations,
                         done=len(self.verified) == len(self.fixable))
