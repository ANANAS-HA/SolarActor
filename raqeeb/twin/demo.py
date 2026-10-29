"""Plot one clear day: simulated twin output vs fault-free expected (Lane A check).

    uv run python -m raqeeb.twin.demo [--date 2025-07-07] [--soiling 0.2]

Writes data/plots/twin_<date>.html. Times are shown in Asia/Riyadh.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from raqeeb.config import DATA_DIR, load_plant
from raqeeb.contracts import DISPLAY_TZ, PLANT_TARGET, PlantState
from raqeeb.twin import plant as physics
from raqeeb.twin.simulator import Twin
from raqeeb.twin.weather import clear_days, load_hourly, weather_5min

PLOT_DIR = DATA_DIR / "plots"


def run_day(day: date, soiling: float) -> dict:
    plant = load_plant()
    start = datetime.combine(day, time(0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    weather = weather_5min(start, end, plant)

    twin = Twin(start, end, plant=plant, seed=1, weather=weather)
    batch = twin.run_to_end()
    soiled_state = PlantState.healthy(plant.inverters.ids)
    for inv in soiled_state.inverters.values():
        inv.soiling_loss = soiling
    soiled = Twin(start, end, plant=plant, seed=1, weather=weather, state=soiled_state)
    soiled.run_to_end()

    def series(rows, field):
        return pd.Series([getattr(r, field) for r in rows], index=pd.DatetimeIndex([r.ts for r in rows]))

    inv01 = [r for r in batch.inverter_readings if r.inverter_id == "INV-01"]
    prepared = physics.prepare(weather, plant)
    return {
        "plant": plant,
        "weather": prepared,
        "hourly": load_hourly().loc[start : end - timedelta(hours=1)],
        "simulated": series(batch.meter, "plant_ac_kw"),
        "expected": physics.expected_plant_ac(weather, plant),
        "soiled": pd.Series([t.plant_ac_kw for t in soiled.truth], index=pd.DatetimeIndex([t.ts for t in soiled.truth])),
        "true_ghi": pd.Series([t.ghi_wm2 for t in twin.truth], index=pd.DatetimeIndex([t.ts for t in twin.truth])),
        "inv_temp": series(inv01, "temp_c"),
        "cell_temp": physics.cell_temperature(prepared["poa_track_wm2"], prepared["ambient_c"], prepared["wind_ms"], plant),
    }


def energy_mwh(power_kw: pd.Series) -> float:
    return float(power_kw.sum()) * 5 / 60 / 1000


def build_figure(day: date, data: dict, soiling: float) -> go.Figure:
    local = lambda idx: idx.tz_convert(DISPLAY_TZ)  # noqa: E731
    w, hourly = data["weather"], data["hourly"]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=("Irradiance (W/m²)", "Plant AC output (MW)", "Temperature (°C)"))

    fig.add_trace(go.Scatter(x=local(hourly.index + pd.Timedelta(minutes=30)), y=hourly["ghi_wm2"], mode="markers",
                             name="NASA POWER GHI (hourly mean)"), row=1, col=1)
    fig.add_trace(go.Scatter(x=local(w.index), y=w["ghi_wm2"], name="Reference GHI, 5-min (NASA)"), row=1, col=1)
    fig.add_trace(go.Scatter(x=local(data["true_ghi"].index), y=data["true_ghi"], name="True on-site GHI (twin)"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=local(w.index), y=w["clearsky_ghi_wm2"], name="Clear-sky GHI (Ineichen)",
                             line={"dash": "dot"}), row=1, col=1)
    fig.add_trace(go.Scatter(x=local(w.index), y=w["poa_track_wm2"], name="Plane of array (tracking)"), row=1, col=1)

    mw = lambda s: s / 1000  # noqa: E731
    fig.add_trace(go.Scatter(x=local(data["expected"].index), y=mw(data["expected"]), name="Expected (fault-free, nominal)",
                             line={"width": 4}), row=2, col=1)
    fig.add_trace(go.Scatter(x=local(data["simulated"].index), y=mw(data["simulated"]), name="Simulated twin (meter)"),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=local(data["soiled"].index), y=mw(data["soiled"]),
                             name=f"Twin with {soiling:.0%} soiling (illustration)", line={"dash": "dash"}), row=2, col=1)
    fig.add_hline(y=data["plant"].rated_ac_kw / 1000, line_dash="dot", annotation_text="AC rating", row=2, col=1)

    fig.add_trace(go.Scatter(x=local(w.index), y=w["ambient_c"], name="Ambient"), row=3, col=1)
    fig.add_trace(go.Scatter(x=local(w.index), y=data["cell_temp"], name="Module cell"), row=3, col=1)
    fig.add_trace(go.Scatter(x=local(data["inv_temp"].index), y=data["inv_temp"], name="INV-01 internal"), row=3, col=1)

    fig.update_layout(
        title=f"Raqeeb twin, {day} ({data['plant'].site.name}). Synthetic plant; weather from NASA POWER.",
        height=900, hovermode="x unified",
    )
    fig.update_xaxes(title_text=f"Time ({DISPLAY_TZ})", row=3, col=1)
    return fig


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot a clear day of the twin")
    parser.add_argument("--date", type=date.fromisoformat, help="UTC date (default: clearest day of the year)")
    parser.add_argument("--soiling", type=float, default=0.2, help="soiling fraction for the illustration line")
    args = parser.parse_args()
    day = args.date or clear_days(1)[0]

    data = run_day(day, args.soiling)
    plant = data["plant"]
    sim, exp, soiled = energy_mwh(data["simulated"]), energy_mwh(data["expected"]), energy_mwh(data["soiled"])
    print(f"Day {day} ({plant.site.name})")
    print(f"  simulated {sim:.1f} MWh, expected from reference {exp:.1f} MWh ({(sim / exp - 1):+.2%}; "
          f"true irradiance vs reference + inverter variation)")
    print(f"  specific yield {sim * 1000 / (plant.inverters.dc_kw * plant.inverters.count):.2f} kWh/kWp, "
          f"peak {data['simulated'].max() / 1000:.1f} MW of {plant.rated_ac_kw / 1000:.0f} MW")
    print(f"  {args.soiling:.0%} soiling on {PLANT_TARGET} -> {soiled:.1f} MWh ({soiled / sim - 1:+.1%} energy; "
          f"clipping hides part of the loss at midday)")

    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    out = PLOT_DIR / f"twin_{day}.html"
    build_figure(day, data, args.soiling).write_html(out, include_plotlyjs="cdn")
    print(f"  plot: {out}")


if __name__ == "__main__":
    main()
