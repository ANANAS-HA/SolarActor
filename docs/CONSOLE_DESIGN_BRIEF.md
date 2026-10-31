# Design brief: the Raqeeb console

Read this first, then `CONSOLE_COMPONENTS.md` (one section per component, with screenshot names) and the
screenshots. The source is in `code/`; `code/README.md` says which file controls what.

## What the software is

Raqeeb is an AI agent that looks after a solar power plant when several things go wrong at once. A desert
plant has 16 inverters (the boxes that turn panel power into grid power), a sun sensor, a substation meter and a
grid connection. A dust storm can cover the panels, cover the sun sensor, overheat an inverter and cut an
inverter's data link, all in the same morning. The usual plant health check compares output with what the sun
sensor says; when the sensor itself is dusty, that check looks normal while the plant quietly loses a fifth of
its energy. Raqeeb works out the real causes from the plant's data alone, fixes what is safe to fix (order
cleaning, stop trusting the sensor, reset a data link, send a technician), asks a person before anything risky
(lowering an inverter's output limit, isolating a device), and then checks that output came back.

Everything in the console is synthetic: the plant, the faults and the alarms are simulated; only the weather is
real (NASA data for Riyadh, 2025). The console must keep saying so.

The console is the window onto one incident. It runs in three modes: a recorded run played back ("Replay"), the
agent live with rules only, or the agent live with a local language model. A judge will see the replay of
incident fin_0029 (storm, dusty sensor, overheating inverter, two approvals) and probably one live run.

## Who will look at it

- Hackathon judges: a few minutes per team, not experts in solar plants or in AI agents. They need to see, in one
  pass, that several faults happened, that the agent found the real causes (not the symptoms), that it acted
  safely and asked a human when it should, and that output recovered.
- A plant manager or operator, the imagined end user: approves or rejects the risky actions, reads the shift
  report, wants plain words and the numbers that matter (megawatts, percent of expected, time).
- Not the developers. The developer view (tool names, scores, raw tables) must stay reachable but hidden.

## The problem that triggered this work

A mentor read the console and could not tell what the "Trace" tab was, or what "TRANSFORMER_TRIP@T4 (score
0.00)" meant. The console had been written by engineers for engineers: enum names, function names, score bars,
white text on pastel chips, tables everywhere, no explanation of what the user was looking at.

## What has been done so far (branch phase4/ux)

- A plain-language layer (`code/raqeeb/ui/plain.py`) turns every identifier into words: faults ("Dust on the sun
  sensor"), devices ("Transformer T4 (inverters 13-16)"), checks ("Compared each inverter with its peers"),
  verdicts with numbers ("Sun sensor reads 28% below the reference on a clear sky"), actions ("Lower Inverter 3's
  output limit to 1.6 MW"), how a cause was found ("Found by the rules" or "Proposed by the model, confirmed by
  5 checks"; never a score).
- A header (plant, mode, simulated clock, model), a notification strip above the tabs (latest event in words,
  open counts, a pulsing "Needs your decision", email status), eight tabs each with a two-line intro, the Summary
  tab first with a three-line explanation and "Play the storm".
- Trace cards rewritten as sentences: a headline per 15-minute step, only the checks that found something, the
  rest counted, decisions and outcomes as sentences, the raw record behind "Show data".
- A light "control room" theme: pale cool page, deep navy text, one blue accent, five semantic colours each with
  a word beside it. Tokens in `code/docs/CONSOLE_DESIGN.md`.

## What still reads badly (from inspection; fix these first)

1. The page is long and the first screen is crowded: header, strip, four big numbers, tabs, intro, explanation,
   five more numbers, then the chart. A judge should see the incident's shape in the first screen.
2. The Trace tab stacks a compact map and a chart on the left of a 900 px scrolling card column; on a laptop the
   cards start below the fold and the left column repeats the Plant tab. The Trace needs a layout of its own.
3. The plant map is a Plotly figure drawn with shapes: tile text is small (10 to 12 px), wraps badly in the
   compact version, and the tiles cannot carry hover or focus states. It may deserve a real HTML component.
4. Charts still speak engineering: "Expected from the weather reference" against "Expected from the on-site sun
   sensor" is the key idea of the whole product (the dusty-sensor trap) and nothing on the chart says why two
   expected lines exist or which one to believe. The clear-sky line adds a third grey line nobody asked about.
5. Numbers are inconsistent in shape: "32.9 MW", "9.74 MWh", "27% / 25%", "16 of 48", "0.0 s", "0.0462" in the
   Results table. Decide a number style and apply it everywhere (decimals, units, rounding).
6. The Agent tab is still a developer page (five tables, metrics for tokens and seconds). Either make it a clear
   "for engineers" page or fold it into Show data.
7. The sidebar mixes setup (mode, model, scenario) with playback (speed, Play, Step, To end); playback belongs
   near the clock and the strip, not in the sidebar.
8. Empty and waiting states are plain captions; the "Needs your decision" moment deserves a stronger, single
   place (the strip, the card and the sidebar all say it now, three times).
9. The two-line intros are grey captions that look like disclaimers; they are the only orientation a judge gets.
10. The alert banner and the email body still use raw action and device ids ("approve or reject set_power_limit on
    INV-04").

## Constraints that are not negotiable

- Streamlit 1.66 is the framework; custom HTML and CSS are allowed in `st.markdown` blocks, but everything must
  still work inside Streamlit's layout and rerun model. No separate front-end build.
- Every colour carries a word or an icon; colour never carries meaning alone. Text contrast at least 4.5:1.
- No raw identifiers (INV-03, SOILING@ALL, set_power_limit, tool names) visible by default. The raw record stays
  one click away behind "Show data"; it is never removed.
- Free text that comes from the plant (alarm messages, technician notes, the model's replies) is untrusted and is
  shown quoted, never rendered as markdown or HTML. This is a safety rule, not a style choice.
- "Synthetic" must stay visible: plant, faults and alarms are simulated; weather is NASA POWER (credit in the footer).
- No claims of novelty ("first", "novel", "nobody does this") in any text.
- Sentence case; no all-caps labels; no numbered markers unless the content is a sequence (the turns are one).
- The agent's logic, thresholds and prompts are frozen: the console only presents the record. A design cannot
  ask for new data from the agent, only for different presentation of what `code/raqeeb/ui/runlog.py` holds.
- Laptop width 1366 px is the target; mobile is not required.
- Times are shown in Asia/Riyadh; the simulated clock steps 15 minutes per agent turn.

## What we want from you

1. A layout for the first screen of the Summary tab that tells the incident's story in one view: what broke,
   what the agent did, what recovered, what needs a person; the chart with the two expected lines explained.
2. A Trace tab layout where the cards are the page, with the map and chart as context rather than a second copy
   of the Plant tab.
3. A consistent number and unit style, and a consistent treatment for the five semantic states across chips,
   tiles, chart marks and tables.
4. A plant map treatment (tile anatomy, states, hover) that reads at a glance; say whether it should stay a
   Plotly figure or become HTML.
5. The waiting-for-approval moment as one clear flow: notice, card, decision, confirmation.

Deliver as annotated mock-ups or as edits to the HTML/CSS in `code/raqeeb/ui/app.py` (the `CSS` block, the
`header()`, `notification_strip()`, `trace_card()`, `summary_tab()` functions) and wording in
`code/raqeeb/ui/plain.py`. Keep the Streamlit widget keys unchanged (`play_storm`, `to_end`, `trace_card_N`,
`approve_<id>` and so on): the tests use them.
