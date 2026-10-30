"""Triage: take in what the environment reports, and group the alarms by time and device.

Alarm text is untrusted data. Triage uses only the structured fields of an event (code,
device_id, ts), checks the code against configs/alarm_codes.yaml and the device against the
plant, and never reads the free-text message. Unknown codes or devices are kept as an
"unrecognised" group so an operator can see them, but they never map to a cause.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pandas as pd

from raqeeb.agent.state import CauseStatus, Memory, PollState
from raqeeb.contracts import PLANT_TARGET, ActionOutcome, ActionStatus

BURST_GAP = timedelta(minutes=30)  # alarms of one code further apart than this start a new burst
PLANT_WIDE_SHARE = 0.75  # a burst on at least this share of inverters is plant-wide
UNRECOGNISED = "UNRECOGNISED"
_DEVICE_RE = re.compile(r"^[A-Z0-9-]{1,16}$")


def known_devices(memory: Memory) -> set[str]:
    plant = memory.plant
    return set(plant.inverters.ids) | {plant.weather_station_id, plant.meter_id, plant.plant_controller_id}


def group_alarms(events: pd.DataFrame, memory: Memory) -> list[dict]:
    """Bursts of one alarm code: devices, first/last time, count, plant-wide or not."""
    inverters = set(memory.plant.inverters.ids)
    devices_ok = known_devices(memory)
    groups: list[dict] = []
    if events.empty:
        return groups
    rows = sorted(zip(events.index, events["code"].astype(str), events["device_id"].astype(str)))
    open_bursts: dict[str, dict] = {}
    for ts, code, device in rows:
        recognised = code in memory.alarm_codes and device in devices_ok and _DEVICE_RE.match(device)
        key = code if recognised else UNRECOGNISED
        burst = open_bursts.get(key)
        if burst is None or ts - burst["last"] > BURST_GAP:
            burst = {"code": key, "devices": set(), "first": ts, "last": ts, "count": 0}
            open_bursts[key] = burst
            groups.append(burst)
        burst["devices"].add(device if recognised else "?")
        burst["last"] = ts
        burst["count"] += 1
    for g in groups:
        spec = memory.alarm_codes.get(g["code"])
        n_inv = len(g["devices"] & inverters)
        g["devices"] = sorted(g["devices"])
        g["first"], g["last"] = g["first"].to_pydatetime(), g["last"].to_pydatetime()
        g["inverters"] = n_inv
        g["plant_wide"] = n_inv >= PLANT_WIDE_SHARE * len(inverters)
        g["related_faults"] = [f.value for f in spec.related_faults] if spec else []
        g["protective"] = spec is not None and not spec.related_faults
    return groups


def take_outcomes(memory: Memory, outcomes: list[ActionOutcome], now: datetime) -> None:
    """Record what the environment did with the requests of the last poll."""
    controls = memory.plant.controls
    delays = {
        "create_cleaning_work_order": controls.cleaning_delay_min,
        "reset_comms_gateway": controls.comms_reset_delay_min,
        "dispatch_technician": controls.technician_delay_min,
    }
    for outcome in outcomes:
        pending = memory.pending.get(outcome.action_id)
        if pending is None:
            memory.log(now, "triage", f"outcome for an unknown action {outcome.action_id!r} ignored")
            continue
        pending.status = outcome.status
        cause = memory.causes.get(pending.cause)
        action = pending.request.action.value
        if outcome.status == ActionStatus.APPLIED:
            pending.applied_at = outcome.ts
            settle = timedelta(minutes=delays.get(action, 0) + 10)  # effect delay + two steps of data
            pending.verify_after = outcome.ts + settle
            pending.give_up_at = pending.verify_after + timedelta(minutes=45)
            memory.log(now, "triage", f"{action} on {pending.request.target} applied")
        else:
            if cause is not None:
                cause.status = CauseStatus.BLOCKED
                cause.note = f"{action} {outcome.status.value.lower()}"
            memory.log(now, "triage", f"{action} on {pending.request.target} {outcome.status.value.lower()}",
                       note=outcome.note[:200])


def triage(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    take_outcomes(memory, state.get("outcomes", []), now)
    events = memory.telemetry.read("events", memory.view.start, now)
    before = sum(g["count"] for g in memory.alarm_groups)
    memory.alarm_groups = group_alarms(events, memory)
    new = sum(g["count"] for g in memory.alarm_groups) - before
    if new:
        summary = "; ".join(
            f"{g['code']} x{g['count']} on {'the whole plant' if g['plant_wide'] else ', '.join(g['devices'])}"
            + (" (protective)" if g["protective"] else "")
            for g in memory.alarm_groups)
        memory.log(now, "triage", f"{new} new alarms, {len(memory.alarm_groups)} groups: {summary}")
    return {}


def group_target(group: dict) -> str | None:
    """The device an alarm group is about: the plant when it is plant-wide or on the plant controller."""
    if group["plant_wide"]:
        return PLANT_TARGET
    if len(group["devices"]) == 1:
        return group["devices"][0]
    return None
