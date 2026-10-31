# Handoff: Raqeeb console redesign (Streamlit)

## Overview
New look for the Raqeeb console: a ChatGPT-style left sidebar listing the features, a sticky top bar holding playback and approval, a Summary page that tells the story first, Trace as a column of cards, an HTML inverter grid, an explained output chart, and **Ask Raqeeb as a floating support-chat popup that stays visible on every tab** (it is no longer a tab).

## About the design files
`Raqeeb Console.dc.html` is a **design reference in HTML**, not code to ship. Recreate it in the existing Streamlit 1.66 app (`app.py`, wording in `plain.py`) using `st.*` widgets plus HTML/CSS inside `st.markdown(..., unsafe_allow_html=True)`. Open the file in a browser to click through it (it runs with `support.js` next to it).

All numbers in the mock (21 %, 78 °C, times, inverter states) are **placeholders**. Bind every value to the agent's frozen record; do not invent data.

## Fidelity
High fidelity for layout, colours, type and copy style. Adapt pixel details to what Streamlit allows.

## Hard constraints (from docs/CONSOLE_DESIGN_BRIEF.md, unchanged)
- Keep all existing widget keys (tests depend on them).
- Words beside every colour; text contrast ≥ 4.5:1.
- No raw ids by default; raw record stays behind "Show data" (`st.expander`).
- Plant free text shown quoted (escaped), never rendered as HTML.
- "Synthetic" visible (sidebar footer + header subtitle).
- Sentence case. Target width 1366 px.

## Layout skeleton
```
┌ sidebar 248px ─┬ sticky header: incident title | Replay clock+bar+Play | "1 action needs your approval" ┐
│ R  Raqeeb      │                                                                                       │
│ THE INCIDENT   │  main column, max-width 1080px, padding 32px, sections gap 28px                       │
│ [P] Plant      │                                                                                       │
│ [S] Summary    │                                                                                       │
│ [T] Trace      │                                                                                       │
│ [A] Agent      │                                                                                       │
│ [R] Report     │                                                                                       │
│ FOR JUDGES     │                                                                                       │
│ [K] Answer key │                                                              ┌ chat panel 380×540 ┐   │
│ [✓] Results    │                                                              └────────────────────┘   │
│ Simulated plant│                                                                  ( R  Ask Raqeeb )    │
└────────────────┴───────────────────────────────────────────────────────────────────────────────────────┘
```

## Streamlit implementation notes
- **Navigation**: replace `st.tabs` with a sidebar selector: `st.sidebar.radio(key=<existing tab key or new "nav">, options=[...], format_func=...)`, styled via CSS so each option is a row with a 22px icon square, label (14px/500, 600 when selected) and a 12px grey hint line. Two group labels ("The incident", "For judges") as small caps markdown. Alternative: `st.navigation`/`st.Page` if keys allow. Selected row background `#ffffff`, hover `#e3e0d9`, sidebar background `#eeece7`.
- **Hint lines** (exact copy): Summary "What happened" · Trace "How it was solved" · Plant "Map and inverters" · Agent "What Raqeeb did" · Report "Shift report" · Answer key "The faults we injected" · Results "How well it did".
- **Run controls (MUST keep; the old sidebar "Run" + "Playback" sections)**: in the header, a small caption "Run the agent" (11px/600, uppercase, 0.06em, `#5d5f63`) directly above a run pill showing the current mode ("Replay: storm run" / "Live: rules only" / "Live: rules + local model") + "· Change". Clicking opens a "Run the agent" panel (`st.popover`, ~440px): Mode as three selectable cards (label, hint, model path in 11px mono), then for Replay: Recorded run selectbox + Load button, and an Upload (.json, 20 MB) row + Load uploaded; for Live modes: Scenario selectbox ("Practice set 3 (scn_0003)"), "Start live run" (primary) and "Dust storm + 2 random faults" (secondary) side by side, caption "Faults are planted in the synthetic plant. The agent sees only the readings." For "Live: rules + language model" (renamed from "rules + local model") add a Language model block above Scenario: 4-way segmented provider picker Claude / ChatGPT / Gemini / Local LLM (selected = dark fill, white text). Cloud: Model selectbox + API key password input with placeholder "Leave blank to use <ENV_VAR> from .env" (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY). Local: Endpoint text input (placeholder http://localhost:8080/v1, default from LOCAL_LLM_ENDPOINT) + detected model path in 11px mono. Under it a status line, dot + words: "<VAR> found in .env" (ok) or "<VAR> not set in .env. Paste a key above." (warn). Never echo key values. Placeholders only for now; wire to existing model/API-tier keys. Bottom: Playback speed slider (sim minutes per tick). Reuse ALL existing widget keys for mode, run select, load, upload, speed. The header playback group holds Sim time clock, progress bar, Step, To end, Play.
- **Header**: render at top of main area on every page (one function, e.g. `render_header()`). Move playback controls out of the sidebar into it (`st.columns([3,2,1.4])`: title block | slider + play button | approval pill button that sets nav to Summary). Make it sticky with CSS on its container (`position:sticky;top:0;z-index:5;background:rgba(246,245,242,.94)`).
- **Ask Raqeeb popup**: delete the Ask tab. On every run, after page content, render a container pinned bottom-right with CSS (`position:fixed;right:24px;bottom:24px;z-index:1000`) holding `st.popover("Ask Raqeeb")`; inside: header ("Ask Raqeeb" / "Answers from this incident's record only"), message history from `st.session_state`, 3 suggested-question buttons while history is short, and `st.chat_input` or `st.text_input` + Send. Reuse the existing Ask tab's answer function and keys. Target the popover with a keyed `st.container(key="ask_fab")` → CSS selector `.st-key-ask_fab`. Popover body ~380×540, radius 16px, shadow `0 18px 50px rgba(29,31,34,.18)`. Trigger: dark pill `#1d1f22`, white 14px/600 text, 28px amber circle `#f6c46b` with "R".
- **"Ask Raqeeb why"** on the approval card should open the chat with that question pre-asked (append to history in session state).

## Screens
### Nav order
Plant (default landing page, the live view), Summary, Trace, Agent, Report; then For judges: Answer key, Results. Plant page order: Sun and wind (top), Plant output chart, Plant map + detail.

### Summary
1. Eyebrow "What happened" 13px/600 `#8a4b00`.
2. H1 34px/700, −0.02em, max 820px. It tells the trap in one sentence (mock: "The plant looked healthy, but it was losing a fifth of its energy.").
3. Lead paragraph 17px/1.55 `#3d3f43`, max 760px: the four faults, the dusty-sensor trap, what was fixed, what waits for the user.
4. Four stat cards (grid 4 cols, gap 12): label 13px grey · value 28px/600 tabular · state line 12px/600 with 8px dot + **word** (ok/warn/bad colours below). Stats: Energy lost to dust · Faults found · Fixed automatically · Waiting for you.
5. "The story in four steps" + link "See every step in Trace →"; 4 cards: mono time 12px, title 15px/600, body 14px/1.5.
6. **Approval card (the only place the approval is presented)**: border 1.5px `#c9822a`, radius 14, pad 22. Status pill ("Waiting for you" / "Approved" / "Postponed"), "Step 3 of 4 · risky action", H2 question 20px/600, one paragraph: what, why, cost, and that Raqeeb will verify. Buttons: Approve (dark), Not now (outline), Ask Raqeeb why (text link). Remove the other two places the approval is repeated; the header pill and the Report button only jump here.

### Trace
H1 "How Raqeeb worked it out" + one-line intro (normal body text, not disclaimer styling). Cards only, full width (no Plant duplicate beside them): grid `90px | 1fr`; mono time; title 16px/600; sentence 15px/1.55; `st.expander("Show data")` with the raw record. Remove the side-by-side map column.

### Plant
- **Output chart** (Plotly is fine here): three lines with legend **written as sentences under the chart**, not the Plotly legend:
  - Solid black 3px: "Actual output. What the plant really produced."
  - Dotted amber `#c9822a`: "Expected, from the plant's sun sensor. The sensor is dusty too, so this line follows the actual one and hides the loss."
  - Dashed green `#1f6b3a`: "Expected, from satellite weather. Not affected by dust. The gap to this line is the real loss."
  - Shade the gap region `#fbe7e5` and annotate "21 % lost to dust" (18px, `#9a2420`). The x-range follows playback time.
- **Sun and wind strip** (Plotly OK, light theme): sun height as amber filled area, on-site wind as black line, red dashed "Stow limit 15 m/s" line with label, vertical playback cursor. Under it, one sentence legend: "Sun height NN° · Wind on site N.N m/s · Above the dashed line, panels must be turned flat to protect them." Caption: "Real weather record".
- **Plant map (MUST exist; it was lost to a bug, restore it)**: card titled "Plant map". First a "Plant-wide" line (e.g. "No plant-wide action needed. Wind stays below the stow limit."). Then 4 rows labelled "Row A"…"Row D" (54px label column), 4 inverter tiles each, in site order. Below a divider, site equipment tiles with dashed border: Sun sensor ("Reads 21 % low" · Dusty, not trusted), Weather station, Grid connection. If the old map has a broken empty Plotly box, replace it with this HTML block.
- **Inverter tiles: recommendation is HTML, not Plotly.** One `st.markdown` block, CSS grid `repeat(auto-fill,minmax(110px,1fr))`, gap 8. Tile anatomy: name 14px/600 ("Inverter 7") · output % of expected 20px/600 tabular · state dot + word 12px/600. Radius 10, pad 10/11, min-height 86. Selection: `st.selectbox`/`st.radio` (existing key) beside the grid drives the detail card (selected tile gets a 2px dark border and `#faf3e6` background). Plotly tile text can't reach readable sizes at 1366 px; HTML can.
- **Detail card** (right column, ratio 1.5 : 1): name 22px, state with word, quoted note, `dl` of Output / Temperature / Data link, "Show data" expander.

### Agent
Table (HTML or `st.dataframe` with column config): Time (mono) · Action (bold) + why (13px grey) · Who decided ("Raqeeb, automatically" / "Needs a person") · Result (dot + word). No raw ids.

### Report
Letter-style card max 760px, pad 32/36: "To: plant manager · Shift ending … · synthetic plant", H2 summary line, 2 short paragraphs, button "Review the approval" → Summary approval card. Strip raw ids from the email banner.

### Answer key / Results
Table: Fault we injected · What Raqeeb concluded · Match (dot + "Correct"/"Missed"). Results can add the score in plain words.

## One number and state style (use everywhere)
- Numbers: IBM Plex Sans, `font-variant-numeric: tabular-nums`, space before unit (`21 %`, `78 °C`), times in IBM Plex Mono `HH:MM`.
- States are always dot + word, never colour alone:
  - ok: text `#1f6b3a`, dot `#2e9b56` (Normal, Confirmed, Recovered, Approved)
  - warn: text `#7a4300`, dot `#c9822a` (Dusty, Link restored, Waiting for you)
  - bad: text `#9a2420`, dot `#d0463f` (Overheating, Hidden loss)

## Visual direction: "desert noon"
- Background sand `#f6f2ea`; sidebar `#efe9dc` with a faint solar-cell grid (two 1px linear-gradients, rgba(27,47,74,.045), 22px).
- Primary = panel navy `#1b2f4a` (primary buttons, selected nav icon, chat bubble/FAB, actual-output line, wind line). Body text stays ink `#1d1f22`.
- Accent = sun amber `#e8a33d`, used sparingly: logo sun, playback progress bar, "now" cursor on charts (2px), 2px underline of the sticky header, FAB badge.
- Logo: 32px navy rounded square; amber sun disc half-risen behind a horizon, three 2px pale-blue (`#8fa6c4`) lines below = panel rows. Built from divs/simple shapes.
- Sun chart fill: horizontal gradient pale → `#f7dca6` at noon → pale.
- Status colours unchanged (always with words). No photos, no decorative gradients elsewhere.
- Where tokens below say `#1d1f22` for buttons/fills or `#f6f5f2`/`#eeece7`/`#f6c46b`, use the values above instead.

## Design tokens
- Background `#f6f5f2`; sidebar `#eeece7`; cards `#ffffff`; subtle fill `#faf9f6` / `#f1efea`
- Borders `#dedad2` (cards), `#ece9e3` (rows), `#c9c5bc` (outline buttons)
- Ink `#1d1f22`; body `#3d3f43`; secondary `#5d5f63`; group label `#6b6d71`
- Accent text/link `#8a4b00` (hover `#5c3200`); approval amber `#c9822a`, tint `#fdf1de`; brand amber `#f6c46b`
- Fonts: IBM Plex Sans 400/500/600/700, IBM Plex Mono 400/500 (Google Fonts `@import` in the global CSS block)
- Type: H1 34 (Summary) / 26 (other pages); H2 18–22; body 15–17; meta 12–13
- Radius: 8 (buttons, nav rows), 10 (tiles, inputs), 12 (cards), 14 (major panels), 16 (chat), 999 (pills)
- Spacing: page pad 32, section gap 28, card gap 12–16, card pad 16–22

## State (session_state)
`nav` (current page) · playback time + playing flag (existing keys) · `approval` ("Waiting for you"/"Approved"/"Postponed") · selected inverter (existing key) · `chat_history` list of {role, text}.

## Files
- `Raqeeb Console.dc.html`: interactive design reference (open in browser with `support.js` alongside)
