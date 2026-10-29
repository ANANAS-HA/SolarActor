"""The plant twin: a simulated clock that turns weather x PlantState into telemetry.

    twin = Twin(start, end, seed=7)
    twin.schedule(at, "dust storm", lambda s: ...)   # faults (Lane B) and delayed controls
    batch = twin.advance(60)                         # TelemetryBatch for 12 steps

Each step: apply scheduled changes due at the clock, compute readings at the clock, then
move the clock forward by STEP_MINUTES. Changes made between steps show up in the next one.
A device with comms down produces power (the meter sees it) but writes no rows.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from raqeeb.config import load_alarm_codes, load_plant
from raqeeb.contracts import (
    STEP_MINUTES,
    AlarmCode,
    Command,
    Event,
    GridSetpoint,
    InverterReading,
    InverterStatus,
    MeterReading,
    PlantConfig,
    PlantState,
    TelemetryBatch,
    WeatherReference,
    WeatherStationReading,
)
from raqeeb.twin import plant as physics
from raqeeb.twin.weather import weather_5min

STEP = timedelta(minutes=STEP_MINUTES)
StateChange = Callable[[PlantState], None]


@dataclass(order=True)
class _Scheduled:
    at: datetime
    seq: int
    label: str = field(compare=False)
    change: StateChange = field(compare=False)


@dataclass
class StepTruth:
    """Exact values behind one step (no noise, no missing rows). For the verifier and bench."""

    ts: datetime
    plant_ac_kw: float
    inverter_ac_kw: dict[str, float]
    stowed: bool


class Twin:
    def __init__(
        self,
        start: datetime,
        end: datetime,
        *,
        plant: PlantConfig | None = None,
        seed: int = 0,
        state: PlantState | None = None,
        weather: pd.DataFrame | None = None,
    ):
        self.plant = plant or load_plant()
        if start.minute % STEP_MINUTES or start.second or start.microsecond:
            raise ValueError(f"start must be on the {STEP_MINUTES}-minute grid, got {start}")
        self.start, self.end = start, end
        self.clock = start
        self.state = state or PlantState.healthy(self.plant.inverters.ids)
        self.alarm_codes: dict[str, AlarmCode] = load_alarm_codes()
        self._rng = np.random.default_rng(seed)
        self._ids = self.plant.inverters.ids
        self._dc_rated = self.plant.inverters.dc_kw * physics.capacity_factors(self.plant)
        weather = weather if weather is not None else weather_5min(start, end, self.plant)
        self._frame = physics.prepare(weather, self.plant)
        self._fixed_poa: dict[float, pd.Series] = {}
        self._queue: list[_Scheduled] = []
        self._seq = itertools.count()
        self._outbox = TelemetryBatch()
        self._active_alarms: set[tuple[str, str]] = set()
        self.truth: list[StepTruth] = []

    # -- clock and scheduling -------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.clock >= self.end

    def schedule(self, at: datetime, label: str, change: StateChange) -> None:
        """Apply `change` to the state at the first step whose time is >= `at`."""
        heapq.heappush(self._queue, _Scheduled(at, next(self._seq), label, change))

    def schedule_in(self, minutes: float, label: str, change: StateChange) -> None:
        self.schedule(self.clock + timedelta(minutes=minutes), label, change)

    @property
    def pending(self) -> list[tuple[datetime, str]]:
        return [(item.at, item.label) for item in sorted(self._queue)]

    def step(self) -> TelemetryBatch:
        if self.finished:
            raise RuntimeError(f"twin finished at {self.end}")
        while self._queue and self._queue[0].at <= self.clock:
            heapq.heappop(self._queue).change(self.state)
        self._compute(self.clock)
        self.clock += STEP
        batch, self._outbox = self._outbox, TelemetryBatch()
        return batch

    def advance(self, minutes: float) -> TelemetryBatch:
        """Run whole steps covering `minutes` (stops early at the end of the window)."""
        batch = TelemetryBatch()
        for _ in range(int(minutes // STEP_MINUTES)):
            if self.finished:
                break
            batch.extend(self.step())
        return batch

    def run_to_end(self) -> TelemetryBatch:
        batch = TelemetryBatch()
        while not self.finished:
            batch.extend(self.step())
        return batch

    # -- outputs used by controls and fault injection -------------------------

    def log_command(self, source: str, device_id: str, action: str, params: dict[str, Any] | None = None) -> Command:
        command = Command(ts=self.clock, source=source, device_id=device_id, action=action, params=params or {})
        self._outbox.commands.append(command)
        return command

    def emit_event(self, device_id: str, code: str, message: str) -> Event:
        spec = self.alarm_codes[code]
        event = Event(ts=self.clock, device_id=device_id, code=code, severity=spec.severity,
                      message=f"[{spec.vendor_id}] {spec.title}: {message}")
        self._outbox.events.append(event)
        return event

    # -- physics for one step ---------------------------------------------------

    def _poa_fixed(self, angle: float) -> pd.Series:
        if angle not in self._fixed_poa:
            self._fixed_poa[angle] = physics.poa_at_angle(self._frame, angle, self.plant)
        return self._fixed_poa[angle]

    def _noise(self, size: int | None, sigma: float):
        return self._rng.normal(0.0, sigma, size)

    def _compute(self, ts: datetime) -> None:
        plant, state, ids = self.plant, self.state, self._ids
        w = self._frame.loc[pd.Timestamp(ts)]
        atm = state.atmosphere
        factor = atm.irradiance_factor
        wind = float(w["wind_ms"]) + atm.wind_add_ms
        ambient = float(w["ambient_c"])
        stowed = wind > plant.tracker.stow_wind_ms
        invs = [state.inverters[i] for i in ids]

        poa = np.empty(len(ids))
        for k, inv in enumerate(invs):
            if inv.tracker_override_deg is not None:
                poa[k] = self._poa_fixed(inv.tracker_override_deg).loc[w.name]
            elif stowed:
                poa[k] = w["poa_stow_wm2"]
            else:
                poa[k] = w["poa_track_wm2"]
        poa *= factor

        soiling = np.array([inv.soiling_loss for inv in invs])
        effective = poa * (1.0 - soiling)
        tcell = physics.cell_temperature(effective, ambient, wind, plant)
        string_loss = np.array([inv.string_loss for inv in invs])
        dc_avail = physics.dc_power_kw(effective, tcell, self._dc_rated, plant) * (1.0 - string_loss)
        ac_potential = physics.ac_power_kw(dc_avail, plant)

        running = np.array([not (inv.stopped or inv.isolated) for inv in invs])
        grid_share = (
            state.grid_limit_kw / max(int(running.sum()), 1) if state.grid_limit_kw is not None else np.inf
        )
        derate_cap = np.array([inv.derate_cap_kw if inv.derate_cap_kw is not None else np.inf for inv in invs])
        power_limit = np.array([inv.power_limit_kw if inv.power_limit_kw is not None else np.inf for inv in invs])
        ac = np.minimum.reduce([ac_potential, derate_cap, power_limit, np.full(len(ids), grid_share)])
        ac = np.where(running, ac, 0.0)

        # When output is held below what the panels could give, the inverter leaves MPP
        held = ac < ac_potential - 1e-6
        eta = plant.inverters.nominal_efficiency
        dc = np.where(held, ac / eta, np.minimum(dc_avail, plant.inverters.ac_kw / eta))
        dc = np.where(running, dc, 0.0)
        offsets = np.array([inv.temp_offset_c for inv in invs])
        temp = physics.inverter_temperature(ambient, ac, offsets, plant)

        # Status and edge-triggered device alarms
        alarms: set[tuple[str, str]] = set()
        statuses = []
        for k, (inv_id, inv) in enumerate(zip(ids, invs)):
            derating = inv.derate_cap_kw is not None and ac_potential[k] > inv.derate_cap_kw and running[k]
            if not running[k]:
                status = InverterStatus.STOPPED
            elif poa[k] < physics.STANDBY_POA_WM2:
                status = InverterStatus.STANDBY
            elif held[k] and ac[k] >= grid_share - 1e-6:
                status = InverterStatus.CURTAILED
            elif held[k]:
                status = InverterStatus.DERATED
            else:
                status = InverterStatus.RUNNING
            statuses.append(status)
            if not inv.comms_up:
                alarms.add(("COMM_LOSS", inv_id))
            if derating:
                alarms.add(("DERATE_TEMP", inv_id))
            if inv.stopped:
                alarms.add(("REMOTE_STOP", inv_id))
            if stowed and inv.tracker_override_deg is None:
                alarms.add(("TRACKER_STOW_WIND", inv_id))
        if wind > plant.high_wind_alarm_ms:
            alarms.add(("HIGH_WIND", plant.weather_station_id))
        if not state.station.comms_up:
            alarms.add(("COMM_LOSS", plant.weather_station_id))
        if state.grid_limit_kw is not None:
            alarms.add(("GRID_LIMIT_ACTIVE", plant.plant_controller_id))
        self._raise_alarms(alarms, wind=wind, temp=dict(zip(ids, temp)), ac=dict(zip(ids, ac)))

        # Readings
        out = self._outbox
        noise = plant.noise
        n_ac = 1.0 + self._noise(len(ids), noise.inverter_power)
        n_dc = 1.0 + self._noise(len(ids), noise.inverter_power)
        n_t = self._noise(len(ids), noise.ambient_c_abs)
        for k, (inv_id, inv) in enumerate(zip(ids, invs)):
            if inv.comms_up:
                out.inverter_readings.append(InverterReading(
                    ts=ts, inverter_id=inv_id, ac_kw=float(ac[k] * n_ac[k]), dc_kw=float(dc[k] * n_dc[k]),
                    temp_c=float(temp[k] + n_t[k]), status=statuses[k],
                ))
        ghi = float(w["ghi_wm2"]) * factor
        if state.station.comms_up:
            sensor = ghi * (1.0 - state.station.sensor_soiling) * (1.0 + self._noise(None, noise.ghi_sensor))
            out.weather_station.append(WeatherStationReading(
                ts=ts, ghi_sensor_wm2=max(float(sensor), 0.0),
                ambient_c=ambient + float(self._noise(None, noise.ambient_c_abs)), wind_ms=wind,
            ))
        out.weather_reference.append(WeatherReference(
            ts=ts, ghi_wm2=ghi, dni_wm2=float(w["dni_wm2"]) * factor, dhi_wm2=float(w["dhi_wm2"]) * factor,
            ambient_c=ambient, wind_ms=wind, clearsky_ghi_wm2=float(w["clearsky_ghi_wm2"]),
        ))
        plant_ac = float(ac.sum())
        out.meter.append(MeterReading(ts=ts, plant_ac_kw=plant_ac))
        if state.grid_limit_kw is not None:
            out.grid_setpoint.append(GridSetpoint(ts=ts, limit_kw=state.grid_limit_kw))
        self.truth.append(StepTruth(ts=ts, plant_ac_kw=plant_ac, inverter_ac_kw=dict(zip(ids, ac.tolist())),
                                    stowed=stowed))

    def _raise_alarms(self, current: set[tuple[str, str]], *, wind: float, temp: dict, ac: dict) -> None:
        details = {
            "COMM_LOSS": lambda d: "no telemetry received from device",
            "DERATE_TEMP": lambda d: f"internal temperature {temp[d]:.0f} C, output held at {ac[d]:.0f} kW",
            "REMOTE_STOP": lambda d: "inverter stopped by network command",
            "TRACKER_STOW_WIND": lambda d: f"wind {wind:.1f} m/s above {self.plant.tracker.stow_wind_ms:.0f} m/s",
            "HIGH_WIND": lambda d: f"wind {wind:.1f} m/s",
            "GRID_LIMIT_ACTIVE": lambda d: f"active-power limit {self.state.grid_limit_kw:.0f} kW",
        }
        for code, device in sorted(current - self._active_alarms):
            self.emit_event(device, code, details[code](device))
        self._active_alarms = current
