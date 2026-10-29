"""Everything the console reads, behind one adapter.

    run = replay("scn_0007")          # or inject_storm(seed)
    run.tables["inverter_readings"]   # one DataFrame per telemetry table, ts = UTC
    run.lines                         # actual vs expected (reference) vs expected (sensor)
    inverter_status(run, at)          # plant map

Today the telemetry comes from running a real scenario in memory (FaultInjector + Twin). When
the Lane C store lands, only `_telemetry()` changes: it reads the same tables from SQLite.
The agent timeline, approvals, report and benchmark are SAMPLE content from ui/samples.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from raqeeb.config import load_plant
from raqeeb.contracts import TABLE_MODELS, AgentScenario, InverterStatus, PlantConfig, Scenario, TelemetryBatch
from raqeeb.faults.generator import is_trap, load_scenarios, storm_scenario
from raqeeb.faults.injector import FaultInjector
from raqeeb.twin import plant as physics
from raqeeb.twin.controls import apply_action
from raqeeb.twin.weather import weather_5min
from raqeeb.ui import samples

IRRADIANCE = ["ghi_wm2", "dni_wm2", "dhi_wm2"]
REFERENCE_WEATHER = [*IRRADIANCE, "ambient_c", "wind_ms"]
MIN_REFERENCE_GHI_WM2 = 50.0  # below this the sensor / reference ratio is not meaningful

# Plant map: an inverter is "degraded" when its status says so, or it is this far below its peers
DEGRADED_STATUSES = frozenset({InverterStatus.DERATED, InverterStatus.FAULT, InverterStatus.STOPPED})
PEER_SHORTFALL = 0.10
MIN_PEER_FRACTION = 0.05  # of the rating; below this (dawn, dusk) peers are not compared

HEALTHY, DEGRADED, NO_DATA = "healthy", "degraded", "no data"


@dataclass
class PlantRun:
    incident: AgentScenario  # what the console shows and an agent may be given: id and window only
    planted: Scenario  # injector side: faults and answer key. Revealed in the UI only after the run;
    #                    never pass it (or anything built from it) to an agent
    tables: dict[str, pd.DataFrame]
    lines: pd.DataFrame
    agent: samples.SampleAgent

    @property
    def start(self) -> datetime:
        return self.incident.start

    @property
    def end(self) -> datetime:
        return self.incident.end

    @property
    def times(self) -> pd.DatetimeIndex:
        return self.lines.index


def scenario_ids() -> list[str]:
    return [s.id for s in load_scenarios()]


def default_scenario_id() -> str:
    """The first trap scenario (dusty panels and a dusty sensor), else the first one."""
    scenarios = load_scenarios()
    return next((s.id for s in scenarios if is_trap(s)), scenarios[0].id)


def replay(scenario_id: str) -> PlantRun:
    scenario = next(s for s in load_scenarios() if s.id == scenario_id)
    return _run(scenario)


def inject_storm(seed: int) -> PlantRun:
    return _run(storm_scenario(seed))


def until(frame: pd.DataFrame, at: datetime) -> pd.DataFrame:
    return frame[frame["ts"] <= at]


def inverter_status(run: PlantRun, at: datetime, plant: PlantConfig | None = None) -> pd.DataFrame:
    """One row per inverter at `at`: status, ac_kw, temp_c, map state (healthy / degraded / no data)."""
    plant = plant or load_plant()
    readings = run.tables["inverter_readings"]
    now = readings[readings["ts"] == at].set_index("inverter_id")
    rows = []
    peer_median = float(now["ac_kw"].median()) if len(now) else 0.0
    compare = peer_median > MIN_PEER_FRACTION * plant.inverters.ac_kw
    for inv_id in plant.inverters.ids:
        if inv_id not in now.index:
            rows.append({"inverter_id": inv_id, "state": NO_DATA, "status": None, "ac_kw": None, "temp_c": None})
            continue
        r = now.loc[inv_id]
        low = compare and r["ac_kw"] < (1 - PEER_SHORTFALL) * peer_median
        state = DEGRADED if r["status"] in DEGRADED_STATUSES or low else HEALTHY
        rows.append({"inverter_id": inv_id, "state": state, "status": str(r["status"]),
                     "ac_kw": float(r["ac_kw"]), "temp_c": float(r["temp_c"])})
    return pd.DataFrame(rows)


def benchmark() -> pd.DataFrame:
    return samples.benchmark_table()


# -- building a run -----------------------------------------------------------


def _run(scenario: Scenario) -> PlantRun:
    plant = load_plant()
    plan = samples.scripted_plan(scenario, plant)
    tables = _telemetry(scenario, plan)
    lines = power_lines(tables, scenario.start, scenario.end, plant)
    agent = samples.build_agent(scenario, plan, tables, lines, plant)
    return PlantRun(incident=scenario.agent_view(), planted=scenario, tables=tables, lines=lines, agent=agent)


def _telemetry(scenario: Scenario, plan: list[samples.PlannedAction]) -> dict[str, pd.DataFrame]:
    """Run the scenario through the injector; the sample plan's automatic actions hit the twin."""
    inj = FaultInjector.for_scenario(scenario)
    due = sorted((p for p in plan if not p.request.needs_approval), key=lambda p: p.at)
    batch = TelemetryBatch()
    while not inj.finished:
        while due and due[0].at <= inj.twin.clock:
            request = due.pop(0).request
            apply_action(inj.twin, request.action, request.target, request.params)
        batch.extend(inj.step())
    return _frames(batch)


def _frames(batch: TelemetryBatch) -> dict[str, pd.DataFrame]:
    frames = {}
    for name, rows in batch.tables().items():
        columns = list(TABLE_MODELS[name].model_fields)
        frame = pd.DataFrame([r.model_dump() for r in rows], columns=columns)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frames[name] = frame
    return frames


def power_lines(tables: dict[str, pd.DataFrame], start: datetime, end: datetime,
                plant: PlantConfig | None = None) -> pd.DataFrame:
    """Plant AC in kW: actual (meter), expected from the trusted reference, expected from the sensor.

    Both expected lines are the fault-free nominal plant (twin.plant physics, wind stow included)
    run on the weather the telemetry reports. The sensor line scales the reference irradiance by
    sensor GHI / reference GHI, as a plant SCADA's performance check does; a dusty sensor pulls it
    down to meet a dusty plant (the trap).
    """
    plant = plant or load_plant()
    frame = weather_5min(start, end, plant)  # solar geometry and clear-sky
    ref = tables["weather_reference"].set_index("ts").reindex(frame.index)
    frame[REFERENCE_WEATHER] = ref[REFERENCE_WEATHER]
    station = tables["weather_station"].set_index("ts").reindex(frame.index)
    ratio = (station["ghi_sensor_wm2"] / frame["ghi_wm2"]).where(frame["ghi_wm2"] > MIN_REFERENCE_GHI_WM2, 1.0)
    sensor_frame = frame.copy()
    sensor_frame[IRRADIANCE] = frame[IRRADIANCE].mul(ratio, axis=0)
    expected_sensor = physics.expected_plant_ac(sensor_frame, plant).where(station["ghi_sensor_wm2"].notna())
    meter = tables["meter"].set_index("ts")["plant_ac_kw"].reindex(frame.index)
    return pd.DataFrame({
        "actual_kw": meter,
        "expected_ref_kw": physics.expected_plant_ac(frame, plant),
        "expected_sensor_kw": expected_sensor,
        "sensor_ghi_wm2": station["ghi_sensor_wm2"],
        "reference_ghi_wm2": frame["ghi_wm2"],
        "clearsky_ghi_wm2": ref["clearsky_ghi_wm2"],
    }, index=frame.index)
