# Raqeeb console: component inventory (for the designer)

Branch `phase4/ux` at 49c3fbe, 2026-10-31. Screenshots in `..\raqeeb-ux-screenshots\inventory\` (1366 px wide,
Microsoft Edge). Moments: the fin_0029 replay at 07:15 (quiet start) and 10:00 (the storm's worst moment: dusty
panels, dusty sun sensor, one inverter overheating), its end (shift report, answer key), three test-model replays
for the tile states fin_0029 never shows, and a live rules-only run of scn_0007 paused at its first approval.
Full-page screenshots of every tab at 07:15 and 10:00 are `16`-`23` and `50`-`57`.

Every component follows the same rules: words beside every colour, sentence case, identifiers translated by
`raqeeb/ui/plain.py`, the raw record one click away behind "Show data", free text from the plant (alarm messages,
notes, model replies) shown quoted and never rendered as markdown. "Agent output" below means the run's record
(`raqeeb/ui/runlog.py` `RunLog`: polls, actions, verifications, escalations, proposals), identical for a live run
and a replay. "Telemetry" means the run's SQLite store (`inverter_readings`, `weather_station`,
`weather_reference`, `meter`, `grid_setpoint`, `events`, `technician_notes`, `audit`).

Semantic states used across the console (text colour on fill, each pair at least 4.5:1):

| State | Word shown | Ink | Fill |
|---|---|---|---|
| healthy / recovered / done | "Recovered", "Fixed and verified", "normal" | `#116b3f` | `#def3e6` |
| attention / finding | "found something", "Fix under way", "capped" | `#8a4b00` | `#fff0cc` |
| critical / escalated | "Handed to a human", "rejected", "not recovered" | `#a8251c` | `#fde2df` |
| no data | "no data", "Not enough data yet" | `#4a5563` | `#e9edf2` (hatched tile) |
| needs a human | "Needs your decision", "Awaiting your approval" | `#5b3aa6` | `#ece5fa` |
| info | "Found, fix not started", counts at zero | `#1d5fb4` | `#dfe9f8` |

## 1. Header and mode banner

- Shows: who the plant is, how the console is running, the simulated time and which model (if any) is in use.
- Data: plant name from `.env` (`SITE_NAME`, via `load_plant().site.name`); mode and model from the run's log
  header (`RunLog.mode`, `RunLog.model`); clock from `run.now`.
- States: one. Mode wording: "Replay with the local model", "Replay, rules only", "Replay with the scripted test
  model", "Live, rules only", "Live with the local model". Model: the model file name, or "none (rules only)".
- Wording: "Raqeeb  plant recovery console"; "Plant Riyadh (synthetic)"; "Mode …"; "Simulated clock 10:00
  Asia/Riyadh, 2025-12-21"; "Model …". Caption below: "Mode: …. Replay run of incident fin_0029: <record label>.
  Synthetic plant and faults; weather data from NASA POWER (2025, Riyadh)."
- Built from: one `st.markdown` HTML block (`div.rq-header`, CSS in `app.py` `CSS`) and one `st.caption`.
- Code: `raqeeb/ui/app.py` `header()`, `mode_words()`.
- Screenshot: `01_header_and_mode_banner_replay_local_model.png`.

## 2. Notification strip

- Shows: the latest thing that happened, in one sentence with its time; how many items are open; whether on-call
  was emailed.
- Data: agent output (`summary.key_moments()` for the latest event, `run.waiting` for approvals,
  `escalations_until`, `verifications_until` for recovered); alert status from the console's `Alerter` records
  (live runs only).
- States and words: left rule blue "info" (quiet: "Nothing has happened yet. Press Play, or Step for one agent
  turn."); green "healthy" once something recovered; red "critical" when something was handed to a human and
  nothing has recovered yet; violet "needs a human" while an action waits, with a pulsing violet dot (still under
  `prefers-reduced-motion`) and the chip "Needs your decision: N" plus the button "Needs your decision: open the
  approval card", which scrolls to the card. Chips always present: "Awaiting your approval: N", "Handed to a
  human: N", "Recovered: N" (grey "no data" when zero, their state colour otherwise). Alert chip: "Recorded run:
  no email alerts" (replay), "Email alerts off in this deployment", "Email alerts not configured", "Email alerts
  ready", "Emailing on-call", "Email sent to on-call", "Email to on-call failed". New events also raise
  `st.toast` with the same sentence.
- Built from: one `st.markdown` HTML block (`div.rq-strip`, chips `span.rq-chip`), one `st.button`.
- Code: `app.py` `notification_strip()`, `alert_status()`, `chip()`; sentences from `summary.Moment.said`.
- Screenshots: `02_notification_strip_quiet.png`, `24_notification_strip_event_just_happened.png`,
  `58_notification_strip_end_recovered.png`, `72_notification_strip_needs_your_decision_and_email_not_configured.png`,
  `78_notification_strip_live_after_approval.png`. "Email sent" was not captured: it needs a real email to
  on-call (see the report).

## 3. Alert banner (email)

- Shows: for a live run, what the console did about emailing the operator for the action waiting now.
- Data: `LiveRun.alerts` (console records, not agent output).
- States: info "Alert (email not configured): approve or reject set_power_limit on INV-04. Set RESEND_API_KEY,
  RAQEEB_ALERT_FROM and RAQEEB_ALERT_TO in .env to email the operator."; success "Alert emailed to …"; warning
  "Alert email failed …"; info "Alert being emailed …". Absent in replays. (The decision text here is still the
  agent's raw wording; see "Not yet in words" at the end.)
- Built from: `st.info` / `st.success` / `st.warning`.
- Code: `app.py` `alerts_banner()`; `raqeeb/ui/alerts.py`.
- Screenshot: `73_alert_banner_email_not_configured.png`.

## 4. Plant map

- Shows: the 16 inverters in their four transformer blocks, the grid connection, the substation meter and the sun
  sensor, each coloured by its state with the word on the tile, and the agent's actions as small marks under a tile.
- Data: telemetry (`inverter_readings` at the latest step, `weather_station`, `weather_reference`, `meter`,
  `grid_setpoint`) and agent output for the marks (actions, escalations, accepted proposals). Never the scenario.
- Inverter tile states (icon + word + fill/ink from `plantmap.MAP_STYLE`): ● normal (`#dcefe2`/`#14532d`),
  ▼ low output (`#fbdcc0`/`#7c2d12`: stopped or at least 10% below the others), ▬ capped (`#e6ddf6`/`#3b0764`: the
  inverter reports a derate or power limit), ∅ no data (`#f2f1ee`/`#3d3a35`, hatched: no reading arrived, never
  zero), ⇩ curtailed (`#d8e6fa`/`#1e3a8a`), ⏸ awaiting approval (`#fff1c2`/`#713f12`, dashed amber border),
  ↺ recovered (`#cfeeed`/`#134e4a`). Sun sensor: normal, or ⚑ flagged (`#fbdcc0`/`#7c2d12`: the agent stopped
  trusting it). Meter: normal or low output. Grid: normal ("no export limit") or curtailed ("export limit 30.0 MW",
  "grid curtailment").
- Wording on a tile: id, icon and state, "2.10 MW", "101% of peer median", "tracker stowed (wind)"; marks:
  "◆ technician sent", "◆ cleaning ordered", "◆ awaiting approval: power limit set", "◆ transformer trip proposed →
  accepted → escalated". Caption under the map explains each state in one sentence.
- Built from: one Plotly figure (`st.plotly_chart`, click selects a tile) drawn with shapes and annotations; the
  sky strip above is a second Plotly figure; the per-inverter chart below is a third.
- Code: `raqeeb/ui/plantmap.py` `map_view()`, `map_figure()`, `sky_strip()`; `app.py` `plant_map()`, `sky_chart()`,
  `inverter_detail()`.
- Screenshots: whole map `09_plant_map_0715_quiet.png`, `31_plant_map_1000_storm.png`; sky strip `08_sky_strip_0715.png`,
  `30_sky_strip_1000_storm.png`; block `32_map_block_T1.png`, `64_map_block_T4_low_output.png`; tiles
  `33_tile_inverter_normal.png`, `63_tile_inverter_low_output.png`, `34_tile_inverter_capped.png`,
  `69_tile_inverter_no_data.png`, `66_tile_inverter_curtailed.png`, `76_tile_inverter_awaiting_approval.png`,
  `60_tile_inverter_recovered.png`; meter `35_tile_meter_normal.png`; sun sensor `10_tile_sun_sensor_normal.png`,
  `36_tile_sun_sensor_flagged.png`; grid `37_tile_grid_normal.png`, `67_tile_grid_curtailed.png`.

## 5. Output chart (recovery)

- Shows: what the plant produced against what the weather says it should have, two ways: from the trusted weather
  reference and from the on-site sun sensor. When the sensor is dusty its line drops with the plant and the loss
  looks normal; the reference line still shows it. Vertical lines are applied actions.
- Data: telemetry `meter`, `weather_reference`, `weather_station`; expected lines computed by
  `raqeeb.tools.physics.expected_plant_ac` (the same code the agent uses); action lines from agent output.
- Series (fixed order): "Actual output (meter)" blue `#1d5fb4`, "Expected from the weather reference" rust
  `#c2571a`, "Expected from the on-site sun sensor" teal `#117a65` dotted. A second figure shows the sunlight
  itself (sensor, reference, clear-sky shape).
- Built from: two Plotly figures (`st.plotly_chart`), a caption each.
- Code: `app.py` `recovery_figure()`, `recovery_chart()`; `source.power_lines()`.
- Screenshots: `38_output_chart_1000_storm.png`, `39_sunlight_chart_1000_storm.png`.

## 6. Headline numbers

- Shows: plant output now and how far from expected, alarms so far, inverters reporting, actions waiting for you.
- Data: telemetry (`meter`, `events`, `inverter_readings`) and `run.waiting`.
- Wording: "Plant output now 32.9 MW, -5% against expected" (red arrow; "on expected" grey when within 1%;
  "no reading yet" at night), "Alarms so far 18, +1 in the last 30 min", "Inverters reporting 16 of 16",
  "Waiting for your approval 0".
- Built from: `st.columns(4)` of `st.metric`.
- Code: `app.py` `headline()`.
- Screenshots: `03_headline_numbers_0715.png`, `25_headline_numbers_1000_storm.png`.

## 7. Tabs row

- Shows: the eight views. Order: Summary, Trace, Plant, Agent, Report, Answer key, Results, Ask Raqeeb; the console
  opens on Summary. Each tab starts with a two-line intro (`app.py` `INTRO`).
- Built from: `st.tabs`.
- Code: `app.py` `_page()`.
- Screenshot: `04_tabs_row_summary_selected.png`.

## 8. Summary tab: explanation and "Play the storm"

- Shows: three lines on what Raqeeb does and one button that plays the loaded run; shown while the run is not
  playing and has not ended (afterwards: "This run has ended. Use the clock in the Trace tab…").
- Wording: "Raqeeb watches a solar plant that has several things going wrong at once. / It works out the causes from
  the plant's data alone, fixes what is safe to fix, asks you before anything risky, and checks that output came
  back. / Press Play the storm to watch it happen; every step is explained in the Trace tab."
- Built from: `st.markdown`, `st.button` (primary).
- Code: `app.py` `first_time_path()`, `_play_storm()`.
- Screenshot: `05_summary_explanation_and_play_0715.png`.

## 9. Summary tab: key moments (timeline)

- Shows: every moment that mattered, as a sentence with its time and a "Turn N" button that opens that step in the
  Trace tab; above it a dot-per-moment chart with one lane per kind (first symptom, cause, model proposal, action,
  approval, verification, escalation), click a dot to jump.
- Data: agent output (`summary.key_moments()`), sentences from `plain.py`.
- States: an outcome chip after the sentence: "confirmed" (info blue), "accepted"/"recovered"/"approved" (green),
  "rejected"/"not recovered"/"never sent" (red), "escalated"/"waiting for a decision" (violet).
- Wording examples: "08:15 First sign: High wind: Sun sensor."; "09:00 Found dust on the panels (all panels), by the
  rules."; "09:00 Ordered cleaning of all panels automatically."; "10:15 Needs your approval: lower Inverter 3's
  output limit to 1.6 MW."; "11:00 The simulated operator approved: …"; "11:45 Verified: removed Inverter 3's
  output limit and output recovered (1.6 MW -> 2.0 MW)."; empty: "Nothing to report yet: the first sign, cause,
  action or model proposal will appear here."
- Built from: Plotly figure (`st.plotly_chart` with selection) + `st.container(height=…)` of rows
  (`st.columns([7, 1])`: markdown sentence, `st.button`) + "Show data" `st.expander` with the raw `st.dataframe`.
- Code: `app.py` `summary_tab()`, `timeline_figure()`; `raqeeb/ui/summary.py` `key_moments()`.
- Screenshots: `07_summary_timeline_0715_empty.png`, `27_summary_timeline_1000_moments.png`,
  `28_summary_timeline_1000_chart.png`.

## 10. Summary tab: causes table

- Shows: each cause the agent named, how it was found, its status, when it was first named, what was done, and
  whether output came back; one "Turn N: <cause>" button per cause.
- Data: agent output (`summary.cause_rows()`, `trace.cause_statuses()`, proposals for the origin).
- Columns and words: Cause ("Dust on the panels (all panels)"); How it was found ("Found by the rules" / "Proposed
  by the model, confirmed by N checks" / "…rejected: <reason>"; never a score); Status ("Found, fix not started",
  "Fix under way", "Fixed and verified", "Handed to a human", "Expected behaviour, no action", "Flagged for security
  review"); First named ("turn 12"); Actions ("Order cleaning of all panels (done)"); Verified ("recovered" /
  "not recovered" / "not yet"). Empty: "No cause named yet. …"
- Built from: `st.dataframe` (index hidden, no selection), `st.columns` of `st.button`, "Show data" expander with
  the raw labels and scores.
- Code: `app.py` `summary_tab()`, `_plain_actions()`.
- Screenshots: `79_summary_causes_table_1000.png`, `85_summary_causes_table_end.png`.

## 11. Summary tab: energy numbers

- Shows: how many agent steps have run, causes found (fixed / to a human), energy below expected in MWh split at
  the first fix, the shortfall in the worst hour and the last hour, model calls and time.
- Data: agent output and telemetry (`summary.energy()` from the power lines).
- Wording: "Agent steps 16 of 48"; "Causes found 3, 0 fixed, 0 to a human"; "Energy below expected 9.74 MWh,
  9.74 before first fix, 0.00 after"; "Shortfall, worst / last hour 27% / 25%"; "Model calls 0, rules only".
  Caption explains that the shortfall is not a proven loss (the reference carries a few percent of error).
- Built from: `st.columns(5)` of `st.metric`, `st.caption`.
- Code: `app.py` `summary_tab()`; `summary.energy()`.
- Screenshots: `06_summary_energy_numbers_0715.png`, `26_summary_energy_numbers_1000.png`.

## 12. Summary tab and Report tab: bilingual shift report

- Shows: the agent's end-of-run report, English left and Arabic right (right-to-left); the Report tab adds a
  Markdown download and the source.
- Data: agent output (`IncidentReport.summary_en`, `summary_ar`, written at the final poll; by the local model when
  one runs, else a template).
- Wording before the end: "The agent writes the shift report at the end of the run."
- Built from: `st.columns(2)` of `st.markdown` (Arabic in a `dir="rtl"` div, text HTML-escaped), `st.download_button`,
  `st.expander` + `st.code`.
- Code: `app.py` `report_columns()`, `report_tab()`, `report_markdown()`.
- Screenshots: `29_summary_report_1000_not_yet.png`, `84_summary_bilingual_report_end.png`, `61_report_tab_end.png`.

## 13. Trace tab: intro lines and clock

- Shows: "This is the agent's log. Each card is one 15-minute step: what it checked, what it found, what it decided,
  and what happened next." Below it the file loader (collapsed) and, once turns exist, the clock slider "Clock
  (scrub through the turns)" with labels "turn 16, 10:00" (only the turns played so far).
- Built from: `st.caption`, `st.expander`, `st.select_slider`.
- Code: `app.py` `trace_tab()`, `trace_loader()`, `_trace_body()`.
- Screenshots: `11_trace_intro_lines.png`, `13_trace_clock_0715.png`, `40_trace_clock_1000.png`.

## 14. Trace tab: header strip

- Shows: simulated time, "Turn 16 of 48", causes so far as chips by status with their names beneath, model calls
  and time in the model.
- Data: agent output (`trace.header()`, `trace.cause_statuses()`).
- Chips: "Fix under way: 3" (attention), "Fixed and verified: 3" (healthy), "Handed to a human: 1" (needs a
  human), "Found, fix not started" (info), "Expected behaviour, no action" (no data), "Flagged for security
  review" (critical). "None named yet." when empty.
- Built from: `st.columns([1,1,3,1,1])` of `st.metric`, markdown chips, `st.caption`.
- Code: `app.py` `trace_header()`.
- Screenshot: inside `51_fullpage_trace_1000.png` (top of the Trace tab).

## 15. Trace tab: quiet folded turns

- Shows: turns where nothing changed, folded into one line: "Turns 2-4 (06:30-07:00): nothing new."
- Data: agent output (`trace.TurnCard.quiet`).
- Built from: `st.caption` inside the scrolling `st.container(height=900)`.
- Code: `app.py` `trace_stream()`; `raqeeb/ui/trace.py` `turn_cards()`.
- Screenshot: `12_trace_quiet_turn_folded.png`.

## 16. Trace tab: turn card

- Shows, top to bottom: title "Turn 12, 09:00" (violet highlight when a Summary jump points at it; "(final step)"
  on the last); the headline sentence "Ran 15 checks; 3 found something: high wind (trackers stowed), plant output
  below expected and …; 17 alarms so far. Named: dust on the panels."; what the plant reported back ("The plant
  reports: ordered cleaning of all panels done."); "New signs: Running hot: Inverter 3."; only the checks that found
  something, each with an amber left rule: "Checked the sun sensor against the reference: Sun sensor reads 28%
  below the reference on a clear sky since 09:25; …"; the quiet line "11 checks found nothing and 1 had too
  little data yet."; causes named ("Found dust on the panels (all panels), by the rules."); the model ("Asked the
  model for possible causes (2 calls, 14.2 s).", proposals with an Accepted / Rejected / Dropped later chip);
  actions with the gate's word ("Ordered cleaning of all panels automatically.", "Needs your approval: lower
  Inverter 3's output limit to 1.6 MW. The simulated operator approved: …", "Held back by the safety gate: … Never
  sent."); verifications ("Verified: … and output recovered (7.2 MW -> 31.5 MW, 3% below expected)."); hand-overs
  ("Handed to a human: transformer trip (Transformer T4 (inverters 13-16)): no automatic fix for this fault.");
  collapsed expanders "Alarms this turn (17): messages quoted, untrusted", "Notes (N): quoted, untrusted", and
  "Show data" with every raw table of the old card (checks with FINDING / OK / INCONCLUSIVE colours, symptoms,
  candidates with scores and "✓ selected", the gate table with "auto (allowlisted)" / "needs approval" / "held
  back: …", model calls, proposal checks, the model's words quoted, transcripts as plain code blocks,
  verifications in kW, escalations, "What the agent did (N steps)").
- Data: agent output (`trace.turn_cards()`), telemetry `events` for the alarms expander.
- The "N checks found nothing" line is a single line, not a collapsible: the checks themselves are under "Show data".
- Built from: `st.container(border=True, key="trace_card_N")`; `st.markdown` (HTML blocks `p.rq-headline`,
  `div.rq-found`, `p.rq-quiet`; markdown bullets for sentences); `st.expander`; `st.dataframe` (Pandas Styler for
  the verdict colours); `st.code` for transcripts; an inline approval card (section 17) while an action waits.
- Code: `app.py` `trace_card()`, `trace_model()`, `trace_actions()`, `trace_show_data()`, `_turn_alarms()`;
  `trace.py` `turn_cards()`; sentences from `plain.py` `checks_headline()`, `verdict()`, `found()`, `acted()`,
  `approval_needed()`, `decided()`, `verified()`, `escalated()`, `proposal()`, `outcome()`.
- Screenshots: `14_trace_card_0715_nothing_found.png`, `41_trace_card_1000_full_collapsed.png`,
  `42_trace_card_headline_turn12.png`, `43_trace_card_check_found_something_turn12.png`,
  `44_trace_card_checks_found_nothing_line.png`, `45_trace_card_1000_show_data_expanded.png`,
  `46_trace_card_1000_turn13_decisions_and_outcomes.png`.

## 17. Approval card

- Shows: one action that needs a person, in words, with why, what to expect, when it was asked and whether it is
  reversible; Approve and Reject with an optional note while it waits; the recorded decision afterwards.
- Data: agent output (`ActionRequest` rationale and expected effect, passed through `plain.humanize()`), the
  decision from `ActionEntry`.
- Wording: "Needs your approval: lower Inverter 4's output limit to 1.8 MW."; "Why: Inverter overheating and holding
  back (Inverter 4): 'Power derating due to temperature' alarm on Inverter 4; Inverter 4 up to 71 C; …"; "What to
  expect: …"; "Asked at 11:15. Reversible: yes."; "Your note (optional)"; buttons "Approve" (primary) and "Reject";
  after: "You approved: lower Inverter 4's output limit to 1.8 MW (fan replaced). (11:15)" / "The simulated
  operator approved: …" in replays, prefixed "Recorded decision:".
- Where: above the tabs (container `approval_card`, with the warning "The agent asked for an action that needs your
  approval. The run is paused until you decide.") while waiting; in the Agent tab's Approvals section otherwise;
  inline on the Trace card of the turn that asked.
- Built from: `st.container(border=True)`, `st.markdown`, `st.text_input`, `st.columns(2)` of `st.button`, "Show
  data" expander.
- Code: `app.py` `approval_card()`, `approvals()`, `_decide()`, `_trace_decide()`.
- Screenshots: `74_approval_card_waiting_approve_reject.png`, `89_approval_card_after_approval_recorded.png` (the recorded decision in the fin_0029 replay; the live-run version of the same card is in `78_notification_strip_live_after_approval.png`'s run but was not captured as a crop).

## 18. Notes panel

- Shows: technician notes and operator approval notes, quoted as written, newest first.
- Data: telemetry `technician_notes` and the operator's notes on decisions (agent output `ActionEntry.note`).
- Columns: Time, Source ("Technician" / "Operator"), Author, About (device in words), Text (quoted). Empty: "No notes
  so far. Notes from site staff and your approval notes would appear here." Caption: "Quoted as written (synthetic,
  untrusted data). The agent's rules never read note text; technician notes reach a model only as delimited data."
- Built from: `st.subheader`, `st.caption`, `st.dataframe`.
- Code: `app.py` `notes_panel()`.
- Screenshots: `81_notes_panel_1000.png`, `88_notes_panel_with_notes.png`.

## 19. Model proposals panel

- Shows: each cause a language model proposed and what the agent's own test made of it; a rejected proposal
  changes nothing.
- Data: agent output (`ProposalRecord`, from the agent's consult lines; `raqeeb/ui/proposals.py`).
- States: chip "Accepted" (green), "Rejected" (red), "Dropped later" (orange). Wording: "The model proposed
  transformer trip (Transformer T3 (inverters 9-12)); confirmed by 5 checks." then "Added TRANSFORMER_TRIP@T3 to
  the diagnosis (explains 4 symptoms). Then: handed to a human (…)."; "The model proposed transformer trip
  (Transformer T1 …); rejected: footprint does not match …. Nothing changed." then "Changed nothing: no cause
  added, no action requested, diagnosis unchanged."; caption "Decided at 11:15. Model call took 9.4 s (<model>)."
  Empty: "No proposals so far: the model was not asked (or this run is rules only)."
- Built from: `st.container(border=True)` per proposal, `st.markdown`, `st.caption`, "Show data" expander with the
  checks table (Check / Result / Pass: pass, FAIL, context) and the model's words quoted.
- Code: `app.py` `proposals_panel()`; `proposals.py` `effect()`.
- Screenshot: `87_model_proposals_accepted_and_rejected.png` (pru_0001: one accepted, one rejected).

## 20. Answer key panel

- Shows: nothing while the run is in progress; after the end, behind a switch, the planted faults against what the
  agent named and did.
- Data: the scenario's answer key, copied into the record only when the run ends (`RunLog.answer_key`); never seen
  by the agent.
- Wording: hidden: "Hidden while the run is in progress. The agent never sees it; it appears here once the run
  ends."; ended: "The run has ended. Turn on the switch to compare the agent with the planted faults."; revealed:
  "The agent named 3 of 3 planted root causes." then two tables: What was planted (words), Planted fault (label),
  From, Kind ("root cause", "decoy (expected behaviour)", "decoy (injected instruction)"), Agent named it (yes/no),
  Agent actions on it; and Actions against the answer key (Time, Action in words, Target, Status "done" /
  "refused" / "failed", Addresses).
- Built from: `st.caption`, `st.toggle`, `st.markdown`, two `st.dataframe`.
- Code: `app.py` `answer_key_tab()`; `source.reveal()`.
- Screenshots: `15_answer_key_hidden.png`, `86_answer_key_revealed.png`.

## 21. Results tab

- Shows: the benchmark table (one row per agent) and a bar chart of energy recovered compared with doing nothing;
  grey bars are reference bounds, not competitors.
- Data: bench files (`bench/final/summary.csv`, `results.csv`, `summary.md`; folder picker and path box).
- Agent names in words: "Raqeeb, rules only", "Raqeeb, rules + local model", "Threshold baseline", "Oracle (reads
  the answer key)", "Does nothing". Caption defines precision, recall, false actions, verified recovery. Empty:
  "Results not available yet at <name>. …"
- Built from: `st.selectbox`, `st.text_input`, `st.dataframe`, Plotly bar chart, two `st.expander`.
- Code: `app.py` `results_tab()`; `source.load_results()`.
- Screenshot: `48_results_tab_final_set.png`.

## 22. Ask Raqeeb

- Shows: a chat about the loaded run; the answer is looked up in the record by code, the facts it used are listed
  under "Based on:", and a reachable local model only rewords them. It cannot take, approve or reject an action.
- Data: agent output and the power lines (`raqeeb/ui/ask.py` lookups).
- Wording: caption with example questions; "No question yet. …"; under an answer "No model reachable: the facts are
  shown as retrieved." or "Worded by <model> in 2.1 s from the facts below, nothing else."; expander "Based on:".
- Built from: `st.chat_input`, `st.chat_message`, `st.markdown`, `st.caption`, `st.expander`.
- Code: `app.py` `ask_tab()`, `_ask_gateway()`; `ask.py`.
- Screenshot: `49_ask_raqeeb_question_and_answer.png`.

## 23. Sidebar: mode, model selector, scenario, playback

- Shows: Mode radio ("Live: rules only" / "Live: rules + local model" / "Replay") with a caption each; Model
  selectbox ("Rules only" or the local server's models) and the disabled "API tier (not configured)" button; the
  scenario or recorded-run pickers; Playback: speed slider "Speed (simulated minutes per tick)" (15 to 240), Play /
  Step / To end, and the status "Paused: an action needs your decision." (warning) or "Run finished. The answer
  key can be revealed." (success); "Save recording (replay JSON)" after a live run.
- Built from: `st.radio`, `st.selectbox`, `st.button`, `st.file_uploader`, `st.select_slider`, `st.columns(3)`,
  `st.download_button`.
- Code: `app.py` `sidebar()`.
- Screenshots: `70_model_selector_rules_only_offline.png`, `71_speed_and_playback_controls_sidebar.png`; Trace
  tab live controls (Run / Pause, pace, Jump to end, status) `77_trace_live_controls_paused_waiting.png`.

## 24. Agent tab tables

- Agent timeline: every agent step newest first (Time, Step in words such as "Ran the checks", What), then "What the
  checks find now" as sentences and "Current diagnosis" with how each cause was found; raw lines behind Show data
  (`82_agent_timeline_table_1000.png`). Actions and verifications: Time, Action in words, For (cause), Approval,
  Outcome, Takes effect, Verified with the power change (`83_actions_and_verifications_1000.png`). LLM calls and
  time: four metrics and the call table (`47_llm_calls_panel_1000.png`). Alarm feed on the Plant tab: Time, Device
  in words, Alarm (title from `configs/alarm_codes.yaml`), Severity, Message (quoted) (`80_alarm_feed_1000.png`).
- Code: `app.py` `agent_timeline()`, `actions_panel()`, `llm_panel()`, `alarm_feed()`.

## Page layout (1366 px)

```
+------------------+---------------------------------------------------------------------------+
| Sidebar          | Raqeeb  plant recovery console   Plant · Mode · Simulated clock · Model   |  header (1)
| Run              | caption: mode, incident, record label, synthetic notice                    |
|  Mode radio      | ▌ 10:00 Stopped trusting the sun sensor…  [Awaiting 0][Handed 0][Recov 0] |  strip (2)
|  Model select    | [Needs your decision: open the approval card]   (only while waiting)      |
|  Scenario /      | [warning + approval card]                       (only while waiting)      |  (17)
|  Recorded run    | Plant output now | Alarms so far | Inverters reporting | Waiting for you  |  numbers (6)
| Playback         | Summary | Trace | Plant | Agent | Report | Answer key | Results | Ask      |  tabs (7)
|  Speed slider    | two-line intro                                                            |
|  Play Step End   | Summary: explanation + Play the storm (8), 5 metrics (11), key moments    |
|  status          |   chart + list (9), causes table + Turn buttons (10), shift report (12)   |
|                  | Trace: intro (13), loader, clock, header strip (14);                      |
|                  |   left: compact map + output chart | right: Auto-scroll, cards (15, 16)   |
|                  | Plant: sky strip, map (4), inverter vs peers, alarm feed, charts (5),     |
|                  |   notes (18)                                                              |
|                  | Agent: timeline | approvals (17); proposals (19); actions; LLM calls (24) |
|                  | footer: synthetic notice, NASA credit                                     |
+------------------+---------------------------------------------------------------------------+
```

## Tokens (from docs/CONSOLE_DESIGN.md)

| Token | Value | Use |
|---|---|---|
| page | `#f2f5f9` | page background |
| surface | `#ffffff` | cards, sidebar, tables |
| ink | `#13293f` | text, headings |
| muted | `#51657a` | captions, secondary labels |
| accent | `#1d5fb4` | links, primary buttons, "actual output" series |
| line | `#d6dee8` | borders |
| series | blue `#1d5fb4`, rust `#c2571a`, teal `#117a65` | actual, reference, sensor |
| type | one sans-serif family; title 1.5 rem/600, section 1.25 rem/600, card headline 1.05 rem/600, body 1 rem, caption 0.85 rem | sentence case, no all-caps |
| radius | 0.3 rem | widgets and cards |
| motion | one pulsing dot on the strip while a decision is needed; none under reduced motion | |

Semantic states: see the table at the top. Where they live: `.streamlit/config.toml` `[theme]`, `app.py` `TOKENS`,
`SEMANTIC`, `SERIES`, `CSS`; `plantmap.py` `MAP_STYLE` for the tiles.

## Not yet in words (left as found)

- The alert banner's "approve or reject set_power_limit on INV-04" and the email body use the agent's raw action
  and device ids (`raqeeb/ui/alerts.py`; the email text is lane C's).
- The Agent timeline's "What" column is the agent's own step line with identifiers replaced, not a full sentence.
- The Trace card's "N checks found nothing" is a line, not a collapsible.
