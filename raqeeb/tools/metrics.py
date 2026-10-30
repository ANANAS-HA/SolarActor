"""Measured vs expected: performance, energy, data gaps, curtailment. Numbers only; reads only the store.

Ratios use daylight steps (expected above DAYLIGHT_FRACTION of rating) outside curtailment.
"Unclipped" ratios also drop steps where expected output is at the inverter AC limit: there,
clipping hides part of a loss (20% soiling is only ~-16% energy on a clear July day).

A data gap is missing rows, never zeros. Each gap is checked against the meter: if the meter
minus the reported inverters still carries about one peer's output per missing device, the
device kept producing and only its data is lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

from raqeeb.contracts import (
    PLANT_TARGET,
    STEP_MINUTES,
    VERDICT_INCONCLUSIVE,
    VERDICT_OK,
    InverterStatus,
    PlantConfig,
    ToolResult,
)
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.common import STEP, get_plant, local_time, window
from raqeeb.tools.physics import Basis, expected_inverter, expected_plant_ac
from raqeeb.tools.weather_tools import UNCLIPPED_FRACTION, sensor_flagged

DAYLIGHT_FRACTION = 0.05  # expected output above this share of rating = a production step
PLANT_LOW_RATIO = 0.90  # plant vs reference at or below this: beyond reference error (max 8%)
ONSET_SMOOTH_STEPS = 5  # rolling median before looking for a loss onset
ONSET_MIN_STEPS = 6  # a loss must last 30 min of usable steps
PRODUCING_FRACTION = 0.8  # meter residual >= this share of peer output: missing device still produces
CURTAIL_AT_LIMIT_TOL = 0.02  # meter within 2% of (or under) the grid limit
CURTAIL_AT_LIMIT_SHARE = 0.8  # share of binding steps the meter must sit at the limit
CURTAILED_SHARE = 0.75  # this share of reporting inverters in CURTAILED status = a step under an export limit


def energy_kwh(power_kw: pd.Series) -> float:
    """Energy of a 5-minute power series (missing steps count as nothing, not zero power)."""
    return float(power_kw.dropna().sum() * STEP_MINUTES / 60.0)


def runs(mask: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Contiguous True runs on a regular 5-minute index: (start, end exclusive, steps)."""
    out = []
    values = mask.to_numpy(dtype=bool)
    index = mask.index
    k = 0
    while k < len(values):
        if values[k]:
            j = k
            while j + 1 < len(values) and values[j + 1] and index[j + 1] - index[j] == STEP:
                j += 1
            out.append((index[k], index[j] + STEP, j - k + 1))
            k = j + 1
        else:
            k += 1
    return out


def loss_onset(ratio: pd.Series, threshold: float, min_steps: int = ONSET_MIN_STEPS) -> pd.Timestamp | None:
    """First step from which the smoothed ratio stays <= threshold for min_steps usable steps.

    `ratio` may have gaps (e.g. clipped midday steps removed): steps are counted, not minutes.
    """
    ratio = ratio.dropna()
    smooth = ratio.rolling(ONSET_SMOOTH_STEPS, center=True, min_periods=1).median()
    low = (smooth <= threshold).to_numpy()
    for k in range(len(low) - min_steps + 1):
        if low[k : k + min_steps].all():
            return ratio.index[k]
    return None


def _curtailed(store: TelemetryStore, index: pd.DatetimeIndex) -> pd.Series:
    """Steps under an export limit: a grid setpoint row, or most reporting inverters themselves in CURTAILED
    status (a limit the plant controller applies without a setpoint row still shows in their status)."""
    if len(index) == 0:
        return pd.Series(False, index=index)
    limits = store.grid_setpoint(index[0], index[-1] + STEP)
    status = store.inverter_wide("status", index[0], index[-1] + STEP).reindex(index)
    held = (status == InverterStatus.CURTAILED.value).sum(axis=1) >= CURTAILED_SHARE * status.notna().sum(axis=1).clip(lower=1)
    return pd.Series(index.isin(limits.index), index=index) | held.fillna(False)


def performance_series(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                       plant: PlantConfig | None = None, basis: Basis = "reference",
                       exclude: Sequence[str] | None = None) -> pd.DataFrame:
    """Per meter step: meter kW, expected kW, ratio, and the usable / clipped / curtailed flags.

    With `exclude` (a list, possibly empty) the fleet is measured from the inverter readings instead
    of the meter: per step, the inverters that reported and are not excluded, against their own
    expected. A known device loss (stopped, derated, silent) then does not count as a plant-wide loss.
    Daylight and clipping flags stay those of the whole plant.
    """
    plant = get_plant(plant)
    meter = store.meter(start, end)["plant_ac_kw"]
    exp = expected_plant_ac(store, start, end, plant, basis).reindex(meter.index)
    daylight = exp > DAYLIGHT_FRACTION * plant.rated_ac_kw
    curtailed = _curtailed(store, meter.index)
    clipped = exp >= UNCLIPPED_FRACTION * plant.rated_ac_kw
    if exclude is not None:
        exp_inv = expected_inverter(store, "ac_kw", start, end, plant, basis).reindex(meter.index)
        measured = store.inverter_wide("ac_kw", start, end).reindex(index=meter.index, columns=exp_inv.columns)
        keep = [c for c in exp_inv.columns if c not in set(exclude)]
        present = measured[keep].notna()
        meter = measured[keep].where(present).sum(axis=1, min_count=1)
        exp = exp_inv[keep].where(present).sum(axis=1)
        daylight &= present.any(axis=1)
    usable = daylight & ~curtailed
    return pd.DataFrame(
        {
            "meter_kw": meter,
            "expected_kw": exp,
            "ratio": (meter / exp).where(usable),
            "usable": usable,
            "clipped": clipped,
            "curtailed": curtailed,
        },
        index=meter.index,
    )


def _ratio_summary(perf: pd.DataFrame) -> dict[str, Any]:
    usable = perf[perf["usable"]]
    unclipped = usable[~usable["clipped"]]

    def ratio(df):
        return round(float(df["meter_kw"].sum() / df["expected_kw"].sum()), 4) if len(df) else None

    return {
        "ratio": ratio(usable),
        "ratio_unclipped": ratio(unclipped),
        "expected_kwh": round(energy_kwh(usable["expected_kw"]), 1),
        "shortfall_kwh": round(energy_kwh(usable["expected_kw"] - usable["meter_kw"]), 1),
        "usable_steps": len(usable),
        "unclipped_steps": len(unclipped),
    }


def plant_performance(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                      plant: PlantConfig | None = None, exclude: Sequence[str] | None = None) -> ToolResult:
    """Meter vs expected from the trusted reference and from the on-site sensor.

    `vs_sensor` is the plant's usual health check (performance ratio on the sensor). When the
    sensor is dusty it can look normal, even above 1, while `vs_reference` shows the real loss.
    With `exclude`, the fleet is the reporting inverters minus those (see performance_series).
    Verdicts: "underperforming" (devices = ALL), ok, inconclusive (no production steps).
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    ref = performance_series(store, start, end, plant, "reference", exclude)
    sensor = performance_series(store, start, end, plant, "sensor", exclude)
    vs_ref, vs_sensor = _ratio_summary(ref), _ratio_summary(sensor)
    unclipped = ref["ratio"].where(~ref["clipped"])
    onset = loss_onset(unclipped, PLANT_LOW_RATIO)
    low = vs_ref["ratio_unclipped"] is not None and vs_ref["ratio_unclipped"] <= PLANT_LOW_RATIO
    values = {
        "meter_kwh": round(energy_kwh(ref.loc[ref["usable"], "meter_kw"]), 1),
        "vs_reference": vs_ref,
        "vs_sensor": vs_sensor,
        "sensor_flagged": bool(sensor_flagged(store, ref.index[-1:], plant).any()) if len(ref) else False,
        "curtailed_steps": int(ref["curtailed"].sum()),
        "onset": None if onset is None else onset.to_pydatetime(),
    }
    thresholds = {"plant_low_ratio": PLANT_LOW_RATIO, "unclipped_fraction": UNCLIPPED_FRACTION,
                  "daylight_fraction": DAYLIGHT_FRACTION, "onset_min_steps": ONSET_MIN_STEPS}
    if vs_ref["ratio"] is None:
        return ToolResult(tool="plant_performance", verdict=VERDICT_INCONCLUSIVE, start=start, end=end,
                          values=values, thresholds=thresholds, evidence="no production steps in the window")
    underperforming = low or onset is not None
    # A short window around noon in summer can be clipped at every step (ratio_unclipped None)
    clipping = (f" ({vs_ref['ratio_unclipped']:.0%} outside clipping)" if vs_ref["ratio_unclipped"] is not None
                else " (every step clipped)")
    evidence = (f"meter {vs_ref['ratio']:.0%} of expected from the reference{clipping}, "
                f"{vs_sensor['ratio']:.0%} of expected from the site sensor"
                if vs_sensor["ratio"] is not None else
                f"meter {vs_ref['ratio']:.0%} of expected from the reference{clipping}; no sensor data")
    if underperforming:
        evidence += f"; shortfall {vs_ref['shortfall_kwh'] / 1000:.1f} MWh"
        if onset is not None:
            evidence += f", below {PLANT_LOW_RATIO:.0%} from {local_time(onset)}"
    if values["curtailed_steps"]:
        evidence += f"; {values['curtailed_steps']} curtailed steps excluded"
    if exclude is not None:
        values["excluded"] = sorted(exclude)
        evidence += (f"; reporting inverters only, without {', '.join(sorted(exclude))} (known device loss)"
                     if exclude else "; reporting inverters only")
    return ToolResult(tool="plant_performance", verdict="underperforming" if underperforming else VERDICT_OK,
                      devices=[PLANT_TARGET] if underperforming else [], start=start, end=end,
                      values=values, thresholds=thresholds, evidence=evidence)


def inverter_performance(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                         plant: PlantConfig | None = None, basis: Basis = "reference") -> ToolResult:
    """Per inverter, over the steps it reported: measured and expected kWh and their ratio.

    Verdicts: "inverters_low" (devices = inverters at or below PLANT_LOW_RATIO outside clipping), ok.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    measured = store.inverter_wide("ac_kw", start, end)
    exp = expected_inverter(store, "ac_kw", start, end, plant, basis)
    measured = measured.reindex(index=exp.index, columns=exp.columns)
    usable = exp > DAYLIGHT_FRACTION * plant.inverters.ac_kw
    usable.loc[_curtailed(store, exp.index).to_numpy()] = False
    unclipped = usable & (exp < UNCLIPPED_FRACTION * plant.inverters.ac_kw)
    rows = {}
    for inv_id in exp.columns:
        present = measured[inv_id].notna()
        u, uc = usable[inv_id] & present, unclipped[inv_id] & present
        m, e = measured[inv_id], exp[inv_id]
        rows[inv_id] = {
            "measured_kwh": round(energy_kwh(m[u]), 1),
            "expected_kwh": round(energy_kwh(e[u]), 1),
            "ratio": round(float(m[u].sum() / e[u].sum()), 4) if u.any() else None,
            "ratio_unclipped": round(float(m[uc].sum() / e[uc].sum()), 4) if uc.any() else None,
            "reported_steps": int(present.sum()),
            "missing_daylight_steps": int((usable[inv_id] & ~present).sum()),
        }
    low = [k for k, r in rows.items() if r["ratio_unclipped"] is not None and r["ratio_unclipped"] <= PLANT_LOW_RATIO]
    evidence = (f"{len(low)} of {len(rows)} inverters at or below {PLANT_LOW_RATIO:.0%} of expected ({basis})"
                + (": " + ", ".join(f"{k} {rows[k]['ratio_unclipped']:.0%}" for k in low) if low else ""))
    return ToolResult(tool="inverter_performance", verdict="inverters_low" if low else VERDICT_OK, devices=low,
                      start=start, end=end, values={"basis": basis, "inverters": rows},
                      thresholds={"plant_low_ratio": PLANT_LOW_RATIO, "unclipped_fraction": UNCLIPPED_FRACTION},
                      evidence=evidence)


def _clock(store: TelemetryStore, start, end) -> pd.DatetimeIndex:
    """Steps the logger ran: every step with a meter or reference row."""
    return store.meter(start, end).index.union(store.weather_reference(start, end).index)


def data_gaps(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
              plant: PlantConfig | None = None) -> ToolResult:
    """Runs of missing rows per device (inverters, weather station, meter), checked against the meter.

    Verdicts: "data_gap" (devices = devices with a gap), ok. values["gaps"] lists each gap.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    thresholds = {"producing_fraction": PRODUCING_FRACTION}
    clock = _clock(store, start, end)
    if len(clock) == 0:
        return ToolResult(tool="data_gaps", verdict=VERDICT_INCONCLUSIVE, start=start, end=end,
                          thresholds=thresholds, evidence="no logger steps in the window")
    ac = store.inverter_wide("ac_kw", start, end).reindex(index=clock, columns=plant.inverters.ids)
    meter = store.meter(start, end)["plant_ac_kw"].reindex(clock)
    n_missing = ac.isna().sum(axis=1)
    peer_median = ac.median(axis=1)
    residual = meter - ac.sum(axis=1)
    producing = (residual / (n_missing * peer_median)).where(
        (n_missing > 0) & (peer_median > DAYLIGHT_FRACTION * plant.inverters.ac_kw))

    gaps = []
    last = clock[-1]
    devices = {inv_id: ac[inv_id].isna() for inv_id in ac.columns}
    devices[plant.weather_station_id] = pd.Series(~clock.isin(store.weather_station(start, end).index), index=clock)
    devices[plant.meter_id] = meter.isna()
    for device_id, missing in devices.items():
        for gap_start, gap_end, steps in runs(missing):
            gap = {
                "device_id": device_id,
                "start": gap_start.to_pydatetime(),
                "end": gap_end.to_pydatetime(),
                "steps": steps,
                "ongoing": gap_end > last,
            }
            if device_id in ac.columns:
                part = slice(gap_start, gap_end - STEP)
                frac = producing.loc[part].dropna()
                gap |= {
                    "meter_residual_kw": round(float(residual.loc[part].median()), 1),
                    "peer_median_kw": round(float(peer_median.loc[part].median()), 1),
                    "producing_fraction": round(float(frac.median()), 3) if len(frac) else None,
                    "still_producing": bool(frac.median() >= PRODUCING_FRACTION) if len(frac) else None,
                }
            gaps.append(gap)
    gaps.sort(key=lambda g: (g["start"], g["device_id"]))

    def describe(g: dict[str, Any]) -> str:
        text = (f"{g['device_id']} sent no rows for {g['steps'] * 5} min from {local_time(g['start'])}"
                + (" (still missing)" if g["ongoing"] else ""))
        if g.get("still_producing") is True:
            text += f"; the meter still carries {g['producing_fraction']:.0%} of a peer's output, so it kept producing"
        elif g.get("still_producing") is False:
            text += f"; the meter carries only {g['producing_fraction']:.0%} of a peer's output, so it is not producing"
        return text

    gap_devices = sorted({g["device_id"] for g in gaps})
    return ToolResult(tool="data_gaps", verdict="data_gap" if gaps else VERDICT_OK, devices=gap_devices,
                      start=start, end=end, values={"gaps": gaps}, thresholds=thresholds,
                      evidence="; ".join(describe(g) for g in gaps) if gaps else "no missing rows")


def curtailment_periods(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                        plant: PlantConfig | None = None) -> ToolResult:
    """Periods with a grid active-power limit, and whether the meter sat at the limit.

    Verdicts: "curtailment" (expected behaviour, no action; devices = the plant controller), ok.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    thresholds = {"at_limit_tol": CURTAIL_AT_LIMIT_TOL, "at_limit_share": CURTAIL_AT_LIMIT_SHARE}
    limits = store.grid_setpoint(start, end)["limit_kw"]
    if limits.empty:
        return ToolResult(tool="curtailment_periods", verdict=VERDICT_OK, start=start, end=end,
                          values={"periods": []}, thresholds=thresholds, evidence="no grid limit in the window")
    meter = store.meter(start, end)["plant_ac_kw"].reindex(limits.index)
    exp = expected_plant_ac(store, start, end, plant).reindex(limits.index)
    periods = []
    changed = limits.ne(limits.shift()).cumsum()
    for _, part in limits.groupby(changed):
        for p_start, p_end, steps in runs(pd.Series(True, index=part.index)):
            limit = float(part.iloc[0])
            m, e = meter.loc[p_start:p_end - STEP], exp.loc[p_start:p_end - STEP]
            binding = e > limit
            mb = m[binding]
            at_limit = (mb <= limit * (1 + CURTAIL_AT_LIMIT_TOL)) & (mb >= limit * (1 - CURTAIL_AT_LIMIT_TOL))
            periods.append({
                "start": p_start.to_pydatetime(),
                "end": p_end.to_pydatetime(),
                "steps": steps,
                "limit_kw": limit,
                "meter_median_kw": round(float(m.median()), 1),
                "expected_median_kw": round(float(e.median()), 1),
                "binding_steps": int(binding.sum()),
                "meter_at_limit": bool(at_limit.mean() >= CURTAIL_AT_LIMIT_SHARE) if binding.any() else None,
            })
    evidence = "; ".join(
        f"grid limit {p['limit_kw'] / 1000:.1f} MW from {local_time(p['start'])} to {local_time(p['end'])}, "
        f"meter median {p['meter_median_kw'] / 1000:.1f} MW vs {p['expected_median_kw'] / 1000:.1f} MW expected"
        for p in periods) + " (curtailment by the grid operator is expected behaviour)"
    return ToolResult(tool="curtailment_periods", verdict="curtailment", devices=[plant.plant_controller_id],
                      start=start, end=end, values={"periods": periods}, thresholds=thresholds, evidence=evidence)
