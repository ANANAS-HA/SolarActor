"""Raqeeb operator console (HANDOFF section 12).

    uv run streamlit run raqeeb/ui/app.py

Three modes: "Live: rules only" (the real agent, no LLM), "Live: rules + local model" (only when
.env names a reachable local model server) and "Replay" (a recorded run, ui/replays/*.json or an
uploaded file). Live runs drive the twin and the agent step by step (ui/live.py); gated actions
pause the run for an operator decision. The planted faults are shown only after the run has ended.

Every screen is written for a plant manager, not a developer: identifiers, tool names, verdicts and
actions go through ui/plain.py; the raw record stays one click away behind "Show data" on each card.
A header names the plant, the mode, the simulated clock and the model; a notification strip above the
tabs carries the latest event, the open items and the alert status. The page opens on the Summary tab.
Plant, telemetry and faults are synthetic; weather is NASA POWER. Times are shown in Asia/Riyadh.
Alarm, note and log text is untrusted data: it is shown in tables or escaped, never as markdown or HTML.
Design tokens and the CSS block are documented in docs/CONSOLE_DESIGN.md.
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
from raqeeb.ui import alerts, ask, plain, plantmap, plantpanels, source, summary, trace
from raqeeb.ui.live import LiveRun, ReplayRun, list_local_models, local_model_status
from raqeeb.ui.runner import DEFAULT_PACE, PACES, Runner
from raqeeb.ui.proposals import effect
from raqeeb.ui.runlog import MODE_LABEL, Mode
from raqeeb.ui.source import html_text, local_hhmm, md_escape

LIVE_RULES, LIVE_LOCAL, REPLAY = MODE_LABEL[Mode.RULES], MODE_LABEL[Mode.LOCAL], "Replay"
MODES = [LIVE_RULES, LIVE_LOCAL, REPLAY]
RULES_ONLY = "Rules only"  # the Model selector's first entry; the others are what the local server lists
API_TIER = "API tier (not configured)"
SPEEDS = [15, 30, 60, 120, 240]  # sim minutes per tick
TICK_S = 0.4  # wall-clock pause between ticks while playing
TRACE_REFRESH_S = 1.0  # the Trace tab re-renders this often while the background runner advances a live run

# -- design tokens ("desert noon", docs/design/REDESIGN_HANDOFF.md): every colour has a word beside it ----
TOKENS = {
    "page": "#f6f2ea", "surface": "#ffffff", "ink": "#1d1f22", "body": "#2e3135", "muted": "#4a4d52",
    "accent": "#8a4b00", "navy": "#1b2f4a", "amber": "#e8a33d", "line": "#dedad2", "row": "#ece9e3",
    "sidebar": "#efe9dc", "subtle": "#faf9f6", "tint": "#fdf1de", "approval": "#c9822a",
}
# semantic states: (text/ink, background); each pair is at least 4.5:1
SEMANTIC = {
    "healthy": ("#1f6b3a", "#e4f2e8"), "attention": ("#7a4300", "#fdf1de"), "critical": ("#9a2420", "#fbe7e5"),
    "no data": ("#4a4d52", "#f1efea"), "needs a human": ("#7a4300", "#fdf1de"), "info": ("#1b2f4a", "#e9eef5"),
}
SERIES = {"actual": "#1b2f4a", "reference": "#1f7a45", "sensor": "#b06d12", "sun": "#b06d12"}  # PLANT_TAB.md colours
SUN_FILL, STORM_FILL, STORM_INK = "rgba(232,163,61,0.22)", "#f6e3c1", "#6b3d00"
THRESHOLD, PANEL_GRID = "#b3261e", "#ece6d9"  # stow-limit line; horizontal grid on the white panel
MUTED = TOKENS["muted"]
CHART_GRID, CHART_AXIS = "#e4ddcf", "#b9b2a3"  # Plotly grid and axis lines on the white chart card
SUMMARY_COLUMNS = {  # bench summary.csv column -> shown name
    "agent": "Agent", "scenarios": "Scenarios", "precision": "Precision", "recall": "Recall",
    "all_causes_found_rate": "All causes found", "false_action_rate": "False-action rate",
    "unsafe_actions": "Unsafe actions", "verified_recovery_rate": "Verified recovery",
    "ttvr_median_min": "Time to verified recovery (median min)", "mwh_recovered": "MWh recovered",
    "mwh_lost": "MWh lost (no action)", "cost_per_incident_usd": "Cost per incident (USD)",
    "llm_calls_per_incident": "LLM calls per incident", "local_call_share": "Local call share", "note": "Note",
}
AGENT_NAMES = {"raqeeb_rules": "Solar Actor, rules only", "raqeeb_local": "Solar Actor, rules + local model",
               "raqeeb_full": "Solar Actor, rules + local + API model", "raqeeb": "Solar Actor", "threshold": "Threshold baseline",
               "oracle": "Oracle (reads the answer key)", "empty": "Does nothing"}
NODE_WORDS = {"start": "Started watching", "triage": "Looked at the new alarms", "investigate": "Ran the checks",
              "rank": "Weighed the causes", "consult": "Asked the model", "plan": "Planned the fix",
              "safety": "Safety gate", "execute": "Requested an action", "verify": "Checked the result",
              "report": "Wrote the report", "approval": "Operator decision"}
PAGES_MAIN = ["Plant", "Summary", "Trace", "Agent", "Report"]  # Plant first in the sidebar; the console still opens on Summary
PAGES_JUDGE = ["Answer key", "Results"]
HINTS = {"Summary": "What happened", "Trace": "How it was solved", "Plant": "Map and inverters",
         "Agent": "What Solar Actor did", "Report": "Shift report", "Answer key": "The faults we injected",
         "Results": "How well it did"}
INTRO = {
    "Summary": "The whole incident on one screen: what went wrong, what Solar Actor did about it and what it cost. "
               "Click a moment to open that step in Trace.",
    "Trace": "This is the agent's log. Each card is one 15-minute step: what it checked, what it found, what it "
             "decided, and what happened next.",
    "Plant": "Every inverter and sensor at the simulated clock, with its state in words; the alarms, the output "
             "against what the weather allows, and the notes from site staff.",
    "Agent": "Each action Solar Actor took and whether it needed your approval, with the raw values behind it.",
    "Report": "The report a plant manager reads at the end of the shift, in English and Arabic.",
    "Answer key": "The faults we hid in the simulation, for checking Solar Actor against. The agent never sees this; it "
                  "appears only after the run ends.",
    "Results": "How well Solar Actor did over many scenarios, against a simple alarm-threshold baseline and two "
               "reference bounds.",
    "Ask the agent": "Ask about this incident in your own words. Answers come from the record; a model only rewords "
                  "them and can take no action.",
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
:root { --rq-page: %(page)s; --rq-surface: %(surface)s; --rq-ink: %(ink)s; --rq-body: %(body)s; --rq-muted: %(muted)s;
        --rq-accent: %(accent)s; --rq-navy: %(navy)s; --rq-amber: %(amber)s; --rq-line: %(line)s; --rq-row: %(row)s;
        --rq-subtle: %(subtle)s; --rq-tint: %(tint)s; --rq-approval: %(approval)s;
        --rq-sans: 'IBM Plex Sans', 'Source Sans 3', system-ui, sans-serif; --rq-mono: 'IBM Plex Mono', ui-monospace, monospace; }
.stApp, .stApp :not([data-testid="stIconMaterial"]):not(.material-symbols-rounded) { font-family: var(--rq-sans); }
.stApp { background: var(--rq-page); color: var(--rq-body); }
[data-testid="stMainBlockContainer"] { max-width: 1180px; }
.stApp p, .stApp li { color: var(--rq-body); font-size: 15px; line-height: 1.55; }
.stApp code, .stApp pre, .rq-mono { font-family: var(--rq-mono) !important; }
[data-testid="stSidebar"] { background-color: %(sidebar)s; border-right: 1px solid var(--rq-line);
  background-image: linear-gradient(rgba(27,47,74,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(27,47,74,.045) 1px, transparent 1px);
  background-size: 22px 22px; }
[data-testid="stSidebar"] [data-testid="stRadio"] > label { display: none; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] { padding: 0.45rem 0.6rem; border-radius: 8px;
  margin: 0; width: 100%%; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:hover { background: #e6dfcf; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) { background: var(--rq-surface); }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] p { color: var(--rq-ink); font-size: 15px; font-weight: 500; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) p { font-weight: 700; }
[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child { display: none; }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-testid="stCaptionContainer"] { margin: 0; }
[data-testid="stSidebar"] [data-testid="stRadio"] [data-testid="stCaptionContainer"] p { font-size: 13px !important; font-weight: 400; }
.rq-group { font-size: 12px; font-weight: 700; color: var(--rq-muted); text-transform: uppercase; letter-spacing: 0.06em;
            padding: 6px 10px 2px; }
.rq-hackathon { font-size: 12px; font-weight: 700; color: var(--rq-accent); text-transform: uppercase; letter-spacing: 0.08em;
            padding: 2px 8px 8px; border-bottom: 1px solid var(--rq-line); margin-bottom: 10px; }
.rq-logo { display: flex; align-items: center; gap: 10px; padding: 4px 8px 10px; }
.rq-logo .mark { width: 32px; height: 32px; border-radius: 8px; background: var(--rq-navy); position: relative; overflow: hidden; flex: none; }
.rq-logo .mark i { position: absolute; left: 8px; top: 7px; width: 16px; height: 16px; border-radius: 50%%; background: var(--rq-amber); }
.rq-logo .mark b { position: absolute; left: 0; right: 0; top: 15px; bottom: 0; background: var(--rq-navy); }
.rq-logo .mark s { position: absolute; left: 5px; right: 5px; height: 2px; background: #8fa6c4; }
.rq-logo .name { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; display: block; }
.rq-logo .sub { font-size: 13px; color: var(--rq-muted); display: block; }
.rq-synthetic { margin-top: 1rem; padding: 12px; border: 1px solid var(--rq-line); border-radius: 10px; background: var(--rq-page);
                font-size: 13px; color: var(--rq-muted); line-height: 1.45; }
.rq-synthetic b { color: var(--rq-ink); display: block; }
.st-key-header { position: sticky; top: 0; z-index: 5; background: rgba(246,242,234,.94); backdrop-filter: blur(8px);
                 border-bottom: 2px solid var(--rq-amber); padding: 0.3rem 0 0.4rem; margin-bottom: 0.4rem; }
.rq-title small { font-size: 13px; color: var(--rq-muted); display: block; }
.st-key-header [data-testid="stCaptionContainer"] p { line-height: 1.35; }
.st-key-header [data-testid="stPopover"] > button p { font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rq-title span { font-size: 17px; font-weight: 600; display: block; color: var(--rq-ink); }
.rq-eyebrow { font-size: 12px; font-weight: 700; color: var(--rq-muted); text-transform: uppercase; letter-spacing: 0.06em; }
.rq-clock { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--rq-muted); padding-top: 0.35rem; }
.rq-clock b { font-family: var(--rq-mono); font-size: 14px; font-weight: 500; color: var(--rq-ink); }
.rq-bar { flex: 1; height: 4px; background: #e6e3dc; border-radius: 2px; position: relative; min-width: 60px; }
.rq-bar i { position: absolute; left: 0; top: 0; bottom: 0; background: var(--rq-amber); border-radius: 2px; }
h1, h2, h3 { color: var(--rq-ink); letter-spacing: -0.01em; }
h1 { font-size: 26px; font-weight: 700; }  h2 { font-size: 20px; font-weight: 600; }  h3 { font-size: 18px; font-weight: 600; }
.rq-intro { font-size: 15px; color: var(--rq-body); margin: -0.4rem 0 0.8rem; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: var(--rq-muted) !important; font-size: 14px !important; }
[data-testid="stMetricLabel"] p { color: var(--rq-body) !important; font-size: 14px !important; font-weight: 600; }
[data-testid="stMetricValue"] { color: var(--rq-ink) !important; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
[data-testid="stMetricDelta"], [data-testid="stMetricDelta"] div { color: var(--rq-body) !important; background: #ebe4d4 !important;
  font-size: 14px !important; border-radius: 6px; }
[data-testid="stMetricDelta"] svg { fill: var(--rq-body) !important; }
[data-testid="stVerticalBlockBorderWrapper"] { background: var(--rq-surface); border-color: var(--rq-line) !important; border-radius: 12px; }
.stButton button, .stDownloadButton button { font-size: 14px; font-weight: 600; border-radius: 8px; }
.stButton button p, .stDownloadButton button p { font-size: 14px; color: inherit; }
.stButton button[kind="primary"], .stButton button[kind="primary"] * { background: var(--rq-navy) !important; color: #fff !important;
  border-color: var(--rq-navy) !important; }
.stButton button[kind="secondary"], .stButton button[kind="secondary"] *, .stDownloadButton button[kind="secondary"],
.stDownloadButton button[kind="secondary"] * { background: var(--rq-surface) !important; color: var(--rq-ink) !important;
  border-color: #b9b2a3 !important; }
.stButton button:disabled, .stButton button:disabled * { color: var(--rq-muted) !important; background: #ebe4d4 !important;
  border-color: #ebe4d4 !important; }
.stButton button[kind="primary"]:hover { background: #24406a !important; border-color: #24406a !important; }
.stButton button[kind="secondary"]:hover { border-color: var(--rq-navy) !important; }
.rq-strip { display: flex; align-items: center; gap: 0.9rem; flex-wrap: wrap; background: var(--rq-surface);
            border: 1px solid var(--rq-line); border-left: 6px solid var(--rq-navy); border-radius: 12px; padding: 0.55rem 0.9rem;
            margin: 0.2rem 0 0.4rem; font-size: 15px; color: var(--rq-body); }
.rq-strip.critical { border-left-color: %(crit)s; }  .rq-strip.human { border-left-color: %(human)s; }
.rq-strip.healthy { border-left-color: %(ok)s; }
.rq-time { font-family: var(--rq-mono); font-variant-numeric: tabular-nums; color: var(--rq-muted); }
.rq-event { flex: 1 1 24rem; min-width: 16rem; }
.rq-chip { display: inline-flex; align-items: center; gap: 6px; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 13px;
           font-weight: 600; white-space: nowrap; }
.rq-chip::before { content: ""; width: 8px; height: 8px; border-radius: 50%%; background: currentColor; }
.rq-pulse::before { animation: rq-pulse 1.2s ease-in-out infinite; }
@keyframes rq-pulse { 0%%, 100%% { opacity: 1; } 50%% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .rq-pulse::before { animation: none; } }
.rq-headline { font-size: 16px; font-weight: 600; color: var(--rq-ink); margin: 0 0 0.2rem; }
.rq-found { border-left: 4px solid %(att)s; padding-left: 0.6rem; margin: 0.25rem 0; color: var(--rq-body); }
.rq-found b { color: var(--rq-ink); font-weight: 600; }
.rq-quiet { color: var(--rq-muted); }
.st-key-ask_fab { position: fixed; right: 24px; bottom: 24px; z-index: 1000; width: auto !important; }
.st-key-ask_fab [data-testid="stPopover"] > button { background: var(--rq-navy); color: #fff; border: 0; border-radius: 999px;
  padding: 10px 18px; font-weight: 600; box-shadow: 0 10px 30px rgba(27,47,74,.25); }
.st-key-ask_fab [data-testid="stPopover"] > button:hover { background: #24406a; color: #fff; }
.st-key-ask_fab [data-testid="stPopover"] > button p { color: #fff; font-size: 15px; }
.stExpander summary p, .stExpander summary span { font-size: 15px; color: var(--rq-ink); }
.st-key-approval_card [data-testid="stVerticalBlockBorderWrapper"] { border: 1.5px solid var(--rq-approval) !important; }
/* Plant tab panels (docs/design/PLANT_TAB.md) */
.rq-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 0.2rem 0 0.9rem; }
.rq-stat { position: relative; background: var(--rq-surface); border: 1px solid #d9d2c3; border-radius: 6px; padding: 12px 14px 42px;
           overflow: hidden; min-height: 124px; }
.rq-stat .label { font-size: 13px; font-weight: 600; color: var(--rq-body); }
.rq-stat .value { font-size: 30px; font-weight: 600; color: var(--rq-ink); font-variant-numeric: tabular-nums; line-height: 1.15;
                  margin: 4px 0 2px; }
.rq-stat .state, .rq-tile .state, .rq-equip .state { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
.rq-stat .state i, .rq-tile .state i, .rq-equip .state i { width: 8px; height: 8px; border-radius: 50%%; display: inline-block; flex: none; }
.rq-spark { position: absolute; left: 0; right: 0; bottom: 0; width: 100%%; height: 36px; display: block; }
.rq-panel-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; padding: 2px 2px 6px; }
.rq-panel-head .title { font-size: 15px; font-weight: 600; color: var(--rq-ink); }
.rq-panel-head .unit { font-size: 13px; color: var(--rq-muted); }
.rq-panel-head .take { margin-left: auto; font-size: 14px; font-weight: 600; text-align: right; }
table.rq-legend { width: 100%%; border-collapse: collapse; margin-top: 6px; font-size: 14px; }
table.rq-legend th { text-align: left; font-size: 13px; font-weight: 600; color: var(--rq-muted); padding: 4px 8px;
                     border-bottom: 1px solid #d9d2c3; }
table.rq-legend td { padding: 6px 8px; border-bottom: 1px solid #ece6d9; vertical-align: top; color: var(--rq-body); }
table.rq-legend td.num, table.rq-legend th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.rq-legend svg { vertical-align: middle; margin-right: 6px; }
table.rq-legend b { color: var(--rq-ink); }
table.rq-legend .meaning { color: var(--rq-muted); }
.rq-markers { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 14px; color: var(--rq-body); padding: 8px 2px 2px; }
.rq-markers b { color: var(--rq-navy); font-size: 17px; }
.rq-map { display: flex; flex-direction: column; gap: 8px; }
.rq-map .head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 14px; color: var(--rq-body);
                padding: 0 2px 2px; }
.rq-map .row { display: grid; grid-template-columns: 104px repeat(var(--cols), minmax(0, 1fr)); gap: 8px; align-items: stretch; }
.rq-map .rowlabel { padding: 10px 0 0 2px; min-width: 0; }
.rq-map .rowlabel b { display: block; font-size: 14px; font-weight: 600; color: var(--rq-ink); }
.rq-map .rowlabel span { font-size: 13px; color: var(--rq-muted); }
.rq-tile { background: var(--rq-surface); border: 1px solid #d9d2c3; border-radius: 6px; padding: 10px 12px; text-align: left;
           min-width: 0; }
.rq-tile.attn, .rq-equip.attn { border-width: 2px; }
.rq-tile .name { font-size: 14px; font-weight: 600; color: var(--rq-ink); }
.rq-tile .mw { font-size: 22px; font-weight: 600; color: var(--rq-ink); font-variant-numeric: tabular-nums; line-height: 1.2; }
.rq-tile .peers, .rq-tile .extra { font-size: 13px; color: var(--rq-body); }
.rq-tile .state { margin-top: 4px; }
.rq-tile .badge, .rq-map .rowlabel .badge { font-size: 13px; color: #6b3d00; margin-top: 2px; }
.rq-equip { background: var(--rq-surface); border: 1px solid #d9d2c3; border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
.rq-equip .name { font-size: 14px; font-weight: 600; color: var(--rq-ink); display: flex; justify-content: space-between; gap: 8px;
                  align-items: center; flex-wrap: wrap; }
.rq-equip table { width: 100%%; border-collapse: collapse; margin-top: 6px; }
.rq-equip td { font-size: 14px; padding: 3px 0; color: var(--rq-body); vertical-align: top; }
.rq-equip td.v { text-align: right; font-weight: 600; color: var(--rq-ink); font-variant-numeric: tabular-nums; white-space: nowrap; padding-left: 8px; }
@media (max-width: 820px) { .rq-map .row { grid-template-columns: 1fr 1fr; } .rq-map .rowlabel { grid-column: 1 / -1; } }
</style>
""" % {**TOKENS, "crit": SEMANTIC["critical"][0], "human": SEMANTIC["needs a human"][0],
       "ok": SEMANTIC["healthy"][0], "att": SEMANTIC["attention"][0]}


def styled(fig: go.Figure) -> go.Figure:
    """The designer's chart text and surface: dark 14 px labels, 13 px ticks and legend, white card background,
    soft grid. Applied to every figure after its own layout."""
    fig.update_layout(font={"family": "IBM Plex Sans, Source Sans 3, sans-serif", "size": 14, "color": TOKENS["body"]},
                      paper_bgcolor=TOKENS["surface"], plot_bgcolor=TOKENS["surface"],
                      legend_font={"size": 13, "color": TOKENS["ink"]}, hoverlabel={"font": {"size": 13}})
    fig.update_xaxes(tickfont={"size": 13, "color": TOKENS["body"]}, gridcolor=CHART_GRID, linecolor=CHART_AXIS,
                     title_font={"size": 14, "color": TOKENS["body"]})
    fig.update_yaxes(tickfont={"size": 13, "color": TOKENS["body"]}, gridcolor=CHART_GRID, linecolor=CHART_AXIS,
                     title_font={"size": 14, "color": TOKENS["body"]})
    fig.update_annotations(font={"size": 13, "color": TOKENS["body"]})
    return fig


def card():
    """A white bordered card for a chart (CONTRAST_FIX: charts sit on white, not on the sand page)."""
    return st.container(border=True)


def inline(text: object) -> str:
    """Record text next to inline HTML (a <span>) in st.markdown: the markdown parser still runs there, so
    the text is escaped for markdown and for HTML."""
    return html_text(md_escape(text))


def block(text: object) -> str:
    """Record text inside a block-level HTML element (<div>, <p>) in st.markdown: no markdown is parsed
    inside an HTML block (html_text leaves no newline, so the block never ends early), so HTML escaping is
    all that is needed; a markdown escape here would show as a backslash."""
    return html_text(text)


def chip(text: str, state: str, in_block: bool = False) -> str:
    ink, bg = SEMANTIC.get(state, SEMANTIC["info"])
    return f'<span class="rq-chip" style="color:{ink};background:{bg}">{(block if in_block else inline)(text)}</span>'


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


def mode_words(run: source.Run) -> str:
    """The mode in words for the header: "Replay with the local model", "Live, rules only", ..."""
    if isinstance(run, ReplayRun):
        if run.log.mode == Mode.TEST:
            return "Replay with the scripted test model"
        return "Replay with the local model" if run.log.model else "Replay, rules only"
    return "Live with the local model" if run.log.model else "Live, rules only"


def intro(tab: str) -> None:
    st.markdown(f'<p class="rq-intro">{html_text(INTRO[tab])}</p>', unsafe_allow_html=True)


def show_data(key: str):
    """The raw record behind a card: one collapsed expander, never deleted."""
    return st.expander("Show data", expanded=False)


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
    st.session_state.pop("strip_seen", None)
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


def _play_storm() -> None:
    """The first-time path: play the loaded run from where it stands."""
    if isinstance(st.session_state.run, LiveRun):
        _ensure_runner().resume(st.session_state.get("trace_pace", DEFAULT_PACE))
        st.session_state.playing = False
    else:
        st.session_state.playing = True


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


# -- navigation, header, run panel, playback ----------------------------------------------------------------

DEFAULT_PAGE = "Summary"


def _nav_main() -> None:
    if st.session_state.get("nav_main"):
        st.session_state.nav = st.session_state.nav_main
        st.session_state.nav_judge = None


def _nav_judge() -> None:
    if st.session_state.get("nav_judge"):
        st.session_state.nav = st.session_state.nav_judge
        st.session_state.nav_main = None


def go_to(page: str) -> None:
    """Switch the page from a callback (the radios read these keys on the next run)."""
    st.session_state.nav = page
    st.session_state.nav_main = page if page in PAGES_MAIN else None
    st.session_state.nav_judge = page if page in PAGES_JUDGE else None


def current_page() -> str:
    page = st.session_state.get("nav") or DEFAULT_PAGE
    return page if page in PAGES_MAIN + PAGES_JUDGE else DEFAULT_PAGE


def sidebar(run: source.Run) -> None:
    """Brand, the two navigation groups and the synthetic-data note. The run controls live in the header."""
    page = current_page()
    st.session_state.setdefault("nav", page)
    st.session_state.setdefault("nav_main", page if page in PAGES_MAIN else None)
    st.session_state.setdefault("nav_judge", page if page in PAGES_JUDGE else None)
    with st.sidebar:
        st.markdown('<div class="rq-hackathon">HUMAIN AI Builders Hackathon</div>', unsafe_allow_html=True)
        st.markdown('<div class="rq-logo"><div class="mark"><i></i><b></b><s style="top:17px"></s><s style="top:21px"></s>'
                    '<s style="top:25px"></s></div><div><span class="name">Solar Actor</span>'
                    '<span class="sub">Solar plant console</span></div></div>', unsafe_allow_html=True)
        st.markdown('<div class="rq-group">The incident</div>', unsafe_allow_html=True)
        st.radio("The incident", PAGES_MAIN, key="nav_main", on_change=_nav_main, label_visibility="collapsed",
                 captions=[HINTS[x] for x in PAGES_MAIN])
        st.markdown('<div class="rq-group">For judges</div>', unsafe_allow_html=True)
        st.radio("For judges", PAGES_JUDGE, key="nav_judge", on_change=_nav_judge, label_visibility="collapsed",
                 captions=[HINTS[x] for x in PAGES_JUDGE])
        st.markdown('<div class="rq-synthetic"><b>Simulated plant</b>16 inverters, synthetic data. Only the weather is '
                    'real (NASA POWER, Riyadh 2025).</div>', unsafe_allow_html=True)


def run_label(run: source.Run) -> str:
    if isinstance(run, ReplayRun):
        return "Replay"
    return "Live, with model" if run.log.model else "Live, rules only"


def run_panel(run: source.Run) -> None:
    """The old sidebar Run and Playback settings, in a popover: same widgets, same keys."""
    ids, _ = scenario_choices()
    available, reason = local_status()
    with st.popover(f"{run_label(run)} · Change", use_container_width=True):
        st.markdown("**Run the agent**")
        st.radio("Mode", MODES, key="mode_choice", on_change=_mode_chosen,
                 captions=["The agent's rules, no language model", f"{'Available' if available else 'Unavailable'}: "
                           f"{reason}", "Play back a recorded run"])
        choice = st.session_state.mode_choice
        models = local_models()
        options = [RULES_ONLY, *models]
        if st.session_state.get("model_choice") not in options:
            st.session_state.model_choice = models[0] if choice == LIVE_LOCAL and models else RULES_ONLY
        if choice != REPLAY:
            st.markdown("Language model")
            st.segmented_control("Provider", ["Local LLM"], default="Local LLM", key="provider",
                                 label_visibility="collapsed")
            st.caption("Claude, ChatGPT and Gemini: not configured in this deployment (no API key; the console runs "
                       "fully local).")
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
                st.button("Load replay", key="load_replay", on_click=_load_replay, type="primary", width="stretch")
            else:
                st.caption("No recorded runs in this deployment.")
            st.file_uploader("Or upload a replay file (.json, up to 20 MB)", type=["json"], key="replay_upload")
            st.button("Load uploaded file", key="load_upload", on_click=_load_upload, width="stretch",
                      disabled=st.session_state.get("replay_upload") is None)
            if st.session_state.get("load_error"):
                st.error(st.session_state.load_error)
        else:
            blocked = choice == LIVE_LOCAL and not available
            st.selectbox("Scenario (practice sets)", ids, key="scenario_id")
            a, b = st.columns(2)
            a.button("Start live run", key="start_live", on_click=_start_live, type="primary", width="stretch",
                     disabled=blocked)
            b.button("Dust storm + 2 random faults", key="inject_storm", on_click=_start_storm, width="stretch",
                     disabled=blocked)
            if blocked:
                st.caption(f"Rules + local model is unavailable: {reason}.")
            st.caption("Faults are planted in the synthetic plant. The agent sees only the readings.")
        st.divider()
        st.select_slider("Playback speed (simulated minutes per tick)", SPEEDS, key="speed")
        if isinstance(run, LiveRun) and run.finished:
            st.download_button("Save recording (replay JSON)", run.recording().to_json(),
                               file_name=f"raqeeb_replay_{run.incident.id}_{run.mode.value}.json",
                               mime="application/json", key="save_recording", width="stretch")


def playback(run: source.Run) -> None:
    """Simulated clock, progress through the window, Step / To end / Play."""
    span = (run.incident.end - run.incident.start).total_seconds() or 1
    done = min(max((run.now - run.incident.start).total_seconds() / span, 0.0), 1.0)
    st.markdown(f'<div class="rq-clock"><span>Sim time</span><b>{local(run.now):%H:%M}</b>'
                f'<div class="rq-bar"><i style="width:{done * 100:.1f}%"></i></div>'
                f'<span>{local(run.incident.end):%H:%M}</span></div>', unsafe_allow_html=True)
    stopped = run.finished or bool(run.waiting)
    runner = _runner(run)
    playing = st.session_state.playing or bool(runner and runner.state().running)
    a, b, c = st.columns([1, 1.25, 1.3])
    a.button("Step", key="step", on_click=_step, width="stretch", disabled=stopped)
    b.button("To end", key="to_end", on_click=_to_end, width="stretch", disabled=stopped)
    c.button("Pause" if playing else "Play", key="play", on_click=_toggle_play, type="primary", width="stretch",
             disabled=stopped)


def _go_approval() -> None:
    go_to("Summary")
    st.session_state.jump_approval = True


def header(run: source.Run) -> None:
    """Sticky: incident and page title, the run pill, playback, the approval pill."""
    plant = load_plant()
    page = current_page()
    with st.container(key="header"):
        title, runcol, play, right = st.columns([3.3, 1.9, 3.4, 1.9], vertical_alignment="top")
        with title:
            st.markdown(f'<div class="rq-title"><small>Incident {html_text(run.incident.id)}, '
                        f'{local(run.incident.start):%d %B %Y}. {html_text(plant.site.name)} plant (synthetic)</small>'
                        f'<span>{html_text(page)}</span></div>', unsafe_allow_html=True)
            record = f" Recording: {md_escape(run.log.label)}." if isinstance(run, ReplayRun) else ""
            st.caption(f"Mode: {md_escape(mode_words(run))}; model {md_escape(run.log.model or 'none')}.{record}")
        with runcol:
            st.markdown('<div class="rq-eyebrow">Run the agent</div>', unsafe_allow_html=True)
            run_panel(run)
        with play:
            playback(run)
        with right:
            waiting = len(run.waiting)
            if waiting:
                st.button(f"{waiting} action{'s' if waiting != 1 else ''} need{'s' if waiting == 1 else ''} your approval",
                          key="strip_decide", on_click=_go_approval, type="primary", width="stretch")
            elif run.finished:
                st.markdown(chip("Run finished", "healthy", in_block=True), unsafe_allow_html=True)
            else:
                st.markdown(chip("Nothing waiting for you", "no data", in_block=True), unsafe_allow_html=True)
            if run.waiting:
                st.caption("Paused: an action needs your decision.")
            elif run.finished:
                st.caption("The answer key can be revealed.")


def alert_status(run: source.Run) -> tuple[str, str]:
    """(text, state) of the email alerts for the strip."""
    if not isinstance(run, LiveRun):
        return "Recorded run: no email alerts", "no data"
    config = run.alerter.config
    if not config.enabled:
        return "Email alerts off in this deployment", "no data"
    if not config.configured:
        return "Email alerts not configured", "no data"
    last = run.alerts[-1] if run.alerts else None
    if last is None:
        return "Email alerts ready", "healthy"
    return {alerts.SENT: ("Email sent to on-call", "healthy"), alerts.FAILED: ("Email to on-call failed", "critical"),
            "sending": ("Emailing on-call", "info")}.get(last.status, ("Email alerts ready", "healthy"))


def notification_strip(run: source.Run) -> None:
    """The latest event in words, the open items and the alert status; a pulsing "Needs your decision" chip
    while an action waits, which jumps to the approval card. New events also raise a toast. Reads the run only
    (the background runner is never touched here)."""
    now, log = run.now, run.log
    moments = summary.key_moments(log, now)
    waiting = len(run.waiting)
    escalated = len(log.escalations_until(now))
    recovered = sum(1 for _, v in log.verifications_until(now) if v.recovered)
    seen = st.session_state.get("strip_seen")
    if seen is not None and len(moments) > seen:
        for m in moments[seen:][-3:]:
            st.toast(md_escape(f"{local_hhmm(m.ts)}  {m.said or m.text}"))
    st.session_state.strip_seen = len(moments)

    if moments:
        last = moments[-1]
        event = f'<span class="rq-time">{local_hhmm(last.ts)}</span> {block(last.said or last.text)}'
    else:
        event = "Nothing has happened yet. Press Play, or Step for one agent turn."
    state = "human" if waiting else "critical" if escalated and not recovered else "healthy" if recovered else "info"
    chips = []
    if waiting:
        chips.append(f'<span class="rq-chip rq-pulse" style="color:{SEMANTIC["needs a human"][0]};'
                     f'background:{SEMANTIC["needs a human"][1]}">Needs your decision: {waiting}</span>')
    chips.append(chip(f"Awaiting your approval: {waiting}", "needs a human" if waiting else "no data", in_block=True))
    chips.append(chip(f"Handed to a human: {escalated}", "attention" if escalated else "no data", in_block=True))
    chips.append(chip(f"Recovered: {recovered}", "healthy" if recovered else "no data", in_block=True))
    text, kind = alert_status(run)
    chips.append(chip(text, kind, in_block=True))
    st.markdown(f'<div class="rq-strip {state}"><span class="rq-event">{event}</span>{"".join(chips)}</div>',
                unsafe_allow_html=True)


# -- panels ----------------------------------------------------------------------------


def stat_row(run: source.Run, power: pd.DataFrame, data: dict[str, pd.DataFrame]) -> None:
    """Four number panels: output now, energy below expected, alarms, waiting for approval (PLANT_TAB.md)."""
    plant = load_plant()
    st.markdown(plantpanels.stats_html(plantpanels.stats(run, power, data["events"], plant)), unsafe_allow_html=True)


def panel_axes(fig: go.Figure, run: source.Run, height: int) -> go.Figure:
    """A PLANT_TAB.md time-series panel: unified hover with a crosshair and a dark hover box, horizontal grid
    only, mono ticks every two hours on the incident window, no Plotly legend (a legend table follows the
    chart) and the amber playback cursor at the simulated clock."""
    styled(fig)
    fig.update_layout(height=height, margin={"l": 0, "r": 0, "t": 12, "b": 0}, hovermode="x unified", showlegend=False,
                      hoverlabel={"bgcolor": TOKENS["navy"], "bordercolor": TOKENS["navy"],
                                  "font": {"color": "#ffffff", "size": 13}})
    fig.update_xaxes(range=[local(run.incident.start), local(run.incident.end)], showgrid=False, showspikes=True,
                     spikemode="across", spikethickness=1, spikecolor=TOKENS["ink"], spikedash="solid",
                     spikesnap="cursor", dtick=2 * 3600 * 1000, tickformat="%H:%M", fixedrange=True,
                     tickfont={"family": "IBM Plex Mono, ui-monospace, monospace", "size": 13, "color": TOKENS["body"]})
    fig.update_yaxes(gridcolor=PANEL_GRID, zeroline=False, fixedrange=True)
    fig.add_vline(x=local(run.now), line={"color": TOKENS["amber"], "width": 2})
    return fig


def _storm_bands(fig: go.Figure, sky: pd.DataFrame, label: bool) -> None:
    """Shaded band while the wind is above the stow limit; the label only where asked (the sun and wind panel)."""
    for a, b in plantmap.storm_bands(sky):
        extra = {"annotation_text": "<b>Dust storm, trackers stowed</b>", "annotation_position": "top left",
                 "annotation_font": {"size": 13, "color": STORM_INK}} if label else {}
        fig.add_vrect(x0=local(a), x1=local(b), fillcolor=STORM_FILL, line_width=0, layer="below", **extra)


def _event_markers(fig: go.Figure, marks: list) -> None:
    """A dotted navy line per applied action with its number in a navy badge near the bottom of the chart."""
    for m in marks:
        fig.add_vline(x=local(m.ts), line={"color": TOKENS["navy"], "width": 1.5, "dash": "dot"})
        fig.add_annotation(x=local(m.ts), y=0.04, yref="paper", text=f"<b>{m.n}</b>", showarrow=False,
                           font={"size": 13, "color": "#ffffff"}, bgcolor=TOKENS["navy"], bordercolor=TOKENS["navy"],
                           borderpad=4, xanchor="center", yanchor="bottom")


def sun_wind_panel(run: source.Run, plant) -> None:
    sky = plantmap.sky_strip(run, plant)
    x = sky.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=sky["sun_elevation_deg"].clip(lower=0), name="Sun height", mode="lines",
                             fill="tozeroy", line={"color": SERIES["sun"], "width": 2}, fillcolor=SUN_FILL,
                             hovertemplate="%{y:.0f}°"))
    fig.add_trace(go.Scatter(x=x, y=sky["wind_ms"], name="Wind on site", mode="lines", yaxis="y2",
                             line={"color": SERIES["actual"], "width": 2.5}, hovertemplate="%{y:.1f} m/s"))
    panel_axes(fig, run, 260)
    stow = plant.tracker.stow_wind_ms
    wmax = sky["wind_ms"].max()
    top = 25.0 if pd.isna(wmax) else max(25.0, float(-(-wmax * 1.1 // 5) * 5))  # whole fives above the peak
    fig.update_layout(yaxis={"title": "sun height (°)", "range": [0, 90], "dtick": 30},
                      yaxis2={"title": "wind (m/s)", "overlaying": "y", "side": "right", "showgrid": False,
                              "range": [0, top], "dtick": 5, "fixedrange": True,
                              "tickfont": {"size": 13, "color": TOKENS["body"]},
                              "title_font": {"size": 14, "color": TOKENS["body"]}})
    _storm_bands(fig, sky, label=True)
    fig.add_hline(y=stow, yref="y2", line={"color": THRESHOLD, "width": 1.5, "dash": "dash"},
                  annotation_text=f"Stow limit {stow:g} m/s", annotation_position="top right",
                  annotation_font={"size": 13, "color": THRESHOLD})
    text, state = plantpanels.takeaway_sky(sky, plant, run.now)
    with card():
        st.markdown(plantpanels.panel_header("Sun and wind", "degrees and m/s", text, state), unsafe_allow_html=True)
        st.plotly_chart(fig, key="sky_strip", config={"displayModeBar": False})
        st.markdown(plantpanels.legend_html(plantpanels.legend_sky(sky, plant, SERIES["sun"], SERIES["actual"], run.now)),
                    unsafe_allow_html=True)


def inverter_detail(run: source.Run, plant) -> None:
    ids = plant.inverters.ids
    choice = st.selectbox("One inverter against the others (click a tile, or pick one)", ids, index=None,
                          key="map_inverter", placeholder="Click a tile on the map", format_func=plain.device)
    if choice is None:
        return
    hist = plantmap.inverter_history(run, choice, plant)
    x = hist.index.tz_convert(DISPLAY_TZ)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=hist["peer_max_kw"] / 1000, mode="lines", line={"width": 0}, showlegend=False,
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=hist["peer_min_kw"] / 1000, mode="lines", line={"width": 0}, fill="tonexty",
                             fillcolor="rgba(81,101,122,0.2)", name="The others, lowest to highest", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=hist["peer_median_kw"] / 1000, mode="lines", name="Typical inverter",
                             line={"color": MUTED, "width": 1.5, "dash": "dash"}, hovertemplate="%{y:.2f} MW"))
    fig.add_trace(go.Scatter(x=x, y=hist["inverter_kw"] / 1000, mode="lines", name=plain.device(choice), connectgaps=False,
                             line={"color": SERIES["actual"], "width": 2.5}, hovertemplate="%{y:.2f} MW"))
    fig.add_vline(x=local(run.now), line={"color": TOKENS["ink"], "width": 1})
    fig.update_layout(height=260, margin={"l": 0, "r": 0, "t": 10, "b": 0}, hovermode="x unified",
                      legend={"orientation": "h", "y": 1.15, "x": 0}, yaxis_title="Output (MW)",
                      xaxis_range=[local(run.incident.start), local(run.incident.end)])
    with card():
        st.plotly_chart(styled(fig), key="inverter_detail")
    gaps = int(hist["inverter_kw"].isna().sum() - hist["peer_median_kw"].isna().sum())
    st.caption(f"{plain.device(choice)} against the other {len(ids) - 1} inverters, up to the simulated clock."
               + (f" Gaps in the line are steps with no reading ({gaps} so far), not zero output." if gaps > 0 else ""))


def alarm_feed(data: dict[str, pd.DataFrame]) -> None:
    st.subheader("Alarm feed")
    events = data["events"].sort_values("ts", ascending=False)
    if not len(events):
        st.caption("No alarms yet. Alarms appear here as the plant raises them, newest first.")
        return
    by_severity = events["severity"].astype(str).value_counts()
    st.caption(f"{len(events)} alarms: " + ", ".join(f"{by_severity.get(s, 0)} {s.lower()}"
                                                     for s in ("CRITICAL", "WARNING", "INFO"))
               + ". Message text is quoted device data (untrusted): the agent's rules never read it.")
    table = pd.DataFrame({
        "Time": events["ts"].map(local_hhmm), "Device": events["device_id"].map(plain.device),
        "Alarm": events["code"].map(plain.alarm_title), "Severity": events["severity"].astype(str).str.capitalize(),
        "Message (quoted)": events["message"],
    })
    st.dataframe(table, hide_index=True, height=380, key="alarm_table")


def notes_panel(run: source.Run) -> None:
    st.subheader("Technician and operator notes")
    notes = run.log.notes_until(run.now)
    st.caption("Quoted as written (synthetic, untrusted data). The agent's rules never read note text; "
               "technician notes reach a model only as delimited data.")
    if not notes:
        st.caption("No notes so far. Notes from site staff and your approval notes would appear here.")
        return
    st.dataframe(pd.DataFrame([{"Time": local_hhmm(n.ts), "Source": n.source.capitalize(), "Author": n.author,
                                "About": plain.device(n.about) if n.source == "technician" else n.about,
                                "Text (quoted)": n.text} for n in reversed(notes)]),
                 hide_index=True, key="notes_table")


def output_panel(run: source.Run, power: pd.DataFrame) -> None:
    """Actual against the two expected lines, the storm band, numbered action markers; a legend table with
    now / min / max and the markers explained under it. The sensor line stops where the agent dropped the sensor."""
    plant = load_plant()
    x = power.index.tz_convert(DISPLAY_TZ)
    dropped = plantpanels.sensor_dropped_at(run)
    sensor = power["expected_sensor_kw"].copy()
    if dropped is not None:
        sensor[sensor.index > dropped] = float("nan")
    fig = go.Figure()
    for y, name, color, dash, width in (
        (power["expected_ref_kw"], "Expected, satellite weather", SERIES["reference"], "dash", 2.5),
        (sensor, "Expected, plant sun sensor", SERIES["sensor"], "dot", 2.5),
        (power["actual_kw"], "Actual", SERIES["actual"], "solid", 3),
    ):
        fig.add_trace(go.Scatter(x=x, y=y / 1000, name=name, mode="lines", connectgaps=False,
                                 line={"color": color, "width": width, "dash": dash}, hovertemplate="%{y:.1f} MW"))
    panel_axes(fig, run, 320)
    fig.update_yaxes(title_text="plant output (MW)")
    _storm_bands(fig, plantmap.sky_strip(run, plant), label=False)
    marks = plantpanels.markers(run)
    _event_markers(fig, marks)
    text, state = plantpanels.takeaway_output(power, plant)
    with card():
        st.markdown(plantpanels.panel_header("Plant output against expected", "megawatts", text, state),
                    unsafe_allow_html=True)
        st.plotly_chart(fig, key="recovery_chart", config={"displayModeBar": False})
        st.markdown(plantpanels.legend_html(plantpanels.legend_output(power, dropped, SERIES)), unsafe_allow_html=True)
        if marks:
            st.markdown(plantpanels.markers_html(marks), unsafe_allow_html=True)
    st.caption("The expected lines are the fault-free plant on the weather each source reports. When the sun sensor is "
               "dusty, its line drops to meet a dusty plant and the loss looks normal (the dusty-sensor trap); the "
               "satellite line still shows it. The shaded band is the dust storm; numbered lines are the agent's actions.")
    with st.expander("Sunlight behind the expected lines"):
        fig = go.Figure()
        for col, name, color, dash in (
            ("sensor_ghi_wm2", "On-site sun sensor", SERIES["sensor"], "dot"),
            ("reference_ghi_wm2", "Satellite reference (NASA POWER)", SERIES["reference"], "solid"),
            ("clearsky_ghi_wm2", "Clear-sky model (shape only)", MUTED, "solid"),
        ):
            fig.add_trace(go.Scatter(x=x, y=power[col], name=name, mode="lines",
                                     line={"color": color, "width": 2 if color != MUTED else 1, "dash": dash},
                                     hovertemplate="%{y:.0f} W/m²"))
        fig.update_layout(height=240, hovermode="x unified", margin={"l": 0, "r": 0, "t": 10, "b": 0},
                          legend={"orientation": "h", "y": 1.2, "x": 0}, yaxis_title="Sunlight (W/m²)",
                          xaxis_range=[local(run.incident.start), local(run.incident.end)])
        st.plotly_chart(styled(fig), key="irradiance_chart")
        st.caption("The clear-sky model gives the shape of a cloudless day for comparison. It is not a ceiling: the "
                   "reference itself can sit above it.")


def map_panel(run: source.Run, plant) -> None:
    """The inverter tiles by transformer row (HTML), the equipment cards beside them, a Review button for any
    inverter waiting on your approval, and one inverter against its peers below."""
    st.subheader("Plant map")
    view = plantmap.map_view(run, plant)
    left, right = st.columns([3, 1])
    with left:
        st.markdown(plantpanels.map_html(view, plant), unsafe_allow_html=True)
        for inv in plantpanels.pending_inverters(view):
            st.button(f"Review: {plain.device(inv)} needs your approval", key=f"map_review_{inv}",
                      on_click=_go_approval, type="primary")
    with right:
        st.markdown(plantpanels.equipment_html(view, plant), unsafe_allow_html=True)
    st.caption(f"Low output means stopped or at least {plantmap.PEER_SHORTFALL:.0%} below the other inverters; capped "
               "means the inverter reports a derate or a power limit; no data means no reading arrived (never zero). "
               "◆ marks the agent's actions on a device. Synthetic plant.")
    with show_data("map"):
        st.dataframe(pd.DataFrame(plantpanels.raw_rows(view)), hide_index=True, key="map_raw")
    inverter_detail(run, plant)


def approval_card(run: source.Run, a, key_prefix: str = "") -> None:
    """One gated action in words: what, why, what to expect; Approve / Reject while it waits (live only)."""
    req = a.request
    with st.container(border=True):
        st.markdown(f"**{md_escape(plain.approval_needed(req.action, req.target, req.params))}**")
        st.markdown(f"Why: {md_escape(plain.humanize(req.rationale))}  \n"
                    f"What to expect: {md_escape(plain.humanize(req.expected_effect))}  \n"
                    f"Asked at {local_hhmm(a.requested_at)}. Reversible: {'yes' if req.reversible else 'no'}.")
        if a.status is not None:
            recorded = "Recorded decision: " if isinstance(run, ReplayRun) else ""
            st.markdown(recorded + md_escape(plain.decided(req.action, req.target, a.status == ActionStatus.APPLIED,
                                                           a.decided_by, a.note, req.params))
                        + f" ({local_hhmm(a.decided_at)})")
        elif not isinstance(run, ReplayRun):
            st.text_input("Your note (optional)", key=f"{key_prefix}note_{req.id}",
                          placeholder="Why you decided this way")
            ok, no = st.columns(2)
            decide = _trace_decide if key_prefix else _decide
            ok.button("Approve", key=f"{key_prefix}approve_{req.id}", on_click=decide, args=(req.id, True),
                      type="primary", width="stretch")
            no.button("Reject", key=f"{key_prefix}reject_{req.id}", on_click=decide, args=(req.id, False),
                      width="stretch")
        with show_data(f"approval_{key_prefix}{req.id}"):
            st.dataframe(pd.DataFrame([{"Action": req.action.value, "Target": req.target, "Params": str(req.params),
                                        "Id": req.id, "Status": a.status.value if a.status else "waiting",
                                        "Decided by": a.decided_by}]), hide_index=True)


def approvals(run: source.Run) -> None:
    """Gated actions waiting now (live), or the recorded decisions (replay and history)."""
    gated = [a for a in run.log.actions_until(run.now) if a.gated]
    st.subheader("Approvals")
    if not gated:
        st.caption("No action has needed your approval so far. Actions that need one appear here with Approve and "
                   "Reject buttons.")
    for a in gated:
        approval_card(run, a)


def agent_timeline(run: source.Run) -> None:
    st.subheader("Agent timeline")
    items = run.log.timeline_until(run.now)
    if not items:
        st.caption("No agent activity yet. Each agent step appears here as the run goes on.")
        return
    st.caption("Newest first. Each step looks at the alarms, runs the checks, weighs the causes, asks the model if "
               "needed, plans, passes the safety gate, acts, checks the result and reports. Every number is computed "
               "by code.")
    st.dataframe(pd.DataFrame([{"Time": local_hhmm(t.ts), "Step": NODE_WORDS.get(t.node, plain._words(t.node)),
                                "What": plain.humanize(t.summary)} for t in reversed(items)]),
                 hide_index=True, height=420, key="timeline_table")
    findings = run.log.findings_until(run.now)
    st.markdown("**What the checks find now**")
    if not findings:
        st.caption("No check reports anything unusual.")
    else:
        for r in findings:
            st.markdown(f"- {md_escape(plain.tool(r.tool))}: {md_escape(plain.verdict(r))}")
    diagnosis = run.log.diagnosis_until(run.now)
    proposals = run.log.proposals_until(run.now)
    if diagnosis:
        st.markdown("**Current diagnosis**")
        for h in diagnosis.root_causes:
            st.markdown(f"- {md_escape(plain.cause(h.labels[0]))}: {md_escape(plain.origin(h.labels[0], proposals))}")
        if diagnosis.unexplained:
            st.caption("Still unexplained: " + ", ".join(md_escape(plain.symptom_from_id(s)) for s in diagnosis.unexplained))
    with show_data("timeline"):
        st.dataframe(pd.DataFrame([{"Time": local_hhmm(t.ts), "Step": t.node, "Actor": t.actor, "What": t.summary,
                                    "Evidence": "; ".join(f"{k}: {v}" for k, v in t.details.items())}
                                   for t in reversed(items)]), hide_index=True, key="timeline_raw")
        if findings:
            st.dataframe(pd.DataFrame([{"Tool": r.tool, "Finding": r.verdict, "Devices": ", ".join(r.devices),
                                        "Evidence": r.evidence} for r in findings]), hide_index=True,
                         key="findings_table")
        if diagnosis:
            st.caption(f"Diagnosis confidence {diagnosis.confidence:.2f}")
            st.dataframe(pd.DataFrame([{"Cause": ", ".join(h.labels), "Score": h.score, "Explains": len(h.explains),
                                        "Evidence": "; ".join(h.evidence)} for h in diagnosis.root_causes]),
                         hide_index=True, key="diagnosis_table")


DECISION_WORD = {"accepted": ":green-background[Accepted]", "rejected": ":red-background[Rejected]",
                 "dropped": ":orange-background[Dropped later]"}


def proposals_panel(run: source.Run) -> None:
    st.subheader("Model proposals")
    proposals = run.log.proposals_until(run.now)
    asked = [t for t in run.log.timeline_until(run.now) if t.node == "consult" and t.summary.startswith("asked the model")]
    st.caption("A model may propose a cause when the rules leave something unexplained. The agent tests every "
               "proposal on the plant data first: only an accepted one enters the diagnosis. A rejected proposal "
               "changes nothing.")
    if not proposals:
        st.caption(f"No proposals tested so far ({len(asked)} model questions)." if asked
                   else "No proposals so far: the model was not asked (or this run is rules only).")
        return
    counts = {d: sum(p.decision == d for p in proposals) for d in ("accepted", "rejected", "dropped")}
    st.markdown(f"The model was asked {len(asked)} time{'s' if len(asked) != 1 else ''}: {counts['accepted']} proposal(s) "
                f"accepted, {counts['rejected']} rejected" + (f", {counts['dropped']} dropped later" if counts["dropped"] else "") + ".")
    actions, escalations = run.log.actions_until(run.now), run.log.escalations_until(run.now)
    for i, p in enumerate(reversed(proposals)):
        with st.container(border=True):
            st.markdown(f"{DECISION_WORD[p.decision]} {md_escape(plain.proposal(p.label, p.decision, p.reason, len(p.checks)))}")
            call = f"Model call took {p.llm_s:.1f} s ({md_escape(p.model)})" if p.model else "No model call this step"
            st.caption(f"Decided at {local_hhmm(p.ts)}. {call}.")
            st.markdown(md_escape(effect(p, actions, escalations)))
            with show_data(f"proposal_{i}"):
                st.dataframe(pd.DataFrame([{"Check": c.check, "Result": c.result,
                                            "Pass": {True: "pass", False: "FAIL", None: "context"}[c.passed]}
                                           for c in p.checks]),
                             hide_index=True, key=f"proposal_checks_{i}")
                if p.model_evidence:
                    st.dataframe(pd.DataFrame({"Model's evidence (quoted)": p.model_evidence}), hide_index=True,
                                 key=f"proposal_evidence_{i}")


def actions_panel(run: source.Run) -> None:
    st.subheader("Actions and verifications")
    actions = run.log.actions_until(run.now)
    verified = {v.action_id: (ts, v) for ts, v in run.log.verifications_until(run.now)}
    if not actions:
        st.caption("No action requested yet. Each action the agent asks for appears here with its result.")
    else:
        rows = []
        for a in actions:
            check = verified.get(a.request.id)
            rows.append({"Time": local_hhmm(a.requested_at),
                         "Action": plain.action_text(a.request.action, a.request.target, a.request.params),
                         "For": plain.cause(a.cause) if a.cause else "",
                         "Approval": plain.gate("needs_approval" if a.gated else "auto"),
                         "Outcome": plain.action_status(a.status) + (f" {plain.decided_by(a.decided_by)}" if a.status else ""),
                         "Takes effect": local_hhmm(a.effective_at) if a.effective_at else "",
                         "Verified": ("" if check is None else
                                      f"{'recovered' if check[1].recovered else 'not recovered'} at {local_hhmm(check[0])}: "
                                      f"{plain.power_change(check[1].before_kw, check[1].after_kw, check[1].expected_kw)}")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, key="actions_table")
        with show_data("actions"):
            st.dataframe(pd.DataFrame([{"Time": local_hhmm(a.requested_at), "Action": a.request.action.value,
                                        "Target": a.request.target, "For cause": a.cause,
                                        "Approval": "needed" if a.gated else "auto",
                                        "Status": a.status.value if a.status else "waiting", "By": a.decided_by,
                                        "Before / after / expected (kW)": (
                                            "" if a.request.id not in verified else
                                            f"{verified[a.request.id][1].before_kw:.0f} / {verified[a.request.id][1].after_kw:.0f} / "
                                            f"{verified[a.request.id][1].expected_kw:.0f}")} for a in actions]),
                         hide_index=True, key="actions_raw")
    escalations = run.log.escalations_until(run.now)
    if escalations:
        st.markdown("**Handed to a human**")
        for e in escalations:
            st.markdown(f"- {local_hhmm(e.ts)}: {md_escape(plain.escalated(e.subject, e.devices, plain.humanize(e.reason)))}")
        with show_data("escalations"):
            st.dataframe(pd.DataFrame([{"Time": local_hhmm(e.ts), "Subject": e.subject, "Devices": ", ".join(e.devices),
                                        "Reason": e.reason} for e in escalations]), hide_index=True,
                         key="escalations_table")


def llm_panel(run: source.Run) -> None:
    st.subheader("LLM calls and time")
    polls = run.log.polls_until(run.now)
    calls = run.log.llm_calls_until(run.now)
    agent_s = sum(p.agent_s for p in polls)
    a, b, c, d = st.columns(4)
    a.metric("Model calls", len(calls))
    b.metric("Time in the model", f"{sum(c.latency_s for c in calls):.1f} s")
    c.metric("Cost", f"{sum(c.cost_usd for c in calls):.4f} USD")
    d.metric("Agent time (model included)", f"{agent_s:.1f} s", f"{len(polls)} steps", delta_color="off")
    if calls:
        st.dataframe(pd.DataFrame([{"Purpose": plain.model_purpose(c.purpose).capitalize(), "Model": c.model,
                                    "Where": "local server" if c.role.value == "local" else "API",
                                    "Tokens in": c.prompt_tokens, "Tokens out": c.completion_tokens,
                                    "Seconds": round(c.latency_s, 2), "Cost (USD)": c.cost_usd,
                                    "Valid reply": "yes" if c.ok else "no", "Escalation": c.escalation} for c in calls]),
                     hide_index=True, key="llm_table")
    else:
        st.caption("No model calls: the rules explained everything so far (or this run is rules only).")


# -- Trace tab ---------------------------------------------------------------------------------------------

KIND_STYLE = {trace.FINDING: ("FINDING", SEMANTIC["attention"][1]), trace.OK: ("OK", SEMANTIC["healthy"][1]),
              trace.INCONCLUSIVE: ("INCONCLUSIVE", SEMANTIC["no data"][1])}  # word + colour: never colour alone
STATUS_CHIP = {trace.OPEN: "info", trace.ACTING: "attention", trace.RESOLVED: "healthy", trace.ESCALATED: "needs a human",
               trace.EXPECTED: "no data", trace.FLAGGED: "critical"}


def trace_header(head: trace.Header) -> None:
    a, b, c, d, e = st.columns([1, 1, 3, 1, 1])
    a.metric("Simulated time", f"{local(head.ts):%H:%M}", DISPLAY_TZ, delta_color="off")
    b.metric("Turn", f"{head.turn}" + (f" of {head.turns_total}" if head.turns_total else ""))
    with c:
        st.markdown("**Causes so far**")
        named = [(s, labels) for s, labels in head.causes.items() if labels]
        if named:
            st.markdown(" ".join(chip(f"{plain.cause_status(s)}: {len(labels)}", STATUS_CHIP[s]) for s, labels in named),
                        unsafe_allow_html=True)
            st.caption("; ".join(f"{plain.cause_status(s)}: {', '.join(md_escape(plain.cause(x)) for x in labels)}"
                                 for s, labels in named))
        else:
            st.caption("None named yet.")
    d.metric("Model calls", head.llm_calls)
    e.metric("Time in the model", f"{head.llm_s:.1f} s")


def _styled_checks(rows: list[trace.CheckRow]):
    frame = pd.DataFrame([{"Verdict": f"{KIND_STYLE[r.kind][0]}: {r.verdict}", "Tool": r.tool, "Devices": r.devices,
                           "Numbers": r.numbers, "Threshold": r.threshold} for r in rows])
    colours = [f"background-color: {KIND_STYLE[r.kind][1]}" for r in rows]
    return frame.style.apply(lambda _: colours, subset=["Verdict"])


def _poll_checks(run: source.Run, card: trace.TurnCard):
    """The ToolResults of this turn (v3 checks, else the findings), for the plain sentences."""
    poll = next((p for p in run.log.polls if p.ts == card.ts), None)
    if poll is None:
        return []
    return list(poll.checks or poll.findings or [])


def trace_card(card: trace.TurnCard, run: source.Run) -> None:
    focused = st.session_state.get(summary.FOCUS_KEY) == card.index
    actions_by_id = {a.request.id: a for a in run.log.actions}
    proposals_so_far = run.log.proposals_until(card.ts)
    results = _poll_checks(run, card) if card.checks is not None else []
    named = [c.label for c in card.candidates or [] if c.selected]
    with st.container(border=True, key=f"trace_card_{card.index}"):
        title = f"Turn {card.index}, {local_hhmm(card.ts)}" + (" (final step)" if card.final else "")
        st.markdown((":violet-background[**%s**]" if focused else "**%s**") % title)
        if card.checks is not None and results:
            st.markdown(f'<p class="rq-headline">{block(plain.checks_headline(results, named))}</p>',
                        unsafe_allow_html=True)
        elif card.checks is None and named:
            st.markdown(f'<p class="rq-headline">{block("Named: " + ", ".join(plain.cause_short(x) for x in named) + ".")}</p>',
                        unsafe_allow_html=True)
        for o in card.outcomes:
            st.markdown(f"- {md_escape(_outcome_sentence(o, actions_by_id))}")
        if card.symptom_ids or card.symptoms:
            signs = [plain.symptom_from_id(s) for s in card.symptom_ids] or card.symptoms
            st.markdown("New signs: " + "; ".join(md_escape(s) for s in signs[:6])
                        + (f"; and {len(signs) - 6} more" if len(signs) > 6 else "") + ".")
        if results:
            found = [r for r in results if r.finding and r.tool not in plain.ALARM_TOOLS]
            quiet = [r for r in results if not r.finding]
            for r in found:
                st.markdown(f'<div class="rq-found"><b>{block(plain.tool(r.tool))}:</b> '
                            f'{block(plain.verdict(r))}</div>', unsafe_allow_html=True)
            unsure = sum(1 for r in quiet if r.verdict == trace.INCONCLUSIVE)
            nothing = len(quiet) - unsure
            if quiet:
                words = []
                if nothing:
                    words.append(f"{nothing} check{'s' if nothing != 1 else ''} found nothing")
                if unsure:
                    words.append(f"{unsure} had too little data yet")
                st.markdown(f'<p class="rq-quiet">{block(" and ".join(words))}.</p>', unsafe_allow_html=True)
        elif card.checks is not None:
            st.markdown('<p class="rq-quiet">Checks ran; nothing unusual.</p>', unsafe_allow_html=True)
        if card.candidates is not None and named:
            for label in named:
                st.markdown(f"- {md_escape(plain.found(label, proposals_so_far))}")
            if card.unexplained:
                st.caption("Still unexplained: " + ", ".join(md_escape(plain.symptom_from_id(s)) for s in card.unexplained))
        if card.llm_calls or card.proposals:
            trace_model(card)
        if card.gate:
            trace_actions(card, run)
        for v in card.verifications:
            a = actions_by_id.get(v.action_id)
            text = (plain.verified(a.request.action, a.request.target, v.recovered, v.before_kw, v.after_kw, v.expected_kw)
                    if a else f"Verified {v.action_id}: {'recovered' if v.recovered else 'not recovered'}.")
            st.markdown(f"- {md_escape(text)}")
        for e in card.escalations:
            st.markdown(f"- {md_escape(plain.escalated(e.subject, e.devices, plain.humanize(e.reason)))}")
        alarms = _turn_alarms(run, card)
        if len(alarms):
            with st.expander(f"Alarms this turn ({len(alarms)}): messages quoted, untrusted", expanded=False):
                st.dataframe(pd.DataFrame({"Time": alarms["ts"].map(local_hhmm), "Device": alarms["device_id"].map(plain.device),
                                           "Code": alarms["code"], "Severity": alarms["severity"].astype(str),
                                           "Message (quoted)": alarms["message"]}),
                             hide_index=True, key=f"trace_alarms_{card.index}")
        if card.notes:
            with st.expander(f"Notes ({len(card.notes)}): quoted, untrusted", expanded=False):
                st.dataframe(pd.DataFrame([{"Time": local_hhmm(n.ts), "Source": n.source, "Author": n.author,
                                            "About": n.about, "Text (quoted)": n.text} for n in card.notes]),
                             hide_index=True, key=f"trace_notes_{card.index}")
        trace_show_data(card, run)


def _outcome_sentence(outcome: str, actions_by_id) -> str:
    """"raqeeb-1: applied" (trace._outcomes) -> "The plant reports: ordered cleaning ... done"."""
    action_id, _, rest = outcome.partition(": ")
    a = actions_by_id.get(action_id)
    if a is None:
        return f"The plant reports: {outcome}"
    status, _, note = rest.partition(" (")
    return plain.outcome(a.request.action, a.request.target, status.upper(), note.rstrip(")"))


def trace_show_data(card: trace.TurnCard, run: source.Run) -> None:
    """The raw record of the turn: every table the card used to show, unchanged, collapsed."""
    with show_data(f"trace_{card.index}"):
        if card.checks:
            st.dataframe(_styled_checks(card.checks), hide_index=True, key=f"trace_checks_{card.index}")
        elif card.checks is not None:
            st.caption("Tool checks: no finding.")
        if card.symptoms:
            st.dataframe(pd.DataFrame({"New symptom (agent's summary)": card.symptoms}), hide_index=True,
                         key=f"trace_symptoms_{card.index}")
        if card.candidates is not None:
            top = max([c.score for c in card.candidates] + [1.0])
            st.dataframe(pd.DataFrame([{"Cause": c.label, "Score": c.score,
                                        "Selected": "✓ selected" if c.selected else "not selected",
                                        "Explains": c.explains} for c in card.candidates]),
                         hide_index=True, key=f"trace_candidates_{card.index}",
                         column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0.0,
                                                                                 max_value=top, format="%.2f")})
        if card.gate:
            st.dataframe(pd.DataFrame([{"Action": g.action, "Target": g.target, "For cause": g.cause, "Gate": g.gate,
                                        "Decision": g.decision, "Note (quoted)": g.entry.note if g.entry else ""}
                                       for g in card.gate]), hide_index=True, key=f"trace_actions_{card.index}")
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
        elif card.llm_calls:
            st.caption("Data sent and replies are not recorded in this file (only call metadata).")
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
        if card.steps:
            st.caption(f"What the agent did ({len(card.steps)} step{'s' if len(card.steps) != 1 else ''})")
            st.dataframe(pd.DataFrame([{"Step": NODE_WORDS.get(s.node, s.node), "What (quoted)": s.summary}
                                       for s in card.steps]), hide_index=True, key=f"trace_steps_{card.index}")


def _turn_alarms(run: source.Run, card: trace.TurnCard) -> pd.DataFrame:
    """Alarms that arrived during this turn, (previous turn, this turn], from the run's telemetry store."""
    start = card.since + timedelta(seconds=1) if card.since else run.incident.start
    events = run.store.read("events", start, card.ts + timedelta(seconds=1)).reset_index()
    return events


def trace_model(card: trace.TurnCard) -> None:
    seconds = sum(c.latency_s for c in card.llm_calls)
    purposes = list(dict.fromkeys(plain.model_purpose(c.purpose) for c in card.llm_calls))
    if card.llm_calls:
        st.markdown("- " + md_escape(f"{'; '.join(purposes).capitalize()} ({len(card.llm_calls)} call"
                                     f"{'s' if len(card.llm_calls) != 1 else ''}, {seconds:.1f} s)."))
    for p in card.proposals:
        st.markdown(f"- {DECISION_WORD[p.decision]} {md_escape(plain.proposal(p.label, p.decision, p.reason, len(p.checks)))}")


def trace_actions(card: trace.TurnCard, run: source.Run) -> None:
    for g in card.gate:
        if g.entry is None:
            text = (f"Held back by the safety gate: {plain.action_text(g.action, g.target)} "
                    f"({g.gate.removeprefix('held back: ')}). Never sent.")
        elif g.entry.gated:
            text = plain.approval_needed(g.action, g.target, g.entry.request.params)
            if g.entry.status is not None:
                text += " " + plain.decided(g.action, g.target, g.entry.status == ActionStatus.APPLIED, g.entry.decided_by,
                                            g.entry.note, g.entry.request.params)
        else:
            text = plain.acted(g.action, g.target, g.entry.request.params)
            if g.entry.status not in (None, ActionStatus.APPLIED):
                text += f" Outcome: {plain.action_status(g.entry.status)}" + (f" ({g.entry.note})" if g.entry.note else "") + "."
        st.markdown(f"- {md_escape(text)}")
    for a in card.actions:
        if a.status is None and isinstance(run, LiveRun) and any(w.request.id == a.request.id for w in run.waiting):
            approval_card(run, a, key_prefix="trace_")


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
            st.caption("No turns yet: press Step or Play. Each agent turn becomes a card here.")
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


def _scroll_to_approval() -> None:
    """Scroll the page to the approval card (the strip's "Needs your decision"). Fixed script."""
    st.html("""<script>
      const card = document.querySelector('.st-key-approval_card');
      if (card) { card.scrollIntoView({block: 'start', behavior: 'smooth'}); }
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
    intro("Trace")
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
        polls = run.log.polls_until(run.now)  # a replay mid-way offers only the turns played so far
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
                                 format_func=lambda i: f"turn {i + 1}, {local_hhmm(polls[i].ts)}")
            if k < len(polls) - 1:
                view = source.RunAt(run, polls[k].ts)
            if st.session_state.get(summary.FOCUS_KEY) not in (None, k + 1):
                st.session_state.pop(summary.FOCUS_KEY)  # the clock was moved by hand: the focus is over
        trace_header(trace.header(view.log, view.now, live=isinstance(run, LiveRun)))
        if summary.FOLLOW_KEY not in st.session_state:
            st.session_state[summary.FOLLOW_KEY] = True
        st.toggle("Auto-scroll to latest", key=summary.FOLLOW_KEY)
        trace_stream(run, view.now)


def report_markdown(run: source.Run, report: IncidentReport) -> str:
    statuses = {a.request.id: a.status for a in run.log.actions_until(run.now)}
    verified = {v.action_id: v for v in report.verifications}
    lines = [f"# Shift report: {run.incident.id}", "",
             f"> Synthetic plant. {source.mode_label(run)}. Times in {DISPLAY_TZ}.", "",
             "## Summary (English)", "", report.summary_en, "",
             "## الملخص (العربية)", "", report.summary_ar, "",
             "## Root causes", ""]
    lines += [f"- {plain.cause(h.labels[0])} (`{', '.join(h.labels)}`): {'; '.join(h.evidence)}" for h in report.causes]
    lines += ["", "## Actions", "", "| Action | Status | Verified |", "|---|---|---|"]
    for a in report.actions:
        status = statuses.get(a.id)
        v = verified.get(a.id)
        check = ("recovered" if v.recovered else "not recovered") if v else "not checked"
        lines.append(f"| {plain.action_text(a.action, a.target, a.params)} (`{a.action.value}` on `{a.target}`) | "
                     f"{plain.action_status(status)} | {check} |")
    lines += ["", f"LLM calls: {report.llm_calls}; cost: {report.cost_usd:.4f} USD", ""]
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

MOMENT_COLOUR = {summary.SYMPTOM: SEMANTIC["attention"][0], summary.CAUSE: "#5b3aa6", summary.PROPOSAL: "#117a65",
                 summary.ACTION: SEMANTIC["info"][0], summary.APPROVAL: "#8a4b00", summary.VERIFICATION: SEMANTIC["healthy"][0],
                 summary.ESCALATION: SEMANTIC["critical"][0]}
MOMENT_SYMBOL = {summary.SYMPTOM: "diamond", summary.CAUSE: "circle", summary.PROPOSAL: "triangle-up",
                 summary.ACTION: "square", summary.APPROVAL: "star", summary.VERIFICATION: "circle-open",
                 summary.ESCALATION: "x"}
OUTCOME_STATE = {"rejected": "critical", "not recovered": "critical", "never sent": "critical", "escalated": "needs a human",
                 "accepted": "healthy", "recovered": "healthy", "approved": "healthy", "confirmed": "info",
                 "waiting for a decision": "needs a human"}


def _jump_to_turn(turn: int) -> None:
    summary.jump(st.session_state, int(turn))
    go_to("Trace")


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
            text=[f"turn {m.turn}, {local_hhmm(m.ts)}<br>{source.html_text(m.said or m.text)}" for m in mine],
            hovertemplate="%{text}<extra></extra>", showlegend=False))
        lookup.update({(curve, i): m.turn for i, m in enumerate(mine)})
    st.session_state.summary_points = lookup
    fig.update_layout(height=60 + 34 * len(summary.KINDS), margin={"l": 0, "r": 0, "t": 10, "b": 0},
                      yaxis={"categoryorder": "array", "categoryarray": list(reversed(summary.KINDS)),
                             "fixedrange": True},
                      xaxis={"range": [local(run.incident.start), local(run.incident.end)],
                             "title": f"Time ({DISPLAY_TZ})"},
                      clickmode="event+select", dragmode=False)
    return styled(fig)


def first_time_path(run: source.Run) -> None:
    """Three lines on what Raqeeb is doing, and one button that plays the loaded run."""
    st.markdown("Solar Actor watches a solar plant that has several things going wrong at once.  \n"
                "It works out the causes from the plant's data alone, fixes what is safe to fix, asks you before "
                "anything risky, and checks that output came back.  \n"
                "Press Play the storm to watch it happen; every step is explained in the Trace tab.")
    stopped = run.finished or bool(run.waiting)
    if run.finished:
        st.caption("This run has ended. Use the clock in the Trace tab to go back through it, or load another run "
                   "from the sidebar.")
    else:
        st.button("Play the storm", key="play_storm", on_click=_play_storm, type="primary", disabled=stopped)


def summary_tab(run: source.Run, power: pd.DataFrame) -> None:
    st.subheader("Incident summary")
    intro("Summary")
    now = run.now
    log = run.log
    moments = summary.key_moments(log, now)
    head = trace.header(log, now, live=isinstance(run, LiveRun))
    e = summary.energy(power, log.actions_until(now), now)
    rows = summary.cause_rows(log, now)
    proposals = log.proposals_until(now)
    if not log.polls_until(now):
        first_time_path(run)
        st.caption("No turns yet: press Play the storm, or Step in the sidebar. The summary fills in as the agent works.")
        return
    if run.waiting:
        with st.container(key="approval_card"):
            st.warning("The agent asked for an action that needs your approval. The run is paused until you decide.")
            approvals(run)
        if st.session_state.pop("jump_approval", False):
            _scroll_to_approval()
    elif not run.finished and not st.session_state.get("playing") and not (_runner(run) and _runner(run).state().running):
        first_time_path(run)
    a, b, c, d, f = st.columns(5)
    a.metric("Agent steps", head.turn if head.turns_total is None else f"{head.turn} of {head.turns_total}")
    b.metric("Causes found", sum(len(v) for v in head.causes.values()),
             f"{len(head.causes[trace.RESOLVED])} fixed, {len(head.causes[trace.ESCALATED])} to a human",
             delta_color="off", delta_arrow="off")
    c.metric("Energy below expected", f"{e.lost_mwh:.2f} MWh",
             f"{e.lost_before_mwh:.2f} before first fix, {e.lost_after_mwh:.2f} after"
             if e.first_effective else "no fix in effect yet", delta_color="off", delta_arrow="off")
    d.metric("Shortfall", "none yet" if e.gap_worst_pct is None
             else f"{percent(e.gap_worst_pct)} → {percent(0 if e.gap_last_pct is None else e.gap_last_pct)}",
             "worst hour → last hour" if e.gap_worst_pct is not None else None, delta_color="off", delta_arrow="off")
    f.metric("Model calls", head.llm_calls, f"{head.llm_s:.1f} s in the model" if head.llm_calls else "rules only",
             delta_color="off", delta_arrow="off")
    with st.expander("How is energy below expected measured?"):
        st.markdown("Energy below expected is the area between the expected-from-reference line and the meter, up to the "
                    "simulated clock: the shortfall, whatever its cause (the reference itself carries a few percent of "
                    "error). Recovery shows as the shortfall after the first fix took effect, and in the last hour's "
                    "percentage. The shortfall reads worst hour first, then the last hour; a negative last hour means "
                    "the plant is above the reference.")

    st.markdown("**Key moments.** Click a marker or a button to open that step in the Trace tab.")
    if moments:
        with card():
            st.plotly_chart(timeline_figure(moments, run), key="summary_timeline", on_select=_timeline_clicked,
                            selection_mode="points", config={"displayModeBar": False})
        st.session_state.summary_moment_turns = [m.turn for m in moments]
        with st.container(height=min(60 + 52 * len(moments), 380)):
            for i, m in enumerate(moments):
                text_col, go_col = st.columns([7, 1])
                state = OUTCOME_STATE.get(m.outcome)
                mark = chip(m.outcome, state) if state else ""
                text_col.markdown(f'<span class="rq-time">{local_hhmm(m.ts)}</span> {inline(m.said or m.text)} {mark}',
                                  unsafe_allow_html=True)
                go_col.button("Open step", key=f"summary_moment_{i}", on_click=_jump_to_turn, args=(m.turn,),
                              width="stretch", help=f"Open turn {m.turn} in Trace")
        with show_data("moments"):
            st.dataframe(pd.DataFrame([{"Time": local_hhmm(m.ts), "Turn": m.turn, "What": m.kind,
                                        "Detail": m.text, "Outcome": m.outcome} for m in moments]),
                         hide_index=True, key="summary_moments")
    else:
        st.caption("Nothing to report yet: the first sign, cause, action or model proposal will appear here.")

    st.markdown("**Causes**")
    if rows:
        st.dataframe(pd.DataFrame([{"Cause": plain.cause(r.cause), "How it was found": plain.origin(r.cause, proposals),
                                    "Status": plain.cause_status(r.status),
                                    "First named": f"turn {r.turn}" if r.turn else "from an action or escalation",
                                    "Actions": _plain_actions(r.cause, log, now), "Verified": r.verified if r.verified != "—" else "not yet"}
                                   for r in rows]),
                     hide_index=True, key="summary_causes")
        picks = [r for r in rows if r.turn]
        if picks:
            cols = st.columns(min(len(picks), 4))
            for i, r in enumerate(picks[:8]):
                cols[i % len(cols)].button(f"Turn {r.turn}: {plain.cause_short(r.cause)}", key=f"summary_go_{r.cause}",
                                           on_click=_jump_to_turn, args=(r.turn,), width="stretch")
        with show_data("causes"):
            st.dataframe(pd.DataFrame([{"Cause": r.cause, "Status": r.status,
                                        "First named": f"turn {r.turn}" if r.turn else "",
                                        "Actions": r.actions, "Verified": r.verified} for r in rows]),
                         hide_index=True, key="summary_causes_raw")
    else:
        st.caption("No cause named yet. The causes the agent names, how it found them and what it did appear here.")

    if not run.waiting and any(a.gated for a in log.actions_until(now)):
        approvals(run)

    report = log.report_until(now)
    st.markdown("**Shift report**")
    if report is None:
        st.caption("The agent writes the shift report at the end of the run.")
    else:
        report_columns(report)
    st.session_state.pop(summary.SWITCH_KEY, None)  # the jump already switched the page


def percent(value: float) -> str:
    """A whole percentage with a space and a real minus sign: 17 → "17 %", -1 → "−1 %"."""
    return f"{value:.0f} %".replace("-", "−")


def _plain_actions(label: str, log, now) -> str:
    mine = [a for a in log.actions_until(now) if a.cause == label]
    if not mine:
        return "none"
    return "; ".join(f"{plain.action_text(a.request.action, a.request.target, a.request.params)} "
                     f"({plain.action_status(a.status)})" for a in mine)


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


SUGGESTIONS = ("what went wrong", "which actions needed approval", "how much energy was lost")


def _suggest(text: str) -> None:
    st.session_state.ask_pending = text


def ask_fab(run: source.Run, power: pd.DataFrame) -> None:
    """Ask Raqeeb as a floating popup, pinned bottom-right on every page (CSS on the keyed container)."""
    history: list[ask.Answer] = st.session_state.setdefault("ask_history", [])
    with st.container(key="ask_fab"):
        with st.popover("Ask the agent"):
            st.markdown("**Ask the agent**  \nAnswers from this incident's record only; the facts used are listed under "
                        "\"Based on:\". The chat cannot request, approve or reject an action.")
            question = st.chat_input("Ask about this incident", key="ask_input", max_chars=400)
            pending = st.session_state.pop("ask_pending", None)
            question = (question or pending or "").strip()
            if question:
                with run_lock(run):
                    history.append(ask.ask(question, run.log, run.now, power, gateway=_ask_gateway()))
            if len(history) < 2:
                cols = st.columns(len(SUGGESTIONS))
                for i, text in enumerate(SUGGESTIONS):
                    cols[i].button(text.capitalize(), key=f"ask_sugg_{i}", on_click=_suggest, args=(text,),
                                   width="stretch")
            if not history:
                st.caption("No question yet. Try one of the suggestions, or type your own.")
            for a in history:
                with st.chat_message("user"):
                    st.markdown(md_escape(a.question))
                with st.chat_message("assistant"):
                    st.markdown(md_escape(a.text if a.phrased_by else plain.humanize(a.text)))
                    if a.phrased_by:
                        st.caption(f"Worded by {md_escape(a.phrased_by)} in {a.llm_s:.1f} s from the facts below, nothing else.")
                    else:
                        st.caption("No model reachable" + (f" ({md_escape(a.note)})" if a.note else "") + ": the facts are "
                                   "shown as retrieved.")
                    with st.expander("Based on:", expanded=bool(a.phrased_by)):
                        st.markdown("\n".join(f"- {md_escape(plain.humanize(f))}" for f in a.facts))


def report_tab(run: source.Run) -> None:
    st.subheader("Shift report")
    intro("Report")
    report = run.log.report_until(run.now)
    if report is None:
        st.caption("The agent writes the shift report at the end of the run. It appears here, in English and Arabic, "
                   "with a download button.")
        return
    report_columns(report)
    st.download_button("Download report (Markdown)", report_markdown(run, report),
                       file_name=f"raqeeb_report_{run.incident.id}.md", mime="text/markdown", key="download_report")
    with st.expander("Markdown source"):
        st.code(report_markdown(run, report), language="markdown")


def answer_key_tab(run: source.Run) -> None:
    st.subheader("Answer key")
    intro("Answer key")
    result = source.reveal(run)
    if result is None:
        st.caption("Hidden while the run is in progress. The agent never sees it; it appears here once the run ends.")
        return
    if not st.toggle("Reveal answer key", key="reveal"):
        st.caption("The run has ended. Turn on the switch to compare the agent with the planted faults.")
        return
    st.markdown(f"The agent named **{result.found} of {result.root_causes}** planted root causes"
                + (f"; it also named {', '.join(md_escape(plain.cause(c)) for c in result.extra_causes)}, which were not planted."
                   if result.extra_causes else "."))
    faults = result.faults.copy()
    faults.insert(0, "What was planted", faults["Planted fault"].map(plain.cause))
    faults["Agent actions on it"] = faults["Agent actions on it"].map(plain.humanize)
    st.dataframe(faults, hide_index=True, key="reveal_faults")
    st.markdown("**Actions against the answer key**")
    actions = result.actions.copy()
    if len(actions):
        by_time = {(local_hhmm(a.requested_at), a.request.action.value, a.request.target): a.request.params
                   for a in run.log.actions_until(run.now)}
        actions["Action"] = [plain.action_text(a, t, by_time.get((ts, a, t), {}))
                             for ts, a, t in zip(actions["Time"], actions["Action"], actions["Target"])]
        actions["Target"] = actions["Target"].map(plain.device)
        actions["Status"] = actions["Status"].map(plain.action_status)
        actions["Addresses"] = actions["Addresses"].map(lambda s: ", ".join(plain.cause(x) for x in s.split(", "))
                                                        if "@" in str(s) else s)
    st.dataframe(actions, hide_index=True, key="reveal_actions")


def results_tab() -> None:
    st.subheader("Benchmark results")
    intro("Results")
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
        st.caption(f"Results not available yet at {md_escape(path.name)}. The final benchmark results arrive after "
                   "the final run; until then pick a practice folder above.")
        return
    table = results.summary
    shown = table[[c for c in SUMMARY_COLUMNS if c in table.columns]].rename(columns=SUMMARY_COLUMNS)
    if "Agent" in shown.columns:
        shown = shown.assign(Agent=shown["Agent"].map(lambda a: AGENT_NAMES.get(a, a)))
    st.caption("Precision and recall are about the root causes named; false actions are actions on a symptom or a "
               "decoy; verified recovery means every applied action was checked and output came back.")
    st.dataframe(shown, hide_index=True, key="results_summary")
    if "mwh_recovered" in table.columns:
        bars = table.sort_values("mwh_recovered")
        notes = bars["note"].fillna("").astype(str) if "note" in bars.columns else pd.Series("", index=bars.index)
        colors = [MUTED if n else SERIES["actual"] for n in notes]
        fig = go.Figure(go.Bar(x=bars["mwh_recovered"], y=bars["agent"].map(lambda a: AGENT_NAMES.get(a, a)),
                               orientation="h", marker_color=colors,
                               text=[f"{v:.1f}" for v in bars["mwh_recovered"]], textposition="outside",
                               hovertemplate="%{y}: %{x:.1f} MWh<extra></extra>"))
        fig.update_layout(height=60 + 45 * len(bars), margin={"l": 0, "r": 40, "t": 10, "b": 0},
                          xaxis_title="Energy recovered compared with doing nothing (MWh)")
        with card():
            st.plotly_chart(styled(fig), key="results_chart")
        lost = table["mwh_lost"].iloc[0] if "mwh_lost" in table.columns else None
        st.caption("Grey bars are reference bounds, not competitors (the oracle reads the answer key; the empty agent "
                   "does nothing)." + (f" Energy lost to the faults with no action: {lost:.1f} MWh." if lost is not None else ""))
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
    st.set_page_config(page_title="Solar Actor console", layout="wide")
    _, default_id = scenario_choices()
    st.session_state.setdefault("scenario_id", default_id)
    st.session_state.setdefault("speed", 60)
    st.session_state.setdefault("playing", False)
    if "run" not in st.session_state:
        st.session_state.run = initial_run()
        st.session_state.mode_choice = REPLAY if isinstance(st.session_state.run, ReplayRun) else LIVE_RULES
    run: source.Run = st.session_state.run
    st.session_state.render_at = time.time()

    st.markdown(CSS, unsafe_allow_html=True)
    with run_lock(run):  # a background runner (live Trace) polls under the same lock
        sidebar(run)
        header(run)
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


def live_tick(run: source.Run) -> None:
    """While the background runner advances a live run, re-render the whole page about once a second so the
    clock, the strip and the page follow it on every page (the Trace page has its own fragment for its cards).
    The fragment's timer, not its first render, triggers the rerun; one more rerun when the runner stops."""
    runner = _runner(run)
    active = runner is not None and runner.state().running
    if active or st.session_state.get("tick_active"):
        st.fragment(run_every=TRACE_REFRESH_S if active else None)(_tick)()


def _tick() -> None:
    runner = _runner()
    active = runner is not None and runner.state().running
    was = st.session_state.get("tick_active", False)
    st.session_state.tick_active = active
    if time.time() - st.session_state.get("render_at", 0.0) < 0.5:
        return  # the page is being rendered now: nothing to refresh yet
    if active or was:
        st.rerun()


def _page(run: source.Run) -> None:
    data = source.tables(run)
    power = lines(run)
    page = current_page()
    if page != "Trace":
        live_tick(run)
    notification_strip(run)
    alerts_banner(run)
    if page == "Summary":
        summary_tab(run, power)
    elif page == "Trace":
        trace_tab(run, power)
    elif page == "Plant":
        st.subheader("Plant")
        intro("Plant")
        plant = load_plant()
        stat_row(run, power, data)
        sun_wind_panel(run, plant)
        map_panel(run, plant)
        output_panel(run, power)
        alarm_feed(data)
        notes_panel(run)
    elif page == "Agent":
        st.subheader("Agent")
        intro("Agent")
        agent_timeline(run)
        proposals_panel(run)
        actions_panel(run)
        llm_panel(run)
    elif page == "Report":
        report_tab(run)
    elif page == "Answer key":
        answer_key_tab(run)
    elif page == "Results":
        results_tab()
    footer()
    ask_fab(run, power)


main()
