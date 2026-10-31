"""Raqeeb operator console (HANDOFF section 12).

    uv run streamlit run raqeeb/ui/app.py

Three modes: "Live: rules only" (the real agent, no LLM), "Live: rules + local model" (only when
.env names a reachable local model server) and "Replay" (a recorded run, ui/replays/*.json or an
uploaded file). Live runs drive the twin and the agent step by step (ui/live.py); gated actions
pause the run for an operator decision. The planted faults are shown only after the run has ended.
The page opens with the plant map (ui/plantmap.py: telemetry and the agent's record only). Panels
include technician and operator notes, and model proposals with the agent's tool test of each
(ui/proposals.py): a rejected proposal changes nothing.
Plant, telemetry and faults are synthetic; weather is NASA POWER. Times are shown in Asia/Riyadh.
Alarm, note and log text is untrusted data: it is shown in tables or escaped, never as markdown or HTML.
"""

from __future__ import annotations

import random
import time
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from raqeeb.config import REPO_ROOT, load_plant
from raqeeb.contracts import DISPLAY_TZ, ActionStatus, IncidentReport
from raqeeb.ui import alerts, ask, plantmap, source, summary, trace
from raqeeb.ui.live import LiveRun, ReplayRun, list_local_models, local_model_status
from raqeeb.ui.runner import DEFAULT_PACE, PACES, Runner
from raqeeb.ui.proposals import effect
from raqeeb.ui.runlog import MODE_LABEL, Mode
from raqeeb.ui.source import local_hhmm, md_escape

LIVE_RULES, LIVE_LOCAL, REPLAY = MODE_LABEL[Mode.RULES], MODE_LABEL[Mode.LOCAL], "Replay"
MODES = [LIVE_RULES, LIVE_LOCAL, REPLAY]
RULES_ONLY = "Rules only"  # the Model selector's first entry; the others are what the local server lists
API_TIER = "API tier (not configured)"
SPEEDS = [15, 30, 60, 120, 240]  # sim minutes per tick
TICK_S = 0.4  # wall-clock pause between ticks while playing
TRACE_REFRESH_S = 1.0  # the Trace tab re-renders this often while the background runner advances a live run

# Reference data-viz palette: status colors carry a text label and an icon too; series slots in fixed order
SERIES = {"actual": "#2a78d6", "reference": "#eb6834", "sensor": "#1baf7a"}
MUTED = "#898781"
RECENT_ALARM_MIN = 30
MIN_GAP_FRACTION = 0.05  # of the plant rating: below this (dawn, dusk) no "vs expected" delta
ON_EXPECTED_FRACTION = 0.01
SUMMARY_COLUMNS = {  # bench summary.csv column -> shown name
    "agent": "Agent", "scenarios": "Scenarios", "precision": "Precision", "recall": "Recall",
    "all_causes_found_rate": "All causes found", "false_action_rate": "False-action rate",
    "unsafe_actions": "Unsafe actions", "verified_recovery_rate": "Verified recovery",
    "ttvr_median_min": "Time to verified recovery (median min)", "mwh_recovered": "MWh recovered",
    "mwh_lost": "MWh lost (no action)", "cost_per_incident_usd": "Cost per incident (USD)",
    "llm_calls_per_incident": "LLM calls per incident", "local_call_share": "Local call share", "note": "Note",
}


# -- data ------------------------------------------------------------------------------


@st.cache_data
def scenario_choices() -> tuple[list[str], str]:
    return source.scenario_ids(), source.default_scenario_id()


@st.cache_data
def replay_choices() -> dict[str, str]:
    return {str(p): source.replay_title(p) for p in source.console_replays()}


@st.cache_data(ttl=60, show_spinner=False)
def local_status() -> tuple[bool, str]:
    return local_model_status()


@st.cache_data(ttl=60, show_spinner=False)
def local_models() -> list[str]:
    return list_local_models()


def lines(run: source.Run) -> pd.DataFrame:
    """Power lines, recomputed only when the run's clock moves."""
    key = (id(run), run.now)
    cached = st.session_state.get("lines_cache")
    if cached is None or cached[0] != key:
        cached = (key, source.power_lines(run))
        st.session_state.lines_cache = cached
    return cached[1]


def local(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_convert(DISPLAY_TZ)


# -- callbacks ---------------------------------------------------------------------------


def _runner(run: source.Run | None = None) -> Runner | None:
    """The background runner of the current live run, if one was started."""
    runner = st.session_state.get("runner")
    run = run or st.session_state.get("run")
    return runner if runner is not None and runner.run is run else None


def _ensure_runner() -> Runner:
    runner = _runner()
    if runner is None:
        runner = Runner(st.session_state.run, st.session_state.get("trace_pace", DEFAULT_PACE))
        st.session_state.runner = runner
    return runner


def run_lock(run: source.Run | None = None):
    """Held while the page reads the run, so a background turn never shows half done."""
    runner = _runner(run)
    return runner.lock if runner is not None else nullcontext()


def _set_run(run: source.Run) -> None:
    runner = st.session_state.pop("runner", None)
    if runner is not None:
        runner.stop()
    old = st.session_state.get("run")
    if old is not None:
        old.close()
    st.session_state.run = run
    st.session_state.playing = False
    st.session_state.pop("lines_cache", None)
    st.session_state.ask_history = []
    st.session_state.load_error = ""


def _start_live(scenario_id: str | None = None) -> None:
    mode = Mode.LOCAL if st.session_state.mode_choice == LIVE_LOCAL else Mode.RULES
    model = st.session_state.get("model_choice")
    model = model if mode == Mode.LOCAL and model and model != RULES_ONLY else None
    _set_run(source.start_live(scenario_id or st.session_state.scenario_id, mode, model=model))


def _model_chosen() -> None:
    """The Model selector and the Mode radio say the same thing: a model means the local-model mode."""
    st.session_state.mode_choice = LIVE_RULES if st.session_state.model_choice == RULES_ONLY else LIVE_LOCAL


def _mode_chosen() -> None:
    choice, models = st.session_state.mode_choice, local_models()
    if choice == LIVE_RULES:
        st.session_state.model_choice = RULES_ONLY
    elif choice == LIVE_LOCAL and models and st.session_state.get("model_choice", RULES_ONLY) == RULES_ONLY:
        st.session_state.model_choice = models[0]


def _start_storm() -> None:
    _start_live(f"storm_{random.randint(1, 10**6)}")


def _load_replay() -> None:
    _set_run(source.start_replay(st.session_state.replay_choice))


def _load_upload() -> None:
    upload = st.session_state.get("replay_upload")
    if upload is None:
        return
    try:
        _set_run(source.start_replay(upload.getvalue()))
    except ValueError as exc:
        st.session_state.load_error = f"Could not load that file: {str(exc).splitlines()[0]}"


def _open_trajectory() -> None:
    path = st.session_state.get("trace_pick") or st.session_state.get("trace_path", "").strip()
    try:
        _set_run(source.start_replay(Path(path)))
        st.session_state.mode_choice = REPLAY
    except (OSError, ValueError) as exc:
        st.session_state.trace_error = f"Could not open {Path(path).name or 'that path'}: {str(exc).splitlines()[0]}"
    else:
        st.session_state.trace_error = ""


def _open_trajectory_upload() -> None:
    upload = st.session_state.get("trace_upload")
    if upload is None:
        return
    try:
        _set_run(source.start_replay(upload.getvalue()))
        st.session_state.mode_choice = REPLAY
    except (OSError, ValueError) as exc:
        st.session_state.trace_error = f"Could not open that file: {str(exc).splitlines()[0]}"
    else:
        st.session_state.trace_error = ""


def _toggle_play() -> None:
    if isinstance(st.session_state.run, LiveRun):  # a live run plays in the background (ui/runner.py)
        _trace_run_toggle()
        return
    st.session_state.playing = not st.session_state.playing


def _pause_runner() -> None:
    runner = _runner()
    if runner is not None:
        runner.pause()


def _step() -> None:
    st.session_state.playing = False
    _pause_runner()
    with run_lock():
        st.session_state.run.advance(st.session_state.speed)


def _to_end() -> None:
    st.session_state.playing = False
    _pause_runner()
    with run_lock():
        st.session_state.run.run_to_end()  # a live run stops at the first action waiting for approval


def _decide(action_id: str, approve: bool) -> None:
    note = st.session_state.get(f"note_{action_id}", "").strip()
    with run_lock():
        st.session_state.run.decide(action_id, approve, note)


def _trace_run_toggle() -> None:
    runner = _ensure_runner()
    st.session_state.playing = False
    if runner.state().running:
        runner.pause()
    else:
        runner.resume(st.session_state.get("trace_pace", DEFAULT_PACE))


def _trace_jump() -> None:
    st.session_state.playing = False
    _ensure_runner().jump_to_end()


def _trace_pace() -> None:
    runner = _runner()
    if runner is not None:
        runner.set_pace(st.session_state.trace_pace)


# -- sidebar -----------------------------------------------------------------------------


def sidebar(run: source.Run) -> None:
    ids, _ = scenario_choices()
    available, reason = local_status()
    with st.sidebar:
        st.header("Run")
        st.radio("Mode", MODES, key="mode_choice", on_change=_mode_chosen,
                 captions=["The agent's rules, no language model", f"{'Available' if available else 'Unavailable'}: "
                           f"{reason}", "Play back a recorded run"])
        choice = st.session_state.mode_choice
        models = local_models()
        options = [RULES_ONLY, *models]
        if st.session_state.get("model_choice") not in options:
            st.session_state.model_choice = models[0] if choice == LIVE_LOCAL and models else RULES_ONLY
        if choice != REPLAY:
            st.selectbox("Model", options, key="model_choice", on_change=_model_chosen,
                         help="Rules only, or one of the models the local server lists at /models. The choice "
                              "changes only the model name the agent's router calls.")
            st.button(API_TIER, key="api_tier", disabled=True, width="stretch",
                      help="An API model (ANTHROPIC_API_KEY, API_LLM_MODEL in .env) is not configured in this "
                           "deployment: the console runs fully local.")
        if choice == REPLAY:
            replays = replay_choices()
            if replays:
                st.selectbox("Recorded run", list(replays), key="replay_choice", format_func=replays.get)
                st.button("Load replay", key="load_replay", on_click=_load_replay, width="stretch")
            else:
                st.caption("No recorded runs in this deployment.")
            st.file_uploader("Or upload a replay file (.json)", type=["json"], key="replay_upload")
            st.button("Load uploaded file", key="load_upload", on_click=_load_upload, width="stretch",
                      disabled=st.session_state.get("replay_upload") is None)
            if st.session_state.get("load_error"):
                st.error(st.session_state.load_error)
        else:
            blocked = choice == LIVE_LOCAL and not available
            st.selectbox("Scenario (practice sets)", ids, key="scenario_id")
            st.button("Start live run", key="start_live", on_click=_start_live, type="primary", width="stretch",
                      disabled=blocked)
            st.button("Dust storm + 2 random faults", key="inject_storm", on_click=_start_storm, width="stretch",
                      disabled=blocked)
            if blocked:
                st.caption(f"Rules + local model is unavailable: {reason}.")
            st.caption("Faults are planted in the synthetic twin; the agent sees only telemetry.")

        st.header("Playback")
        st.select_slider("Speed (sim minutes per tick)", SPEEDS, key="speed")
        play, step, end = st.columns(3)
        stopped = run.finished or bool(run.waiting)
        runner = _runner(run)
        playing = st.session_state.playing or bool(runner and runner.state().running)
        play.button("Pause" if playing else "Play", key="play", on_click=_toggle_play,
                    width="stretch", disabled=stopped)
        step.button("Step", key="step", on_click=_step, width="stretch", disabled=stopped)
        end.button("To end", key="to_end", on_click=_to_end, width="stretch", disabled=stopped)
        if run.waiting:
            st.warning("Paused: an action waits for operator approval.")
        elif run.finished:
            st.success("Run finished. The answer key can be revealed.")
        if isinstance(run, LiveRun) and run.finished:
            st.download_button("Save recording (replay JSON)", run.recording().to_json(),
                               file_name=f"raqeeb_replay_{run.incident.id}_{run.mode.value}.json",
                               mime="application/json", key="save_recording", width="stretch")


# -- panels ----------------------------------------------------------------------------


def headline(run: source.Run, power: pd.DataFrame, data: dict[str, pd.DataFrame]) -> None:
    plant = load_plant()
    shown = power.dropna(subset=["actual_kw"])
    a, b, c, d = st.columns(4)
    if len(shown):
        line = shown.iloc[-1]
        daylight = line["expected_ref_kw"] > MIN_GAP_FRACTION * plant.rated_ac_kw
        gap = line["actual_kw"] / line["expected_ref_kw"] - 1 if daylight else None
        on_line = gap is not None and abs(gap) < ON_EXPECTED_FRACTION
        a.metric("Plant output (meter)", f"{line['actual_kw'] / 1000:.1f} MW",
                 None if gap is None else "on expected (reference)" if on_line
                 else f"{gap:+.0%} vs expected (reference)", delta_color="off" if on_line else "normal")
    else:
        a.metric("Plant output (meter)", "—")
    events = data["events"]
    recent = int((events["ts"] > run.now - timedelta(minutes=RECENT_ALARM_MIN)).sum()) if len(events) else 0
    b.metric("Alarms so far", len(events), f"+{recent} in last {RECENT_ALARM_MIN} min" if recent else None,
             delta_color="inverse")
    readings = data["inverter_readings"]
    last = readings["ts"].max() if len(readings) else None
    reporting = readings.loc[readings["ts"] == last, "inverter_id"].nunique() if last is not None else 0
    c.metric("Inverters reporting", f"{reporting} / {plant.inverters.count}")
    d.metric("Waiting for approval", len(run.waiting))


def plant_map(run: source.Run) -> None:
    """Sky strip, the top-down map (blocks T1-T4, weather station, substation meter, grid point) and, for the
    clicked tile, that inverter against its peers. Telemetry and the agent's record only (ui/plantmap.py)."""
    plant = load_plant()
    st.subheader("Plant map")
    sky_chart(run, plant)
    view = plantmap.map_view(run, plant)
    st.plotly_chart(plantmap.map_figure(view, plant), key="plant_map", on_select=_map_clicked, selection_mode="points",
                    config={"displayModeBar": False})
    counts = pd.Series([tile.state for tile in view.inverters]).value_counts()
    shown = f"telemetry at {local_hhmm(view.at)}" if view.at is not None else "no telemetry yet"
    st.caption(f"{shown} · " + " · ".join(f"{plantmap.ICON[s]} {s}: {counts[s]}" for s in plantmap.STATES
                                           if counts.get(s, 0))
               + f". Low output = stopped or ≥{plantmap.PEER_SHORTFALL:.0%} below the peer median; capped = the "
               "inverter reports a derate or power limit; ∅ no data = no reading arrived (hatched, never zero). "
               "Badges come from the agent's actions, escalations and accepted model proposals. Synthetic plant.")
    inverter_detail(run, plant)


def _map_clicked() -> None:
    event = st.session_state.get("plant_map")
    points = (event or {}).get("selection", {}).get("points", []) if isinstance(event, dict) else \
        getattr(getattr(event, "selection", None), "points", [])
    ids = load_plant().inverters.ids
    for point in points:
        index = point.get("point_index", point.get("point_number"))
        if index is not None and 0 <= int(index) < len(ids):
            st.session_state.map_inverter = ids[int(index)]


def sky_chart(run: source.Run, plant) -> None:
    sky = plantmap.sky_strip(run, plant)
    x = sky.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=sky["sun_elevation_deg"].clip(lower=0), name="Sun elevation (°)", mode="lines",
                             fill="tozeroy", line={"color": "#d9a400", "width": 1}, fillcolor="rgba(240,190,40,0.25)",
                             hovertemplate="sun %{y:.0f}°"))
    fig.add_trace(go.Scatter(x=x, y=sky["wind_ms"], name="Wind, on-site (m/s)", mode="lines", yaxis="y2",
                             line={"color": "#2a78d6", "width": 2}, hovertemplate="wind %{y:.1f} m/s"))
    stow = plant.tracker.stow_wind_ms
    fig.add_hline(y=stow, yref="y2", line={"color": "#2a78d6", "width": 1, "dash": "dot"},
                  annotation_text=f"stow limit {stow:g} m/s", annotation_font_size=10)
    for a, b in plantmap.storm_bands(sky):
        fig.add_vrect(x0=local(a), x1=local(b), fillcolor="rgba(150,110,60,0.25)", line_width=0,
                      annotation_text="storm: trackers stow", annotation_position="top left",
                      annotation_font_size=10)
    fig.add_vline(x=local(run.now), line={"color": "#3d3a35", "width": 1})
    fig.update_layout(height=150, margin={"l": 0, "r": 0, "t": 18, "b": 0}, hovermode="x unified",
                      legend={"orientation": "h", "y": 1.35, "x": 0, "font": {"size": 11}},
                      yaxis={"title": "sun (°)", "range": [0, 90], "fixedrange": True},
                      yaxis2={"title": "wind (m/s)", "overlaying": "y", "side": "right", "fixedrange": True,
                              "range": [0, max(25.0, float(sky["wind_ms"].max() or 0) * 1.1)]},
                      xaxis={"range": [local(run.incident.start), local(run.incident.end)], "fixedrange": True})
    st.plotly_chart(fig, key="sky_strip", config={"displayModeBar": False})


def inverter_detail(run: source.Run, plant) -> None:
    ids = plant.inverters.ids
    choice = st.selectbox("Inverter output vs its peers (click a tile, or pick one)", ids, index=None,
                          key="map_inverter", placeholder="Click a tile on the map")
    if choice is None:
        return
    hist = plantmap.inverter_history(run, choice, plant)
    x = hist.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=hist["peer_max_kw"] / 1000, mode="lines", line={"width": 0}, showlegend=False,
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=hist["peer_min_kw"] / 1000, mode="lines", line={"width": 0}, fill="tonexty",
                             fillcolor="rgba(137,135,129,0.2)", name="Peers, min to max", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=hist["peer_median_kw"] / 1000, mode="lines", name="Peer median",
                             line={"color": MUTED, "width": 1.5, "dash": "dash"}, hovertemplate="%{y:.2f} MW"))
    fig.add_trace(go.Scatter(x=x, y=hist["inverter_kw"] / 1000, mode="lines", name=choice, connectgaps=False,
                             line={"color": SERIES["actual"], "width": 2.5}, hovertemplate="%{y:.2f} MW"))
    fig.add_vline(x=local(run.now), line={"color": "#3d3a35", "width": 1})
    fig.update_layout(height=260, margin={"l": 0, "r": 0, "t": 10, "b": 0}, hovermode="x unified",
                      legend={"orientation": "h", "y": 1.15, "x": 0}, yaxis_title="AC (MW)",
                      xaxis_range=[local(run.incident.start), local(run.incident.end)])
    st.plotly_chart(fig, key="inverter_detail")
    gaps = int(hist["inverter_kw"].isna().sum() - hist["peer_median_kw"].isna().sum())
    st.caption(f"{choice} against the other {len(ids) - 1} inverters, from telemetry up to the sim clock."
               + (f" Gaps in the line are steps with no reading ({gaps} so far), not zero output." if gaps > 0 else ""))


def alarm_feed(data: dict[str, pd.DataFrame]) -> None:
    st.subheader("Alarm feed")
    events = data["events"].sort_values("ts", ascending=False)
    by_severity = events["severity"].astype(str).value_counts()
    st.caption(f"{len(events)} alarms · " + " · ".join(f"{s}: {by_severity.get(s, 0)}"
                                                       for s in ("CRITICAL", "WARNING", "INFO"))
               + ". Message text is quoted device data (untrusted): the agent's rules never read it.")
    table = pd.DataFrame({
        "Time": events["ts"].map(local_hhmm), "Device": events["device_id"], "Code": events["code"],
        "Severity": events["severity"].astype(str), "Message (quoted)": events["message"],
    })
    st.dataframe(table, hide_index=True, height=380, key="alarm_table")


def notes_panel(run: source.Run) -> None:
    st.subheader("Technician and operator notes")
    notes = run.log.notes_until(run.now)
    st.caption("Quoted as written (synthetic, untrusted data). The agent's rules never read note text; "
               "technician notes reach a model only as delimited data.")
    if not notes:
        st.info("No notes so far.")
        return
    st.dataframe(pd.DataFrame([{"Time": local_hhmm(n.ts), "Source": n.source, "Author": n.author, "About": n.about,
                                "Text (quoted)": n.text} for n in reversed(notes)]),
                 hide_index=True, key="notes_table")


def recovery_figure(run: source.Run, power: pd.DataFrame, height: int = 380) -> go.Figure:
    """Actual vs expected (reference) vs expected (sensor), with a line per applied action."""
    x = power.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    for col, name, color, dash in (
        ("actual_kw", "Actual (meter)", SERIES["actual"], "solid"),
        ("expected_ref_kw", "Expected from trusted reference", SERIES["reference"], "solid"),
        ("expected_sensor_kw", "Expected from on-site sensor", SERIES["sensor"], "dot"),
    ):
        fig.add_trace(go.Scatter(x=x, y=power[col] / 1000, name=name, mode="lines",
                                 line={"color": color, "width": 2, "dash": dash}, hovertemplate="%{y:.1f} MW"))
    for a in run.log.actions_until(run.now):
        if a.status == ActionStatus.APPLIED:
            fig.add_vline(x=local(a.decided_at), line={"color": MUTED, "width": 1})
            fig.add_annotation(x=local(a.decided_at), y=1, yref="paper", text=a.request.action.value,
                               showarrow=False, textangle=-90, xanchor="right", yanchor="top",
                               font={"size": 10, "color": MUTED})
    fig.update_layout(height=height, hovermode="x unified", margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      legend={"orientation": "h", "y": 1.12, "x": 0},
                      yaxis_title="Plant AC (MW)", xaxis_title=f"Time ({DISPLAY_TZ})",
                      xaxis_range=[local(run.incident.start), local(run.incident.end)])
    return fig


def recovery_chart(run: source.Run, power: pd.DataFrame) -> None:
    st.subheader("Recovery")
    x = power.index.tz_convert(DISPLAY_TZ)
    st.plotly_chart(recovery_figure(run, power), key="recovery_chart")
    st.caption("Expected lines are the fault-free plant run on the weather each source reports. When the sensor is "
               "dusty, the sensor line drops to meet a dusty plant and the loss looks normal (the dusty-sensor "
               "trap); the reference line still shows it. Vertical lines mark actions applied to the plant.")

    fig = go.Figure()
    for col, name, color, dash in (
        ("sensor_ghi_wm2", "On-site sensor GHI", SERIES["sensor"], "dot"),
        ("reference_ghi_wm2", "Reference GHI (NASA POWER)", SERIES["reference"], "solid"),
        ("clearsky_ghi_wm2", "Clear-sky model (shape reference)", MUTED, "solid"),
    ):
        fig.add_trace(go.Scatter(x=x, y=power[col], name=name, mode="lines",
                                 line={"color": color, "width": 2 if color != MUTED else 1, "dash": dash},
                                 hovertemplate="%{y:.0f} W/m²"))
    fig.update_layout(height=260, hovermode="x unified", margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      legend={"orientation": "h", "y": 1.2, "x": 0}, yaxis_title="GHI (W/m²)",
                      xaxis_range=[local(run.incident.start), local(run.incident.end)])
    st.plotly_chart(fig, key="irradiance_chart")
    st.caption("The clear-sky model gives the shape of a cloudless day for comparison. It is not a ceiling: the "
               "reference itself can sit above it.")


def approvals(run: source.Run) -> None:
    """Gated actions waiting now (live), or the recorded decisions (replay and history)."""
    gated = [a for a in run.log.actions_until(run.now) if a.gated]
    st.subheader("Approvals")
    if not gated:
        st.info("No action has needed approval so far.")
    for a in gated:
        req = a.request
        with st.container(border=True):
            params = ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in req.params.items())
            st.markdown(f"**{md_escape(req.action.value)}** on **{md_escape(req.target)}**"
                        + (f" ({md_escape(params)})" if params else "")
                        + f"  \nProposed {local_hhmm(a.requested_at)} · reversible: {'yes' if req.reversible else 'no'}")
            st.markdown(f"*Rationale:* {md_escape(req.rationale)}  \n"
                        f"*Expected effect:* {md_escape(req.expected_effect)}")
            if a.status is not None:
                verb = {ActionStatus.APPLIED: "Approved", ActionStatus.REJECTED: "Rejected"}.get(a.status, "Failed")
                recorded = "Recorded decision: " if isinstance(run, ReplayRun) else ""
                st.markdown(f"{recorded}**{verb}** by {md_escape(a.decided_by)} at {local_hhmm(a.decided_at)}"
                            + (f": {md_escape(a.note)}" if a.note else ""))
                continue
            if isinstance(run, ReplayRun):
                continue
            st.text_input("Operator note", key=f"note_{req.id}", placeholder="Optional reason for the decision")
            ok, no = st.columns(2)
            ok.button("Approve", key=f"approve_{req.id}", on_click=_decide, args=(req.id, True), type="primary",
                      width="stretch")
            no.button("Reject", key=f"reject_{req.id}", on_click=_decide, args=(req.id, False), width="stretch")


def agent_timeline(run: source.Run) -> None:
    st.subheader("Agent timeline")
    items = run.log.timeline_until(run.now)
    if not items:
        st.info("No agent activity yet.")
        return
    st.caption("Newest first. Each poll runs triage → investigate → rank → consult → plan → safety → execute → "
               "verify → report; evidence is computed by code.")
    st.dataframe(pd.DataFrame([{"Time": local_hhmm(t.ts), "Step": t.node, "Actor": t.actor, "What": t.summary,
                                "Evidence": "; ".join(f"{k}: {v}" for k, v in t.details.items())}
                               for t in reversed(items)]),
                 hide_index=True, height=420, key="timeline_table")
    findings = run.log.findings_until(run.now)
    st.markdown("**Tool findings at the last poll**")
    if not findings:
        st.caption("No tool reports a finding.")
    else:
        st.dataframe(pd.DataFrame([{"Tool": r.tool, "Finding": r.verdict, "Devices": ", ".join(r.devices),
                                    "Evidence": r.evidence} for r in findings]), hide_index=True,
                     key="findings_table")
    diagnosis = run.log.diagnosis_until(run.now)
    if diagnosis:
        st.markdown(f"**Current diagnosis** (confidence {diagnosis.confidence:.2f})")
        st.dataframe(pd.DataFrame([{"Cause": ", ".join(h.labels), "Score": h.score, "Explains": len(h.explains),
                                    "Evidence": "; ".join(h.evidence)} for h in diagnosis.root_causes]),
                     hide_index=True, key="diagnosis_table")


def proposals_panel(run: source.Run) -> None:
    st.subheader("Model proposals")
    proposals = run.log.proposals_until(run.now)
    asked = [t for t in run.log.timeline_until(run.now) if t.node == "consult" and t.summary.startswith("asked the model")]
    st.caption("A model may propose a cause when the rules leave symptoms unexplained. The agent tests every "
               "proposal on tool data first: only an accepted one enters the diagnosis. A rejected proposal "
               "changes nothing.")
    if not proposals:
        st.info(f"No proposals tested so far ({len(asked)} model questions)." if asked
                else "No proposals so far: the model was not asked (or this run is rules only).")
        return
    counts = {d: sum(p.decision == d for p in proposals) for d in ("accepted", "rejected", "dropped")}
    st.markdown(f"{len(asked)} model questions · {counts['accepted']} accepted · {counts['rejected']} rejected"
                + (f" · {counts['dropped']} dropped later" if counts["dropped"] else ""))
    actions, escalations = run.log.actions_until(run.now), run.log.escalations_until(run.now)
    badge = {"accepted": ":green-background[ACCEPTED]", "rejected": ":red-background[REJECTED]",
             "dropped": ":orange-background[DROPPED]"}
    for i, p in enumerate(reversed(proposals)):
        with st.container(border=True):
            call = f"model call {p.llm_s:.2f} s ({md_escape(p.model)})" if p.model else "no model call this poll"
            st.markdown(f"{badge[p.decision]} **{md_escape(p.fault_type)}** on **{md_escape(p.target)}** · "
                        f"decided {local_hhmm(p.ts)} · {call}")
            st.dataframe(pd.DataFrame([{"Check": c.check, "Result": c.result,
                                        "Pass": {True: "pass", False: "FAIL", None: "context"}[c.passed]}
                                       for c in p.checks]),
                         hide_index=True, key=f"proposal_checks_{i}")
            if p.model_evidence:
                st.dataframe(pd.DataFrame({"Model's evidence (quoted)": p.model_evidence}), hide_index=True,
                             key=f"proposal_evidence_{i}")
            outcome = effect(p, actions, escalations)
            (st.success if p.decision == "accepted" else st.warning)(md_escape(outcome))


def actions_panel(run: source.Run) -> None:
    st.subheader("Actions and verifications")
    actions = run.log.actions_until(run.now)
    verified = {v.action_id: (ts, v) for ts, v in run.log.verifications_until(run.now)}
    if not actions:
        st.info("No actions requested yet.")
    else:
        rows = []
        for a in actions:
            check = verified.get(a.request.id)
            rows.append({"Time": local_hhmm(a.requested_at), "Action": a.request.action.value,
                         "Target": a.request.target, "For cause": a.cause,
                         "Approval": "needed" if a.gated else "auto",
                         "Status": a.status.value if a.status else "waiting", "By": a.decided_by,
                         "Takes effect": local_hhmm(a.effective_at) if a.effective_at else "",
                         "Verified": ("" if check is None else
                                      f"{'recovered' if check[1].recovered else 'not recovered'} at "
                                      f"{local_hhmm(check[0])}"),
                         "Before / after / expected (kW)": ("" if check is None else
                                                            f"{check[1].before_kw:.0f} / {check[1].after_kw:.0f} / "
                                                            f"{check[1].expected_kw:.0f}")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, key="actions_table")
    escalations = run.log.escalations_until(run.now)
    if escalations:
        st.markdown("**Handed to a human**")
        st.dataframe(pd.DataFrame([{"Time": local_hhmm(e.ts), "Subject": e.subject, "Devices": ", ".join(e.devices),
                                    "Reason": e.reason} for e in escalations]), hide_index=True,
                     key="escalations_table")


def llm_panel(run: source.Run) -> None:
    st.subheader("LLM calls and time")
    polls = run.log.polls_until(run.now)
    calls = run.log.llm_calls_until(run.now)
    agent_s = sum(p.agent_s for p in polls)
    a, b, c, d = st.columns(4)
    a.metric("LLM calls", len(calls))
    b.metric("Model time", f"{sum(c.latency_s for c in calls):.1f} s")
    c.metric("Cost", f"{sum(c.cost_usd for c in calls):.4f} USD")
    d.metric("Agent time (incl. model)", f"{agent_s:.1f} s", f"{len(polls)} polls", delta_color="off")
    if calls:
        st.dataframe(pd.DataFrame([{"Role": c.role.value, "Model": c.model, "Purpose": c.purpose,
                                    "Tokens in": c.prompt_tokens, "Tokens out": c.completion_tokens,
                                    "Latency (s)": round(c.latency_s, 2), "Cost (USD)": c.cost_usd,
                                    "Valid": c.ok, "Escalation": c.escalation} for c in calls]),
                     hide_index=True, key="llm_table")
    else:
        st.caption("No model calls: the rules explained everything so far (or this run is rules only).")


# -- Trace tab ---------------------------------------------------------------------------------------------

KIND_STYLE = {trace.FINDING: ("FINDING", "#fbdcc0"), trace.OK: ("OK", "#dcefe2"),
              trace.INCONCLUSIVE: ("INCONCLUSIVE", "#ecebe7")}  # word + colour: never colour alone
STATUS_BADGE = {trace.OPEN: "blue", trace.ACTING: "orange", trace.RESOLVED: "green", trace.ESCALATED: "red",
                trace.EXPECTED: "gray", trace.FLAGGED: "violet"}
DECISION_BADGE = {"accepted": ":green-background[ACCEPTED]", "rejected": ":red-background[REJECTED]",
                  "dropped": ":orange-background[DROPPED]"}


def trace_header(head: trace.Header) -> None:
    a, b, c, d, e = st.columns([1, 1, 3, 1, 1])
    a.metric("Sim time", f"{local(head.ts):%H:%M}", DISPLAY_TZ, delta_color="off")
    b.metric("Turn", f"{head.turn}" + (f" / {head.turns_total}" if head.turns_total else ""))
    with c:
        st.markdown("**Causes by status**")
        named = [(s, labels) for s, labels in head.causes.items() if labels]
        st.markdown(" ".join(f":{STATUS_BADGE[s]}-background[{md_escape(s)}: {len(labels)}]" for s, labels in named)
                    if named else "none named yet")
        if named:
            st.caption(" · ".join(f"{md_escape(s)}: {', '.join(md_escape(x) for x in labels)}" for s, labels in named))
    d.metric("Model calls", head.llm_calls)
    e.metric("Model time", f"{head.llm_s:.1f} s")


def _styled_checks(rows: list[trace.CheckRow]):
    frame = pd.DataFrame([{"Verdict": f"{KIND_STYLE[r.kind][0]}: {r.verdict}", "Tool": r.tool, "Devices": r.devices,
                           "Numbers": r.numbers, "Threshold": r.threshold} for r in rows])
    colours = [f"background-color: {KIND_STYLE[r.kind][1]}" for r in rows]
    return frame.style.apply(lambda _: colours, subset=["Verdict"])


def trace_card(card: trace.TurnCard, run: source.Run) -> None:
    focused = st.session_state.get(summary.FOCUS_KEY) == card.index
    with st.container(border=True, key=f"trace_card_{card.index}"):
        st.markdown((":violet-background[**Turn %d**]" if focused else "**Turn %d**") % card.index
                    + f" · {local_hhmm(card.ts)} {DISPLAY_TZ}" + (" · final poll" if card.final else ""))
        if card.outcomes:
            st.caption("Told by the plant: " + "; ".join(md_escape(o) for o in card.outcomes))
        if card.symptoms:
            st.markdown("New symptoms: " + " ".join(f":orange-background[{md_escape(s)}]" for s in card.symptoms))
        if card.checks is not None:
            if card.checks:
                st.dataframe(_styled_checks(card.checks), hide_index=True, key=f"trace_checks_{card.index}")
            else:
                st.caption("Tool checks: no finding.")
        if card.candidates is not None:
            top = max([c.score for c in card.candidates] + [1.0])
            st.dataframe(pd.DataFrame([{"Cause": c.label, "Score": c.score,
                                        "Selected": "✓ selected" if c.selected else "not selected",
                                        "Explains": c.explains} for c in card.candidates]),
                         hide_index=True, key=f"trace_candidates_{card.index}",
                         column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0.0,
                                                                                 max_value=top, format="%.2f")})
            if card.unexplained:
                st.caption("Unexplained: " + ", ".join(md_escape(s) for s in card.unexplained))
        if card.llm_calls or card.proposals:
            trace_model(card)
        if card.gate:
            trace_actions(card, run)
        if card.verifications:
            st.dataframe(pd.DataFrame([{"Verified": f"{'✓ recovered' if v.recovered else '✗ not recovered'}",
                                        "Action": v.action_id, "Before kW": round(v.before_kw),
                                        "After kW": round(v.after_kw), "Expected kW": round(v.expected_kw)}
                                       for v in card.verifications]),
                         hide_index=True, key=f"trace_verify_{card.index}")
        if card.escalations:
            st.dataframe(pd.DataFrame([{"Escalated to a human": e.subject, "Devices": ", ".join(e.devices),
                                        "Reason": e.reason} for e in card.escalations]),
                         hide_index=True, key=f"trace_escalations_{card.index}")
        alarms = _turn_alarms(run, card)
        if len(alarms):
            with st.expander(f"Alarms this turn ({len(alarms)}): messages quoted, untrusted", expanded=False):
                st.dataframe(pd.DataFrame({"Time": alarms["ts"].map(local_hhmm), "Device": alarms["device_id"],
                                           "Code": alarms["code"], "Severity": alarms["severity"].astype(str),
                                           "Message (quoted)": alarms["message"]}),
                             hide_index=True, key=f"trace_alarms_{card.index}")
        if card.notes:
            with st.expander(f"Notes ({len(card.notes)}): quoted, untrusted", expanded=False):
                st.dataframe(pd.DataFrame([{"Time": local_hhmm(n.ts), "Source": n.source, "Author": n.author,
                                            "About": n.about, "Text (quoted)": n.text} for n in card.notes]),
                             hide_index=True, key=f"trace_notes_{card.index}")
        if card.steps:
            with st.expander(f"Agent steps ({len(card.steps)})", expanded=False):
                st.dataframe(pd.DataFrame([{"Step": s.node, "What": s.summary} for s in card.steps]),
                             hide_index=True, key=f"trace_steps_{card.index}")


def _turn_alarms(run: source.Run, card: trace.TurnCard) -> pd.DataFrame:
    """Alarms that arrived during this turn, (previous turn, this turn], from the run's telemetry store."""
    start = card.since + timedelta(seconds=1) if card.since else run.incident.start
    events = run.store.read("events", start, card.ts + timedelta(seconds=1)).reset_index()
    return events


def trace_model(card: trace.TurnCard) -> None:
    seconds = sum(c.latency_s for c in card.llm_calls)
    decided = " · ".join(f"{sum(p.decision == d for p in card.proposals)} {d}"
                         for d in ("accepted", "rejected", "dropped") if any(p.decision == d for p in card.proposals))
    st.markdown(f"Model: {len(card.llm_calls)} call(s), {seconds:.2f} s" + (f" · proposals: {decided}" if decided else ""))
    for p in card.proposals:
        st.markdown(f"{DECISION_BADGE[p.decision]} **{md_escape(p.label)}**"
                    + (f": {md_escape(p.reason)}" if p.reason and p.decision != "accepted" else "")
                    + (" (changed nothing)" if p.decision == "rejected" else ""))
    with st.expander("Model calls: data sent and replies", expanded=False):
        if card.llm_calls:
            st.dataframe(pd.DataFrame([{"Purpose": c.purpose, "Model": c.model, "Role": c.role.value,
                                        "Latency (s)": round(c.latency_s, 2), "Tokens in": c.prompt_tokens,
                                        "Tokens out": c.completion_tokens, "Valid reply": "yes" if c.ok else "no"}
                                       for c in card.llm_calls]), hide_index=True, key=f"trace_calls_{card.index}")
        for k, p in enumerate(card.proposals):
            st.dataframe(pd.DataFrame([{"Check": c.check, "Result": c.result,
                                        "Pass": {True: "pass", False: "FAIL", None: "context"}[c.passed]}
                                       for c in p.checks]), hide_index=True, key=f"trace_pchecks_{card.index}_{k}")
            if p.model_evidence:
                st.dataframe(pd.DataFrame({f"{p.label}: the model's words (quoted)": p.model_evidence}),
                             hide_index=True, key=f"trace_pwords_{card.index}_{k}")
        if card.transcripts:
            for k, tr in enumerate(card.transcripts):  # st.code shows the text as is: never rendered
                st.caption(f"Call {k + 1} ({md_escape(tr.purpose)}): data sent")
                st.code(tr.prompt, language=None, wrap_lines=True)
                st.caption(f"Call {k + 1}: reply" + ("" if tr.reply else " (none: the call failed)"))
                st.code(tr.reply, language=None, wrap_lines=True)
        else:
            st.caption("Data sent and replies are not recorded in this file (only call metadata).")


def trace_actions(card: trace.TurnCard, run: source.Run) -> None:
    st.dataframe(pd.DataFrame([{"Action": g.action, "Target": g.target, "For cause": g.cause, "Gate": g.gate,
                                "Decision": g.decision, "Note (quoted)": g.entry.note if g.entry else ""}
                               for g in card.gate]), hide_index=True, key=f"trace_actions_{card.index}")
    for a in card.actions:
        if a.status is None and isinstance(run, LiveRun) and any(w.request.id == a.request.id for w in run.waiting):
            req = a.request
            with st.container(border=True):
                st.markdown(f"**Approve {md_escape(req.action.value)} on {md_escape(req.target)}?**  \n"
                            f"*Rationale:* {md_escape(req.rationale)}")
                st.text_input("Operator note", key=f"trace_note_{req.id}", placeholder="Optional reason")
                ok, no = st.columns(2)
                ok.button("Approve", key=f"trace_approve_{req.id}", on_click=_trace_decide, args=(req.id, True),
                          type="primary", width="stretch")
                no.button("Reject", key=f"trace_reject_{req.id}", on_click=_trace_decide, args=(req.id, False),
                          width="stretch")


def _trace_decide(action_id: str, approve: bool) -> None:
    note = st.session_state.get(f"trace_note_{action_id}", "").strip()
    runner = _runner()
    if runner is not None:
        runner.decide(action_id, approve, note)  # LiveRun.decide under the runner's lock; it then carries on
    else:
        st.session_state.run.decide(action_id, approve, note)


def trace_stream(run: source.Run, now) -> None:
    cards = trace.turn_cards(run.log, now)
    box = st.container(height=900, key="trace_stream")
    with box:
        if not cards:
            st.info("No turns yet: press Step or Play.")
        quiet: list[trace.TurnCard] = []

        def flush() -> None:
            if quiet:
                span = f"Turn {quiet[0].index}" if len(quiet) == 1 else f"Turns {quiet[0].index}-{quiet[-1].index}"
                st.caption(f"{span} ({local_hhmm(quiet[0].ts)}-{local_hhmm(quiet[-1].ts)}): nothing new.")
                quiet.clear()

        for card in cards:
            if card.quiet:
                quiet.append(card)
                continue
            flush()
            trace_card(card, run)
        flush()
    focus = st.session_state.get(summary.FOCUS_KEY)
    if focus and any(c.index == focus for c in cards):
        _scroll_to_card("trace_stream", int(focus))
    elif st.session_state.get(summary.FOLLOW_KEY, True) and cards:
        _scroll_to_bottom("trace_stream", len(cards))


def _scroll_to_bottom(key: str, marker: int) -> None:
    """Scroll the trace container to its newest card (newest at the bottom). Fixed script, no user text."""
    st.html(f"""<script>
      // turn {marker}
      for (const el of document.querySelectorAll('.st-key-{key}, .st-key-{key} *')) {{
        if (el.scrollHeight > el.clientHeight + 4) {{ el.scrollTop = el.scrollHeight; }}
      }}
    </script>""", unsafe_allow_javascript=True)


def _scroll_to_card(key: str, turn: int) -> None:
    """Scroll the trace container to the card of `turn` (a Summary jump). Fixed script, integers only."""
    st.html(f"""<script>
      // focus turn {turn}
      const card = document.querySelector('.st-key-trace_card_{turn}');
      if (card) {{ card.scrollIntoView({{block: 'start'}}); }}
    </script>""", unsafe_allow_javascript=True)


def _switch_to_trace() -> None:
    """Click the Trace tab after a Summary jump (st.tabs has no Python API for that). Fixed script."""
    st.html("""<script>
      for (const tab of document.querySelectorAll('button[role="tab"]')) {
        if (tab.textContent.trim() === 'Trace') { tab.click(); break; }
      }
    </script>""", unsafe_allow_javascript=True)


def trace_loader() -> None:
    """Open a bench trajectory file (bench/<set>/trajectories/<scenario>__<agent>.json[.gz]) or any replay."""
    with st.expander("Open a trajectory or replay file", expanded=False):
        st.caption("A bench trajectory (written by `raqeeb.bench.run`, plain or .json.gz) or a console replay, any "
                   "version. Older files show the fields they have. The answer key stays hidden until the end.")
        st.text_input("File or folder", key="trace_path", placeholder="bench/final/trajectories")
        files = source.trajectory_files(st.session_state.get("trace_path", "").strip()) \
            if st.session_state.get("trace_path", "").strip() else []
        if len(files) > 1 or (files and files[0] != Path(st.session_state.trace_path.strip())):
            st.selectbox(f"{len(files)} files in that folder", [str(f) for f in files], key="trace_pick",
                         format_func=lambda f: Path(f).name)
        else:
            st.session_state.pop("trace_pick", None)
        st.button("Open", key="trace_open", on_click=_open_trajectory, disabled=not files)
        st.file_uploader("Or upload one (.json or .json.gz)", type=["json", "gz"], key="trace_upload")
        st.button("Open uploaded file", key="trace_open_upload", on_click=_open_trajectory_upload)
        if st.session_state.get("trace_error"):
            st.error(md_escape(st.session_state.trace_error))


def trace_tab(run: source.Run, power: pd.DataFrame) -> None:
    st.subheader("Trace")
    st.caption("One card per agent turn, newest at the bottom: the checks it ran, the symptoms it saw, the causes it "
               "ranked, model calls and how each proposal was tested, actions and the gate's decision, "
               "verifications and escalations. Quiet turns are folded into one line. Free text is quoted data, "
               "collapsed by default.")
    trace_loader()
    if isinstance(run, LiveRun):
        trace_live_controls(run)
    runner = _runner(run)
    active = runner is not None and runner.state().running
    st.session_state.trace_active = active
    # While the background runner advances the run, only this part re-renders, about once a second
    st.fragment(run_every=TRACE_REFRESH_S if active else None)(_trace_body)()


def trace_live_controls(run: LiveRun) -> None:
    runner = _runner(run)
    state = runner.state() if runner is not None else None
    running = bool(state and state.running)
    a, b, c, d = st.columns([1, 2, 1, 3])
    a.button("Pause" if running else "Run", key="trace_run", on_click=_trace_run_toggle, type="primary",
             width="stretch", disabled=run.finished)
    b.select_slider("Pace (agent turns per second)", PACES, value=st.session_state.get("trace_pace", DEFAULT_PACE),
                    key="trace_pace", on_change=_trace_pace)
    c.button("Jump to end", key="trace_jump", on_click=_trace_jump, width="stretch", disabled=run.finished)
    with d:
        if state is None:
            st.caption("Run plays the live run in the background at the chosen pace; the page keeps up. Jump to end "
                       "runs without pausing between turns. Both stop when an action needs your approval.")
        elif state.error:
            st.error(md_escape(f"The run stopped: {state.error}"))
        elif state.finished:
            st.caption(f"Run finished after {state.turns} turns. Use the clock below to go back through it.")
        elif state.waiting:
            st.warning("Waiting for your decision: approve or reject on the card below (or in Approvals).")
        else:
            st.caption(("Running" + (", jumping to the end" if state.fast else "")) if running else "Paused")


def _trace_body() -> None:
    run: source.Run = st.session_state.run
    with run_lock(run):
        runner = _runner(run)
        active = runner is not None and runner.state().running
        if st.session_state.get("trace_active") and not active:
            # The runner stopped (approval needed, or the end): refresh the whole page once
            st.session_state.trace_active = False
            st.rerun()
        view: source.Run | source.RunAt = run
        polls = run.log.polls
        if polls and not active:
            # The clock: the newest turn, unless a Summary item asked for an earlier one; a live run that moved
            # on since the clock was last set goes back to following the newest turn
            if st.session_state.get("trace_clock_n") != len(polls) and summary.FOCUS_KEY not in st.session_state:
                st.session_state.pop(summary.CLOCK_KEY, None)
            st.session_state.trace_clock_n = len(polls)
            if summary.CLOCK_KEY not in st.session_state:
                st.session_state[summary.CLOCK_KEY] = len(polls) - 1
            st.session_state[summary.CLOCK_KEY] = min(st.session_state[summary.CLOCK_KEY], len(polls) - 1)
            k = st.select_slider("Clock (scrub through the turns)", options=list(range(len(polls))),
                                 key=summary.CLOCK_KEY,
                                 format_func=lambda i: f"turn {i + 1} · {local_hhmm(polls[i].ts)}")
            if k < len(polls) - 1:
                view = source.RunAt(run, polls[k].ts)
            if st.session_state.get(summary.FOCUS_KEY) not in (None, k + 1):
                st.session_state.pop(summary.FOCUS_KEY)  # the clock was moved by hand: the focus is over
        trace_header(trace.header(view.log, view.now, live=isinstance(run, LiveRun)))
        left, right = st.columns([5, 6], gap="medium")
        with left:  # the plant as the agent reasons: same map and chart as the Operations view, compact
            plant = load_plant()
            st.plotly_chart(plantmap.map_figure(plantmap.map_view(view, plant), plant, compact=True),
                            key="trace_map", config={"displayModeBar": False})
            st.plotly_chart(recovery_figure(view, source.power_lines(view), height=300), key="trace_recovery")
            st.caption("Map: telemetry and the agent's record at the sim clock. Chart: meter vs expected from the "
                       "reference and from the on-site sensor; vertical lines are applied actions.")
        with right:
            if summary.FOLLOW_KEY not in st.session_state:
                st.session_state[summary.FOLLOW_KEY] = True
            st.toggle("Follow the newest turn", key=summary.FOLLOW_KEY)
            trace_stream(run, view.now)



def report_markdown(run: source.Run, report: IncidentReport) -> str:
    statuses = {a.request.id: a.status for a in run.log.actions_until(run.now)}
    verified = {v.action_id: v for v in report.verifications}
    lines = [f"# Shift report: {run.incident.id}", "",
             f"> Synthetic plant. {source.mode_label(run)}. Times in {DISPLAY_TZ}.", "",
             "## Summary (English)", "", report.summary_en, "",
             "## الملخص (العربية)", "", report.summary_ar, "",
             "## Root causes", ""]
    lines += [f"- `{', '.join(h.labels)}`: {'; '.join(h.evidence)}" for h in report.causes]
    lines += ["", "## Actions", "", "| Action | Target | Status | Verified |", "|---|---|---|---|"]
    for a in report.actions:
        status = statuses.get(a.id)
        v = verified.get(a.id)
        check = ("recovered" if v.recovered else "not recovered") if v else "—"
        lines.append(f"| {a.action.value} | {a.target} | {status.value.lower() if status else 'waiting'} | {check} |")
    lines += ["", f"LLM calls: {report.llm_calls} · cost: {report.cost_usd:.4f} USD", ""]
    return "\n".join(lines)


def report_columns(report: IncidentReport) -> None:
    """The agent's bilingual summary, English left, Arabic right (right-to-left)."""
    en, ar = st.columns(2)
    with en:
        st.markdown("**English**")
        st.markdown(md_escape(report.summary_en))
    with ar:
        st.markdown("**العربية**")
        st.markdown(f'<div dir="rtl" lang="ar" style="text-align: right">{source.html_text(report.summary_ar)}</div>',
                    unsafe_allow_html=True)


# -- summary tab ------------------------------------------------------------------------------------------

MOMENT_COLOUR = {summary.SYMPTOM: "#e07b39", summary.CAUSE: "#6b4fbb", summary.PROPOSAL: "#1f8a9e",
                 summary.ACTION: "#2b7bba", summary.APPROVAL: "#b8860b", summary.VERIFICATION: "#2e8b57",
                 summary.ESCALATION: "#c0392b"}
MOMENT_SYMBOL = {summary.SYMPTOM: "diamond", summary.CAUSE: "circle", summary.PROPOSAL: "triangle-up",
                 summary.ACTION: "square", summary.APPROVAL: "star", summary.VERIFICATION: "circle-open",
                 summary.ESCALATION: "x"}
OUTCOME_COLOUR = {"rejected": "#fdecea", "not recovered": "#fdecea", "never sent": "#fdecea", "escalated": "#fdecea",
                  "accepted": "#e8f5e9", "recovered": "#e8f5e9", "approved": "#e8f5e9", "confirmed": "#ede7f6"}


def _jump_to_turn(turn: int) -> None:
    summary.jump(st.session_state, int(turn))


def _timeline_clicked() -> None:
    event = st.session_state.get("summary_timeline")
    points = (event or {}).get("selection", {}).get("points", []) if isinstance(event, dict) else \
        getattr(getattr(event, "selection", None), "points", [])
    lookup = st.session_state.get("summary_points", {})
    for point in points:
        turn = lookup.get((point.get("curve_number"), point.get("point_index", point.get("point_number"))))
        if turn is not None:
            _jump_to_turn(turn)
            return


def _moment_selected() -> None:
    event = st.session_state.get("summary_moments")
    rows = (event or {}).get("selection", {}).get("rows", []) if isinstance(event, dict) else \
        getattr(getattr(event, "selection", None), "rows", [])
    turns = st.session_state.get("summary_moment_turns", [])
    if rows and 0 <= rows[0] < len(turns):
        _jump_to_turn(turns[rows[0]])


def timeline_figure(moments: list[summary.Moment], run: source.Run) -> go.Figure:
    """One lane per kind of moment, time across; hovering shows the text, clicking jumps to the turn."""
    fig = go.Figure()
    lookup: dict[tuple[int, int], int] = {}
    for curve, kind in enumerate(summary.KINDS):
        mine = [m for m in moments if m.kind == kind]
        fig.add_trace(go.Scatter(
            x=[local(m.ts) for m in mine], y=[kind] * len(mine), name=kind, mode="markers",
            marker={"size": 13, "color": MOMENT_COLOUR[kind], "symbol": MOMENT_SYMBOL[kind],
                    "line": {"width": 1, "color": "white"}},
            text=[f"turn {m.turn} · {local_hhmm(m.ts)}<br>{source.html_text(m.text)}" for m in mine],
            hovertemplate="%{text}<extra></extra>", showlegend=False))
        lookup.update({(curve, i): m.turn for i, m in enumerate(mine)})
    st.session_state.summary_points = lookup
    fig.update_layout(height=60 + 34 * len(summary.KINDS), margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      yaxis={"categoryorder": "array", "categoryarray": list(reversed(summary.KINDS)),
                             "fixedrange": True},
                      xaxis={"range": [local(run.incident.start), local(run.incident.end)],
                             "title": f"Time ({DISPLAY_TZ})"},
                      clickmode="event+select", dragmode=False)
    return fig


def summary_tab(run: source.Run, power: pd.DataFrame) -> None:
    st.subheader("Incident summary")
    now = run.now
    log = run.log
    moments = summary.key_moments(log, now)
    head = trace.header(log, now, live=isinstance(run, LiveRun))
    e = summary.energy(power, log.actions_until(now), now)
    rows = summary.cause_rows(log, now)
    if not log.polls_until(now):
        st.info("No turns yet: press Step or Play. The summary fills in as the agent works.")
        return
    a, b, c, d, f = st.columns(5)
    a.metric("Turns", head.turn if head.turns_total is None else f"{head.turn} / {head.turns_total}")
    b.metric("Causes named", sum(len(v) for v in head.causes.values()),
             f"{len(head.causes[trace.RESOLVED])} resolved · {len(head.causes[trace.ESCALATED])} escalated",
             delta_color="off")
    c.metric("Energy below expected", f"{e.lost_mwh:.2f} MWh",
             f"{e.lost_before_mwh:.2f} before the first action took effect · {e.lost_after_mwh:.2f} after"
             if e.first_effective else "no action has taken effect yet", delta_color="off")
    d.metric("Shortfall, worst hour → last hour",
             "—" if e.gap_worst_pct is None else f"{e.gap_worst_pct:.0f}% → "
             + ("—" if e.gap_last_pct is None else f"{e.gap_last_pct:.0f}%"))
    f.metric("Model calls", head.llm_calls, f"{head.llm_s:.1f} s in the model" if head.llm_calls else "rules only",
             delta_color="off")
    st.caption("Energy is the area under the expected-from-reference line minus the meter, up to the sim clock: the "
               "shortfall, whatever its cause (the reference itself carries a few percent of error). Recovery shows "
               "as the shortfall after the first applied action took effect, and in the last hour's percentage.")

    st.markdown("**Key moments** — click a marker or a row to open that turn in the Trace tab.")
    if moments:
        st.plotly_chart(timeline_figure(moments, run), key="summary_timeline", on_select=_timeline_clicked,
                        selection_mode="points", config={"displayModeBar": False})
        frame = pd.DataFrame([{"Time": local_hhmm(m.ts), "Turn": m.turn, "What": m.kind,
                               "Detail": m.text, "Outcome": m.outcome} for m in moments])
        st.session_state.summary_moment_turns = [m.turn for m in moments]
        styled = frame.style.apply(
            lambda col: [f"background-color: {OUTCOME_COLOUR.get(v, '')}" if OUTCOME_COLOUR.get(v) else ""
                         for v in col], subset=["Outcome"])
        st.dataframe(styled, hide_index=True, key="summary_moments", on_select=_moment_selected,
                     selection_mode="single-row", column_config={"Turn": st.column_config.NumberColumn(width="small")})
    else:
        st.caption("Nothing to report yet: no symptom, cause, action or model proposal so far.")

    st.markdown("**Causes**")
    if rows:
        st.dataframe(pd.DataFrame([{"Cause": r.cause, "Status": r.status,
                                    "First named": f"turn {r.turn}" if r.turn else "—",
                                    "Actions": r.actions, "Verified": r.verified} for r in rows]),
                     hide_index=True, key="summary_causes")
        picks = [r for r in rows if r.turn]
        if picks:
            cols = st.columns(min(len(picks), 4))
            for i, r in enumerate(picks[:8]):
                cols[i % len(cols)].button(f"Turn {r.turn}: {r.cause}", key=f"summary_go_{r.cause}",
                                           on_click=_jump_to_turn, args=(r.turn,), width="stretch")
    else:
        st.caption("No cause named yet.")

    report = log.report_until(now)
    st.markdown("**Shift report**")
    if report is None:
        st.caption("The agent writes the shift report at the end of the run.")
    else:
        report_columns(report)
    if st.session_state.pop(summary.SWITCH_KEY, False):
        focus = st.session_state.get(summary.FOCUS_KEY)
        st.toast(f"Trace tab opened at turn {focus}.")
        _switch_to_trace()


# -- ask raqeeb -------------------------------------------------------------------------------------------


def _ask_gateway():
    """The local model's gateway when the server is reachable (the sidebar's model, else .env's), else None."""
    if not local_status()[0]:
        return None
    from raqeeb.llm.gateway import Gateway
    from raqeeb.config import load_settings
    settings = load_settings()
    model = st.session_state.get("model_choice")
    if model and model != RULES_ONLY and model != settings.local_llm_model:
        settings = settings.model_copy(update={"local_llm_model": model})
    return Gateway(settings)


def ask_tab(run: source.Run, power: pd.DataFrame) -> None:
    st.subheader("Ask Raqeeb")
    st.caption("Questions about this run, answered from its record: code looks the facts up (actions and their "
               "rationale, model proposals, approvals, what is unexplained, the energy shortfall); when the local "
               "model is reachable it only rewords those facts, shown underneath as \"Based on:\". The chat cannot "
               "request, approve or reject an action. Try: " + "; ".join(f"*{e}*" for e in ask.EXAMPLES) + ".")
    history: list[ask.Answer] = st.session_state.setdefault("ask_history", [])
    question = st.chat_input("Ask about this run", key="ask_input", max_chars=400)
    if question and question.strip():
        with run_lock(run):
            history.append(ask.ask(question.strip(), run.log, run.now, power, gateway=_ask_gateway()))
    if not history:
        st.info("No question yet.")
    for a in history:
        with st.chat_message("user"):
            st.markdown(md_escape(a.question))
        with st.chat_message("assistant"):
            st.markdown(md_escape(a.text))
            if a.phrased_by:
                st.caption(f"Worded by {md_escape(a.phrased_by)} in {a.llm_s:.1f} s from the facts below, nothing else.")
            else:
                st.caption("No model reachable" + (f" ({md_escape(a.note)})" if a.note else "") + ": the facts are "
                           "shown as retrieved.")
            with st.expander("Based on:", expanded=bool(a.phrased_by)):
                st.markdown("\n".join(f"- {md_escape(f)}" for f in a.facts))


def report_tab(run: source.Run) -> None:
    st.subheader("Shift report")
    report = run.log.report_until(run.now)
    if report is None:
        st.info("The agent writes the shift report at the end of the run.")
        return
    report_columns(report)
    st.download_button("Download report (Markdown)", report_markdown(run, report),
                       file_name=f"raqeeb_report_{run.incident.id}.md", mime="text/markdown", key="download_report")
    with st.expander("Markdown source"):
        st.code(report_markdown(run, report), language="markdown")


def answer_key_tab(run: source.Run) -> None:
    st.subheader("Answer key")
    result = source.reveal(run)
    if result is None:
        st.info("Hidden while the run is in progress. The agent never sees it; it appears here once the run ends.")
        return
    if not st.toggle("Reveal answer key", key="reveal"):
        st.caption("The run has ended. Turn on the switch to compare the agent with the planted faults.")
        return
    st.markdown(f"The agent named **{result.found} of {result.root_causes}** planted root causes"
                + (f"; it also named {', '.join(md_escape(c) for c in result.extra_causes)}, which were not planted."
                   if result.extra_causes else "."))
    st.dataframe(result.faults, hide_index=True, key="reveal_faults")
    st.markdown("**Actions against the answer key**")
    st.dataframe(result.actions, hide_index=True, key="reveal_actions")


def results_tab() -> None:
    st.subheader("Benchmark results")
    folders = source.results_folders()
    st.session_state.setdefault("results_path", str(source.default_results_path()))
    if folders:
        st.selectbox("Bench output folders", [str(p) for p in folders], index=None, key="results_pick",
                     placeholder="Pick a folder, or type a path below", format_func=lambda p: Path(p).name,
                     on_change=lambda: st.session_state.update(results_path=st.session_state.results_pick))
    st.text_input("Results folder or summary file (.csv / .json)", key="results_path")
    path = Path(st.session_state.results_path)
    path = path if path.is_absolute() else REPO_ROOT / path
    results = source.load_results(path)
    if results is None:
        st.info(f"Results not available yet at {md_escape(path.name)}. The final benchmark results arrive after "
                "the final run; until then pick a practice folder above.")
        return
    summary = results.summary
    shown = summary[[c for c in SUMMARY_COLUMNS if c in summary.columns]].rename(columns=SUMMARY_COLUMNS)
    st.dataframe(shown, hide_index=True, key="results_summary")
    if "mwh_recovered" in summary.columns:
        bars = summary.sort_values("mwh_recovered")
        notes = bars["note"].fillna("").astype(str) if "note" in bars.columns else pd.Series("", index=bars.index)
        colors = [MUTED if n else SERIES["actual"] for n in notes]
        fig = go.Figure(go.Bar(x=bars["mwh_recovered"], y=bars["agent"], orientation="h", marker_color=colors,
                               text=[f"{v:.1f}" for v in bars["mwh_recovered"]], textposition="outside",
                               hovertemplate="%{y}: %{x:.1f} MWh<extra></extra>"))
        fig.update_layout(height=60 + 45 * len(bars), margin={"l": 0, "r": 40, "t": 10, "b": 0},
                          xaxis_title="Energy recovered vs no action (MWh)")
        st.plotly_chart(fig, key="results_chart")
        lost = summary["mwh_lost"].iloc[0] if "mwh_lost" in summary.columns else None
        st.caption("Grey bars are reference bounds, not competitors (oracle reads the answer key; empty does nothing)."
                   + (f" Energy lost to the faults with no action: {lost:.1f} MWh." if lost is not None else ""))
    if results.results is not None and {"agent", "scenario_id", "mwh_recovered"} <= set(results.results.columns):
        with st.expander("Per scenario: MWh recovered"):
            st.dataframe(results.results.pivot_table(index="scenario_id", columns="agent", values="mwh_recovered"),
                         key="results_per_scenario")
    if results.markdown:
        with st.expander("Full summary (summary.md)"):
            st.markdown(results.markdown)


def footer() -> None:
    st.divider()
    st.caption(f"{source.FOOTER} {source.NASA_CREDIT}")


# -- page ------------------------------------------------------------------------------------


def initial_run() -> source.Run:
    replays = replay_choices()
    if replays:
        st.session_state.setdefault("replay_choice", next(iter(replays)))
        return source.start_replay(st.session_state.replay_choice)
    return source.start_live(scenario_choices()[1], Mode.RULES)


def main() -> None:
    st.set_page_config(page_title="Raqeeb console", layout="wide")
    _, default_id = scenario_choices()
    st.session_state.setdefault("scenario_id", default_id)
    st.session_state.setdefault("speed", 60)
    st.session_state.setdefault("playing", False)
    if "run" not in st.session_state:
        st.session_state.run = initial_run()
        st.session_state.mode_choice = REPLAY if isinstance(st.session_state.run, ReplayRun) else LIVE_RULES
    run: source.Run = st.session_state.run

    with run_lock(run):  # a background runner (live Trace) polls under the same lock
        sidebar(run)
        st.title("Raqeeb console")
        st.caption(source.FOOTER)
        kind = "Replay" if isinstance(run, ReplayRun) else "Live"
        st.info(f"**Mode: {md_escape(source.mode_label(run))}**  \n{kind} run of incident "
                f"**{md_escape(run.incident.id)}** · {local(run.incident.start):%Y-%m-%d} · sim clock "
                f"**{local(run.now):%H:%M}** ({DISPLAY_TZ}) · site {md_escape(load_plant().site.name)}")
        _page(run)

    if st.session_state.playing:  # replays (live runs play in the background runner)
        if run.finished or run.waiting:
            st.session_state.playing = False
        else:
            time.sleep(TICK_S)
            run.advance(st.session_state.speed)
            st.rerun()


def alerts_banner(run: source.Run) -> None:
    """Email alerts of this live run that still matter: for actions waiting now, and for this turn's
    escalations. Off (no alerts recorded) when RAQEEB_ALERTS=0 or in a replay."""
    if not isinstance(run, LiveRun):
        return
    waiting = {a.request.id for a in run.waiting}
    for r in run.alerts:
        if not (r.alert.ref in waiting or r.alert.sim_time == run.now):
            continue
        who = ", ".join(run.alerter.config.recipients)
        what = md_escape(r.alert.decision)
        if r.status == alerts.NOT_CONFIGURED:
            st.info(f"**{alerts.BANNER_NOT_CONFIGURED}:** {what}. Set RESEND_API_KEY, RAQEEB_ALERT_FROM and "
                    "RAQEEB_ALERT_TO in .env to email the operator.")
        elif r.status == alerts.SENT:
            st.success(f"**Alert emailed** to {md_escape(who)}: {what}.")
        elif r.status == alerts.FAILED:
            st.warning(f"**Alert email failed** ({md_escape(r.error)}, {r.attempts} attempts): {what}. "
                       "The run went on.")
        else:
            st.info(f"**Alert being emailed** to {md_escape(who)}: {what}.")


def _page(run: source.Run) -> None:
    data = source.tables(run)
    power = lines(run)
    if run.waiting:
        st.warning("The agent requested an action that needs human approval. The run is paused until you decide "
                   "(Approvals, below the map).")
    alerts_banner(run)
    plant_map(run)
    if run.waiting:
        approvals(run)
    headline(run, power, data)

    summary_page, ops, trace_page, agent, report, key, results, ask_page = st.tabs(
        ["Summary", "Operations", "Trace", "Agent", "Report", "Answer key", "Results", "Ask Raqeeb"])
    with trace_page:
        trace_tab(run, power)
    with summary_page:
        summary_tab(run, power)
    with ops:
        alarm_feed(data)
        recovery_chart(run, power)
        notes_panel(run)
    with agent:
        left, right = st.columns([3, 2])
        with left:
            agent_timeline(run)
        with right:
            if not run.waiting:
                approvals(run)
        proposals_panel(run)
        actions_panel(run)
        llm_panel(run)
    with report:
        report_tab(run)
    with key:
        answer_key_tab(run)
    with results:
        results_tab()
    with ask_page:
        ask_tab(run, power)
    footer()


main()
