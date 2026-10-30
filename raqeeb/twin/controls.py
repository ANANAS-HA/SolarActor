"""Control actions the Executor calls. Each one logs a Command and changes only twin state.

Delays come from configs/plant.yaml `controls`. Every function takes (twin, target, ...)
and returns the logged Command. `apply_action` dispatches an allowlisted ActionType.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from raqeeb.contracts import PLANT_TARGET, ActionType, Command, PlantState
from raqeeb.twin.simulator import Twin

SOURCE = "raqeeb"


def _inverters(twin: Twin, target: str) -> list[str]:
    if target == PLANT_TARGET:
        return list(twin.state.inverters)
    if target not in twin.state.inverters:
        raise ValueError(f"unknown inverter {target!r}")
    return [target]


def _check_device(twin: Twin, device_id: str) -> None:
    if device_id != twin.plant.weather_station_id and device_id not in twin.state.inverters:
        raise ValueError(f"unknown device {device_id!r}")


def reset_comms(twin: Twin, device_id: str, source: str = SOURCE) -> Command:
    """Restart a device's comms gateway; data returns after comms_reset_delay_min.

    The restart also reboots a hung inverter controller (a controlled restart), so it produces again.
    It does not fix a link that fails under load (comms_load_limit_wm2 stays: a technician's job).
    """
    _check_device(twin, device_id)

    def restore(state: PlantState) -> None:
        if device_id == twin.plant.weather_station_id:
            state.station.comms_up = True
        else:
            state.inverters[device_id].comms_up = True
            state.inverters[device_id].hung = False

    twin.schedule_in(twin.plant.controls.comms_reset_delay_min, f"comms restored {device_id}", restore)
    return twin.log_command(source, device_id, ActionType.RESET_COMMS_GATEWAY.value)


def clean_panels(twin: Twin, target: str, source: str = SOURCE) -> Command:
    """Cleaning work order: soiling on the target is removed after cleaning_delay_min."""
    ids = _inverters(twin, target)

    def clean(state: PlantState) -> None:
        for inv_id in ids:
            state.inverters[inv_id].soiling_loss = 0.0

    delay = twin.plant.controls.cleaning_delay_min
    twin.schedule_in(delay, f"panels cleaned {target}", clean)
    return twin.log_command(source, target, ActionType.CREATE_CLEANING_WORK_ORDER.value, {"delay_min": delay})


def set_power_limit(twin: Twin, inverter_id: str, kw: float | None, source: str = SOURCE) -> Command:
    """Limit one inverter's AC output (None removes the limit). Immediate."""
    (inv_id,) = _inverters(twin, inverter_id)
    twin.state.inverters[inv_id].power_limit_kw = kw
    return twin.log_command(source, inv_id, ActionType.SET_POWER_LIMIT.value, {"kw": kw})


def flag_sensor(twin: Twin, station_id: str, flagged: bool = True, source: str = SOURCE) -> Command:
    """Mark the irradiance sensor suspect so tools use the reference. Readings do not change."""
    if station_id != twin.plant.weather_station_id:
        raise ValueError(f"unknown weather station {station_id!r}")
    twin.state.station.flagged = flagged
    return twin.log_command(source, station_id, ActionType.FLAG_SENSOR_USE_REFERENCE.value, {"flagged": flagged})


def resume_tracking(twin: Twin, target: str, source: str = SOURCE) -> Command:
    """Clear a stuck tracker override. Immediate."""
    for inv_id in _inverters(twin, target):
        twin.state.inverters[inv_id].tracker_override_deg = None
    return twin.log_command(source, target, ActionType.RESUME_TRACKING.value)


def dispatch_technician(twin: Twin, device_id: str, source: str = SOURCE) -> Command:
    """Technician repairs device hardware (strings, cooling, comms, controller parameters, a hung
    controller, sensor calibration) after technician_delay_min.

    Does not clean panels, clear an operator limit, undo isolation, re-energise a tripped
    transformer (a high-voltage crew's job) or remove shading.
    """
    _check_device(twin, device_id)

    def repair(state: PlantState) -> None:
        if device_id == twin.plant.weather_station_id:
            state.station.comms_up = True
            state.station.sensor_soiling = 0.0
            state.station.sensor_bias = 0.0  # recalibrated
            state.station.sensor_drift_per_h = 0.0
            return
        inv = state.inverters[device_id]
        inv.string_loss = 0.0
        inv.derate_cap_kw = None
        inv.temp_offset_c = 0.0
        inv.comms_up = True
        inv.tracker_override_deg = None
        inv.firmware_cap_kw = None  # parameters reloaded / firmware rolled back on site
        inv.hung = False  # power-cycled
        inv.comms_load_limit_wm2 = None  # comms module replaced

    delay = twin.plant.controls.technician_delay_min
    twin.schedule_in(delay, f"technician repaired {device_id}", repair)
    return twin.log_command(source, device_id, ActionType.DISPATCH_TECHNICIAN.value, {"delay_min": delay})


def isolate_device(twin: Twin, inverter_id: str, source: str = SOURCE) -> Command:
    """Disconnect an inverter. Not reversible in the twin."""
    (inv_id,) = _inverters(twin, inverter_id)
    twin.state.inverters[inv_id].isolated = True
    return twin.log_command(source, inv_id, ActionType.ISOLATE_DEVICE.value)


CONTROLS: dict[ActionType, Callable[..., Command]] = {
    ActionType.RESET_COMMS_GATEWAY: reset_comms,
    ActionType.FLAG_SENSOR_USE_REFERENCE: flag_sensor,
    ActionType.CREATE_CLEANING_WORK_ORDER: clean_panels,
    ActionType.RESUME_TRACKING: resume_tracking,
    ActionType.DISPATCH_TECHNICIAN: dispatch_technician,
    ActionType.SET_POWER_LIMIT: set_power_limit,
    ActionType.ISOLATE_DEVICE: isolate_device,
}


def apply_action(twin: Twin, action: ActionType | str, target: str, params: dict[str, Any] | None = None,
                 source: str = SOURCE) -> Command:
    """Run an allowlisted action. Anything outside ActionType raises ValueError.

    Approval is the safety gate's job (agent/safety.py), not this function's.
    """
    try:
        action = ActionType(action)
    except ValueError:
        raise ValueError(f"action {action!r} is not in the allowlist") from None
    return CONTROLS[action](twin, target, **(params or {}), source=source)
