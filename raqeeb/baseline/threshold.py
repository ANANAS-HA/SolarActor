"""Threshold-alarm baseline (HANDOFF section 11): one ticket per alarm, no correlation.

Each new (alarm code, device) pair opens one ticket. The code maps straight to a fault type: the
first of its `related_faults` in configs/alarm_codes.yaml (its primary cause). The ticket names
FAULT_TYPE@<alarm device> as a root cause and requests that fault type's standard action on the
same device. Alarms with no related fault (HIGH_WIND, TRACKER_STOW_WIND) open no ticket, and the
decoy type is skipped because its alarm text says it needs no action. A repeat of the same
code on the same device does not open a second ticket. The baseline never verifies recovery.

Expected behaviour: plant-wide soiling becomes one ticket per inverter, symptoms become causes,
and a dusty sensor that hides the symptom alarms hides the soiling too.
"""

from __future__ import annotations

from datetime import datetime

from raqeeb.config import load_actions, load_alarm_codes, load_plant
from raqeeb.contracts import (
    DECOY_FAULTS,
    ActionOutcome,
    ActionRequest,
    ActionType,
    AgentScenario,
    AgentTurn,
    Diagnosis,
    FaultType,
    Hypothesis,
    TelemetryReader,
)

FAULT_ACTION: dict[FaultType, ActionType] = {
    FaultType.SOILING: ActionType.CREATE_CLEANING_WORK_ORDER,
    FaultType.SENSOR_SOILING: ActionType.FLAG_SENSOR_USE_REFERENCE,
    FaultType.INVERTER_DERATE: ActionType.SET_POWER_LIMIT,
    FaultType.COMMS_DROPOUT: ActionType.RESET_COMMS_GATEWAY,
    FaultType.TRACKER_STUCK: ActionType.RESUME_TRACKING,
    FaultType.STRING_OPEN: ActionType.DISPATCH_TECHNICIAN,
    FaultType.UNAUTHORIZED_CMD: ActionType.ISOLATE_DEVICE,
}
POWER_LIMIT_FRACTION = 0.5  # fixed limit for a hot inverter, as a share of its rating


class ThresholdBaseline:
    name = "threshold"

    def start(self, view: AgentScenario, telemetry: TelemetryReader) -> None:
        self.telemetry = telemetry
        self.since = view.start
        self.codes = load_alarm_codes()
        self.actions = load_actions()
        self.limit_kw = load_plant().inverters.ac_kw * POWER_LIMIT_FRACTION
        self.tickets: dict[tuple[str, str], Hypothesis] = {}  # (code, device) -> ticket
        self.acted: set[tuple[ActionType, str]] = set()

    def _action(self, fault_type: FaultType, device: str, ticket: Hypothesis) -> ActionRequest | None:
        action = FAULT_ACTION.get(fault_type)
        if action is None or (action, device) in self.acted:
            return None
        self.acted.add((action, device))
        spec = self.actions[action]
        params = {"kw": self.limit_kw} if action == ActionType.SET_POWER_LIMIT else {}
        return ActionRequest(id=f"threshold-{len(self.acted)}", action=action, target=device, params=params,
                             reversible=spec.reversible, needs_approval=spec.needs_approval,
                             rationale=f"ticket {ticket.id}: {ticket.evidence[0]}",
                             expected_effect=f"clears {fault_type.value} on {device}")

    def step(self, now: datetime, outcomes: list[ActionOutcome], final: bool = False) -> AgentTurn:
        events = self.telemetry.read("events", self.since, now)
        self.since = now
        actions = []
        opened = False
        for code, device, message in zip(events["code"], events["device_id"], events["message"]):
            spec = self.codes.get(code)
            if spec is None or not spec.related_faults or (code, device) in self.tickets:
                continue
            fault_type = spec.related_faults[0]
            if fault_type in DECOY_FAULTS:
                continue
            ticket = Hypothesis(id=f"T{len(self.tickets) + 1}", fault_type=fault_type, targets=[device],
                                evidence=[f"{code} on {device}: {message}"], score=1.0)
            self.tickets[(code, device)] = ticket
            opened = True
            if not final and (request := self._action(fault_type, device, ticket)):
                actions.append(request)
        diagnosis = Diagnosis(root_causes=list(self.tickets.values()), confidence=1.0) if opened else None
        return AgentTurn(diagnosis=diagnosis, actions=actions)
