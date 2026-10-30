"""Safety gate: every planned request is checked against configs/actions.yaml before it leaves.

- Only allowlisted ActionTypes pass; changing the grid setpoint is not one of them.
- `reversible` and `needs_approval` are copied from the allowlist, never trusted from the planner.
  Gated actions still go out: the environment asks a human (Approve / Reject).
- Targets must be real devices of the right kind; a power limit must be within the rating.
- No action on an expected-behaviour cause (curtailment), no duplicate of a request in flight.
- Irreversible actions also need a confident diagnosis that has held for two polls.
"""

from __future__ import annotations

from datetime import timedelta

from raqeeb.agent.state import PASSIVE, Memory, PollState
from raqeeb.contracts import PLANT_TARGET, ActionRequest, ActionStatus, ActionType

MAX_ACTIONS_PER_POLL = 6
IRREVERSIBLE_MIN_CONFIDENCE = 0.8
IRREVERSIBLE_MIN_AGE = timedelta(minutes=15)

INVERTER_ACTIONS = {ActionType.SET_POWER_LIMIT, ActionType.ISOLATE_DEVICE, ActionType.RESUME_TRACKING}
STATION_ACTIONS = {ActionType.FLAG_SENSOR_USE_REFERENCE}


def check(memory: Memory, request: ActionRequest, label: str, now) -> tuple[ActionRequest | None, str]:
    """The request as it may leave (flags from the allowlist), or None and the reason."""
    try:
        action = ActionType(request.action)
    except ValueError:
        return None, f"{request.action!r} is not in the allowlist"
    spec = memory.action_specs.get(action)
    if spec is None:
        return None, f"{action.value} is not in configs/actions.yaml"
    plant, target = memory.plant, request.target
    inverters, station = set(plant.inverters.ids), plant.weather_station_id
    if action == ActionType.CREATE_CLEANING_WORK_ORDER:
        valid = target == PLANT_TARGET or target in inverters
    elif action in INVERTER_ACTIONS:
        valid = target in inverters
    elif action in STATION_ACTIONS:
        valid = target == station
    else:
        valid = target in inverters or target == station
    if not valid:
        return None, f"{action.value} cannot target {target!r}"
    if action == ActionType.SET_POWER_LIMIT:
        kw = request.params.get("kw")
        if kw is not None and not (0 < float(kw) <= plant.inverters.ac_kw):
            return None, f"power limit {kw} kW outside (0, {plant.inverters.ac_kw:.0f}]"
    cause = memory.causes.get(label)
    if cause is None or cause.status in PASSIVE:
        return None, f"no confirmed root cause behind it ({label})"
    for pending in memory.pending.values():
        same = (pending.request.action == action and pending.request.target == target
                and pending.request.params == request.params)
        in_flight = pending.status is None or (pending.status == ActionStatus.APPLIED and pending.verification is None)
        if same and in_flight:
            return None, "the same request is already in flight"
    if not spec.reversible:
        diagnosis = memory.diagnosis
        if diagnosis is None or diagnosis.confidence < IRREVERSIBLE_MIN_CONFIDENCE:
            return None, "irreversible: diagnosis confidence too low"
        if now - cause.first_seen < IRREVERSIBLE_MIN_AGE:
            return None, "irreversible: diagnosis too recent"
    return request.model_copy(update={"action": action, "reversible": spec.reversible,
                                      "needs_approval": spec.needs_approval}), ""


def safety(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    approved, rejected = [], []
    for request, label in state.get("proposals", []):
        checked, reason = check(memory, request, label, now)
        if checked is None:
            rejected.append(f"{request.action} on {request.target}: {reason}")
        elif len(approved) >= MAX_ACTIONS_PER_POLL:
            rejected.append(f"{request.action.value} on {request.target}: deferred, {MAX_ACTIONS_PER_POLL} per poll")
        else:
            approved.append((checked, label))
    if rejected:
        memory.log(now, "safety", "held back: " + "; ".join(rejected))
    gated = [r.action.value + " on " + r.target for r, _ in approved if r.needs_approval]
    if gated:
        memory.log(now, "safety", "needs human approval: " + ", ".join(gated))
    return {"approved": approved}
