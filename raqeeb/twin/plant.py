"""Plant physics (pvlib, kept simple).

weather -> single-axis tracking -> plane-of-array irradiance (isotropic sky, so it scales
linearly with the irradiance components) -> SAPM cell temperature -> PVWatts DC -> PVWatts
inverter with clipping at the AC rating.

The same functions give both the twin's truth (simulator.py) and the fault-free expected
output that tools compare against, so a healthy twin matches expected exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from raqeeb.contracts import PlantConfig

STANDBY_POA_WM2 = 5.0  # below this an inverter reports STANDBY


def capacity_factors(plant: PlantConfig) -> np.ndarray:
    """Fixed per-inverter DC capacity multipliers (+/- variation_pct) so peers are not identical."""
    inv = plant.inverters
    rng = np.random.default_rng(inv.variation_seed)
    return 1.0 + rng.uniform(-inv.variation_pct, inv.variation_pct, inv.count) / 100.0


def poa_at_angle(weather: pd.DataFrame, angle_deg: float | pd.Series, plant: PlantConfig) -> pd.Series:
    """Plane-of-array global irradiance for a tracker held at `angle_deg` (0 = flat)."""
    angle = angle_deg if isinstance(angle_deg, pd.Series) else pd.Series(float(angle_deg), index=weather.index)
    orient = pvlib.tracking.calc_surface_orientation(
        angle, axis_tilt=plant.tracker.axis_tilt_deg, axis_azimuth=plant.tracker.axis_azimuth_deg
    )
    poa = pvlib.irradiance.get_total_irradiance(
        orient["surface_tilt"],
        orient["surface_azimuth"],
        weather["zenith"],
        weather["azimuth"],
        weather["dni_wm2"],
        weather["ghi_wm2"],
        weather["dhi_wm2"],
        albedo=plant.module.albedo,
        model="isotropic",
    )["poa_global"]
    return poa.fillna(0.0).clip(lower=0.0)


def tracking_angle(weather: pd.DataFrame, plant: PlantConfig) -> pd.Series:
    """Normal tracking rotation (with backtracking). Night -> stow angle."""
    tr = plant.tracker
    result = pvlib.tracking.singleaxis(
        weather["apparent_zenith"],
        weather["azimuth"],
        axis_tilt=tr.axis_tilt_deg,
        axis_azimuth=tr.axis_azimuth_deg,
        max_angle=tr.max_angle_deg,
        backtrack=tr.backtrack,
        gcr=tr.gcr,
    )
    return result["tracker_theta"].fillna(tr.stow_angle_deg)


def prepare(weather: pd.DataFrame, plant: PlantConfig) -> pd.DataFrame:
    """Add tracker angle and POA for normal tracking and for wind stow to a weather frame."""
    out = weather.copy()
    out["tracker_theta"] = tracking_angle(weather, plant)
    out["poa_track_wm2"] = poa_at_angle(weather, out["tracker_theta"], plant)
    out["poa_stow_wm2"] = poa_at_angle(weather, plant.tracker.stow_angle_deg, plant)
    return out


def cell_temperature(poa, ambient_c, wind_ms, plant: PlantConfig):
    params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"][plant.module.sapm_temp_model]
    return pvlib.temperature.sapm_cell(poa, ambient_c, wind_ms, **params)


def dc_power_kw(effective_poa, cell_temp_c, dc_rated_kw, plant: PlantConfig):
    dc = pvlib.pvsystem.pvwatts_dc(effective_poa, cell_temp_c, dc_rated_kw, plant.module.gamma_pdc_per_c)
    return np.clip(dc * (1.0 - plant.module.dc_losses_pct / 100.0), 0.0, None)


def ac_power_kw(dc_kw, plant: PlantConfig):
    """PVWatts inverter; output is clipped at the AC rating."""
    inv = plant.inverters
    pdc0 = inv.ac_kw / inv.nominal_efficiency
    ac = pvlib.inverter.pvwatts(dc_kw, pdc0, eta_inv_nom=inv.nominal_efficiency)
    return np.clip(np.nan_to_num(ac, nan=0.0), 0.0, inv.ac_kw)


def inverter_temperature(ambient_c, ac_kw, temp_offset_c, plant: PlantConfig):
    inv = plant.inverters
    load = np.asarray(ac_kw) / inv.ac_kw
    return ambient_c + inv.temp_rise_idle_c + inv.temp_rise_full_load_c * load + temp_offset_c


def expected_power(weather: pd.DataFrame, plant: PlantConfig, with_variation: bool = False) -> pd.DataFrame:
    """Fault-free AC and DC output per inverter for a weather frame (from weather_5min).

    Wind stow is normal behaviour, so it is included. `with_variation=False` uses the
    nominal rating for every inverter (what an operator would expect).
    """
    prepared = weather if "poa_track_wm2" in weather else prepare(weather, plant)
    stowed = prepared["wind_ms"] > plant.tracker.stow_wind_ms
    poa = prepared["poa_track_wm2"].where(~stowed, prepared["poa_stow_wm2"])
    tcell = cell_temperature(poa, prepared["ambient_c"], prepared["wind_ms"], plant)
    factors = capacity_factors(plant) if with_variation else np.ones(plant.inverters.count)

    columns = {}
    for inv_id, factor in zip(plant.inverters.ids, factors):
        dc = dc_power_kw(poa, tcell, plant.inverters.dc_kw * factor, plant)
        columns[(inv_id, "dc_kw")] = dc
        columns[(inv_id, "ac_kw")] = ac_power_kw(dc, plant)
    out = pd.DataFrame(columns, index=prepared.index)
    out.columns = pd.MultiIndex.from_tuples(out.columns, names=["inverter_id", "quantity"])
    return out


def expected_plant_ac(weather: pd.DataFrame, plant: PlantConfig, with_variation: bool = False) -> pd.Series:
    power = expected_power(weather, plant, with_variation)
    return power.xs("ac_kw", axis=1, level="quantity").sum(axis=1).rename("plant_ac_kw")
