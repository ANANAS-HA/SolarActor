"""Plan: choose the fix for each confirmed cause, check that now is the time for it, and order the
fixes by energy at risk (MWh) and then by safety (automatic before approval).

Fix ladders (HANDOFF sections 8 and 9): a cause starts on its first rung; a failed verification
(reopened) moves it one rung up. Thermal derate is repair-and-protect: the technician ticket goes
out at once, a power limit at the observed cap (needs approval) follows as soon as the cap level
is known, and once the repair is verified the limit is lifted again. Curtailment is expected
behaviour and gets nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from raqeeb.agent.state import Cause, CauseStatus, Memory, PendingAction, PollState
from raqeeb.contracts import ActionRequest, ActionStatus, ActionType, FaultType, InverterStatus, SymptomKind
from raqeeb.tools.metrics import performance_series

A, F = ActionType, FaultType

LADDERS: dict[FaultType, list[list[ActionType]]] = {
    F.SOILING: [[A.CREATE_CLEANING_WORK_ORDER]],
    F.SENSOR_SOILING: [[A.FLAG_SENSOR_USE_REFERENCE]],
    F.INVERTER_DERATE: [[A.DISPATCH_TECHNICIAN]],  # + the protective limit, see follow_ups()
    F.COMMS_DROPOUT: [[A.RESET_COMMS_GATEWAY], [A.DISPATCH_TECHNICIAN]],
    F.TRACKER_STUCK: [[A.RESUME_TRACKING], [A.DISPATCH_TECHNICIAN]],
    F.STRING_OPEN: [[A.DISPATCH_TECHNICIAN]],
    F.UNAUTHORIZED_CMD: [[A.ISOLATE_DEVICE]],
}
CALM_AFTER_STORM = timedelta(minutes=15)  # wind below the stow limit this long before cleaning
RECENT = timedelta(hours=1)  # window for the current shortfall
CAP_WINDOW = timedelta(minutes=30)  # DERATED readings this recent give the cap level
MIN_CAP_READINGS = 2


def _symptom(memory: Memory, kind: SymptomKind, device: str):
    return memory.symptoms.get(f"{kind.value}:{device}")


def observed_cap_kw(memory: Memory, device: str, now: datetime) -> float | None:
    """The level an inverter is held at: median AC of its recent DERATED readings, else the flat-cap
    detector's level."""
    readings = memory.telemetry.inverter_readings(now - CAP_WINDOW, now, inverter_id=device)
    held = readings.loc[readings["status"] == InverterStatus.DERATED.value, "ac_kw"]
    if len(held) >= MIN_CAP_READINGS:
        return round(float(held.median()), 1)
    cap = _symptom(memory, SymptomKind.FLAT_CAP_DEVICE, device)
    return round(float(cap.evidence["level_kw"]), 1) if cap is not None else None


def storm_over(memory: Memory, now: datetime) -> bool:
    wind = memory.results.get("wind_summary")
    last = wind.values.get("last_above_stow") if wind is not None else None
    return last is None or now - last >= CALM_AFTER_STORM


def mwh_at_risk(memory: Memory, cause: Cause, now: datetime) -> float:
    """Rough energy at risk until the end of the incident window if nothing is done."""
    hours_left = max((memory.view.end - now).total_seconds() / 3600.0, 0.0)
    fault, target = cause.hypothesis.fault_type, cause.hypothesis.targets[0]
    short_kw = 0.0
    if fault == F.SOILING:
        perf = performance_series(memory.telemetry, max(memory.view.start, now - RECENT), now, memory.plant)
        usable = perf[perf["usable"]]
        if len(usable):
            short_kw = float((usable["expected_kw"] - usable["meter_kw"]).clip(lower=0).mean())
    elif fault == F.INVERTER_DERATE and (cap := _symptom(memory, SymptomKind.FLAT_CAP_DEVICE, target)):
        short_kw = max(cap.evidence["peer_median_kw"] - cap.evidence["level_kw"], 0.0)
    return round(short_kw * hours_left * 0.5 / 1000.0, 2)  # x0.5: about half the remaining hours produce


def _request(memory: Memory, action: ActionType, target: str, cause: Cause, params: dict | None = None,
             effect: str = "") -> ActionRequest:
    spec = memory.action_specs[action]
    evidence = "; ".join(cause.hypothesis.evidence[:3])
    return ActionRequest(id=memory.next_action_id(), action=action, target=target, params=params or {},
                         reversible=spec.reversible, needs_approval=spec.needs_approval,
                         rationale=f"{cause.label}: {evidence}", expected_effect=effect)


def _active_limit(memory: Memory, cause: Cause) -> PendingAction | None:
    """The power limit currently applied for this cause (None if never set or already lifted)."""
    for action_id in reversed(cause.action_ids):
        p = memory.pending[action_id]
        if p.request.action == A.SET_POWER_LIMIT and p.status in (None, ActionStatus.APPLIED):
            return p if p.request.params.get("kw") is not None else None
    return None


def fixes_for(memory: Memory, cause: Cause, now: datetime) -> tuple[list[ActionRequest], str]:
    """Requests for this cause now, or ([], reason it waits)."""
    fault, target = cause.hypothesis.fault_type, cause.hypothesis.targets[0]
    ladder = LADDERS.get(fault, [])
    if cause.attempts >= len(ladder):
        return [], "no further automatic fix; left to the operators"
    rung = ladder[cause.attempts]
    out: list[ActionRequest] = []
    for action in rung:
        if action == A.CREATE_CLEANING_WORK_ORDER:
            if not storm_over(memory, now):
                return [], "storm still blowing: cleaning now would be wasted"
            out.append(_request(memory, action, target, cause, effect="plant output back to expected after cleaning"))
        elif action == A.RESET_COMMS_GATEWAY:
            gap = _symptom(memory, SymptomKind.DATA_GAP, target)
            if gap is not None and not gap.evidence.get("ongoing"):
                cause.status, cause.note = CauseStatus.RESOLVED, "data returned on its own"
                memory.log(now, "plan", f"{cause.label}: data returned on its own, no reset needed")
                return [], ""
            out.append(_request(memory, action, target, cause, effect=f"{target} data rows return"))
        elif action == A.DISPATCH_TECHNICIAN:
            out.append(_request(memory, action, target, cause, effect=f"{target} repaired on site"))
        else:
            out.append(_request(memory, action, target, cause, effect=f"{cause.label} cleared"))
    return out, ""


def follow_ups(memory: Memory, cause: Cause, now: datetime) -> list[ActionRequest]:
    """Derate only: protect the unit with a limit at its observed cap until the repair is verified,
    then lift the limit."""
    if cause.hypothesis.fault_type != F.INVERTER_DERATE:
        return []
    target = cause.hypothesis.targets[0]
    repaired = any(memory.pending[a].request.action == A.DISPATCH_TECHNICIAN
                   and memory.pending[a].verification is not None and memory.pending[a].verification.recovered
                   for a in cause.action_ids)
    limited_before = any(memory.pending[a].request.action == A.SET_POWER_LIMIT for a in cause.action_ids)
    if not repaired and not limited_before:
        kw = observed_cap_kw(memory, target, now)
        if kw is None:
            return []
        return [_request(memory, A.SET_POWER_LIMIT, target, cause, {"kw": kw},
                         effect=f"{target} held at {kw:.0f} kW so it stops overheating further until the repair")]
    if repaired and _active_limit(memory, cause) is not None:
        return [_request(memory, A.SET_POWER_LIMIT, target, cause, {"kw": None},
                         effect=f"{target} back to full output after the repair")]
    return []


def plan(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    if state.get("final"):
        return {"proposals": []}
    proposals: list[tuple[float, bool, ActionRequest, str]] = []
    for cause in memory.causes.values():
        requests: list[ActionRequest] = []
        if cause.status in (CauseStatus.OPEN, CauseStatus.WAITING, CauseStatus.REOPENED):
            requests, reason = fixes_for(memory, cause, now)
            if not requests and reason and cause.status != CauseStatus.WAITING:
                cause.status = CauseStatus.WAITING if "no further" not in reason else CauseStatus.BLOCKED
                memory.log(now, "plan", f"{cause.label}: {reason}")
        elif cause.status in (CauseStatus.ACTING, CauseStatus.RESOLVED):
            requests = follow_ups(memory, cause, now)
        if requests:
            risk = mwh_at_risk(memory, cause, now)
            for request in requests:
                proposals.append((risk, request.needs_approval, request, cause.label))
    proposals.sort(key=lambda p: (-p[0], p[1]))
    if proposals:
        memory.log(now, "plan", "; ".join(f"{r.action.value} on {r.target} ({risk} MWh at risk)"
                                          for risk, _, r, _ in proposals))
    return {"proposals": [(request, label) for _, _, request, label in proposals]}
