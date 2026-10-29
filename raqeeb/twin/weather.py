"""Weather for the twin: NASA POWER hourly -> 5-minute frames with solar position and clear-sky.

NASA POWER hourly values are averages over [H, H+1) and are labelled H. Irradiance is
resampled by interpolating the clear-sky index (measured / hourly-mean clear-sky) at the
hour centres and multiplying by the 5-minute clear-sky, so sunrise and sunset follow the sun
and hourly energy is approximately kept. Temperature and wind are interpolated linearly.
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pvlib

from raqeeb.config import load_plant, load_settings
from raqeeb.contracts import STEP_MINUTES, PlantConfig

FREQ = f"{STEP_MINUTES}min"
MIN_CLEARSKY_WM2 = 20.0  # below this the clear-sky index is too noisy to use
MAX_CLEARSKY_INDEX = 1.3

# Columns of the frame returned by weather_5min()
COLUMNS = [
    "ghi_wm2", "dni_wm2", "dhi_wm2", "ambient_c", "wind_ms",
    "clearsky_ghi_wm2", "clearsky_dni_wm2",
    "apparent_zenith", "zenith", "azimuth", "dni_extra",
]


@lru_cache
def load_hourly(path: Path | None = None) -> pd.DataFrame:
    """The processed NASA POWER CSV, indexed by UTC hour label."""
    path = path or load_settings().weather_csv
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run: uv run python scripts/download_weather.py")
    df = pd.read_csv(path)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts_utc"), utc=True), name="ts")
    return df


def location(plant: PlantConfig | None = None) -> pvlib.location.Location:
    plant = plant or load_plant()
    return pvlib.location.Location(
        plant.site.lat, plant.site.lon, tz="UTC", altitude=plant.site.altitude_m, name=plant.site.name
    )


def _utc(value: datetime | str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware UTC, got {value!r}")
    return ts.tz_convert("UTC")


def _clearsky_index(measured: pd.Series, clearsky_mean: pd.Series) -> pd.Series:
    cs = clearsky_mean.reindex(measured.index)
    kt = (measured / cs).where(cs > MIN_CLEARSKY_WM2)
    return kt.clip(0.0, MAX_CLEARSKY_INDEX)


def _to_grid(hourly_values: pd.Series, grid: pd.DatetimeIndex) -> pd.Series:
    """Place hourly values at hour centres and interpolate onto the 5-minute grid."""
    centred = pd.Series(hourly_values.to_numpy(), index=hourly_values.index + pd.Timedelta(minutes=30))
    union = centred.index.union(grid)
    filled = centred.reindex(union).interpolate(method="time", limit_area="inside").ffill().bfill()
    return filled.reindex(grid)


def weather_5min(
    start: datetime | str,
    end: datetime | str,
    plant: PlantConfig | None = None,
    hourly: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Trusted weather on the 5-minute grid for [start, end), with solar geometry.

    Returns the columns in COLUMNS, indexed by UTC timestamp.
    """
    plant = plant or load_plant()
    hourly = load_hourly() if hourly is None else hourly
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("end must be after start")
    first, last = hourly.index[0], hourly.index[-1] + pd.Timedelta(hours=1)
    if start < first or end > last:
        raise ValueError(f"window {start}..{end} is outside the weather data {first}..{last}")

    pad = pd.Timedelta(hours=2)
    hours = hourly.loc[start.floor("h") - pad : end.ceil("h") + pad]
    loc = location(plant)

    # 5-minute clear-sky over the padded hours, and its mean over each [H, H+1)
    fine = pd.date_range(hours.index[0], hours.index[-1] + pd.Timedelta(minutes=55), freq=FREQ)
    cs_fine = loc.get_clearsky(fine, model="ineichen")
    cs_hour = cs_fine.resample("1h", label="left", closed="left").mean()

    grid = pd.date_range(start, end, freq=FREQ, inclusive="left", name="ts")
    solpos = loc.get_solarposition(grid)
    cs = cs_fine.reindex(grid)
    cos_z = np.cos(np.radians(solpos["zenith"])).clip(lower=0.0)

    kt_ghi = _to_grid(_clearsky_index(hours["ghi_wm2"], cs_hour["ghi"]), grid).fillna(0.0)
    kt_dni = _to_grid(_clearsky_index(hours["dni_wm2"], cs_hour["dni"]), grid).fillna(0.0)
    ghi = kt_ghi * cs["ghi"]
    dni = kt_dni * cs["dni"]
    # Keep closure: beam on the horizontal cannot exceed global
    beam_h = np.minimum(dni * cos_z, ghi)
    dni = (beam_h / cos_z.where(cos_z > 0.01)).fillna(0.0)
    dhi = (ghi - beam_h).clip(lower=0.0)

    return pd.DataFrame(
        {
            "ghi_wm2": ghi,
            "dni_wm2": dni,
            "dhi_wm2": dhi,
            "ambient_c": _to_grid(hours["ambient_c"], grid),
            "wind_ms": _to_grid(hours["wind_ms"], grid).clip(lower=0.0),
            "clearsky_ghi_wm2": cs["ghi"],
            "clearsky_dni_wm2": cs["dni"],
            "apparent_zenith": solpos["apparent_zenith"],
            "zenith": solpos["zenith"],
            "azimuth": solpos["azimuth"],
            "dni_extra": pvlib.irradiance.get_extra_radiation(grid),
        },
        index=grid,
    )[COLUMNS]


def clear_days(n: int = 5, plant: PlantConfig | None = None, hourly: pd.DataFrame | None = None) -> list[date]:
    """The n clearest days in the weather year (high and steady clear-sky index)."""
    hourly = load_hourly() if hourly is None else hourly
    loc = location(plant)
    cs = loc.get_clearsky(hourly.index + pd.Timedelta(minutes=30), model="ineichen")["ghi"].to_numpy()
    df = pd.DataFrame({"ghi": hourly["ghi_wm2"].to_numpy(), "cs": cs}, index=hourly.index)
    df = df[df["cs"] > 200]
    df["kt"] = df["ghi"] / df["cs"]
    daily = df.groupby(df.index.date).agg(ratio=("kt", "mean"), spread=("kt", "std"), hours=("kt", "size"))
    daily = daily[daily["hours"] >= 6]
    score = daily["ratio"] - 2 * daily["spread"]
    return list(score.sort_values(ascending=False).index[:n])
