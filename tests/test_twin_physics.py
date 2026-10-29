from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from raqeeb.config import load_plant
from raqeeb.twin import plant as physics
from raqeeb.twin.weather import COLUMNS, clear_days, load_hourly, weather_5min

DAY = datetime(2025, 7, 7, tzinfo=timezone.utc)  # one of the clearest days at the site


@pytest.fixture(scope="module")
def plant():
    return load_plant()


@pytest.fixture(scope="module")
def day_weather(plant):
    return weather_5min(DAY, DAY + pd.Timedelta(days=1), plant)


def test_weather_frame_shape(day_weather):
    assert list(day_weather.columns) == COLUMNS
    assert len(day_weather) == 288
    assert str(day_weather.index.tz) == "UTC"
    assert (day_weather.index.to_series().diff().dropna() == pd.Timedelta(minutes=5)).all()
    assert not day_weather.isna().any().any()


def test_resampling_keeps_hourly_energy(day_weather):
    nasa = load_hourly().loc[DAY : DAY + pd.Timedelta(hours=23)]
    hourly_mean = day_weather["ghi_wm2"].resample("1h").mean()
    assert hourly_mean.sum() == pytest.approx(nasa["ghi_wm2"].sum(), rel=0.01)
    sunny = nasa["ghi_wm2"] > 300
    assert np.allclose(hourly_mean[sunny], nasa["ghi_wm2"][sunny], rtol=0.03)


def test_irradiance_is_physical(day_weather):
    w = day_weather
    assert (w["dhi_wm2"] >= 0).all()
    cos_z = np.cos(np.radians(w["zenith"])).clip(lower=0)
    assert (w["dni_wm2"] * cos_z <= w["ghi_wm2"] + 1e-6).all()
    assert (w.loc[w["zenith"] > 92, "ghi_wm2"] == 0).all()


def test_window_outside_weather_year_raises(plant):
    with pytest.raises(ValueError, match="outside the weather data"):
        weather_5min(datetime(2024, 6, 1, tzinfo=timezone.utc), datetime(2024, 6, 2, tzinfo=timezone.utc), plant)


def test_naive_window_raises(plant):
    with pytest.raises(ValueError, match="timezone-aware"):
        weather_5min(datetime(2025, 6, 1), datetime(2025, 6, 2), plant)


def test_clear_days_are_in_the_year():
    days = clear_days(3)
    assert len(days) == 3 and all(d.year == 2025 for d in days)


def test_capacity_factors_fixed_and_bounded(plant):
    factors = physics.capacity_factors(plant)
    assert np.array_equal(factors, physics.capacity_factors(plant))
    assert np.all(np.abs(factors - 1) <= plant.inverters.variation_pct / 100)
    assert len(set(factors.round(6))) == plant.inverters.count


def test_clear_day_expected_output_is_plausible(plant, day_weather):
    ac = physics.expected_plant_ac(day_weather, plant)
    assert ac.max() == pytest.approx(plant.rated_ac_kw)  # clips at the AC rating
    assert (ac[day_weather["zenith"] > 92] == 0).all()
    specific_yield = ac.sum() * 5 / 60 / (plant.inverters.dc_kw * plant.inverters.count)
    assert 6.5 < specific_yield < 9.0  # kWh/kWp, tracker on a clear summer day in Riyadh


def test_stow_reduces_output(plant, day_weather):
    windy = day_weather.assign(wind_ms=plant.tracker.stow_wind_ms + 1)
    calm = physics.expected_plant_ac(day_weather, plant).sum()
    stowed = physics.expected_plant_ac(windy, plant).sum()
    assert stowed < 0.9 * calm


def test_tracking_beats_flat(plant, day_weather):
    prepared = physics.prepare(day_weather, plant)
    assert prepared["poa_track_wm2"].sum() > 1.15 * prepared["poa_stow_wm2"].sum()
    assert prepared["tracker_theta"].abs().max() <= plant.tracker.max_angle_deg + 1e-6
