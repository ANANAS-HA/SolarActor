# Lane D handoff: the console (front end), finished 2026-10-31

For the lead / orchestrator. Lane D is done from the user's side; this is what to merge, how to check it, and what was left out on purpose.

## Where the work is

- Worktree `..\raqeeb-ux`, branch `phase4/design`, head **f57364d**. Branched from `phase2/lane-c` at 7cd312d (the lane C head, unchanged since). `phase4/ux` (dac108d) is the earlier plain-language pass and is fully contained in `phase4/design`.
- 23 files changed, all under `raqeeb/ui/`, `tests/test_ui*.py`, `docs/`, `.streamlit/config.toml` and the HANDOFF status log. Nothing under `raqeeb/agent/`, `raqeeb/llm/`, `raqeeb/bench/`, `contracts.py` or `configs/` (checked with `git diff --stat 7cd312d..HEAD` excluding those paths: empty).
- Merge: `git merge f57364d` into the integration branch. Expect no conflicts outside `raqeeb/ui/` and `tests/test_ui*.py`; if lane C moved `raqeeb/ui/runlog.py` or `live.py` since 7cd312d, those files were not touched here and merge cleanly.

## What changed, by commit

| Commit | What |
|---|---|
| 03cf857 | `raqeeb/ui/plain.py`: plain-language layer (faults, devices, tools, verdicts, actions, gate, numbers, timeline sentences) with tests |
| aa03d6b, 488140d | plain language on every page, readable Trace cards, notification strip, header, first theme |
| 02fe95d, 3840d86, 49c3fbe | escaping rules for record text next to and inside HTML (no markdown rendering of plant text, no stray backslashes) |
| 6ab614f, e8889db, 4fe93ef | docs for the designer: component inventory, design brief, the designer's handoff (reference HTML and notes) |
| be0a937, 8fd0d11, c040009 | redesign phase A: desert-noon theme, sidebar navigation (radios `nav_main` / `nav_judge`), sticky header with the Run popover and playback, floating Ask Raqeeb, full-width Trace cards, approval card on Summary |
| a047a96 | live runs refresh about once a second on every page (Play no longer looks stuck outside Trace) |
| 04452f7 | CONTRAST_FIX (dark 14-15 px text everywhere, button styles, one Plotly style, white chart cards, "→" arrows) and PLANT_TAB (new `raqeeb/ui/plantpanels.py`: stat row, Sun and wind, Plant output against expected, HTML plant map, equipment cards) |
| f57364d | Plant first in the sidebar, map between the two charts, "HUMAIN AI Builders Hackathon" label above the logo |

Designer notes applied are kept under `docs/design/` (REDESIGN_HANDOFF.md, CONTRAST_FIX.md, PLANT_TAB.md, reference HTML).

## How to check it

```
uv run pytest tests/test_ui.py tests/test_ui_plain.py tests/test_ui_summary.py tests/test_ui_trace.py tests/test_ui_ask.py tests/test_ui_models.py tests/test_ui_map.py tests/test_ui_alerts.py tests/test_ui_plant.py
```

- Last run on f57364d's parent state: 85 passed (3 min 18 s). Run it with no `.env` in the worktree: with a real `.env` present, three alert and model tests read the live settings and fail, as they would on main.
- Serve: `uv run streamlit run raqeeb/ui/app.py --server.port 8511 --server.headless true`. It is running from the worktree now on http://localhost:8511.
- Streamlit reloads `app.py` on its own but kept stale copies of edited helper modules (`plain.py`, `plantpanels.py`) during this session. Restart the server after merging.
- Screenshots of every page and state are in `..\raqeeb-ux-screenshots\` (outside the repo): `inventory\` (89 component shots), `design-a\`, `design-contrast\`, `design-plant\`, `design-plant2\`.

## Behaviour the rest of the system can rely on

- All existing widget keys are unchanged (`mode_choice`, `provider`, `model_choice`, `api_tier`, `replay_choice`, `load_replay`, `replay_upload`, `load_upload`, `scenario_id`, `start_live`, `inject_storm`, `speed`, `save_recording`, `step`, `to_end`, `play`, `approve_*`, `reject_*`, `note_*`, `trace_*`, `ask_input`, `play_storm`, `summary_moment_{i}`, `summary_go_{cause}`).
- Pages are chosen by the sidebar radios, not tabs. Tests open a page with `at.radio(key="nav_main").set_value("Plant").run()`; `nav_judge` for Answer key and Results. The console opens on Summary.
- Plant free text (alarm messages, notes, model replies, badges) is escaped once for its context and never rendered as markdown or HTML. Raw ids and enums appear only under "Show data".
- Agent tools and the UI read only the telemetry database and the run record; `tests/test_agent_isolation.py` and the map tests (`test_map_reads_no_scenario_or_answer_key`) still pass.
- The twin-run email alert path (`raqeeb/ui/alerts.py`, lane C) is untouched. The copied `.env` in the worktree holds a real Resend key: a live run with alerts enabled sends real mail. The "email sent" state was never screenshotted for that reason.

## Left out on purpose

- Designer phases B to E (story-first Summary with headline and stat cards, Agent table, letter-style Report, Answer key match table): not started; the notes are in `docs/design/REDESIGN_HANDOFF.md`.
- From the designer's mock: the Claude / ChatGPT / Gemini provider picker and API-key fields (the project is fully local; shown as "not configured" like the API tier), "Not now / Postponed" decisions (the agent knows Approve and Reject only), and the mock's placeholder numbers (everything binds to the record).
- `plantmap.map_figure` (the old Plotly map) is kept because `tests/test_ui_map.py` exercises it; the page draws the HTML map instead. It can be removed with its test when convenient.

## Memory for the next session

- Lane-local notes are in the project memory file `ux-worktree-and-screenshots` (Edge via `channel="msedge"` for Playwright, the server restart rule, never commit `.env` or `.env.aside`).
