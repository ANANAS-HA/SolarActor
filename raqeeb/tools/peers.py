"""Peer comparisons between inverters. Numbers only; reads only the telemetry store.

Peers see the same sky, so one inverter far from the peer median points at that device, and
all of them moving together points at the weather, the panels plant-wide, or the grid.
Peers differ by up to +/-2% (unit variation) plus 0.3% measurement noise when healthy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from raqeeb.contracts import PLANT_TARGET, VERDICT_INCONCLUSIVE, VERDICT_OK, InverterStatus, PlantConfig, ToolResult
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.common import STEP, get_plant, local_time, window
from raqeeb.tools.metrics import DAYLIGHT_FRACTION, loss_onset, runs

PEER_LOW_RATIO = 0.93  # below peer median by more than unit variation + noise
PEER_MIN_STEPS = 6  # an outlier must last 30 min
FLAT_STEPS = 6  # window for flatness (30 min)
FLAT_TOL = 0.02  # (max - min) / mean over the window at or below this = flat
FLAT_PEER_MOVE = 0.02  # peers' relative range must exceed the device's by this much
FLAT_BELOW_PEERS = 0.95  # a device cap sits below the peer median by at least 5%
NOT_CLIPPING = 0.97  # flat below this share of the AC rating (flat at the rating = clipping)
PLANT_FLAT_SHARE = 0.75  # share of reporting inverters flat together = plant-wide cap
PLANT_FLAT_SPREAD = 0.03  # their levels within 3% of each other
PLANT_PRODUCING_MIN = 0.5  # the producing units judged for a plant cap must be at least this share of the reporting ones
HOT_EXCESS_C = 8.0  # internal temperature this far above the peer median = hot

FLAT_THRESHOLDS = {"flat_steps": FLAT_STEPS, "flat_tol": FLAT_TOL, "peer_move": FLAT_PEER_MOVE,
                   "below_peers": FLAT_BELOW_PEERS, "not_clipping": NOT_CLIPPING}


def peer_ratio(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
               plant: PlantConfig | None = None, quantity: str = "ac_kw") -> pd.DataFrame:
    """Each inverter / median of reporting inverters, per production step (NaN = no row)."""
    plant = get_plant(plant)
    wide = store.inverter_wide(quantity, start, end).astype(float)
    median = wide.median(axis=1)
    production = median > DAYLIGHT_FRACTION * plant.inverters.ac_kw
    return wide.div(median, axis=0).loc[production]


def peer_outliers(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                  plant: PlantConfig | None = None, quantity: str = "ac_kw") -> ToolResult:
    """Per inverter: how far below its peers it ran, since when, and what status it reported.

    Verdicts: "peer_outlier" (devices = inverters below their peers), ok, inconclusive (no daylight).
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    ratios = peer_ratio(store, start, end, plant, quantity)
    status = store.inverter_wide("status", start, end)
    per_inverter = {}
    for inv_id in ratios.columns:
        r = ratios[inv_id].dropna()
        onset = loss_onset(r, PEER_LOW_RATIO, PEER_MIN_STEPS)
        low = r[r.index >= onset] if onset is not None else r.iloc[:0]
        per_inverter[inv_id] = {
            "steps": len(r),
            "median_ratio": round(float(r.median()), 4) if len(r) else None,
            "min_ratio": round(float(r.min()), 4) if len(r) else None,
            "low_steps": int((r < PEER_LOW_RATIO).sum()),
            "outlier": onset is not None,
            "onset": None if onset is None else onset.to_pydatetime(),
            "ratio_since_onset": round(float(low.median()), 4) if len(low) else None,
            "status_counts": status[inv_id].dropna().value_counts().to_dict() if inv_id in status else {},
        }
    outliers = [k for k, v in per_inverter.items() if v["outlier"]]
    if ratios.empty:
        verdict, evidence = VERDICT_INCONCLUSIVE, "no production steps to compare peers"
    elif outliers:
        verdict = "peer_outlier"
        evidence = "; ".join(f"{k} at {per_inverter[k]['ratio_since_onset']:.0%} of the peer median ({quantity}) "
                             f"from {local_time(per_inverter[k]['onset'])}" for k in outliers)
    else:
        verdict, evidence = VERDICT_OK, f"all {len(per_inverter)} reporting inverters within {1 - PEER_LOW_RATIO:.0%} of their peers"
    return ToolResult(tool="peer_outliers", verdict=verdict, devices=outliers, start=start, end=end,
                      values={"quantity": quantity, "inverters": per_inverter},
                      thresholds={"peer_low_ratio": PEER_LOW_RATIO, "min_steps": PEER_MIN_STEPS}, evidence=evidence)


def _rel_range(series: pd.Series | pd.DataFrame) -> pd.Series | pd.DataFrame:
    roll = series.rolling(FLAT_STEPS)
    return (roll.max() - roll.min()) / roll.mean()


def _expand(window_end: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Mark every step inside a flagged window (flags are on the window's last step)."""
    return window_end.iloc[::-1].rolling(FLAT_STEPS, min_periods=1).max().iloc[::-1].fillna(0).astype(bool)


def flat_caps(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
              plant: PlantConfig | None = None) -> ToolResult:
    """Single-device flat caps: output flat below rating and below peers while peers move.

    This is the shape of a thermal derate (or a power limit). Clipping (flat at the rating)
    and a plant-wide cap (all flat together, see plant_flat_cap) are not reported here.
    Verdicts: "device_flat_cap" (devices = capped inverters), ok. values["caps"] lists each cap.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    ac = store.inverter_wide("ac_kw", start, end).astype(float)
    caps = []
    if not ac.empty:
        median = ac.median(axis=1)
        status = store.inverter_wide("status", start, end)
        device_range = _rel_range(ac)
        peer_range = _rel_range(median)
        window_mean = ac.rolling(FLAT_STEPS).mean()
        peer_mean = median.rolling(FLAT_STEPS).mean()
        flagged = (
            (device_range <= FLAT_TOL)
            & (device_range.rsub(peer_range, axis=0) >= FLAT_PEER_MOVE)
            & window_mean.lt(FLAT_BELOW_PEERS * peer_mean, axis=0)
            & (window_mean < NOT_CLIPPING * plant.inverters.ac_kw)
        )
        flagged.loc[(peer_mean <= DAYLIGHT_FRACTION * plant.inverters.ac_kw).to_numpy()] = False
        marked = _expand(flagged)
        for inv_id in ac.columns:
            # Join pieces of one cap split where the peers were flat too (around noon): the device
            # stayed at the same level through the gap
            merged: list[tuple[pd.Timestamp, pd.Timestamp]] = []
            for c_start, c_end, _ in runs(marked[inv_id]):
                if merged:
                    level = ac.loc[merged[-1][0]:merged[-1][1] - STEP, inv_id].median()
                    between = ac.loc[merged[-1][1]:c_start - STEP, inv_id]
                    if between.notna().all() and ((between / level - 1).abs() <= FLAT_TOL).all():
                        merged[-1] = (merged[-1][0], c_end)
                        continue
                merged.append((c_start, c_end))
            for c_start, c_end in merged:
                part = slice(c_start, c_end - STEP)
                level = ac.loc[part, inv_id]
                caps.append({
                    "inverter_id": inv_id,
                    "start": c_start.to_pydatetime(),
                    "end": c_end.to_pydatetime(),
                    "steps": int(level.notna().sum()),
                    "level_kw": round(float(level.median()), 1),
                    "level_sd_kw": round(float(level.std()), 1),
                    "peer_median_kw": round(float(median.loc[part].median()), 1),
                    "ratio_to_peers": round(float((level / median.loc[part]).median()), 4),
                    "derated_status_steps": int((status.loc[part, inv_id] == InverterStatus.DERATED.value).sum()),
                })
    devices = sorted({c["inverter_id"] for c in caps})
    evidence = "; ".join(
        f"{c['inverter_id']} flat at {c['level_kw']:.0f} kW ({c['ratio_to_peers']:.0%} of peers) from "
        f"{local_time(c['start'])} to {local_time(c['end'])} while peers moved"
        + (f", reporting DERATED for {c['derated_status_steps']} steps" if c["derated_status_steps"] else "")
        for c in caps) if caps else "no single-inverter flat cap"
    return ToolResult(tool="flat_caps", verdict="device_flat_cap" if caps else VERDICT_OK, devices=devices,
                      start=start, end=end, values={"caps": caps}, thresholds=FLAT_THRESHOLDS, evidence=evidence)


def plant_flat_cap(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                   plant: PlantConfig | None = None) -> ToolResult:
    """Periods when most inverters are flat together at the same level below their rating.
    Only producing units are judged: a unit at zero (stopped, tripped) or with no data neither breaks nor
    makes a cap, so a grid cap is still found while one unit is stopped.

    Verdicts: "plant_flat_cap_grid_limit" (a grid limit was present: curtailment, expected
    behaviour), "plant_flat_cap" (no grid limit: unexplained), ok. devices = ALL when found.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    thresholds = FLAT_THRESHOLDS | {"plant_flat_share": PLANT_FLAT_SHARE, "plant_flat_spread": PLANT_FLAT_SPREAD,
                                    "plant_producing_min": PLANT_PRODUCING_MIN}
    ac = store.inverter_wide("ac_kw", start, end).astype(float)
    periods = []
    if not ac.empty:
        window_mean = ac.rolling(FLAT_STEPS).mean()
        producing = window_mean > DAYLIGHT_FRACTION * plant.inverters.ac_kw
        flat = (_rel_range(ac) <= FLAT_TOL) & (window_mean < NOT_CLIPPING * plant.inverters.ac_kw) & producing
        n_producing = producing.sum(axis=1)
        levels = window_mean.where(producing)
        level_spread = (levels.max(axis=1) - levels.min(axis=1)) / levels.median(axis=1)
        together = ((flat.sum(axis=1) >= PLANT_FLAT_SHARE * n_producing) & (level_spread <= PLANT_FLAT_SPREAD)
                    & (n_producing > 0) & (n_producing >= PLANT_PRODUCING_MIN * ac.notna().sum(axis=1)))
        limits = store.grid_setpoint(start, end)["limit_kw"]
        status = store.inverter_wide("status", start, end)
        for c_start, c_end, steps in runs(_expand(together)):
            part = slice(c_start, c_end - STEP)
            in_limit = limits.loc[part]
            periods.append({
                "start": c_start.to_pydatetime(),
                "end": c_end.to_pydatetime(),
                "steps": steps,
                "level_kw": round(float(ac.loc[part].where(producing.loc[part]).median(axis=1).median()), 1),
                "plant_kw": round(float(ac.loc[part].sum(axis=1).median()), 1),
                "grid_limit_steps": len(in_limit),
                "grid_limit_kw": float(in_limit.iloc[0]) if len(in_limit) else None,
                "curtailed_status_share": round(float((status.loc[part] == InverterStatus.CURTAILED.value)
                                                      .mean().mean()), 3),
            })
    if not periods:
        verdict = VERDICT_OK
    elif all(p["grid_limit_steps"] >= 0.8 * p["steps"] for p in periods):
        verdict = "plant_flat_cap_grid_limit"
    else:
        verdict = "plant_flat_cap"
    evidence = "; ".join(
        f"producing inverters flat together at {p['level_kw']:.0f} kW (plant {p['plant_kw'] / 1000:.1f} MW) from "
        f"{local_time(p['start'])} to {local_time(p['end'])}, "
        + (f"grid limit {p['grid_limit_kw'] / 1000:.1f} MW active" if p["grid_limit_kw"] is not None else "no grid limit")
        for p in periods) if periods else "no plant-wide flat cap"
    return ToolResult(tool="plant_flat_cap", verdict=verdict, devices=[PLANT_TARGET] if periods else [],
                      start=start, end=end, values={"periods": periods}, thresholds=thresholds, evidence=evidence)


def temperature_check(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None) -> ToolResult:
    """Per inverter: internal temperature vs peers and vs the derate threshold.

    Verdicts: "hot" (devices = hot inverters), ok, inconclusive (no readings).
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    temp = store.inverter_wide("temp_c", start, end).astype(float)
    excess = temp.sub(temp.median(axis=1), axis=0)
    limit = plant.inverters.derate_temp_c
    per_inverter = {}
    for inv_id in temp.columns:
        t, x = temp[inv_id].dropna(), excess[inv_id].dropna()
        above = t[t >= limit]
        hot = x[x >= HOT_EXCESS_C]
        per_inverter[inv_id] = {
            "max_temp_c": round(float(t.max()), 1) if len(t) else None,
            "max_excess_c": round(float(x.max()), 1) if len(x) else None,
            "steps_above_derate": len(above),
            "first_above_derate": above.index[0].to_pydatetime() if len(above) else None,
            "first_hot": hot.index[0].to_pydatetime() if len(hot) else None,
            "hot": bool(len(above) > 0 or len(hot) >= PEER_MIN_STEPS),
        }
    hot = [k for k, v in per_inverter.items() if v["hot"]]
    if temp.empty:
        verdict, evidence = VERDICT_INCONCLUSIVE, "no inverter temperature readings"
    elif hot:
        verdict = "hot"
        evidence = "; ".join(
            f"{k} up to {per_inverter[k]['max_temp_c']:.0f} C ({per_inverter[k]['max_excess_c']:.0f} C above peers)"
            + (f", above the {limit:.0f} C derate limit from {local_time(per_inverter[k]['first_above_derate'])}"
               if per_inverter[k]["first_above_derate"] else "")
            for k in hot)
    else:
        verdict, evidence = VERDICT_OK, f"no inverter more than {HOT_EXCESS_C:.0f} C above its peers or above {limit:.0f} C"
    return ToolResult(tool="temperature_check", verdict=verdict, devices=hot, start=start, end=end,
                      values={"inverters": per_inverter},
                      thresholds={"hot_excess_c": HOT_EXCESS_C, "derate_temp_c": limit, "min_steps": PEER_MIN_STEPS},
                      evidence=evidence)
