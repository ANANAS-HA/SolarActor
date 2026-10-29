"""Fixture scenarios for the tool tests, driven through Lane B's FaultInjector (so the symptom
alarms appear) and stored in an in-memory TelemetryStore.

Day: 15 Jan 2025 (clear, cool, so midday soiling loss is not hidden by clipping), 07:00-18:00
Riyadh = 04:00-15:00 UTC. Fault times are UTC on the scenario's start date.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from raqeeb.contracts import FaultSpec, PlantConfig, Scenario
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin.controls import flag_sensor

DAY = datetime(2025, 1, 15, tzinfo=timezone.utc)
START_H, HOURS = 4, 11
SEED = 7
FLAG_AT = "09:00"  # sensor_flagged: flag_sensor is called through the real control at this time
DERATE_SEVERITY = 0.44  # cap = 0.56 x 2500 kW = 1400 kW

STORM = {"type": "SOILING", "target": "ALL", "start": "06:00", "end": "07:00", "severity": 0.20}
SENSOR_DUST = {"type": "SENSOR_SOILING", "target": "WS-01", "start": "07:00", "severity": 0.20}

SCENARIOS: dict[str, list[dict]] = {
    "healthy": [],
    "sensor_only": [SENSOR_DUST],
    "soiling_only": [STORM],
    "trap": [
        STORM,
        SENSOR_DUST,
        {"type": "INVERTER_DERATE", "target": "INV-07", "start": "08:30", "severity": DERATE_SEVERITY},
        {"type": "COMMS_DROPOUT", "target": "INV-12", "start": "09:15", "end": "11:00"},
    ],
    "curtailment": [{"type": "CURTAILMENT", "target": "ALL", "start": "07:30", "end": "09:30", "severity": 0.40}],
    "tracker_stuck": [{"type": "TRACKER_STUCK", "target": "INV-03", "start": "05:00"}],
    "sensor_flagged": [SENSOR_DUST],
    "station_dropout": [{"type": "COMMS_DROPOUT", "target": "WS-01", "start": "07:00", "end": "07:30"}],
    # INV-05 stopped by a command AND silent: the meter shows the loss
    "dead_and_silent": [{"type": "UNAUTHORIZED_CMD", "target": "INV-05", "start": "08:00"},
                        {"type": "COMMS_DROPOUT", "target": "INV-05", "start": "08:00"}],
}


def at(scenario: Scenario, hhmm: str) -> datetime:
    return scenario.resolve_time(hhmm)


def make_scenario(name: str, seed: int = SEED, day: datetime = DAY) -> Scenario:
    scenario = Scenario(id=f"fx_{name}", seed=seed, start=day + timedelta(hours=START_H), duration_hours=HOURS,
                        faults=[FaultSpec(**f) for f in SCENARIOS[name]])
    return scenario.model_copy(update={"answer_key": scenario.expected_answer_key()})


def build(name: str, seed: int = SEED, day: datetime = DAY,
          plant: PlantConfig | None = None) -> tuple[TelemetryStore, Scenario, FaultInjector]:
    """Run the scenario through the injector into a fresh store. Tools get only the store and
    scenario.agent_view(); the Scenario and injector are for test assertions. `plant` lets a
    sweep change the twin (e.g. its reference error); the tools keep the default plant."""
    scenario = make_scenario(name, seed, day)
    inj = FaultInjector.for_scenario(scenario, plant=plant)
    store = TelemetryStore()
    if name == "sensor_flagged":
        store.write_batch(inj.advance((at(scenario, FLAG_AT) - scenario.start).total_seconds() / 60))
        flag_sensor(inj.twin, inj.twin.plant.weather_station_id)
    store.write_batch(inj.advance(HOURS * 60))
    return store, scenario, inj
