"""Execute: hand the approved requests to the environment and remember them.

The agent never touches the plant itself. The environment (bench harness or console) runs each
request with twin.controls.apply_action after its own allowlist check and, for gated actions,
a human's approval; the outcome comes back on the next poll (triage.take_outcomes).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from raqeeb.agent.state import CauseStatus, Memory, PendingAction, PollState
from raqeeb.contracts import PLANT_TARGET, ActionRequest

BEFORE_WINDOW = timedelta(minutes=30)


def output_kw(memory: Memory, target: str, start: datetime, end: datetime) -> float | None:
    """Mean measured AC over [start, end): the meter for the plant, else the inverter's readings."""
    telemetry = memory.telemetry
    if target == PLANT_TARGET:
        series = telemetry.meter(start, end)["plant_ac_kw"]
    elif target in memory.plant.inverters.ids:
        series = telemetry.inverter_readings(start, end, inverter_id=target)["ac_kw"]
    else:
        return None
    return float(series.mean()) if len(series) else None


def execute(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    sent: list[ActionRequest] = list(state.get("actions", []))
    for request, label in state.get("approved", []):
        cause = memory.causes[label]
        memory.pending[request.id] = PendingAction(
            request=request, cause=label, requested_at=now,
            before_kw=output_kw(memory, request.target, now - BEFORE_WINDOW, now))
        cause.action_ids.append(request.id)
        if cause.status != CauseStatus.RESOLVED:
            cause.status = CauseStatus.ACTING
        sent.append(request)
        memory.log(now, "execute", f"requested {request.action.value} on {request.target}"
                   + (" (awaiting approval)" if request.needs_approval else ""), rationale=request.rationale)
    return {"actions": sent, "approved": []}
