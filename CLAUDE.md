# Raqeeb — working rules for Claude Code

Full spec: `handoffs/HANDOFF.md`. Read it before each session. Only edit its status log (section 15).

## What this is
An agent that diagnoses and recovers a simulated solar plant under compound faults (hackathon Challenge 1),
with low compute (Challenge 2): rules + local model first, API model only when needed.
Code computes every number; the LLM proposes and prunes hypotheses, orders plans and writes reports.

## Contracts
- All shared types and enums live in `raqeeb/contracts.py`. Do not define shared types anywhere else.
- Changing a contract means updating every user and its tests in the same commit.
- Timestamps: timezone-aware UTC everywhere; SQLite TEXT `YYYY-MM-DDTHH:MM:SSZ` (`to_db_ts`/`from_db_ts`).
  Display in Asia/Riyadh. Telemetry step = 5 min.
- Comms dropout = missing rows, never zeros.
- Root cause label = `FAULT_TYPE@TARGET` (`cause_label`). `CURTAILMENT` is a decoy, never a root cause, no action.
- Scenario fault times `"HH:MM"` = UTC on the scenario start date. Always quote them in YAML.
- The agent sees only `Scenario.agent_view()` (id, start, duration_hours). Faults, seed and answer key are for
  the injector and `bench/` only.
- Actions: only `configs/actions.yaml` (= `ActionType`). `auto: false` needs human approval. Never a grid setpoint change.

## Environment (Windows)
- uv for everything: `uv sync`, `uv add <pkg>`, `uv run pytest`, `uv run python -m raqeeb.<module>`.
- `pathlib.Path` for every path; paths come from `raqeeb/config.py` (`REPO_ROOT`, `WEATHER_DIR`, ...).
- No Linux-only shell scripts for core tasks; use Python entry points.
- Settings: `load_settings()` (from `.env`); YAML: `load_plant()`, `load_alarm_codes()`, `load_actions()`.

## Data notes
- Weather: `data/weather/nasa_power_<lat>_<lon>_<year>.csv`, hourly, UTC. A value labelled hour H is the
  average over [H, H+1): centre it at H+30 min before resampling to 5 min (checked against solar position).
- Riyadh 2025 hourly wind maxes at ~11 m/s (below the 15 m/s stow limit): the dust-storm fault must inject its own wind.
- NASA clear-day GHI is ~10% ABOVE pvlib Ineichen (it assumes hazier air): compare the sensor to the reference,
  and never present the clear-sky line as "the maximum possible" in the UI.
- The reference is NOT ground truth: true irradiance = NASA x (1 + bias + slow error), per-run sd ~2%, 5-min sd ~3%,
  max 8% (`reference_error` in plant.yaml). Detection must combine sensor vs reference, peers and clear-sky shape.

## Twin (Lane A)
- `Twin(start, end, seed=)`; faults and delayed effects go through `twin.schedule(at, label, fn(state))`;
  `twin.advance(min)` returns a `TelemetryBatch`; `twin.truth` holds exact values for the bench.
- Faults/controls change only `PlantState` (contracts). Controls: `twin/controls.py`, `apply_action()`.
- Expected (fault-free) output: `twin.plant.expected_power()` / `expected_plant_ac()` on `weather_5min()`.
- `temp_c` = inverter internal temp (ambient + load rise + fault offset), not cell temp. Readings carry `tracker_angle_deg`.
- DC/AC 1.25 clips ~5 h on clear summer days: midday soiling loss is partly hidden (20% soiling = -16% energy in July).
  Demo trap scenario: use a cooler month or morning/afternoon hours so the loss is clear.

## Faults (Lane B)
- Run the plant through the injector: `inj = FaultInjector.for_scenario(s)`, then `inj.advance(min)` / `inj.step()`,
  never `twin.advance` directly, or the symptom alarms (LOW_PR, LOW_STRING_CURRENT, DC_INPUT_LOW) never appear.
- SOILING `start`/`end` = the dust-storm window; the soiling persists until cleaned.

## Rules
- Agent tools may read only the telemetry database: never `scenarios/` files, `Scenario`, the twin's `truth` log
  or its `state`. `tests/test_agent_isolation.py` enforces this for `agent/ tools/ llm/ baseline/`;
  run `tests/leakcheck.assert_no_answer_leak` on every agent-facing tool output in its tests.
- One module at a time, each with tests. Run `uv run pytest` before every commit; commit after each working piece.
- No network calls except the LLM endpoints in `.env` and the one-time NASA POWER download.
- Never commit `.env` or keys; never print keys (`Settings` repr hides them).
- Numbers stay in code, not in prompts. Prompts are files in `raqeeb/llm/prompts/`.
- Synthetic data (plant, telemetry, faults, alarm codes) must be labelled synthetic in README and UI.
- No novelty claims ("first", "novel", "nobody does this") in any text.
- Report benchmark results honestly, including where Raqeeb loses.
- If something is ambiguous or impossible, stop and ask instead of guessing.
- After finishing a phase or lane, append a dated line to the HANDOFF status log.

## Useful commands
- `uv run pytest` (LLM test skips until `.env` has a live local server)
- `uv run python scripts/download_weather.py [--force]`
- `uv run python -m raqeeb.llm.smoke [--api]`
- `uv run python -m raqeeb.faults.generator [--n 20] [--seed 2025] [--full]` (rewrites `scenarios/`)
