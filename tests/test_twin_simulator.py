from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from raqeeb.config import load_plant
import numpy as np

from raqeeb.contracts import InverterStatus, ReferenceErrorConfig, TelemetryBatch
from raqeeb.twin import plant as physics
from raqeeb.twin.simulator import Twin
from raqeeb.twin.weather import weather_5min

START = datetime(2025, 7, 7, 6, 0, tzinfo=timezone.utc)  # 09:00 Riyadh, sunny
END = START + timedelta(hours=3)
N = 16


@pytest.fixture(scope="module")
def real_plant():
    return load_plant()


@pytest.fixture(scope="module")
def plant(real_plant):
    """Plant whose true irradiance equals the reference, for exact comparisons."""
    exact = ReferenceErrorConfig(bias_sigma=0.0, sigma=0.0, tau_min=60, max_abs=0.01)
    return real_plant.model_copy(update={"reference_error": exact})


@pytest.fixture(scope="module")
def weather(plant):
    return weather_5min(START, END, plant)


@pytest.fixture
def twin(plant, weather):
    return Twin(START, END, plant=plant, seed=3, weather=weather)


def meter_kw(batch: TelemetryBatch) -> list[float]:
    return [m.plant_ac_kw for m in batch.meter]


def codes(batch: TelemetryBatch) -> list[tuple[str, str]]:
    return [(e.code, e.device_id) for e in batch.events]


def test_telemetry_batch_fields_are_tables():
    from raqeeb.contracts import TABLE_MODELS

    assert set(TelemetryBatch.model_fields) == set(TABLE_MODELS) - {"audit"}


def test_healthy_twin_matches_expected(twin, plant, weather):
    batch = twin.run_to_end()
    expected = physics.expected_plant_ac(weather, plant, with_variation=True)
    assert meter_kw(batch) == pytest.approx(expected.tolist(), rel=1e-9)
    assert len(batch.inverter_readings) == N * len(weather)
    assert len(batch.weather_station) == len(batch.weather_reference) == len(weather)
    assert batch.events == [] and batch.grid_setpoint == []
    assert {r.status for r in batch.inverter_readings} <= {InverterStatus.RUNNING}


def test_clock_and_end(twin):
    twin.advance(30)
    assert twin.clock == START + timedelta(minutes=30)
    twin.run_to_end()
    assert twin.finished
    with pytest.raises(RuntimeError):
        twin.step()


def test_same_seed_same_readings(plant, weather):
    a = Twin(START, END, plant=plant, seed=5, weather=weather).advance(30)
    b = Twin(START, END, plant=plant, seed=5, weather=weather).advance(30)
    c = Twin(START, END, plant=plant, seed=6, weather=weather).advance(30)
    assert a == b
    assert a.inverter_readings != c.inverter_readings


def test_off_grid_start_rejected(plant, weather):
    with pytest.raises(ValueError, match="grid"):
        Twin(START + timedelta(minutes=2), END, plant=plant, weather=weather)


def test_scheduled_change_applies_at_its_time(twin):
    at = START + timedelta(minutes=20)
    twin.schedule(at, "soil INV-01", lambda s: setattr(s.inverters["INV-01"], "soiling_loss", 0.5))
    assert twin.pending == [(at, "soil INV-01")]
    batch = twin.advance(30)
    inv1 = [r for r in batch.inverter_readings if r.inverter_id == "INV-01"]
    inv2 = [r for r in batch.inverter_readings if r.inverter_id == "INV-02"]
    ratio = [a.dc_kw / b.dc_kw for a, b in zip(inv1, inv2)]
    assert all(r > 0.9 for r in ratio[:4]) and all(r < 0.65 for r in ratio[4:])
    assert twin.pending == []


def test_comms_dropout_means_missing_rows_not_zeros(twin):
    healthy = twin.advance(5)
    twin.state.inverters["INV-12"].comms_up = False
    batch = twin.advance(15)
    assert all(r.inverter_id != "INV-12" for r in batch.inverter_readings)
    assert len(batch.inverter_readings) == (N - 1) * 3
    assert meter_kw(batch)[0] == pytest.approx(meter_kw(healthy)[0], rel=0.05)  # meter still sees it
    assert codes(batch) == [("COMM_LOSS", "INV-12")]  # raised once, not every step


def test_sensor_soiling_affects_only_the_sensor(twin):
    clean = twin.advance(5)
    twin.state.station.sensor_soiling = 0.2
    dirty = twin.advance(5)
    ratio = dirty.weather_station[0].ghi_sensor_wm2 / dirty.weather_reference[0].ghi_wm2
    assert ratio == pytest.approx(0.8, abs=0.03)
    assert meter_kw(dirty)[0] == pytest.approx(meter_kw(clean)[0], rel=0.05)


def test_dust_storm_atmosphere_and_stow(twin, plant):
    calm = twin.advance(5)
    twin.state.atmosphere.wind_add_ms = 12.0
    twin.state.atmosphere.irradiance_factor = 0.6
    storm = twin.advance(10)
    assert storm.weather_reference[0].ghi_wm2 == pytest.approx(calm.weather_reference[0].ghi_wm2 * 0.6, rel=0.05)
    assert storm.weather_station[0].wind_ms > plant.tracker.stow_wind_ms
    raised = codes(storm)
    assert ("HIGH_WIND", "WS-01") in raised
    assert sum(code == "TRACKER_STOW_WIND" for code, _ in raised) == N
    assert len(raised) == N + 1  # once per device, not per step
    assert meter_kw(storm)[0] < 0.6 * meter_kw(calm)[0]


def test_grid_limit_curtails_all_inverters(twin):
    twin.state.grid_limit_kw = 20_000
    batch = twin.advance(10)
    assert max(meter_kw(batch)) == pytest.approx(20_000)
    assert {r.status for r in batch.inverter_readings} == {InverterStatus.CURTAILED}
    assert [g.limit_kw for g in batch.grid_setpoint] == [20_000, 20_000]
    assert codes(batch) == [("GRID_LIMIT_ACTIVE", "PPC-01")]


def test_inverter_derate_caps_output_and_alarms(twin, plant):
    inv = twin.state.inverters["INV-07"]
    inv.derate_cap_kw = 1500
    inv.temp_offset_c = 30
    batch = twin.advance(5)
    row = next(r for r in batch.inverter_readings if r.inverter_id == "INV-07")
    assert row.ac_kw == pytest.approx(1500, rel=0.02)
    assert row.status == InverterStatus.DERATED
    assert row.temp_c > plant.inverters.derate_temp_c
    assert codes(batch) == [("DERATE_TEMP", "INV-07")]


def test_stuck_tracker_loses_morning_output(twin):
    twin.state.inverters["INV-03"].tracker_override_deg = 0.0
    batch = twin.advance(5)
    by_id = {r.inverter_id: r.ac_kw for r in batch.inverter_readings}
    assert by_id["INV-03"] < 0.85 * by_id["INV-04"]


def test_string_open_and_stop(twin):
    twin.state.inverters["INV-05"].string_loss = 0.25
    twin.state.inverters["INV-06"].stopped = True
    batch = twin.advance(5)
    by_id = {r.inverter_id: r for r in batch.inverter_readings}
    assert by_id["INV-05"].dc_kw == pytest.approx(0.75 * by_id["INV-04"].dc_kw, rel=0.05)
    assert by_id["INV-06"].ac_kw == 0 and by_id["INV-06"].status == InverterStatus.STOPPED
    assert ("REMOTE_STOP", "INV-06") in codes(batch)


def test_reference_is_not_ground_truth(real_plant, weather):
    cfg = real_plant.reference_error
    twin = Twin(START, END, plant=real_plant, seed=11, weather=weather)
    batch = twin.run_to_end()
    # The reference table reports NASA unchanged ...
    assert [r.ghi_wm2 for r in batch.weather_reference] == pytest.approx(weather["ghi_wm2"].tolist())
    # ... while the plant sees true irradiance a few percent away from it
    true_over_ref = np.array([t.ghi_wm2 for t in twin.truth]) / weather["ghi_wm2"].to_numpy()
    assert np.all(np.abs(true_over_ref - 1) <= cfg.max_abs + 1e-9)
    assert np.abs(true_over_ref - 1).mean() > 0.005
    # A clean sensor therefore disagrees with the reference by more than its own noise
    sensor_over_ref = np.array([s.ghi_sensor_wm2 for s in batch.weather_station]) / weather["ghi_wm2"].to_numpy()
    assert np.std(sensor_over_ref - 1) < 0.05
    assert np.abs(sensor_over_ref - 1).max() > 2 * real_plant.noise.ghi_sensor


def test_reference_error_differs_by_seed_and_repeats(real_plant, weather):
    a = Twin(START, END, plant=real_plant, seed=1, weather=weather).irradiance_factor
    b = Twin(START, END, plant=real_plant, seed=1, weather=weather).irradiance_factor
    c = Twin(START, END, plant=real_plant, seed=2, weather=weather).irradiance_factor
    assert a.equals(b) and not a.equals(c)


def test_tracker_angle_reported(twin, plant, weather):
    normal = twin.step()
    theta = physics.prepare(weather, plant)["tracker_theta"].iloc[0]
    assert [r.tracker_angle_deg for r in normal.inverter_readings] == pytest.approx([theta] * N)
    assert abs(theta) > 20  # mid-morning: tracker well off flat
    twin.state.inverters["INV-03"].tracker_override_deg = 10.0
    twin.state.atmosphere.wind_add_ms = 20.0
    stuck_and_stowed = {r.inverter_id: r.tracker_angle_deg for r in twin.step().inverter_readings}
    assert stuck_and_stowed["INV-03"] == 10.0
    assert stuck_and_stowed["INV-04"] == plant.tracker.stow_angle_deg


def test_truth_records_exact_values(twin):
    batch = twin.advance(10)
    assert [t.plant_ac_kw for t in twin.truth] == meter_kw(batch)
    assert pd.Timestamp(twin.truth[0].ts) == pd.Timestamp(START)


def test_practice2_runs_carry_the_reference_error(real_plant):
    """Every practice2 run sees true irradiance = reference x (1 + bias + AR(1) error), capped at max_abs:
    a per-run offset that differs between runs and a slow wander within each run."""
    from raqeeb.config import PRACTICE2_SCENARIO_DIR
    from raqeeb.faults.generator import load_scenarios

    cfg = real_plant.reference_error
    errors = [Twin(s.start, s.end, plant=real_plant, seed=s.seed).irradiance_factor - 1
              for s in load_scenarios(PRACTICE2_SCENARIO_DIR, "pr2")]
    run_means = np.array([e.mean() for e in errors])
    assert run_means.std() > cfg.bias_sigma / 2  # the offset differs between runs
    assert np.mean([e.std() for e in errors]) > cfg.sigma / 2  # and wanders within a run
    assert max(e.abs().max() for e in errors) <= cfg.max_abs + 1e-9
