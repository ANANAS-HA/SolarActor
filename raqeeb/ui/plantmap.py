"""The 2D plant map: what each device shows at the sim clock, from telemetry and the agent's record only.

    view = map_view(run)          # tiles (inverters, weather station, meter, grid), block and plant badges
    sky = sky_strip(run)          # sun elevation, on-site wind, storm (stow) intervals
    hist = inverter_history(run, "INV-09")   # one inverter vs its peers over the run so far
    fig = map_figure(view, plant)            # Plotly figure; the app draws it and the sky and detail charts

Inputs are run.store (telemetry written so far, cut at run.now) and run.log (the agent's actions,
verifications, escalations and tested model proposals as recorded). Never the scenario, its faults or
the answer key. A device with no row at the latest step is NO_DATA (comms loss = missing rows), which
is never shown as zero output.
"""

from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from raqeeb.config import load_plant
from raqeeb.contracts import (
    PLANT_TARGET,
    STEP_MINUTES,
    ActionStatus,
    ActionType,
    InverterStatus,
    PlantConfig,
    parse_cause_label,
)
from raqeeb.tools import physics as tool_physics

STEP = timedelta(minutes=STEP_MINUTES)
PEER_SHORTFALL = 0.10  # an inverter this far below the peer median is "low output"
MIN_PEER_FRACTION = 0.05  # of the rating; below this (dawn, dusk, night) peers are not compared

# Inverter tile states (each has a text label and an icon; colour never carries the meaning alone)
NORMAL, LOW, CAPPED, NO_DATA, CURTAILED, PENDING, RECOVERED = (
    "normal", "low output", "capped", "no data", "curtailed", "awaiting approval", "recovered")
STATES = (NORMAL, LOW, CAPPED, NO_DATA, CURTAILED, PENDING, RECOVERED)
FLAGGED = "flagged"  # weather station: the agent flagged the sensor and uses the reference
ICON = {NORMAL: "●", LOW: "▼", CAPPED: "▬", NO_DATA: "∅", CURTAILED: "⇩", PENDING: "⏸", RECOVERED: "↺",
        FLAGGED: "⚑"}

ACTION_BADGE = {
    ActionType.CREATE_CLEANING_WORK_ORDER: "cleaning ordered",
    ActionType.RESET_COMMS_GATEWAY: "reset sent",
    ActionType.DISPATCH_TECHNICIAN: "technician sent",
    ActionType.SET_POWER_LIMIT: "power limit set",
    ActionType.ISOLATE_DEVICE: "isolated",
    ActionType.FLAG_SENSOR_USE_REFERENCE: "flagged",
}


@dataclass
class Tile:
    id: str
    kind: str  # "inverter", "station", "meter", "grid"
    state: str
    lines: list[str] = field(default_factory=list)  # values shown on the tile, computed from telemetry
    badges: list[str] = field(default_factory=list)  # from the agent's record
    ac_kw: float | None = None  # None = no data (never 0 for a missing row)
    hover: str = ""


@dataclass
class MapView:
    at: datetime | None  # telemetry step shown
    inverters: list[Tile]
    station: Tile
    meter: Tile
    grid: Tile
    blocks: dict[str, list[str]]  # transformer -> badges
    edges: dict[str, list[str]]  # field edge -> badges
    plant: list[str]  # plant-wide badges


# -- badges from the agent's record ----------------------------------------------------------------------


def _badges(run, plant: PlantConfig) -> dict[str, list[str]]:
    """Target (device, transformer, edge, ALL) -> badges, from actions, escalations and accepted proposals."""
    now, log = run.now, run.log
    out: dict[str, list[str]] = defaultdict(list)
    actions = log.actions_until(now)
    escalations = log.escalations_until(now)
    escalated = {e.subject for e in escalations}
    for a in actions:
        name = ACTION_BADGE.get(a.request.action, a.request.action.value.replace("_", " "))
        if a.status is None:
            badge = f"awaiting approval: {name}"
        elif a.status == ActionStatus.REJECTED:
            badge = f"{name}: rejected"
        elif a.status == ActionStatus.FAILED:
            badge = f"{name}: failed"
        elif a.request.action == ActionType.CREATE_CLEANING_WORK_ORDER and a.effective_at and a.effective_at <= now:
            badge = "cleaned"
        else:
            badge = name
        _add(out, a.request.target, badge)
    proposed = {p.label: p for p in log.proposals_until(now) if p.decision in ("accepted", "dropped")}
    for label, p in proposed.items():
        from raqeeb.ui import plain

        chain = [f"{plain.fault_name(p.fault_type).lower()} proposed", "accepted" if p.decision == "accepted" else "dropped"]
        chain += [ACTION_BADGE.get(a.request.action, a.request.action.value) for a in actions if a.cause == label]
        if label in escalated:
            chain.append("escalated")
        _add(out, p.target, " → ".join(chain))
    for e in escalations:
        if e.subject in proposed:
            continue
        try:
            _, target = parse_cause_label(e.subject)
            _add(out, target, "escalated")
        except ValueError:  # "unexplained": the devices it names
            for device in e.devices:
                _add(out, device, "escalated: unexplained")
    return out


def _add(out: dict[str, list[str]], target: str, badge: str) -> None:
    if badge not in out[target]:
        out[target].append(badge)


# -- the view --------------------------------------------------------------------------------------------


def _latest(frame: pd.DataFrame, at) -> pd.Series | None:
    if at is None or frame.empty or at not in frame.index:
        return None
    row = frame.loc[at]
    return row.iloc[-1] if isinstance(row, pd.DataFrame) else row


def map_view(run, plant: PlantConfig | None = None) -> MapView:
    plant = plant or load_plant()
    store, now = run.store, run.now
    readings = store.read("inverter_readings", end=now + STEP).reset_index()
    at = readings["ts"].max() if len(readings) else None
    current = readings[readings["ts"] == at].set_index("inverter_id") if at is not None else readings
    badges = _badges(run, plant)
    pending = {a.request.target for a in run.log.actions_until(now) if a.status is None}
    recovered = {a.request.target for a in run.log.actions_until(now)
                 for _, v in run.log.verifications_until(now) if v.action_id == a.request.id and v.recovered}

    rating = plant.inverters.ac_kw
    wind = store.read("weather_station", end=now + STEP)["wind_ms"]
    stowing = len(wind) > 0 and float(wind.iloc[-1]) > plant.tracker.stow_wind_ms
    median = float(current["ac_kw"].median()) if len(current) else 0.0
    compare = median > MIN_PEER_FRACTION * rating
    tiles = []
    for inv in plant.inverters.ids:
        if inv not in current.index:
            last = readings[readings["inverter_id"] == inv]["ts"].max() if len(readings) else None
            since = f"last reading {pd.Timestamp(last).tz_convert(plant.site.timezone):%H:%M}" if pd.notna(last) \
                else "no reading yet"
            tiles.append(Tile(inv, "inverter", NO_DATA, ["no data", since], badges.get(inv, []), None,
                              f"{inv}: no data received at this step ({since}); not the same as zero output"))
            continue
        r = current.loc[inv]
        status, ac = InverterStatus(r["status"]), float(r["ac_kw"])
        if inv in pending:
            state = PENDING
        elif status == InverterStatus.CURTAILED:
            state = CURTAILED
        elif status == InverterStatus.DERATED:
            state = CAPPED
        elif status in (InverterStatus.STOPPED, InverterStatus.FAULT) or (compare and ac < (1 - PEER_SHORTFALL) * median):
            state = LOW
        elif inv in recovered:
            state = RECOVERED
        else:
            state = NORMAL
        share = f"{ac / median:.0%} of peer median" if compare else status.value.lower()
        angle = r.get("tracker_angle_deg")
        stowed = stowing and angle is not None and pd.notna(angle) and abs(float(angle)) <= 1.0
        lines = [f"{ac / 1000:.2f} MW", share] + (["tracker stowed (wind)"] if stowed else [])
        tiles.append(Tile(inv, "inverter", state, lines, badges.get(inv, []), ac,
                          f"{inv}: {state}<br>status {status.value}<br>AC {ac / 1000:.2f} MW<br>"
                          f"internal temp {float(r['temp_c']):.0f} °C"))

    return MapView(at=at, inverters=tiles, station=_station(store, now, plant, badges),
                   meter=_meter(store, now, plant, badges), grid=_grid(store, now, plant),
                   blocks={t: badges.get(t, []) for t in plant.layout.transformers},
                   edges={e: badges.get(e, []) for e in plant.layout.edges},
                   plant=badges.get(PLANT_TARGET, []))


def _station(store, now, plant: PlantConfig, badges) -> Tile:
    ws = plant.weather_station_id
    sensor = store.read("weather_station", end=now + STEP)
    ref = store.read("weather_reference", end=now + STEP)
    at = sensor.index.max() if len(sensor) else None
    row, ref_row = _latest(sensor, at), _latest(ref, ref.index.max() if len(ref) else None)
    flagged = any("flagged" == b for b in badges.get(ws, []))
    state = FLAGGED if flagged else NORMAL
    if row is None or at < now - STEP:
        return Tile(ws, "station", NO_DATA, ["no data"], badges.get(ws, []), None, f"{ws}: no data at this step")
    lines = [f"sensor {row['ghi_sensor_wm2']:.0f} W/m²"]
    if ref_row is not None:
        lines.append(f"reference {ref_row['ghi_wm2']:.0f} W/m²")
        if ref_row["ghi_wm2"] > 50:
            lines.append(f"sensor/reference {row['ghi_sensor_wm2'] / ref_row['ghi_wm2']:.0%}")
    lines.append(f"wind {row['wind_ms']:.1f} m/s")
    return Tile(ws, "station", state, lines, [b for b in badges.get(ws, []) if b != "flagged"], None,
                f"{ws} (on-site sensor){' · flagged by the agent: using the reference' if flagged else ''}")


def _meter(store, now, plant: PlantConfig, badges) -> Tile:
    meter = store.read("meter", end=now + STEP)
    at = meter.index.max() if len(meter) else None
    row = _latest(meter, at)
    targets = (plant.meter_id, plant.plant_controller_id)
    marks = [b for t in targets for b in badges.get(t, [])]
    if row is None or at < now - STEP:
        return Tile(plant.meter_id, "meter", NO_DATA, ["no data"], marks, None, "substation meter: no data")
    kw = float(row["plant_ac_kw"])
    lines, state = [f"{kw / 1000:.1f} MW", f"{kw / plant.rated_ac_kw:.0%} of rated"], NORMAL
    for basis, name in (("reference", "reference"), ("sensor", "on-site sensor")):
        expected = tool_physics.expected_plant_ac(store, at, at + STEP, plant, basis=basis)
        if len(expected) and float(expected.iloc[-1]) > MIN_PEER_FRACTION * plant.rated_ac_kw:
            ratio = kw / float(expected.iloc[-1])
            lines.append(f"{ratio:.0%} of expected ({name})")
            if basis == "reference" and ratio < 1 - PEER_SHORTFALL:
                state = LOW
    return Tile(plant.meter_id, "meter", state, lines, marks, kw,
                f"substation meter {plant.meter_id}: {kw / 1000:.2f} MW. Expected = the fault-free plant on the "
                "weather each source reports; a dusty sensor makes its own line look normal (the trap)")


def _grid(store, now, plant: PlantConfig) -> Tile:
    setpoint = store.read("grid_setpoint", end=now + STEP)
    if setpoint.empty:
        return Tile("GRID", "grid", NORMAL, ["no export limit"], [], None, "grid connection point: no setpoint")
    limit = float(setpoint["limit_kw"].iloc[-1])
    if limit < plant.rated_ac_kw:
        return Tile("GRID", "grid", CURTAILED, [f"export limit {limit / 1000:.1f} MW", "grid curtailment"], [],
                    None, f"grid operator export limit {limit / 1000:.1f} MW (normal operation, not a fault)")
    return Tile("GRID", "grid", NORMAL, ["no export limit"], [], None, "grid connection point: no limit below rated")


# -- sky strip and inverter detail -----------------------------------------------------------------------


def sky_strip(run, plant: PlantConfig | None = None) -> pd.DataFrame:
    """Over the incident window: sun elevation (deg, astronomy), on-site wind (m/s, NaN after now or when
    missing), and `stow` = wind above the trackers' stow limit."""
    from pvlib.solarposition import get_solarposition

    plant = plant or load_plant()
    start, end = run.incident.start, run.incident.end
    grid = pd.date_range(start, end, freq=f"{STEP_MINUTES}min", inclusive="left", name="ts")
    sun = get_solarposition(grid, plant.site.lat, plant.site.lon, altitude=plant.site.altitude_m)
    wind = run.store.read("weather_station", end=min(run.now + STEP, end))["wind_ms"].reindex(grid)
    frame = pd.DataFrame({"sun_elevation_deg": sun["apparent_elevation"].to_numpy(), "wind_ms": wind}, index=grid)
    frame["stow"] = frame["wind_ms"] > plant.tracker.stow_wind_ms
    return frame


def storm_bands(sky: pd.DataFrame) -> list[tuple[datetime, datetime]]:
    """Intervals [start, end) with wind above the stow limit."""
    bands, start = [], None
    for ts, stow in sky["stow"].items():
        if stow and start is None:
            start = ts
        elif not stow and start is not None:
            bands.append((start, ts))
            start = None
    if start is not None:
        bands.append((start, sky.index[-1] + STEP))
    return bands


def inverter_history(run, inverter_id: str, plant: PlantConfig | None = None) -> pd.DataFrame:
    """kW of one inverter vs its peers (the others) on the 5-min grid up to now; NaN where no row arrived."""
    plant = plant or load_plant()
    start = run.incident.start
    grid = pd.date_range(start, run.incident.end, freq=f"{STEP_MINUTES}min", inclusive="left", name="ts")
    readings = run.store.read("inverter_readings", end=min(run.now + STEP, run.incident.end)).reset_index()
    wide = readings.pivot_table(index="ts", columns="inverter_id", values="ac_kw").reindex(grid)
    wide = wide.reindex(columns=plant.inverters.ids)
    peers = wide.drop(columns=[inverter_id])
    return pd.DataFrame({
        "inverter_kw": wide[inverter_id],
        "peer_median_kw": peers.median(axis=1, skipna=True),
        "peer_min_kw": peers.min(axis=1, skipna=True),
        "peer_max_kw": peers.max(axis=1, skipna=True),
        "peers_reporting": peers.notna().sum(axis=1).where(wide.notna().any(axis=1), np.nan),
    }, index=grid)


# -- drawing (Plotly) ------------------------------------------------------------------------------------

MAP_STYLE = {  # map state -> (fill, ink); every state also has a text label and an icon
    NORMAL: ("#dcefe2", "#14532d"), LOW: ("#fbdcc0", "#7c2d12"),
    CAPPED: ("#e6ddf6", "#3b0764"), NO_DATA: ("#f2f1ee", "#3d3a35"),
    CURTAILED: ("#d8e6fa", "#1e3a8a"), PENDING: ("#fff1c2", "#713f12"),
    RECOVERED: ("#cfeeed", "#134e4a"), FLAGGED: ("#fbdcc0", "#7c2d12"),
}
MUTED = "#898781"
TILE_W, TILE_H, GAP = 1.5, 1.4, 0.1
BLOCK_PITCH, BLOCK_TOP, HEADER_H, LEFT = 2.05, -0.75, 0.42, 1.0
SIDE_X0, SIDE_X1 = 7.95, 10.45
BUS_X, METER_Y = 7.7, -3.55  # site bus (blocks' feeders) and where it meets the substation meter
BADGE = "#9a3412"


def _tile_xy(index: int, cols: int) -> tuple[float, float, float, float]:
    row, col = divmod(index, cols)
    x0 = LEFT + col * (TILE_W + GAP)
    y0 = BLOCK_TOP - row * BLOCK_PITCH - HEADER_H
    return x0, x0 + TILE_W, y0, y0 - TILE_H


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _tile(fig: go.Figure, x0, x1, y0, y1, state: str, title: str, lines: list[str], badges: list[str],
          size: int = 12, max_lines: int = 9, max_badges: int = 2) -> None:
    fill, ink = MAP_STYLE[state]
    pending = state == PENDING
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=fill, layer="below",
                  line={"width": 3 if pending else 1, "color": "#d97706" if pending else "#b8b5ad",
                        "dash": "dash" if pending else "solid"})
    if state == NO_DATA:  # hatching: diagonal strokes, so "no data" never reads as an empty (zero) tile
        step = 0.18
        for k in range(1, int(((x1 - x0) + (y0 - y1)) / step)):
            d = k * step
            ax, ay = x0 + max(0.0, d - (y0 - y1)), y0 - min(d, y0 - y1)
            bx, by = x0 + min(d, x1 - x0), y0 - max(0.0, d - (x1 - x0))
            fig.add_shape(type="line", x0=ax, y0=ay, x1=bx, y1=by, line={"color": "#c9c6bf", "width": 1}, layer="below")
    shown = badges[:max_badges] + ([f"+{len(badges) - max_badges} more"] if len(badges) > max_badges else [])
    text = f"<b>{_esc(title)}</b><br>{ICON.get(state, '')} {_esc(state)}"
    text += "".join(f"<br>{_esc(line)}" for line in [x for x in lines if x != state][:max_lines])
    text += "".join(f"<br><span style='font-size:{size - 2}px;color:{BADGE}'>◆ {_esc(b)}</span>" for b in shown)
    fig.add_annotation(x=(x0 + x1) / 2, y=(y0 + y1) / 2, text=text, showarrow=False,
                       font={"size": size, "color": ink}, bgcolor="rgba(255,255,255,0.75)"
                       if state == NO_DATA else None)


def map_figure(view: MapView, plant, compact: bool = False) -> go.Figure:
    """The map. `compact` (for a half-width column): smaller text, one value line and one badge per tile."""
    fig = go.Figure()
    size, lines, badges_shown = (10, 1, 1) if compact else (12, 9, 2)
    cols = plant.inverters.layout_cols
    groups = plant.layout.groups
    mids: list[float] = []
    # Plant-wide badges
    plant_text = "Plant-wide: " + (" · ".join(_esc(b) for b in view.plant) if view.plant else "no plant-wide action")
    fig.add_annotation(x=0.05, y=-0.3, xanchor="left", text=plant_text, showarrow=False,
                       font={"size": size, "color": BADGE if view.plant else MUTED})
    # Blocks (one transformer each) with their tiles
    for b, (transformer, members) in enumerate(plant.layout.transformers.items()):
        top = BLOCK_TOP - b * BLOCK_PITCH + 0.05
        bottom = top - HEADER_H - TILE_H - 0.15
        fig.add_shape(type="rect", x0=0.1, x1=LEFT + cols * (TILE_W + GAP) + 0.05, y0=top, y1=bottom,
                      line={"color": "#8f8c84", "width": 1}, fillcolor="rgba(0,0,0,0)", layer="below")
        fig.add_annotation(x=0.55, y=(top + bottom) / 2, text=f"<b>{_esc(transformer)}</b><br>group {_esc(groups.get(transformer, ''))}",
                           showarrow=False, font={"size": size, "color": "#3d3a35"})
        badges = view.blocks.get(transformer, [])
        fig.add_annotation(x=LEFT, y=top - 0.2, xanchor="left", showarrow=False, font={"size": size - 1, "color": BADGE},
                           text=("block: " + " · ".join(f"◆ {_esc(x)}" for x in badges)) if badges else "")
        # feeder from the block's transformer to the site bus
        fig.add_shape(type="line", x0=LEFT + cols * (TILE_W + GAP) + 0.05, x1=BUS_X, y0=(top + bottom) / 2,
                      y1=(top + bottom) / 2, line={"color": "#b8b5ad", "width": 1.5})
        mids.append((top + bottom) / 2)
    fig.add_shape(type="line", x0=BUS_X, x1=BUS_X, y0=max(mids), y1=min(mids), line={"color": "#8f8c84", "width": 2})
    fig.add_shape(type="line", x0=BUS_X, x1=SIDE_X0, y0=METER_Y, y1=METER_Y, line={"color": "#8f8c84", "width": 2})
    for k, tile in enumerate(view.inverters):
        x0, x1, y0, y1 = _tile_xy(k, cols)
        _tile(fig, x0, x1, y0, y1, tile.state, tile.id, tile.lines, tile.badges, size, lines, badges_shown)
    # Field edges (map columns along the fences)
    bottom = BLOCK_TOP - len(plant.layout.transformers) * BLOCK_PITCH - 0.05
    for edge, members in plant.layout.edges.items():
        col = plant.inverters.ids.index(members[0]) % cols
        x = LEFT + col * (TILE_W + GAP) + TILE_W / 2
        badges = view.edges.get(edge, [])
        fig.add_annotation(x=x, y=bottom, text=f"{_esc(edge)} (fence)" + "".join(f"<br>◆ {_esc(b)}" for b in badges),
                           showarrow=False, yanchor="top", font={"size": size - 1, "color": BADGE if badges else MUTED})
    # Site devices on the right: grid connection point, substation meter, weather station
    for tile, (y0, y1), title in ((view.grid, (-0.75, -2.35), "Grid connection point"),
                                  (view.meter, (-2.75, -4.35), f"Substation meter {view.meter.id}"),
                                  (view.station, (-4.75, -7.1), f"Weather station {view.station.id}")):
        _tile(fig, SIDE_X0, SIDE_X1, y0, y1, tile.state, title, tile.lines, tile.badges, size,
              3 if compact else 9, badges_shown)
    fig.add_shape(type="line", x0=(SIDE_X0 + SIDE_X1) / 2, x1=(SIDE_X0 + SIDE_X1) / 2, y0=-2.35, y1=-2.75,
                  line={"color": "#8f8c84", "width": 2})
    # Click targets: one invisible marker per inverter tile (point index = inverter order)
    xs, ys, hover = [], [], []
    for k, tile in enumerate(view.inverters):
        x0, x1, y0, y1 = _tile_xy(k, cols)
        xs.append((x0 + x1) / 2)
        ys.append((y0 + y1) / 2)
        hover.append(tile.hover + "<br><i>click for output vs peers</i>")
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", hoverinfo="text", hovertext=hover, showlegend=False,
                             customdata=[t.id for t in view.inverters],
                             marker={"size": 64, "symbol": "square", "opacity": 0.01, "color": "#000"},
                             selected={"marker": {"opacity": 0.01}}, unselected={"marker": {"opacity": 0.01}}))
    fig.update_xaxes(visible=False, range=[0, SIDE_X1 + 0.1], fixedrange=True)
    fig.update_yaxes(visible=False, range=[bottom - 0.75, 0], fixedrange=True)
    fig.update_layout(height=520 if compact else 700, margin={"l": 0, "r": 0, "t": 0, "b": 0}, dragmode=False,
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", clickmode="event+select")
    return fig


