"""Raqeeb operator console (HANDOFF section 12).

    uv run streamlit run raqeeb/ui/app.py

All data comes through raqeeb/ui/source.py. Plant telemetry is synthetic (simulated from NASA
POWER weather); the agent timeline, approvals, results and report are SAMPLE content until the
agent and benchmark are integrated. Times are shown in Asia/Riyadh.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from raqeeb.config import load_plant
from raqeeb.contracts import DISPLAY_TZ, STEP_MINUTES, AuditEntry, IncidentReport
from raqeeb.ui import source
from raqeeb.ui.samples import local_hhmm

# Reference data-viz palette: status colors carry a text label too; series slots in fixed order
STATE_STYLE = {  # map state -> (fill, label ink, cell label)
    source.HEALTHY: ("#0ca30c", "#ffffff", "OK"),
    source.DEGRADED: ("#fab219", "#0b0b0b", "DEGRADED"),
    source.NO_DATA: ("#898781", "#ffffff", "NO DATA"),
}
SERIES = {"actual": "#2a78d6", "reference": "#eb6834", "sensor": "#1baf7a"}
MUTED = "#898781"
CLOCK_STEP_MIN = 30
RECENT_ALARM_MIN = 30
MIN_GAP_FRACTION = 0.05  # of the plant rating: below this (dawn, dusk) no "vs expected" delta
ON_EXPECTED_FRACTION = 0.01

SYNTHETIC_NOTE = ("**Synthetic data.** The plant, its telemetry, the faults and the alarm codes are simulated "
                  "(weather from NASA POWER). The agent timeline, approvals, results and report are **SAMPLE** "
                  "content until the agent and the benchmark are integrated.")


# -- data (cached per run) -----------------------------------------------------


@st.cache_data(show_spinner="Running the plant twin...")
def load_run(kind: str, key: str) -> source.PlantRun:
    return source.inject_storm(int(key)) if kind == "storm" else source.replay(key)


@st.cache_data
def scenario_ids() -> tuple[list[str], str]:
    return source.scenario_ids(), source.default_scenario_id()


def local(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_convert(DISPLAY_TZ)


# -- state and callbacks ---------------------------------------------------------


def _set_run(kind: str, key: str) -> None:
    st.session_state.run_key = (kind, key)
    st.session_state.pop("clock", None)  # a new run starts with its whole window shown
    st.session_state.decisions = {}


def _inject_storm() -> None:
    _set_run("storm", str(random.randint(1, 10**6)))


def _replay() -> None:
    _set_run("scenario", st.session_state.replay_id)


def _move_clock(steps: int, last: int) -> None:
    st.session_state.clock = min(max(st.session_state.clock + steps, 0), last)


def _decide(action_id: str, decision: str) -> None:
    st.session_state.decisions[action_id] = {
        "decision": decision,
        "note": st.session_state.get(f"note_{action_id}", "").strip(),
        "ts": st.session_state.now,
    }


# -- panels --------------------------------------------------------------------


def sidebar(ids: list[str], run: source.PlantRun) -> None:
    with st.sidebar:
        st.header("Inject faults")
        st.caption("Plant faults are injected into the synthetic twin.")
        st.button("Dust storm + 2 random faults", key="inject_storm", on_click=_inject_storm, width="stretch")
        st.selectbox("Scenario", ids, key="replay_id")
        st.button("Replay scenario", key="replay", on_click=_replay, width="stretch")
        with st.expander("Planted faults (answer key, never shown to the agent)"):
            for f in run.scenario.faults:
                when = local_hhmm(run.scenario.resolve_time(f.start))
                decoy = " (decoy, expected behaviour)" if f.type == "CURTAILMENT" else ""
                st.markdown(f"- `{f.label}` from {when}{decoy}")

        st.header("Simulated clock")
        last = len(run.times) - 1
        st.select_slider("Time (Asia/Riyadh)", options=list(range(last + 1)), key="clock",
                         format_func=lambda i: local(run.times[i]).strftime("%H:%M"))
        back, fwd, end = st.columns(3)
        steps = CLOCK_STEP_MIN // STEP_MINUTES
        back.button(f"◀ {CLOCK_STEP_MIN}m", key="clock_back", on_click=_move_clock, args=(-steps, last),
                    width="stretch")
        fwd.button(f"{CLOCK_STEP_MIN}m ▶", key="clock_fwd", on_click=_move_clock, args=(steps, last),
                   width="stretch")
        end.button("End", key="clock_end", on_click=_move_clock, args=(last, last), width="stretch")


def default_clock(run: source.PlantRun) -> int:
    """Open on the last sample-agent event, so the whole incident is visible in daylight."""
    events = [e.ts for e in run.agent.timeline]
    if not events:
        return len(run.times) - 1
    return min(int(run.times.searchsorted(max(events), side="right")) - 1, len(run.times) - 1)


def headline(run: source.PlantRun, now: datetime, pending: int) -> None:
    line = run.lines.loc[now]
    events = source.until(run.tables["events"], now)
    readings = run.tables["inverter_readings"]
    reporting = readings.loc[readings["ts"] == now, "inverter_id"].nunique()
    a, b, c, d = st.columns(4)
    plant = load_plant()
    daylight = line["expected_ref_kw"] > MIN_GAP_FRACTION * plant.rated_ac_kw
    gap = line["actual_kw"] / line["expected_ref_kw"] - 1 if daylight else None
    on_line = gap is not None and abs(gap) < ON_EXPECTED_FRACTION
    a.metric("Plant output (meter)", f"{line['actual_kw'] / 1000:.1f} MW",
             None if gap is None else "on expected (reference)" if on_line else f"{gap:+.0%} vs expected (reference)",
             delta_color="off" if on_line else "normal")
    recent = int((events["ts"] > now - timedelta(minutes=RECENT_ALARM_MIN)).sum())
    b.metric("Alarms so far", len(events), f"+{recent} in last {RECENT_ALARM_MIN} min" if recent else None,
             delta_color="inverse")
    c.metric("Inverters reporting", f"{reporting} / {plant.inverters.count}")
    d.metric("Pending approvals (sample)", pending)


def plant_map(run: source.PlantRun, now: datetime) -> None:
    st.subheader("Plant map")
    plant = load_plant()
    status = source.inverter_status(run, now, plant)
    rows, cols = plant.inverters.layout_rows, plant.inverters.layout_cols
    fig = go.Figure()
    for k, r in status.iterrows():
        row, col = divmod(k, cols)
        fill, ink, label = STATE_STYLE[r["state"]]
        reporting = r["state"] != source.NO_DATA
        detail = f"{r['ac_kw'] / 1000:.2f} MW" if reporting else "—"
        fig.add_shape(type="rect", x0=col + 0.04, x1=col + 0.96, y0=-row - 0.04, y1=-row - 0.96,
                      fillcolor=fill, line={"width": 0}, layer="below")
        fig.add_annotation(x=col + 0.5, y=-row - 0.5, showarrow=False, font={"color": ink, "size": 13},
                           text=f"<b>{r['inverter_id']}</b><br>{detail}<br>{label}")
        hover = (f"{r['inverter_id']}: {r['state']}<br>status {r['status']}<br>AC {detail}<br>"
                 f"internal temp {r['temp_c']:.0f} °C" if reporting else f"{r['inverter_id']}: no data received")
        fig.add_trace(go.Scatter(x=[col + 0.5], y=[-row - 0.5], mode="markers", hoverinfo="text", hovertext=hover,
                                 marker={"size": 60, "opacity": 0, "symbol": "square"}, showlegend=False))
    fig.update_xaxes(visible=False, range=[0, cols], fixedrange=True)
    fig.update_yaxes(visible=False, range=[-rows, 0], fixedrange=True, scaleanchor="x")
    fig.update_layout(height=420, margin={"l": 0, "r": 0, "t": 0, "b": 0})
    st.plotly_chart(fig, key="plant_map")
    counts = status["state"].value_counts()
    st.caption(" · ".join(f"{STATE_STYLE[s][2]}: {counts.get(s, 0)}" for s in STATE_STYLE)
               + f". Degraded = derated, stopped or ≥{source.PEER_SHORTFALL:.0%} below peers. Synthetic plant.")


def alarm_feed(run: source.PlantRun, now: datetime) -> None:
    st.subheader("Alarm feed")
    events = source.until(run.tables["events"], now).sort_values("ts", ascending=False)
    by_severity = events["severity"].astype(str).value_counts()
    st.caption(f"{len(events)} alarms · " + " · ".join(f"{s}: {by_severity.get(s, 0)}"
                                                       for s in ("CRITICAL", "WARNING", "INFO")))
    table = pd.DataFrame({
        "Time": events["ts"].map(lambda t: local(t).strftime("%H:%M")),
        "Device": events["device_id"], "Code": events["code"],
        "Severity": events["severity"].astype(str), "Message": events["message"],
    })
    st.dataframe(table, hide_index=True, height=380)


def recovery_chart(run: source.PlantRun, now: datetime) -> None:
    st.subheader("Recovery")
    lines = run.lines[run.lines.index <= now]
    x = lines.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    for col, name, color, dash in (
        ("actual_kw", "Actual (meter)", SERIES["actual"], "solid"),
        ("expected_ref_kw", "Expected from trusted reference", SERIES["reference"], "solid"),
        ("expected_sensor_kw", "Expected from on-site sensor", SERIES["sensor"], "dot"),
    ):
        fig.add_trace(go.Scatter(x=x, y=lines[col] / 1000, name=name, mode="lines",
                                 line={"color": color, "width": 2, "dash": dash},
                                 hovertemplate="%{y:.1f} MW"))
    for p in run.agent.executed:
        if p.at <= now:
            fig.add_vline(x=local(p.at), line={"color": MUTED, "width": 1})
            fig.add_annotation(x=local(p.at), y=1, yref="paper", text=f"{p.request.action.value} (sample)",
                               showarrow=False, textangle=-90, xanchor="right", yanchor="top",
                               font={"size": 10, "color": MUTED})
    fig.update_layout(height=380, hovermode="x unified", margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      legend={"orientation": "h", "y": 1.12, "x": 0},
                      yaxis_title="Plant AC (MW)", xaxis_title=f"Time ({DISPLAY_TZ})",
                      xaxis_range=[local(run.start), local(run.end)])
    st.plotly_chart(fig, key="recovery_chart")
    st.caption("Expected lines are the fault-free plant run on the weather each source reports. When the sensor is "
               "dusty, the sensor line drops to meet a dusty plant and the loss looks normal; the reference line "
               "still shows it. Vertical lines mark the sample agent's automatic actions.")

    fig = go.Figure()
    for col, name, color, dash in (
        ("sensor_ghi_wm2", "On-site sensor GHI", SERIES["sensor"], "dot"),
        ("reference_ghi_wm2", "Reference GHI (NASA POWER)", SERIES["reference"], "solid"),
        ("clearsky_ghi_wm2", "Clear-sky model (shape reference)", MUTED, "solid"),
    ):
        fig.add_trace(go.Scatter(x=x, y=lines[col], name=name, mode="lines",
                                 line={"color": color, "width": 2 if color != MUTED else 1, "dash": dash},
                                 hovertemplate="%{y:.0f} W/m²"))
    fig.update_layout(height=260, hovermode="x unified", margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      legend={"orientation": "h", "y": 1.2, "x": 0}, yaxis_title="GHI (W/m²)",
                      xaxis_range=[local(run.start), local(run.end)])
    st.plotly_chart(fig, key="irradiance_chart")
    st.caption("The clear-sky model gives the shape of a cloudless day for comparison. It is not a ceiling: the "
               "reference itself can sit above it.")


def agent_timeline(run: source.PlantRun, now: datetime) -> None:
    st.subheader("Agent timeline (sample)")
    st.caption("SAMPLE: scripted from the planted faults to exercise the console. The real agent replaces it.")
    entries = [e for e in run.agent.timeline if e.ts <= now] + [
        AuditEntry(ts=d["ts"], step="approval", actor="human:operator",
                   payload={"evidence": d["note"] or "No note.", "decision": f"{d['decision']} {action_id}."})
        for action_id, d in st.session_state.decisions.items()
    ]
    if not entries:
        st.info("No agent activity yet at this time.")
    for e in sorted(entries, key=lambda e: e.ts):
        with st.container(border=True):
            st.markdown(f"**{local_hhmm(e.ts)}** · `{e.step}` · {e.actor}  \n{e.payload.get('decision', '')}")
            st.caption(e.payload.get("evidence", ""))


def approvals(run: source.PlantRun, now: datetime) -> None:
    st.subheader("Approvals (sample)")
    visible = [p for p in run.agent.approvals if p.at <= now]
    decisions = st.session_state.decisions
    if not visible:
        st.info("No gated actions waiting at this time.")
    for p in visible:
        req = p.request
        with st.container(border=True):
            params = ", ".join(f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in req.params.items())
            st.markdown(f"**{req.action.value}** on **{req.target}**" + (f" ({params})" if params else "")
                        + f"  \nProposed {local_hhmm(p.at)} · reversible: {'yes' if req.reversible else 'no'}")
            st.markdown(f"*Rationale:* {req.rationale}  \n*Expected effect:* {req.expected_effect}")
            if req.id in decisions:
                d = decisions[req.id]
                st.success(f"{d['decision']} at {local_hhmm(d['ts'])}" + (f": {d['note']}" if d["note"] else ""))
                continue
            st.text_input("Operator note", key=f"note_{req.id}", placeholder="Optional reason for the decision")
            ok, no = st.columns(2)
            ok.button("Approve", key=f"approve_{req.id}", on_click=_decide, args=(req.id, "Approved"),
                      type="primary", width="stretch")
            no.button("Reject", key=f"reject_{req.id}", on_click=_decide, args=(req.id, "Rejected"),
                      width="stretch")
    st.caption("SAMPLE: decisions are kept in this session. With the agent, they resume its approval interrupt.")


def results_tab() -> None:
    st.subheader("Benchmark: Raqeeb vs threshold-alarm baseline")
    st.warning("SAMPLE values, not measured. The benchmark harness will write the real table, "
               "including the metrics where the baseline does better.")
    st.dataframe(source.benchmark(), hide_index=True, width="stretch")


def report_markdown(run: source.PlantRun, report: IncidentReport) -> str:
    executed = {p.request.id for p in run.agent.executed}
    verified = {v.action_id: v for v in report.verifications}
    lines = [f"# Shift report: {run.scenario.id}", "",
             "> SAMPLE report on a synthetic plant. Times in Asia/Riyadh.", "",
             "## Summary (English)", "", report.summary_en, "",
             "## الملخص (العربية)", "", report.summary_ar, "",
             "## Root causes", ""]
    lines += [f"- `{h.labels[0]}`: {'; '.join(h.evidence)}" for h in report.causes]
    lines += ["", "## Actions", "", "| Action | Target | Status | Verified |", "|---|---|---|---|"]
    for a in report.actions:
        status = "executed" if a.id in executed else "waiting for approval"
        v = verified.get(a.id)
        check = ("recovered" if v.recovered else "not recovered") if v else "—"
        lines.append(f"| {a.action.value} | {a.target} | {status} | {check} |")
    lines += ["", f"LLM calls: {report.llm_calls} · cost: {report.cost_usd:.3f} USD", ""]
    return "\n".join(lines)


def report_tab(run: source.PlantRun) -> None:
    report = run.agent.report
    st.subheader("Shift report (sample)")
    st.caption("SAMPLE: built from the scripted agent. The agent's report step replaces it.")
    en, ar = st.columns(2)
    with en:
        st.markdown("**English**")
        st.write(report.summary_en)
    with ar:
        st.markdown("**العربية**")
        st.markdown(f'<div dir="rtl" lang="ar" style="text-align: right">{report.summary_ar}</div>',
                    unsafe_allow_html=True)
    st.download_button("Download report (Markdown)", report_markdown(run, report),
                       file_name=f"raqeeb_report_{run.scenario.id}.md", mime="text/markdown", key="download_report")
    with st.expander("Preview Markdown"):
        st.markdown(report_markdown(run, report))


# -- page -----------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Raqeeb console", layout="wide")
    ids, default_id = scenario_ids()
    st.session_state.setdefault("run_key", ("scenario", default_id))
    st.session_state.setdefault("decisions", {})
    st.session_state.setdefault("replay_id", default_id)
    run = load_run(*st.session_state.run_key)
    st.session_state.setdefault("clock", default_clock(run))

    sidebar(ids, run)
    now = run.times[st.session_state.clock].to_pydatetime().astimezone(timezone.utc)
    st.session_state.now = now

    st.title("Raqeeb console")
    st.markdown(f"Incident **{run.scenario.id}** · {local(run.start):%Y-%m-%d} · "
                f"clock **{local(now):%H:%M}** ({DISPLAY_TZ}) · site {load_plant().site.name}")
    st.info(SYNTHETIC_NOTE)
    pending = sum(p.at <= now and p.request.id not in st.session_state.decisions for p in run.agent.approvals)
    headline(run, now, pending)

    ops, agent, results, report = st.tabs(["Operations", "Agent", "Results", "Report"])
    with ops:
        left, right = st.columns([5, 6])
        with left:
            plant_map(run, now)
        with right:
            alarm_feed(run, now)
        recovery_chart(run, now)
    with agent:
        left, right = st.columns([3, 2])
        with right:
            approvals(run, now)
        with left:
            agent_timeline(run, now)
    with results:
        results_tab()
    with report:
        report_tab(run)


main()
