"""Each fault produces the data effects in HANDOFF section 8 (real weather, real plant)."""

from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path

import numpy as np
import pytest

from raqeeb.config import load_plant
from raqeeb.contracts import FaultSpec, FaultType, InverterStatus, Scenario, TelemetryBatch
from raqeeb.faults.catalogue import CATALOGUE
from raqeeb.faults.injector import FaultInjector
from raqeeb.twin.weather import weather_5min

JAN = datetime(2025, 1, 4, 3, 0, tzinfo=timezone.utc)  # clear, cool: little clipping
JUL = datetime(2025, 7, 7, 3, 0, tzinfo=timezone.utc)  # clear, hot: clips ~5 h
HOURS = 12
SYMPTOM_CODES = {"LOW_PR", "LOW_STRING_CURRENT", "DC_INPUT_LOW"}
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def plant():
    return load_plant()


@pytest.fixture(scope="module")
def weather(plant):
    return {start: weather_5min(start, start.replace(hour=3 + HOURS), plant) for start in (JAN, JUL)}


@pytest.fixture(scope="module")
def run(plant, weather):
    cache = {}

    def _run(*faults: dict, start: datetime = JAN, seed: int = 1):
        key = (start, seed, repr(faults))
        if key not in cache:
            scenario = Scenario(id="test", seed=seed, start=start, duration_hours=HOURS,
                                faults=[FaultSpec(**f) for f in faults])
            inj = FaultInjector.for_scenario(scenario, plant, weather[start])
            cache[key] = (inj, inj.run_to_end())
        return cache[key]

    return _run


def at(start: datetime, hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    return datetime.combine(start.date(), time(hh, mm), tzinfo=timezone.utc)


def codes(batch: TelemetryBatch) -> Counter:
    return Counter(e.code for e in batch.events)


def truth_plant(inj: FaultInjector) -> np.ndarray:
    return np.array([t.plant_ac_kw for t in inj.twin.truth])


STORM = {"type": "SOILING", "target": "ALL", "start": "05:00", "end": "06:30", "severity": 0.2}
SENSOR = {"type": "SENSOR_SOILING", "target": "WS-01", "start": "05:00", "severity": 0.2}


def test_catalogue_covers_every_fault_type():
    assert set(CATALOGUE) == set(FaultType)


@pytest.mark.parametrize("fault", [
    {"type": "SOILING", "target": "INV-01", "start": "05:00"},
    {"type": "SENSOR_SOILING", "target": "INV-01", "start": "05:00"},
    {"type": "INVERTER_DERATE", "target": "WS-01", "start": "05:00"},
    {"type": "COMMS_DROPOUT", "target": "INV-99", "start": "05:00"},
])
def test_bad_target_rejected(run, fault):
    with pytest.raises(ValueError, match="target"):
        run(fault)


@pytest.mark.parametrize("start", [JAN, JUL])
def test_healthy_plant_raises_no_alarms(run, start):
    _, batch = run(start=start)
    assert batch.events == []


def test_injector_is_deterministic(plant, weather):
    scenario = Scenario(id="t", seed=4, start=JAN, duration_hours=HOURS, faults=[FaultSpec(**STORM)])
    a = FaultInjector.for_scenario(scenario, plant, weather[JAN]).run_to_end()
    b = FaultInjector.for_scenario(scenario, plant, weather[JAN]).run_to_end()
    assert a == b


# -- SOILING (dust storm) ---------------------------------------------------------


def test_dust_storm_wind_dims_and_stows(run, plant):
    healthy, _ = run()
    inj, batch = run(STORM)
    storm = [i for i, r in enumerate(batch.weather_reference) if at(JAN, "05:00") <= r.ts < at(JAN, "06:30")]
    after = [i for i, r in enumerate(batch.weather_reference) if r.ts >= at(JAN, "06:30")]
    assert all(batch.weather_station[i].wind_ms > plant.tracker.stow_wind_ms for i in storm)
    assert all(batch.weather_station[i].wind_ms < plant.tracker.stow_wind_ms for i in after)
    assert all(inj.twin.truth[i].stowed for i in storm)
    healthy_ref = [r.ghi_wm2 for r in run()[1].weather_reference]
    dimming = {round(batch.weather_reference[i].ghi_wm2 / healthy_ref[i], 6) for i in storm}
    assert len(dimming) == 1 and 0.3 < dimming.pop() < 0.65
    assert all(batch.weather_reference[i].ghi_wm2 == pytest.approx(healthy_ref[i]) for i in after)
    stow = [e for e in batch.events if e.code in ("HIGH_WIND", "TRACKER_STOW_WIND")]
    assert len(stow) == 17 and {e.ts for e in stow} == {at(JAN, "05:00")}
    assert healthy.twin.state.atmosphere == inj.twin.state.atmosphere  # calm again at the end


def test_dust_storm_soiling_persists_plant_wide(run, plant):
    healthy, _ = run()
    inj, batch = run(STORM)
    losses = [inv.soiling_loss for inv in inj.twin.state.inverters.values()]
    assert all(0.18 - 1e-9 <= x <= 0.22 + 1e-9 for x in losses) and len(set(losses)) > 1
    # After the storm, outside clipping, every inverter is ~20% below a clean plant
    ref, soiled = healthy.twin.truth, inj.twin.truth
    rated = plant.inverters.ac_kw
    for h, s in zip(ref, soiled):
        if h.ts >= at(JAN, "07:00") and 0.2 * rated < min(h.inverter_ac_kw.values()) < 0.95 * rated:
            ratios = [s.inverter_ac_kw[i] / h.inverter_ac_kw[i] for i in h.inverter_ac_kw]
            assert all(0.74 < r < 0.86 for r in ratios), h.ts


def test_dust_storm_alarm_flood(run):
    _, batch = run(STORM)
    counts = codes(batch)
    assert 30 <= len(batch.events) <= 50
    assert counts["LOW_STRING_CURRENT"] == counts["LOW_PR"] == 16  # once per inverter, not per step
    assert counts["HIGH_WIND"] == 1 and counts["TRACKER_STOW_WIND"] == 16
    assert min(e.ts for e in batch.events if e.code in SYMPTOM_CODES) > at(JAN, "05:00")  # after the wind


@pytest.mark.parametrize("start", [JAN, JUL])
def test_dust_storm_symptoms_appear_in_summer_too(run, start):
    _, batch = run(STORM, start=start)
    assert codes(batch)["LOW_PR"] == 16


# -- SENSOR_SOILING and the trap -------------------------------------------------


def sensor_over_reference(batch: TelemetryBatch, after: datetime) -> np.ndarray:
    ref = {r.ts: r.ghi_wm2 for r in batch.weather_reference}
    return np.array([s.ghi_sensor_wm2 / ref[s.ts] for s in batch.weather_station if s.ts >= after and ref[s.ts] > 200])


def test_sensor_soiling_only_affects_the_sensor(run):
    healthy, healthy_batch = run()
    inj, batch = run(SENSOR)
    before = sensor_over_reference(healthy_batch, at(JAN, "05:00"))
    dirty = sensor_over_reference(batch, at(JAN, "05:00"))
    assert np.median(dirty / before) == pytest.approx(0.8, abs=0.01)
    assert np.median(dirty) == pytest.approx(0.8, abs=0.05)  # vs the reference: within its own error
    assert truth_plant(inj) == pytest.approx(truth_plant(healthy))
    assert batch.events == []  # a low sensor only makes the plant look better


def test_trap_dusty_sensor_hides_soiling(run):
    healthy, _ = run()
    inj, batch = run(STORM, SENSOR)
    assert not SYMPTOM_CODES & set(codes(batch))  # only wind alarms remain
    assert len(batch.events) == 17
    energy = truth_plant(inj).sum() / truth_plant(healthy).sum()
    assert energy < 0.85  # the plant really lost energy
    assert np.median(sensor_over_reference(batch, at(JAN, "06:30"))) < 0.85  # the evidence: sensor vs reference


# -- INVERTER_DERATE ---------------------------------------------------------------


@pytest.mark.parametrize("start", [JAN, JUL])
def test_derate_flat_cap_hot_and_alarmed(run, plant, start):
    derate = {"type": "INVERTER_DERATE", "target": "INV-07", "start": "06:00", "severity": 0.38}
    _, batch = run(derate, start=start)
    cap = plant.inverters.ac_kw * 0.62
    inv7 = [r for r in batch.inverter_readings if r.inverter_id == "INV-07"]
    derated = [r for r in inv7 if r.status == InverterStatus.DERATED]
    assert len(derated) >= 24  # at least two hours capped around midday
    assert all(r.ac_kw == pytest.approx(cap, rel=0.02) for r in derated)
    assert all(r.temp_c > plant.inverters.derate_temp_c for r in derated)
    assert all(r.ts >= at(start, "06:00") for r in derated)
    peers = [r for r in batch.inverter_readings if r.inverter_id != "INV-07" and r.ts in {d.ts for d in derated}]
    assert all(r.status == InverterStatus.RUNNING and r.ac_kw > cap * 0.97 for r in peers)
    assert max(r.ac_kw for r in peers) > cap * 1.4  # the cap is the inverter's, not the sun's
    assert max(r.temp_c for r in peers) < plant.inverters.derate_temp_c
    assert {(e.code, e.device_id) for e in batch.events} == {("DERATE_TEMP", "INV-07"), ("LOW_PR", "INV-07")}


# -- COMMS_DROPOUT -------------------------------------------------------------------


def test_comms_dropout_missing_rows_meter_unchanged(run):
    healthy, _ = run()
    dropout = {"type": "COMMS_DROPOUT", "target": "INV-12", "start": "06:00", "end": "08:00"}
    inj, batch = run(dropout)
    inv12 = {r.ts for r in batch.inverter_readings if r.inverter_id == "INV-12"}
    all_ts = {m.ts for m in batch.meter}
    missing = all_ts - inv12
    assert min(missing) == at(JAN, "06:00") and max(missing) < at(JAN, "08:00") and len(missing) == 24
    assert all(r.ac_kw > 0 for r in batch.inverter_readings if r.inverter_id == "INV-12" and r.status == "RUNNING")
    assert truth_plant(inj) == pytest.approx(truth_plant(healthy))  # the meter still counts INV-12
    assert [(e.code, e.device_id, e.ts) for e in batch.events] == [("COMM_LOSS", "INV-12", at(JAN, "06:00"))]


def test_comms_dropout_without_end_lasts_to_the_end(run):
    _, batch = run({"type": "COMMS_DROPOUT", "target": "INV-03", "start": "10:00"})
    assert max(r.ts for r in batch.inverter_readings if r.inverter_id == "INV-03") < at(JAN, "10:00")


def test_comms_dropout_of_weather_station(run):
    _, batch = run({"type": "COMMS_DROPOUT", "target": "WS-01", "start": "06:00", "end": "07:00"})
    assert len(batch.weather_station) == len(batch.weather_reference) - 12
    assert [(e.code, e.device_id) for e in batch.events] == [("COMM_LOSS", "WS-01")]


# -- CURTAILMENT (decoy) -------------------------------------------------------------


def test_curtailment_caps_all_together_with_setpoint(run, plant):
    curtail = {"type": "CURTAILMENT", "target": "ALL", "start": "07:30", "end": "09:30", "severity": 0.4}
    _, batch = run(curtail)
    limit = plant.rated_ac_kw * 0.6
    window = {g.ts for g in batch.grid_setpoint}
    assert min(window) == at(JAN, "07:30") and max(window) < at(JAN, "09:30") and len(window) == 24
    assert {g.limit_kw for g in batch.grid_setpoint} == {limit}
    assert all(m.plant_ac_kw == pytest.approx(limit) for m in batch.meter if m.ts in window)  # the limit binds
    assert {r.status for r in batch.inverter_readings if r.ts in window} == {InverterStatus.CURTAILED}
    assert [e.code for e in batch.events] == ["GRID_LIMIT_ACTIVE"]  # no false symptom alarms


def test_curtailment_is_not_a_root_cause():
    scenario = Scenario(id="t", seed=1, start=JAN, duration_hours=HOURS, faults=[
        FaultSpec(type="CURTAILMENT", target="ALL", start="07:30", end="09:30", severity=0.4),
        FaultSpec(type="INVERTER_DERATE", target="INV-02", start="06:00", severity=0.3),
    ])
    assert scenario.expected_answer_key().root_causes == ["INVERTER_DERATE@INV-02"]


# -- full scope ------------------------------------------------------------------------


def test_tracker_stuck_flat_with_alarm(run, plant):
    _, batch = run({"type": "TRACKER_STUCK", "target": "INV-03", "start": "04:00"})
    rows = [r for r in batch.inverter_readings if r.inverter_id == "INV-03" and r.ts >= at(JAN, "04:00")]
    assert {r.tracker_angle_deg for r in rows} == {plant.tracker.stow_angle_deg}
    assert ("TRACKER_FAULT", "INV-03") in {(e.code, e.device_id) for e in batch.events}
    assert ("LOW_PR", "INV-03") in {(e.code, e.device_id) for e in batch.events}


def test_string_open_fixed_dc_fraction(run):
    _, batch = run({"type": "STRING_OPEN", "target": "INV-05", "start": "05:00", "severity": 0.35})
    raised = {(e.code, e.device_id) for e in batch.events}
    assert raised == {("LOW_STRING_CURRENT", "INV-05"), ("DC_INPUT_LOW", "INV-05"), ("LOW_PR", "INV-05")}


def test_unauthorized_command_stops_inverter(run):
    _, batch = run({"type": "UNAUTHORIZED_CMD", "target": "INV-06", "start": "07:00"})
    (command,) = batch.commands
    assert command.device_id == "INV-06" and command.ts == at(JAN, "07:00") and command.source != "raqeeb"
    after = [r for r in batch.inverter_readings if r.inverter_id == "INV-06" and r.ts >= at(JAN, "07:00")]
    assert all(r.ac_kw == 0 and r.status == InverterStatus.STOPPED for r in after)
    assert [(e.code, e.device_id) for e in batch.events] == [("REMOTE_STOP", "INV-06")]  # no electrical alarm


def test_handoff_example_scenario_runs(plant):
    scenario = Scenario.from_yaml(FIXTURES / "scn_example.yaml")
    batch = FaultInjector.for_scenario(scenario.without_answer_key(), plant).run_to_end()
    raised = codes(batch)
    assert raised["COMM_LOSS"] == 1 and raised["DERATE_TEMP"] >= 1 and raised["TRACKER_STOW_WIND"] == 16
    assert not {"LOW_STRING_CURRENT", "DC_INPUT_LOW"} & set(raised)  # panel and sensor soiled alike
