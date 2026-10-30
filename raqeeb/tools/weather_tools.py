"""Weather, on-site sensor and wind tools. Numbers only; reads only the telemetry store.

- sensor_scaled_weather: THE expected-from-sensor weather of the project: the reference
  irradiance scaled by sensor GHI / reference GHI, with the station's ambient and wind. It keeps
  the reference's beam/diffuse split, so a dust layer on the sensor is an exact uniform scale.
  The tools use it (physics.expected_from_sensor) and so do the fault injector's LOW_PR family of
  alarms, so the alarm feed and the agent's numbers agree. (Splitting sensor GHI with pvlib erbs
  instead was tried: erbs puts ~17% of midday GHI in diffuse vs NASA's ~32%, expected runs 2-10%
  high on trackers, and healthy days raised symptom alarms in 43 of 52 runs.)
- sensor_flagged: from the commands table (flag_sensor_use_reference).
- sensor_check: the dusty-sensor test. The reference is not ground truth (per-run bias sd ~2%,
  slow error sd ~2.5%, max 8%), so a single sensor/reference ratio is not enough. It combines
  (1) sensor vs reference on clear steps, (2) clear-sky shape: the sky is clear per the
  reference and the sensor follows it at a steady scale, as a dust layer would, (3) a step down
  in the ratio inside the window, (4) peers: the inverters' DC vs expected from the reference
  says whether the panels see the sensor's loss too.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from raqeeb.contracts import VERDICT_INCONCLUSIVE, VERDICT_OK, ActionType, InverterStatus, PlantConfig, ToolResult
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.common import get_plant, local_time, window
from raqeeb.twin import plant as physics
from raqeeb.twin.weather import location

DAYLIGHT_CLEARSKY_WM2 = 200.0  # steps used for irradiance comparisons
CLEAR_KT_MIN = 0.80  # reference GHI / clear-sky GHI at or above this ...
CLEAR_KT_ROLL_SD = 0.03  # ... and steady (rolling sd over CLEAR_ROLL_STEPS) = clear sky
CLEAR_ROLL_STEPS = 7  # 35 min, centred
MIN_CLEAR_STEPS = 6  # 30 min of clear sky needed for a verdict
SMOOTH_STEPS = 5  # rolling median of sensor/reference before looking for the low period

SENSOR_LOW_RATIO = 0.90  # sensor/reference at or below this: beyond the reference's 8% max error
SENSOR_CLEAR_KT_MAX = 0.90  # a clean sensor under a clear sky reads >= ~1.0x pvlib Ineichen here
SENSOR_STEP_DROP = 0.08  # ratio fell this much vs the clear steps before (reference bias cancels)
SENSOR_SHAPE_CV_MAX = 0.05  # sensor/reference steady: uniform scaling, not clouds or shading
PEERS_AGREE_TOL = 0.05  # |peers - sensor| (both vs reference) below this: panels see the same loss
PEERS_DISAGREE_TOL = 0.10  # above this: the panels do not see the sensor's loss
UNCLIPPED_FRACTION = 0.95  # peers compared only where expected AC is below this share of rating

GEOMETRY = ["apparent_zenith", "zenith", "azimuth"]
IRRADIANCE = ["ghi_wm2", "dni_wm2", "dhi_wm2"]
PREPARED_POA = ["poa_track_wm2", "poa_stow_wm2"]  # columns added by twin.plant.prepare()


def solar_geometry(index: pd.DatetimeIndex, plant: PlantConfig | None = None) -> pd.DataFrame:
    return location(get_plant(plant)).get_solarposition(index)[GEOMETRY]


def sensor_scaled_weather(ghi_sensor_wm2: pd.Series, ambient_c: pd.Series, wind_ms: pd.Series,
                          reference: pd.DataFrame) -> pd.DataFrame:
    """Weather frame as the on-site sensor sees it, for twin.plant.expected_power().

    `reference` is a reference weather frame with GEOMETRY (reference_weather(), or a
    weather_5min frame). Its irradiance is scaled by sensor GHI / reference GHI (0 where the
    reference GHI is 0). If it is a prepared frame (twin.plant.prepare()), its plane-of-array
    columns are scaled too: POA is linear in the irradiance components, so this is the same
    result without recomputing the tracker geometry (the fault injector calls it every step).
    """
    ref = reference.reindex(ghi_sensor_wm2.index)
    scale = (ghi_sensor_wm2 / ref["ghi_wm2"]).where(ref["ghi_wm2"] > 0, 0.0)
    frame = ref[GEOMETRY + [c for c in ("tracker_theta",) if c in ref]].copy()
    for column in IRRADIANCE + [c for c in PREPARED_POA if c in ref]:
        frame[column] = ref[column] * scale
    frame["ambient_c"] = ambient_c
    frame["wind_ms"] = wind_ms
    return frame


def reference_weather(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> pd.DataFrame:
    """weather_reference rows + solar geometry, in the column names of twin.weather.weather_5min()."""
    ref = store.weather_reference(start, end)
    return ref.join(solar_geometry(ref.index, plant))


def sensor_weather(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                   plant: PlantConfig | None = None) -> pd.DataFrame:
    """sensor_scaled_weather() on stored station rows, plus clear-sky GHI. Only steps the station
    reported (a station dropout stays missing)."""
    ws = store.weather_station(start, end)
    frame = sensor_scaled_weather(ws["ghi_sensor_wm2"], ws["ambient_c"], ws["wind_ms"],
                                  reference_weather(store, start, end, plant))
    frame["clearsky_ghi_wm2"] = store.weather_reference(start, end)["clearsky_ghi_wm2"].reindex(ws.index)
    return frame


def sensor_flagged(store: TelemetryStore, index: pd.DatetimeIndex, plant: PlantConfig | None = None) -> pd.Series:
    """True at each step after the last flag_sensor_use_reference command set the flag on.

    The flag applies from the command's timestamp onward.
    """
    station = get_plant(plant).weather_station_id
    cmds = store.commands(device_id=station, action=ActionType.FLAG_SENSOR_USE_REFERENCE.value)
    flags = pd.Series([bool(p.get("flagged", True)) for p in cmds["params"]], index=cmds.index, dtype=bool)
    flags = flags[~flags.index.duplicated(keep="last")]
    if flags.empty:
        return pd.Series(False, index=index)
    return flags.reindex(flags.index.union(index)).ffill().fillna(False).astype(bool).reindex(index)


def wind_series(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> pd.Series:
    """Wind from the station; the reference where the station has no row."""
    wind = store.weather_reference(start, end)["wind_ms"].copy()
    station = store.weather_station(start, end)["wind_ms"]
    wind.loc[station.index.intersection(wind.index)] = station
    return wind


def wind_summary(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> ToolResult:
    """Wind vs the tracker stow limit and the high-wind alarm level.

    Verdicts: "stow_wind" (trackers should be stowed), "high_wind", ok, inconclusive (no data).
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    wind = wind_series(store, start, end)
    stow, alarm = plant.tracker.stow_wind_ms, plant.high_wind_alarm_ms
    thresholds = {"stow_wind_ms": stow, "high_wind_alarm_ms": alarm}
    if wind.empty:
        return ToolResult(tool="wind_summary", verdict=VERDICT_INCONCLUSIVE, start=start, end=end,
                          thresholds=thresholds, evidence="no wind data in the window")
    above_stow = wind[wind > stow]
    above_alarm = wind[wind > alarm]
    values = {
        "steps": len(wind),
        "max_wind_ms": round(float(wind.max()), 2),
        "max_at": wind.idxmax().to_pydatetime(),
        "steps_above_stow": len(above_stow),
        "first_above_stow": above_stow.index[0].to_pydatetime() if len(above_stow) else None,
        "last_above_stow": above_stow.index[-1].to_pydatetime() if len(above_stow) else None,
        "steps_above_high_wind_alarm": len(above_alarm),
        "first_above_high_wind_alarm": above_alarm.index[0].to_pydatetime() if len(above_alarm) else None,
    }
    verdict = "stow_wind" if len(above_stow) else "high_wind" if len(above_alarm) else VERDICT_OK
    evidence = f"max wind {values['max_wind_ms']:.1f} m/s at {local_time(values['max_at'])}"
    if len(above_stow):
        evidence += (f"; above the {stow:.0f} m/s stow limit for {len(above_stow) * 5} min from "
                     f"{local_time(values['first_above_stow'])} (tracker stow is normal then)")
    return ToolResult(tool="wind_summary", verdict=verdict, devices=[plant.weather_station_id] if verdict != VERDICT_OK
                      else [], start=start, end=end, values=values, thresholds=thresholds, evidence=evidence)


def clear_steps(ref: pd.DataFrame) -> pd.Series:
    """Daylight steps where the reference follows the clear-sky curve (high, steady kt)."""
    daylight = ref["clearsky_ghi_wm2"] >= DAYLIGHT_CLEARSKY_WM2
    kt = (ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).where(daylight)
    steady = kt.rolling(CLEAR_ROLL_STEPS, center=True, min_periods=3).std() <= CLEAR_KT_ROLL_SD
    return daylight & (kt >= CLEAR_KT_MIN) & steady


def _peers_vs_reference(store: TelemetryStore, ref_weather: pd.DataFrame, steps: pd.Index,
                        plant: PlantConfig) -> pd.Series:
    """Per step: median over RUNNING, unclipped inverters of measured DC / expected DC (reference)."""
    if len(steps) == 0:
        return pd.Series(dtype=float)
    expected = physics.expected_power(ref_weather.loc[steps], plant)
    exp_dc = expected.xs("dc_kw", axis=1, level="quantity")
    exp_ac = expected.xs("ac_kw", axis=1, level="quantity")
    start, end = steps[0], steps[-1] + pd.Timedelta(minutes=5)
    dc = store.inverter_wide("dc_kw", start, end).reindex(index=steps, columns=exp_dc.columns)
    status = store.inverter_wide("status", start, end).reindex(index=steps, columns=exp_dc.columns)
    usable = (status == InverterStatus.RUNNING.value) & (exp_ac < UNCLIPPED_FRACTION * plant.inverters.ac_kw)
    return (dc / exp_dc).where(usable).median(axis=1).dropna()


SENSOR_THRESHOLDS = {
    "sensor_low_ratio": SENSOR_LOW_RATIO, "sensor_clear_kt_max": SENSOR_CLEAR_KT_MAX,
    "sensor_step_drop": SENSOR_STEP_DROP, "shape_cv_max": SENSOR_SHAPE_CV_MAX,
    "peers_agree_tol": PEERS_AGREE_TOL, "peers_disagree_tol": PEERS_DISAGREE_TOL,
    "clear_kt_min": CLEAR_KT_MIN, "min_clear_steps": MIN_CLEAR_STEPS,
}


def sensor_check(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> ToolResult:
    """Is the on-site irradiance sensor reading low (dusty)? See the module docstring.

    A finding needs: sensor/reference <= SENSOR_LOW_RATIO on clear steps, a steady deficit, and
    at least one more vote (sensor below clear-sky level, a step drop, or peers disagreeing with
    the sensor). Verdicts: "sensor_low" (the panels do not share the loss), "sensor_low_panels_too"
    (they do: the trap, where performance ratio vs the sensor looks normal), ok, inconclusive.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    station = plant.weather_station_id
    ref_weather = reference_weather(store, start, end, plant)
    ghi_sensor = store.weather_station(start, end)["ghi_sensor_wm2"]
    clear = clear_steps(ref_weather).reindex(ghi_sensor.index, fill_value=False)
    steps = ghi_sensor.index[clear.to_numpy()]
    flagged = bool(sensor_flagged(store, ghi_sensor.index[-1:], plant).any()) if len(ghi_sensor) else False
    base = {"tool": "sensor_check", "start": start, "end": end, "thresholds": SENSOR_THRESHOLDS}
    if len(steps) < MIN_CLEAR_STEPS:
        return ToolResult(**base, verdict=VERDICT_INCONCLUSIVE,
                          values={"clear_steps": len(steps), "flagged": flagged},
                          evidence=f"only {len(steps)} clear-sky steps (need {MIN_CLEAR_STEPS}); "
                                   "the sensor cannot be judged against the reference")

    ratio_all = ghi_sensor.loc[steps] / ref_weather.loc[steps, "ghi_wm2"]
    # Current low period: the last run of low (smoothed) ratio that reaches the end of the window
    smooth = ratio_all.rolling(SMOOTH_STEPS, center=True, min_periods=1).median()
    low = (smooth <= SENSOR_LOW_RATIO).to_numpy()
    onset = None
    if low[-1]:
        first = len(low) - int(np.argmin(low[::-1])) if not low.all() else 0
        if len(low) - first >= MIN_CLEAR_STEPS:
            onset = steps[first]
    if onset is None:  # not below the reference's error band: look for a sustained step drop instead
        onset = _drop_onset(ratio_all, smooth, steps)
    period = steps[steps >= onset] if onset is not None else steps
    before = steps[steps < onset] if onset is not None else steps[:0]

    ref = ref_weather.loc[period]
    ratio = ratio_all.loc[period]
    sensor_kt = ghi_sensor.loc[period] / ref["clearsky_ghi_wm2"]
    vs_ref = float(ratio.median())
    shape_cv = float(ratio.std() / ratio.mean()) if len(period) > 1 else 0.0
    # Step drop vs the clear steps before the onset (the reference's slow bias mostly cancels)
    drop = float(ratio_all.loc[before].median() - vs_ref) if len(before) >= MIN_CLEAR_STEPS else None

    peers = _peers_vs_reference(store, ref_weather, period, plant)
    peers_vs_ref = float(peers.median()) if len(peers) else None
    gap = None if peers_vs_ref is None else peers_vs_ref - vs_ref

    votes = {
        "below_reference": vs_ref <= SENSOR_LOW_RATIO,
        "below_clear_sky": float(sensor_kt.median()) <= SENSOR_CLEAR_KT_MAX,
        "step_drop": drop is not None and drop >= SENSOR_STEP_DROP,
        "peers_disagree": gap is not None and gap >= PEERS_DISAGREE_TOL,
    }
    shape_ok = shape_cv <= SENSOR_SHAPE_CV_MAX
    support = votes["below_clear_sky"] or votes["step_drop"] or votes["peers_disagree"]
    # A steady step drop vs the clear steps before it is enough on its own: the reference's slow bias
    # cancels in it, so a lightly dusted sensor is caught even when the reference itself reads low
    suspect = shape_ok and ((votes["below_reference"] and support) or votes["step_drop"])
    panels_share = gap is not None and abs(gap) < PEERS_AGREE_TOL

    since = f" since {local_time(onset)}" if onset is not None else ""
    parts = [f"sensor reads {vs_ref:.0%} of the reference on {len(period)} clear-sky steps{since}",
             f"{sensor_kt.median():.0%} of the clear-sky model (deficit spread {shape_cv:.1%})"]
    if drop is not None:
        parts.append(f"down {drop:.0%} from before")
    if peers_vs_ref is not None:
        parts.append(f"inverter DC is {peers_vs_ref:.0%} of expected from the reference")
    if suspect:
        verdict = "sensor_low_panels_too" if panels_share else "sensor_low"
        parts.append("the panels lose about the same, so a performance ratio on this sensor hides the loss"
                     if panels_share else "the panels do not see this loss")
    elif votes["below_reference"]:
        verdict = VERDICT_INCONCLUSIVE
        parts.append("the deficit is not steady (clouds or shading?)" if not shape_ok
                     else "no clear-sky, step or peer evidence backs it")
    else:
        verdict = VERDICT_OK
    values = {
        "clear_steps": len(steps),
        "flagged": flagged,
        "sensor_vs_reference": round(vs_ref, 4),
        "sensor_kt_clear": round(float(sensor_kt.median()), 4),
        "reference_kt_clear": round(float((ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).median()), 4),
        "shape_cv": round(shape_cv, 4),
        "ratio_drop": None if drop is None else round(drop, 4),
        "peers_vs_reference": None if peers_vs_ref is None else round(peers_vs_ref, 4),
        "panels_share_loss": bool(suspect and panels_share),
        "onset": None if onset is None else onset.to_pydatetime(),
        "votes": votes,
    }
    return ToolResult(**base, verdict=verdict, devices=[station] if suspect else [], values=values,
                      evidence="; ".join(parts))


def _drop_onset(ratio: pd.Series, smooth: pd.Series, steps: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First clear step from which the smoothed sensor/reference ratio stays SENSOR_STEP_DROP or more
    below the median of the clear steps before it, to the end of the window (MIN_CLEAR_STEPS each side)."""
    values = smooth.to_numpy()
    for k in range(MIN_CLEAR_STEPS, len(values) - MIN_CLEAR_STEPS + 1):
        before = float(ratio.iloc[:k].median())
        if (values[k:] <= before - SENSOR_STEP_DROP).all():
            return steps[k]
    return None


def irradiance_ratios(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> pd.DataFrame:
    """Per step: sensor/reference, sensor/clear-sky, reference/clear-sky, clear flag (for charts)."""
    ref = reference_weather(store, start, end, plant)
    ghi = store.weather_station(start, end)["ghi_sensor_wm2"].reindex(ref.index)
    daylight = ref["clearsky_ghi_wm2"] >= DAYLIGHT_CLEARSKY_WM2
    return pd.DataFrame(
        {
            "sensor_vs_reference": (ghi / ref["ghi_wm2"]).where(daylight),
            "sensor_kt": (ghi / ref["clearsky_ghi_wm2"]).where(daylight),
            "reference_kt": (ref["ghi_wm2"] / ref["clearsky_ghi_wm2"]).where(daylight),
            "clear": clear_steps(ref),
        },
        index=ref.index,
    ).replace([np.inf, -np.inf], np.nan)
