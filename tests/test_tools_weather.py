from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from raqeeb.tools import weather_tools
from tests.tool_scenarios import FLAG_AT, at, build


def test_reference_weather_has_geometry(scenario):
    store, twin = scenario("healthy")
    ref = weather_tools.reference_weather(store)
    assert len(ref) == len(store.weather_reference())
    assert {"ghi_wm2", "dni_wm2", "dhi_wm2", "zenith", "apparent_zenith", "azimuth"} <= set(ref.columns)
    assert ref.index[0] == twin.start


def test_sensor_weather_erbs_closure(scenario):
    store, _ = scenario("healthy")
    ws = weather_tools.sensor_weather(store)
    day = ws[ws["zenith"] < 80]
    closure = day["dni_wm2"] * np.cos(np.radians(day["zenith"])) + day["dhi_wm2"]
    assert closure.to_numpy() == pytest.approx(day["ghi_wm2"].to_numpy(), rel=1e-6)


def test_station_dropout_stays_missing(scenario):
    store, twin = scenario("station_dropout")
    ws = weather_tools.sensor_weather(store)
    gap = [at(twin, "07:00") + timedelta(minutes=5 * k) for k in range(6)]
    assert len(ws) == len(store.weather_reference()) - 6
    assert not ws.index.isin(gap).any()
    assert ws.notna().all().all()


def test_sensor_flag_comes_from_commands(scenario):
    store, twin = scenario("sensor_flagged")
    index = store.meter().index
    flagged = weather_tools.sensor_flagged(store, index)
    assert not flagged[index < at(twin, FLAG_AT)].any()
    assert flagged[index >= at(twin, FLAG_AT)].all()
    assert not weather_tools.sensor_flagged(scenario("sensor_only")[0], index).any()


def test_sensor_flag_can_be_cleared(scenario):
    store, twin = build("sensor_flagged")
    from raqeeb.contracts import Command

    off = at(twin, "12:00")
    store.write_rows("commands", [Command(ts=off, source="operator", device_id="WS-01",
                                          action="flag_sensor_use_reference", params={"flagged": False})])
    flagged = weather_tools.sensor_flagged(store, store.meter().index)
    assert flagged[at(twin, "11:55")] and not flagged[off]


def test_healthy_sensor_is_ok(scenario):
    result = weather_tools.sensor_check(scenario("healthy")[0])
    assert result["verdict"] == "ok" and not result["suspect"]
    assert abs(result["sensor_vs_reference"] - 1) < 0.05
    assert result["clear_steps"] >= 60


def test_dusty_sensor_alone(scenario):
    store, twin = scenario("sensor_only")
    result = weather_tools.sensor_check(store)
    assert result["verdict"] == "sensor_low" and result["suspect"]
    assert result["sensor_vs_reference"] == pytest.approx(0.80, abs=0.04)
    assert result["votes"] == {"below_reference": True, "below_clear_sky": True, "step_drop": True,
                               "peers_disagree": True}
    assert not result["panels_share_loss"]
    assert abs(result["onset"] - at(twin, "07:00")) <= timedelta(minutes=20)


def test_dusty_panels_alone_do_not_flag_the_sensor(scenario):
    result = weather_tools.sensor_check(scenario("soiling_only")[0])
    assert result["verdict"] == "ok"
    assert result["peers_vs_reference"] == pytest.approx(0.80, abs=0.05)


def test_trap_sensor_and_panels_dusty(scenario):
    store, twin = scenario("trap")
    result = weather_tools.sensor_check(store)
    assert result["verdict"] == "sensor_low_panels_too"
    assert result["suspect"] and result["panels_share_loss"]
    assert not result["votes"]["peers_disagree"]
    assert result["peers_vs_reference"] == pytest.approx(result["sensor_vs_reference"], abs=0.05)
    assert abs(result["onset"] - at(twin, "07:00")) <= timedelta(minutes=20)


def test_cloudy_day_is_inconclusive():
    store, _ = build("sensor_only", day=datetime(2025, 2, 15, tzinfo=timezone.utc))
    result = weather_tools.sensor_check(store)
    assert result["verdict"] == "inconclusive" and not result["suspect"]


def test_wind_summary_storm(scenario):
    store, twin = scenario("trap")
    wind = weather_tools.wind_summary(store)
    assert wind["steps_above_stow"] == 12
    assert wind["first_above_stow"] == at(twin, "06:00")
    assert wind["max_wind_ms"] > 15
    assert weather_tools.wind_summary(scenario("healthy")[0])["steps_above_stow"] == 0


def test_irradiance_ratios_frame(scenario):
    df = weather_tools.irradiance_ratios(scenario("sensor_only")[0])
    late = df.loc[df.index >= df.index[0] + timedelta(hours=4)]
    assert late["sensor_vs_reference"].median() == pytest.approx(0.80, abs=0.04)
    assert df["clear"].sum() > 50
