"""Verify: after each applied action has had time to act, compare actual output with expected
from the trusted reference (or the device-level check that fits the action).

A check that has no data yet (night, device silent) waits. A check that fails is retried until
the action's give-up time; then the verification is recorded as not recovered and the cause is
reopened, so plan moves it to the next fix on its ladder (in the same poll, via the graph).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from raqeeb.agent.execute import output_kw
from raqeeb.agent.plan import LADDERS, storm_over
from raqeeb.agent.state import CauseStatus, Memory, PendingAction, PollState
from raqeeb.contracts import PLANT_TARGET, ActionStatus, ActionType, FaultType, Verification
from raqeeb.tools.metrics import performance_series
from raqeeb.tools.peers import HOT_EXCESS_C
from raqeeb.tools.physics import TRACKER_DEVIATION_DEG, expected_inverter, expected_tracker_angle

RECOVERED_RATIO = 0.93  # actual / expected-from-reference at or above this = recovered (reference error up to 8%)
BACK_TO_PEERS = 0.95  # a repaired inverter is within 5% of its peers
LIMIT_TOLERANCE = 1.02  # output at or below limit x this = the limit holds
AFTER_WINDOW = timedelta(minutes=30)
NO_DATA_GRACE = timedelta(minutes=60)  # a device fix still without data this long after its give-up time has failed
NO_DATA_FAILS = {ActionType.DISPATCH_TECHNICIAN, ActionType.RESUME_TRACKING}
MIN_STEPS = 2

Judgement = tuple[bool | None, float | None, float | None, str]  # recovered, after_kw, expected_kw, note


def _window(pending: PendingAction, now: datetime) -> tuple[datetime, datetime]:
    effect = pending.verify_after - timedelta(minutes=10)
    return max(effect, now - AFTER_WINDOW), now


def _plant_vs_reference(memory: Memory, start: datetime, end: datetime) -> Judgement:
    perf = performance_series(memory.telemetry, start, end, memory.plant, exclude=memory.known_losses)
    usable = perf[perf["usable"]]
    if len(usable) < MIN_STEPS:
        return None, None, None, "no daylight steps yet to compare with the reference"
    after, expected = float(usable["meter_kw"].mean()), float(usable["expected_kw"].mean())
    ratio = after / expected
    return ratio >= RECOVERED_RATIO, after, expected, f"plant at {ratio:.0%} of expected from the reference"


def _device_frame(memory: Memory, device: str, start: datetime, end: datetime) -> pd.DataFrame:
    return memory.telemetry.inverter_readings(start, end, inverter_id=device)


def _peer_ratio(memory: Memory, device: str, start: datetime, end: datetime) -> float | None:
    wide = memory.telemetry.inverter_wide("ac_kw", start, end).astype(float)
    if device not in wide or wide[device].dropna().empty:
        return None
    peers = wide.drop(columns=device).median(axis=1)
    both = pd.concat([wide[device], peers], axis=1).dropna()
    both = both[both.iloc[:, 1] > 0.05 * memory.plant.inverters.ac_kw]
    return float(both.iloc[:, 0].sum() / both.iloc[:, 1].sum()) if len(both) >= MIN_STEPS else None


def _temp_excess(memory: Memory, device: str, start: datetime, end: datetime) -> float | None:
    temps = memory.telemetry.inverter_wide("temp_c", start, end).astype(float)
    if device not in temps or temps[device].dropna().empty:
        return None
    excess = (temps[device] - temps.drop(columns=device).median(axis=1)).dropna()
    return float(excess.tail(MIN_STEPS).mean()) if len(excess) else None


def judge(memory: Memory, pending: PendingAction, now: datetime) -> Judgement:
    request = pending.request
    action, target = request.action, request.target
    start, end = _window(pending, now)
    station = memory.plant.weather_station_id

    if action == ActionType.CREATE_CLEANING_WORK_ORDER:
        if not storm_over(memory, now):
            return None, None, None, "storm still blowing: judge the wash once it has passed"
        if target == PLANT_TARGET:
            return _plant_vs_reference(memory, start, end)
        after = output_kw(memory, target, start, end)
        exp = expected_inverter(memory.telemetry, "ac_kw", start, end, memory.plant)
        expected = float(exp[target].mean()) if target in exp and len(exp) else None
        if after is None or not expected:
            return None, after, expected, "no data yet"
        return after / expected >= RECOVERED_RATIO, after, expected, f"{target} at {after / expected:.0%} of expected"

    if action == ActionType.FLAG_SENSOR_USE_REFERENCE:
        check = memory.results.get("sensor_check")
        flagged = bool(check and check.values.get("flagged"))
        recovered, after, expected, note = _plant_vs_reference(memory, start, end)
        return flagged, after, expected, ("expected output now comes from the reference; " + note) if flagged \
            else "the sensor flag is not in effect yet"

    if action == ActionType.RESET_COMMS_GATEWAY:
        rows = (memory.telemetry.weather_station(start, end) if target == station
                else _device_frame(memory, target, start, end))
        back = len(rows) >= MIN_STEPS
        return back, output_kw(memory, target, start, end), None, \
            f"{target} {'reporting again' if back else 'still silent'} ({len(rows)} rows since the reset)"

    if action == ActionType.SET_POWER_LIMIT:
        frame = _device_frame(memory, target, start, end)
        if len(frame) < MIN_STEPS:
            return None, None, None, f"{target} has no readings yet"
        after = float(frame["ac_kw"].mean())
        kw = request.params.get("kw")
        if kw is None:  # the limit was lifted after a repair
            ratio = _peer_ratio(memory, target, start, end)
            if ratio is None:
                return None, after, None, "no daylight peers to compare yet"
            return ratio >= BACK_TO_PEERS, after, None, f"{target} at {ratio:.0%} of its peers without a limit"
        holds = float(frame["ac_kw"].max()) <= kw * LIMIT_TOLERANCE
        return holds, after, kw, f"{target} {'holds' if holds else 'exceeds'} its {kw:.0f} kW limit"

    if action == ActionType.DISPATCH_TECHNICIAN:
        if target == station:
            rows = memory.telemetry.weather_station(start, end)
            return len(rows) >= MIN_STEPS, None, None, f"{len(rows)} station rows after the visit"
        frame = _device_frame(memory, target, start, end)
        if len(frame) < MIN_STEPS:
            return None, None, None, f"{target} has no readings after the visit yet"
        # The check that fits the cause the technician was sent for
        cause = memory.causes.get(pending.cause)
        fault = cause.hypothesis.fault_type if cause is not None else None
        if fault == FaultType.TRACKER_STUCK:
            return _tracker_back(memory, target, start, end)
        if fault in (FaultType.STRING_OPEN, FaultType.UNAUTHORIZED_CMD) or (fault is not None and fault not in LADDERS):
            ratio = _peer_ratio(memory, target, start, end)
            if ratio is None:
                return None, None, None, "no daylight peers to compare yet"
            return ratio >= BACK_TO_PEERS, float(frame["ac_kw"].mean()), None, f"{target} at {ratio:.0%} of its peers"
        excess = _temp_excess(memory, target, start, end)
        cool = excess is not None and excess < HOT_EXCESS_C
        return cool, float(frame["ac_kw"].mean()), None, (
            f"{target} reporting, {excess:.0f} C above peers" if excess is not None else f"{target} reporting")

    if action == ActionType.RESUME_TRACKING:
        return _tracker_back(memory, target, start, end)

    if action == ActionType.ISOLATE_DEVICE:
        after = output_kw(memory, target, start, end)
        return after is None or after < 1.0, after, 0.0, f"{target} isolated"

    return None, None, None, f"no check for {action.value}"


def _tracker_back(memory: Memory, target: str, start: datetime, end: datetime) -> Judgement:
    measured = _device_frame(memory, target, start, end)["tracker_angle_deg"].dropna()
    if len(measured) < MIN_STEPS:
        return None, None, None, "no tracker angles yet"
    expected = expected_tracker_angle(memory.telemetry, start, end, memory.plant).reindex(measured.index)
    deviation = float((measured - expected).abs().median())
    return deviation < TRACKER_DEVIATION_DEG, None, None, f"{target} {deviation:.0f} deg from its tracking angle"


def verify(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    verifications = list(state.get("verifications", []))
    reopened = list(state.get("reopened", []))
    for action_id, pending in memory.pending.items():
        if pending.status != ActionStatus.APPLIED or pending.verification is not None:
            continue
        if pending.verify_after is None or now < pending.verify_after:
            continue
        recovered, after, expected, note = judge(memory, pending, now)
        if (recovered is None and pending.request.action in NO_DATA_FAILS
                and now >= pending.give_up_at + NO_DATA_GRACE):
            recovered, note = False, f"no data to confirm the fix ({note})"
        if recovered is None or (recovered is False and now < pending.give_up_at):
            continue  # no data yet, or give the fix more time
        verification = Verification(action_id=action_id, recovered=recovered, before_kw=pending.before_kw or 0.0,
                                    after_kw=after or 0.0, expected_kw=expected or 0.0, note=note)
        pending.verification = verification
        verifications.append(verification)
        cause = memory.causes.get(pending.cause)
        request = pending.request
        memory.log(now, "verify", f"{request.action.value} on {request.target}: "
                   f"{'recovered' if recovered else 'NOT recovered'} ({note})")
        if cause is None:
            continue
        if not recovered:
            cause.attempts += 1
            cause.status, cause.note = CauseStatus.REOPENED, f"{request.action.value} did not recover: {note}"
            reopened.append(cause.label)
        elif all(memory.pending[a].verification is not None and memory.pending[a].verification.recovered
                 for a in cause.action_ids if memory.pending[a].status == ActionStatus.APPLIED):
            cause.status = CauseStatus.RESOLVED
    return {"verifications": verifications, "reopened": reopened}
