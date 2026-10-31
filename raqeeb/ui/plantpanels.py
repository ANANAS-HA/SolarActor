"""Plant tab panels (docs/design/PLANT_TAB.md): the stat row, the legend tables and numbered event markers under
the two time-series charts, the HTML plant map and the equipment cards.

    stats(run, power, events, plant)      # four Stat values with a state in words and a sparkline series
    legend_output(power, dropped_at)      # one LegendRow per line: name, meaning, now / min / max
    markers(run)                          # applied actions as numbered markers, in time order
    map_html(view, plant)                 # the inverter tiles, one row per transformer
    equipment_html(view, plant)           # grid connection, substation meter, weather station as cards

Everything here is words and HTML strings built from telemetry (`power`, the map view) and the agent's record
(`run.log`); the app draws the Plotly charts. Plant free text (badges, notes) is HTML-escaped once, here.
Raw device ids never appear: devices are named through ui/plain.py.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from raqeeb.contracts import ActionStatus, ActionType, PlantConfig
from raqeeb.ui import plain, plantmap, summary
from raqeeb.ui.source import local_hhmm

# state words -> (text colour, dot colour, tint); each text colour is at least 4.5:1 on white and on the tint
STATE_INK = {"ok": "#1f6b3a", "warn": "#7a4300", "bad": "#9a2420", "none": "#4a4d52"}
STATE_DOT = {"ok": "#2e9b56", "warn": "#c9822a", "bad": "#d0463f", "none": "#8a8c90"}
STATE_TINT = {"ok": "#e4f2e8", "warn": "#fdf4e3", "bad": "#fbe7e5", "none": "#f1efea"}
GAP_WARN, GAP_BAD = 0.05, 0.15  # shortfall against expected: under 5% is fine, over 15% is bad
MIN_GAP_FRACTION = 0.05  # of the plant rating: below this (dawn, dusk) output is not compared with expected
RECENT_MIN = 30
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# tile and card state words: map state -> (word, state)
TILE_STATE = {
    plantmap.NORMAL: ("Normal", "ok"), plantmap.LOW: ("Low output", "bad"), plantmap.CAPPED: ("Capped", "warn"),
    plantmap.NO_DATA: ("No data", "none"), plantmap.CURTAILED: ("Curtailed", "none"),
    plantmap.PENDING: ("Needs approval", "warn"), plantmap.RECOVERED: ("Recovered", "ok"),
    plantmap.FLAGGED: ("Not trusted", "warn"),
}
EQUIPMENT_NAMES = {"grid": "Grid connection", "meter": "Substation meter", "station": "Weather station"}


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _pct_space(text: str) -> str:
    """"93%" -> "93 %", and a real minus sign."""
    return re.sub(r"(\d)%", r"\1 %", text).replace("-", "−")


def _gap_state(shortfall: float) -> str:
    """Shortfall as a fraction of expected (negative = above expected) -> state word."""
    if shortfall < GAP_WARN:
        return "ok"
    return "warn" if shortfall < GAP_BAD else "bad"


# -- stat row ---------------------------------------------------------------------------------------------


@dataclass
class Stat:
    label: str
    value: str
    note: str  # the state in words
    state: str  # ok | warn | bad | none
    spark: list[float] = field(default_factory=list)  # values over the run so far (empty = no sparkline)


def stats(run, power: pd.DataFrame, events: pd.DataFrame, plant: PlantConfig) -> list[Stat]:
    now = run.now
    shown = power.dropna(subset=["actual_kw"])
    actions = run.log.actions_until(now)
    # plant output now
    if len(shown):
        line = shown.iloc[-1]
        actual, expected = float(line["actual_kw"]), float(line["expected_ref_kw"])
        if expected > MIN_GAP_FRACTION * plant.rated_ac_kw:
            gap = actual / expected - 1
            note = "on expected" if abs(gap) < 0.01 else _pct_space(f"{gap * 100:+.0f}% against expected")
            state = _gap_state(-gap)
        else:
            note, state = "too little sun to compare", "none"
        output = Stat("Plant output now", plain.kw(actual), note, state, [float(v) / 1000 for v in shown["actual_kw"]])
    else:
        output = Stat("Plant output now", "no reading yet", "", "none")
    # energy below expected
    e = summary.energy(power, actions, now)
    if e.first_effective:
        note = f"{e.lost_before_mwh:.2f} before the first fix, {e.lost_after_mwh:.2f} after"
    else:
        note = "no fix in effect yet" if e.lost_mwh else "nothing lost so far"
    state = _gap_state(e.lost_mwh / e.expected_mwh) if e.expected_mwh > 0 else "none"
    loss = (shown["expected_ref_kw"] - shown["actual_kw"]).clip(lower=0).fillna(0).cumsum() if len(shown) else []
    energy = Stat("Energy below expected", f"{e.lost_mwh:.2f} MWh", note, state, [float(v) for v in loss])
    # alarms
    n = int(len(events))
    recent = int((events["ts"] > now - timedelta(minutes=RECENT_MIN)).sum()) if n else 0
    critical = bool(n) and bool(((events["ts"] > now - timedelta(minutes=RECENT_MIN))
                                 & (events["severity"].astype(str) == "CRITICAL")).any())
    note = f"{recent} in the last {RECENT_MIN} min" if recent else ("site alarms raised" if n else "none raised")
    if n and len(shown):
        stamps = pd.to_datetime(events["ts"], utc=True).dt.floor(f"{plantmap.STEP_MINUTES}min")
        counts = stamps.value_counts().reindex(shown.index, fill_value=0).cumsum()
        spark = [float(v) for v in counts]
    else:
        spark = []
    alarms = Stat("Alarms so far", str(n), note, "bad" if critical else "warn" if n else "ok", spark)
    # approvals
    waiting = run.waiting
    if waiting:
        first = waiting[0].request
        note = plain.action_text(first.action, first.target, getattr(first, "params", None) or {})
        if len(waiting) > 1:
            note += f" and {len(waiting) - 1} more"
    else:
        note = "Nothing to approve"
    approvals = Stat("Waiting for your approval", str(len(waiting)), note, "warn" if waiting else "ok")
    return [output, energy, alarms, approvals]


def sparkline_svg(values: list[float], state: str, width: int = 200, height: int = 36) -> str:
    vals = [float(v) for v in values if v == v]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = [(i / (len(vals) - 1) * width, height - 3 - (v - lo) / span * (height - 6)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"M0,{height} L{line.replace(' ', ' L')} L{width},{height} Z"
    return (f'<svg class="rq-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" aria-hidden="true">'
            f'<path d="{area}" fill="{STATE_TINT[state]}"/>'
            f'<polyline points="{line}" fill="none" stroke="{STATE_DOT[state]}" stroke-width="1.5"/></svg>')


def stats_html(items: list[Stat]) -> str:
    cards = []
    for s in items:
        state = s.state if s.state in STATE_INK else "none"
        note = (f'<div class="state" style="color:{STATE_INK[state]}"><i style="background:{STATE_DOT[state]}"></i>'
                f'{esc(s.note)}</div>') if s.note else ""
        cards.append(f'<div class="rq-stat"><div class="label">{esc(s.label)}</div><div class="value">{esc(s.value)}</div>'
                     f'{note}{sparkline_svg(s.spark, state)}</div>')
    return '<div class="rq-stats">' + "".join(cards) + "</div>"


# -- panel headers, legend tables, markers ----------------------------------------------------------------


def panel_header(title: str, unit: str, takeaway: str, state: str) -> str:
    ink = STATE_INK.get(state, STATE_INK["none"])
    return (f'<div class="rq-panel-head"><span class="title">{esc(title)}</span><span class="unit">{esc(unit)}</span>'
            f'<span class="take" style="color:{ink}">{esc(takeaway)}</span></div>')


@dataclass
class LegendRow:
    name: str
    meaning: str
    colour: str
    dash: str  # solid | dash | dot
    now: str
    low: str
    high: str


def _now_min_max(series: pd.Series, fmt) -> tuple[str, str, str]:
    shown = series.dropna()
    if shown.empty:
        return "—", "—", "—"
    return fmt(float(shown.iloc[-1])), fmt(float(shown.min())), fmt(float(shown.max()))


def legend_sky(sky: pd.DataFrame, plant: PlantConfig, sun_colour: str = "#b06d12", wind_colour: str = "#1b2f4a",
               now: datetime | None = None) -> list[LegendRow]:
    """Sun height is astronomy and runs to the end of the window: cut at `now` so Now / Min / Max are up to the clock."""
    stow = plant.tracker.stow_wind_ms
    if now is not None:
        sky = sky[sky.index <= now]
    return [
        LegendRow("Sun height", "how high the sun stands above the horizon", sun_colour, "solid",
                  *_now_min_max(sky["sun_elevation_deg"].clip(lower=0), lambda v: f"{v:.0f}°")),
        LegendRow("Wind on site", f"above {stow:g} m/s the trackers turn the panels flat (stow)", wind_colour, "solid",
                  *_now_min_max(sky["wind_ms"], lambda v: f"{v:.1f} m/s")),
    ]


def legend_output(power: pd.DataFrame, dropped_at: datetime | None, colours: dict[str, str] | None = None) -> list[LegendRow]:
    c = colours or {"actual": "#1b2f4a", "reference": "#1f7a45", "sensor": "#b06d12"}
    mw = lambda v: f"{v / 1000:.1f} MW"  # noqa: E731
    sensor = power["expected_sensor_kw"]
    if dropped_at is not None:
        sensor = sensor[sensor.index <= dropped_at]
    dropped = f"; the agent stopped using it at {local_hhmm(dropped_at)}" if dropped_at is not None else ""
    return [
        LegendRow("Actual", "what the substation meter measured", c["actual"], "solid",
                  *_now_min_max(power["actual_kw"], mw)),
        LegendRow("Expected, satellite weather", "the fault-free plant on the satellite weather; dust on site does "
                  "not move it, so the gap to Actual is the real loss", c["reference"], "dash",
                  *_now_min_max(power["expected_ref_kw"], mw)),
        LegendRow("Expected, plant sun sensor", "the same plant on the on-site sun sensor; a dusty sensor follows a "
                  f"dusty plant and hides the loss{dropped}", c["sensor"], "dot", *_now_min_max(sensor, mw)),
    ]


def _sample(colour: str, dash: str) -> str:
    dasharray = {"dash": "7 5", "dot": "2 4"}.get(dash, "")
    attr = f' stroke-dasharray="{dasharray}"' if dasharray else ""
    return (f'<svg width="36" height="10" viewBox="0 0 36 10" aria-hidden="true"><line x1="0" y1="5" x2="36" y2="5" '
            f'stroke="{colour}" stroke-width="2.5"{attr}/></svg>')


def legend_html(rows: list[LegendRow]) -> str:
    body = "".join(
        f'<tr><td>{_sample(r.colour, r.dash)}<b>{esc(r.name)}</b> <span class="meaning">{esc(r.meaning)}</span></td>'
        f'<td class="num">{esc(r.now)}</td><td class="num">{esc(r.low)}</td><td class="num">{esc(r.high)}</td></tr>'
        for r in rows)
    return ('<table class="rq-legend"><thead><tr><th>Series</th><th class="num">Now</th><th class="num">Min</th>'
            f'<th class="num">Max</th></tr></thead><tbody>{body}</tbody></table>')


@dataclass
class Marker:
    n: int
    ts: datetime
    text: str


def markers(run) -> list[Marker]:
    """Applied actions as numbered markers, in the order they were decided."""
    applied = sorted((a for a in run.log.actions_until(run.now) if a.status == ActionStatus.APPLIED and a.decided_at),
                     key=lambda a: a.decided_at)
    return [Marker(i + 1, a.decided_at, plain.action_text(a.request.action, a.request.target,
                                                           getattr(a.request, "params", None) or {}, past=True))
            for i, a in enumerate(applied[:len(CIRCLED)])]


def circled(n: int) -> str:
    return CIRCLED[n - 1] if 1 <= n <= len(CIRCLED) else f"({n})"


def markers_html(items: list[Marker]) -> str:
    return '<div class="rq-markers">' + "".join(
        f'<span><b>{circled(m.n)}</b> <span class="rq-time">{local_hhmm(m.ts)}</span> {esc(m.text)}</span>' for m in items
    ) + "</div>"


def sensor_dropped_at(run) -> datetime | None:
    """When the agent stopped trusting the sun sensor (the first applied flag action), else None."""
    flags = [a for a in run.log.actions_until(run.now)
             if a.request.action == ActionType.FLAG_SENSOR_USE_REFERENCE and a.status == ActionStatus.APPLIED]
    return min((a.decided_at or a.requested_at) for a in flags) if flags else None


def takeaway_output(power: pd.DataFrame, plant: PlantConfig) -> tuple[str, str]:
    shown = power.dropna(subset=["actual_kw"])
    if shown.empty:
        return "No meter reading yet", "none"
    line = shown.iloc[-1]
    actual, expected = float(line["actual_kw"]), float(line["expected_ref_kw"])
    if expected <= MIN_GAP_FRACTION * plant.rated_ac_kw:
        return f"Now {plain.kw(actual)}, too little sun to compare", "none"
    ratio = actual / expected
    return f"Now {plain.kw(actual)}, {ratio * 100:.0f} % of what the weather allows", _gap_state(1 - ratio)


def takeaway_sky(sky: pd.DataFrame, plant: PlantConfig, now: datetime | None = None) -> tuple[str, str]:
    wind = (sky[sky.index <= now] if now is not None else sky)["wind_ms"].dropna()
    if wind.empty:
        return "No wind reading yet", "none"
    w, stow = float(wind.iloc[-1]), plant.tracker.stow_wind_ms
    if w > stow:
        return f"Wind {w:.1f} m/s, trackers stowed", "warn"
    return f"Wind {w:.1f} m/s, below the {stow:g} m/s stow limit", "ok"


# -- plant map and equipment (HTML) -----------------------------------------------------------------------


def row_label(transformer: str, plant: PlantConfig) -> tuple[str, str]:
    """"T1" -> ("Transformer 1", "Group A")."""
    digits = re.sub(r"\D", "", transformer)
    name = f"Transformer {digits}" if digits else transformer
    group = plant.layout.groups.get(transformer, "")
    return name, f"Group {group}" if group else ""


def _dot(state: str) -> str:
    return f'<i style="background:{STATE_DOT[state]}"></i>'


def tile_html(tile: plantmap.Tile, plant: PlantConfig | None = None) -> str:
    word, state = TILE_STATE.get(tile.state, (str(tile.state).capitalize(), "none"))
    mw = next((line for line in tile.lines if line.endswith(" MW")), None)
    peers = next((line for line in tile.lines if "of peer median" in line), None)
    if peers:
        peers = _pct_space(peers.replace("of peer median", "of peers"))
    rest = [line for line in tile.lines if line not in (mw, tile.state) and "of peer median" not in line
            and not (peers is None and line == tile.lines[0] and mw is None)]
    status_word = None
    if mw and peers is None:  # dawn or dusk: peers are not compared; the inverter's own status word is shown
        status_word = next((line for line in rest if " " not in line), None)
        rest = [line for line in rest if line != status_word]
    attention = tile.state != plantmap.NORMAL
    style = (f' style="border-color:{STATE_DOT[state]};background:{STATE_TINT[state]}"' if attention else "")
    parts = [f'<div class="rq-tile{" attn" if attention else ""}"{style}>',
             f'<div class="name">{esc(plain.device(tile.id, plant))}</div>',
             f'<div class="mw">{esc(mw) if mw else "—"}</div>']
    if peers:
        parts.append(f'<div class="peers">{esc(peers)}</div>')
    elif status_word:
        parts.append(f'<div class="peers">{esc(status_word)}</div>')
    parts.append(f'<div class="state" style="color:{STATE_INK[state]}">{_dot(state)}{esc(word)}</div>')
    parts += [f'<div class="extra">{esc(line)}</div>' for line in rest]
    parts += [f'<div class="badge">◆ {esc(plain.humanize(b))}</div>' for b in tile.badges]
    parts.append("</div>")
    return "".join(parts)


def pending_inverters(view: plantmap.MapView) -> list[str]:
    return [t.id for t in view.inverters if t.state == plantmap.PENDING]


def map_html(view: plantmap.MapView, plant: PlantConfig) -> str:
    tiles = {t.id: t for t in view.inverters}
    cols = plant.inverters.layout_cols
    rows: list[tuple[str, str, list[plantmap.Tile], list[str]]] = []
    if plant.layout.transformers:
        for transformer, members in plant.layout.transformers.items():
            name, group = row_label(transformer, plant)
            rows.append((name, group, [tiles[m] for m in members if m in tiles], view.blocks.get(transformer, [])))
    else:
        ids = plant.inverters.ids
        for r in range(0, len(ids), cols):
            rows.append((f"Row {r // cols + 1}", "", [tiles[i] for i in ids[r:r + cols]], []))
    plant_wide = ("Plant-wide: " + ", ".join(plain.humanize(b) for b in view.plant)) if view.plant else "No plant-wide action"
    at = f"Telemetry at {local_hhmm(view.at)}" if view.at is not None else "No telemetry yet"
    out = [f'<div class="rq-map" style="--cols:{cols}"><div class="head"><span>{esc(at)}</span><span>{esc(plant_wide)}</span></div>']
    for name, group, members, badges in rows:
        label = f'<div class="rowlabel"><b>{esc(name)}</b><span>{esc(group)}</span>' + "".join(
            f'<div class="badge">◆ {esc(plain.humanize(b))}</div>' for b in badges) + "</div>"
        out.append('<div class="row">' + label + "".join(tile_html(t, plant) for t in members) + "</div>")
    out.append("</div>")
    return "".join(out)


_KV: list[tuple[re.Pattern, str, str | None]] = [  # tile line -> (key, value); None value = the match
    (re.compile(r"^sensor (.+)$"), "Sun sensor", None),
    (re.compile(r"^reference (.+)$"), "Satellite reference", None),
    (re.compile(r"^sensor/reference (.+)$"), "Sensor against reference", None),
    (re.compile(r"^wind (.+)$"), "Wind", None),
    (re.compile(r"^(\S+ MW)$"), "Output now", None),
    (re.compile(r"^(\d+%) of rated$"), "Of rated power", None),
    (re.compile(r"^(\d+%) of expected \(reference\)$"), "Of expected, satellite", None),
    (re.compile(r"^(\d+%) of expected \(on-site sensor\)$"), "Of expected, sun sensor", None),
    (re.compile(r"^export limit (.+)$"), "Export limit", None),
    (re.compile(r"^no export limit$"), "Export limit", "None"),
    (re.compile(r"^grid curtailment$"), "Grid operator", "curtailing"),  # normal operation, not a fault
    (re.compile(r"^no data$"), "Reading", "none at this step"),
    (re.compile(r"^last reading (.+)$"), "Last reading", None),
    (re.compile(r"^no reading yet$"), "Last reading", "none yet"),
]


def kv_rows(tile: plantmap.Tile) -> list[tuple[str, str]]:
    """A tile's value lines as (key, value) rows in words."""
    rows = []
    for line in tile.lines:
        for pattern, key, value in _KV:
            m = pattern.match(line)
            if m:
                rows.append((key, _pct_space(value if value is not None else m.group(1))))
                break
        else:
            rows.append(("Note", line))
    rows += [("Agent", plain.humanize(b)) for b in tile.badges]
    return rows


def equipment_card(tile: plantmap.Tile, name: str) -> str:
    word, state = TILE_STATE.get(tile.state, (str(tile.state).capitalize(), "none"))
    attention = tile.state != plantmap.NORMAL
    style = f' style="border-color:{STATE_DOT[state]}"' if attention else ""
    rows = "".join(f'<tr><td>{esc(k)}</td><td class="v">{esc(v)}</td></tr>' for k, v in kv_rows(tile))
    return (f'<div class="rq-equip{" attn" if attention else ""}"{style}><div class="name"><span>{esc(name)}</span>'
            f'<span class="state" style="color:{STATE_INK[state]}">{_dot(state)}{esc(word)}</span></div>'
            f'<table>{rows}</table></div>')


def equipment_html(view: plantmap.MapView, plant: PlantConfig) -> str:
    return "".join(equipment_card(tile, EQUIPMENT_NAMES[tile.kind])
                   for tile in (view.grid, view.meter, view.station))


def raw_rows(view: plantmap.MapView) -> list[dict[str, str]]:
    """The record behind the map, for Show data (raw ids and states)."""
    devices = list(view.inverters) + [view.grid, view.meter, view.station]
    return [{"Device": t.id, "Kind": t.kind, "State": t.state, "Values": "; ".join(t.lines),
             "Agent": "; ".join(t.badges)} for t in devices]


__all__ = ["Stat", "stats", "stats_html", "panel_header", "LegendRow", "legend_sky", "legend_output", "legend_html",
           "Marker", "markers", "markers_html", "sensor_dropped_at", "takeaway_output", "takeaway_sky", "map_html",
           "tile_html", "equipment_html", "kv_rows", "pending_inverters", "raw_rows"]
