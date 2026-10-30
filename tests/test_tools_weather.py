from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from raqeeb.contracts import Command
from raqeeb.tools import weather_tools
from tests.conftest import call
from tests.tool_scenarios import FLAG_AT, at, build


def test_reference_weather_has_geometry(run):
    store, s, _ = run("healthy")
    ref = weather_tools.reference_weather(store)
    assert len(ref) == len(store.weather_reference())
    assert {"ghi_wm2", "dni_wm2", "dhi_wm2", "zenith", "apparent_zenith", "azimuth"} <= set(ref.columns)
    assert ref.index[0] == s.start


def test_sensor_weather_is_the_reference_scaled_to_the_sensor(run):
    store, s, _ = run("sensor_only")
    ws = weather_tools.sensor_weather(store)
    ref = weather_tools.reference_weather(store).reindex(ws.index)
    station = store.weather_station()
    assert ws["ghi_wm2"].to_numpy() == pytest.approx(station["ghi_sensor_wm2"].to_numpy())
    day = ws[ref["ghi_wm2"] > 50]
    # same beam/diffuse split as the reference, only scaled
    assert (day["dni_wm2"] / day["ghi_wm2"]).to_numpy() == pytest.approx(
        (ref.loc[day.index, "dni_wm2"] / ref.loc[day.index, "ghi_wm2"]).to_numpy())
    assert ws["ambient_c"].to_numpy() == pytest.approx(station["ambient_c"].to_numpy())
    late = day[day.index >= at(s, "08:00")]
    assert (late["dhi_wm2"] / ref.loc[late.index, "dhi_wm2"]).median() == pytest.approx(0.80, abs=0.04)


def test_scaling_a_prepared_frame_gives_the_same_weather(run):
    from raqeeb.twin import plant as twin_physics

    store, _, inj = run("sensor_only")
    station = store.weather_station()
    ref = weather_tools.reference_weather(store)
    args = (station["ghi_sensor_wm2"], station["ambient_c"], station["wind_ms"])
    plain = weather_tools.sensor_scaled_weather(*args, ref)
    prepared = weather_tools.sensor_scaled_weather(*args, twin_physics.prepare(ref, inj.twin.plant))
    assert np.allclose(twin_physics.expected_power(plain, inj.twin.plant).to_numpy(),
                       twin_physics.expected_power(prepared, inj.twin.plant).to_numpy())


def test_station_dropout_stays_missing(run):
    store, s, _ = run("station_dropout")
    ws = weather_tools.sensor_weather(store)
    gap = [at(s, "07:00") + timedelta(minutes=5 * k) for k in range(6)]
    assert len(ws) == len(store.weather_reference()) - 6
    assert not ws.index.isin(gap).any()
    assert ws.notna().all().all()


def test_sensor_flag_comes_from_commands(run):
    store, s, _ = run("sensor_flagged")
    index = store.meter().index
    flagged = weather_tools.sensor_flagged(store, index)
    assert not flagged[index < at(s, FLAG_AT)].any()
    assert flagged[index >= at(s, FLAG_AT)].all()
    assert not weather_tools.sensor_flagged(run("sensor_only")[0], index).any()
    assert call(weather_tools.sensor_check, store, s).values["flagged"]


def test_sensor_flag_can_be_cleared():
    store, s, _ = build("sensor_flagged")
    off = at(s, "12:00")
    store.write_rows("commands", [Command(ts=off, source="operator", device_id="WS-01",
                                          action="flag_sensor_use_reference", params={"flagged": False})])
    flagged = weather_tools.sensor_flagged(store, store.meter().index)
    assert flagged[at(s, "11:55")] and not flagged[off]


def test_healthy_sensor_is_ok(run):
    store, s, _ = run("healthy")
    result = call(weather_tools.sensor_check, store, s)
    assert result.verdict == "ok" and not result.finding and result.devices == []
    assert abs(result.values["sensor_vs_reference"] - 1) < 0.05
    assert result.values["clear_steps"] >= 60
    assert result.thresholds["sensor_low_ratio"] == weather_tools.SENSOR_LOW_RATIO


def test_dusty_sensor_alone(run):
    store, s, _ = run("sensor_only")
    result = call(weather_tools.sensor_check, store, s)
    assert result.verdict == "sensor_low" and result.devices == ["WS-01"]
    assert result.values["sensor_vs_reference"] == pytest.approx(0.80, abs=0.04)
    assert result.values["votes"] == {"below_reference": True, "below_clear_sky": True, "step_drop": True,
                                      "peers_disagree": True}
    assert not result.values["panels_share_loss"]
    assert abs(result.values["onset"] - at(s, "07:00")) <= timedelta(minutes=20)
    assert "Riyadh" in result.evidence


def test_dusty_panels_alone_do_not_flag_the_sensor(run):
    store, s, _ = run("soiling_only")
    result = call(weather_tools.sensor_check, store, s)
    assert result.verdict == "ok"
    assert result.values["peers_vs_reference"] == pytest.approx(0.81, abs=0.05)


def test_trap_sensor_and_panels_dusty(run):
    store, s, _ = run("trap")
    result = call(weather_tools.sensor_check, store, s)
    assert result.verdict == "sensor_low_panels_too" and result.finding
    assert result.values["panels_share_loss"]
    assert not result.values["votes"]["peers_disagree"]
    assert result.values["peers_vs_reference"] == pytest.approx(result.values["sensor_vs_reference"], abs=0.05)
    assert abs(result.values["onset"] - at(s, "07:00")) <= timedelta(minutes=20)


def test_cloudy_day_is_inconclusive():
    store, s, _ = build("sensor_only", day=datetime(2025, 2, 15, tzinfo=timezone.utc))
    result = call(weather_tools.sensor_check, store, s)
    assert result.verdict == "inconclusive" and not result.finding


def test_wind_summary_storm(run):
    store, s, _ = run("trap")
    wind = call(weather_tools.wind_summary, store, s)
    assert wind.verdict == "stow_wind"
    assert wind.values["steps_above_stow"] == 12
    assert wind.values["first_above_stow"] == at(s, "06:00")
    assert wind.values["max_wind_ms"] > 15
    calm = call(weather_tools.wind_summary, *run("healthy")[:2])
    assert calm.verdict == "ok" and calm.values["steps_above_stow"] == 0


def test_irradiance_ratios_frame(run):
    df = weather_tools.irradiance_ratios(run("sensor_only")[0])
    late = df.loc[df.index >= df.index[0] + timedelta(hours=4)]
    assert late["sensor_vs_reference"].median() == pytest.approx(0.80, abs=0.04)
    assert df["clear"].sum() > 50


def test_lightly_dusted_sensor_is_caught_by_a_step_drop_when_the_reference_reads_low():
    """pr2_0016: the reference reads ~3.5% low that day, so an 11% dusty sensor sits at 0.93 of it,
    above the 0.90 band. The steady step drop from ~1.06 to ~0.93 still flags it (the bias cancels)."""
    from raqeeb.config import PRACTICE2_SCENARIO_DIR
    from raqeeb.contracts import Scenario
    from raqeeb.faults.injector import FaultInjector
    from raqeeb.telemetry.store import TelemetryStore
    from raqeeb.tools.weather_tools import sensor_check
    from tests.leakcheck import assert_no_answer_leak

    scenario = Scenario.from_yaml(PRACTICE2_SCENARIO_DIR / "pr2_0016.yaml")
    store = TelemetryStore()
    store.write_batch(FaultInjector.for_scenario(scenario).run_to_end())
    result = sensor_check(store)
    assert result.verdict.startswith("sensor_low") and result.values["sensor_vs_reference"] > 0.90
    assert result.values["votes"]["step_drop"] and not result.values["votes"]["below_reference"]
    assert_no_answer_leak(result, scenario)
