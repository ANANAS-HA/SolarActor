# HANDOFF — Raqeeb (Misk x HUMAIN "Future AI Builders" hackathon)

> **For the human:** put this file in an empty folder, e.g. `C:\Users\<you>\Desktop\raqeeb\HANDOFF.md`. If your Desktop is synced by OneDrive, pause OneDrive sync for the weekend (or move the folder to `C:\projects\raqeeb`); syncing fights with Git and Python environments.
> Open the folder in VS Code, start Claude Code in the integrated terminal, and send:
> **"Read HANDOFF.md fully. Do Phase 0 only, then stop and show me what you created."**
> Before Phase 0, fill in section 3 ("Values the human must fill in").
> Check the hackathon rules first: if code written before the official start isn't allowed, do Phase 0 only after the start.

---

## 1. What we are building (read this first)

**Raqeeb** is an AI agent that recovers a **solar power plant** when **several faults happen at once**.

- A **digital twin** (a Python physics model, not 3D) turns real Saudi weather into what each inverter *should* produce.
- A **fault injector** secretly breaks the plant (dust storm, dusty sensor, overheated inverter, comms loss, etc.) and keeps a hidden **answer key**.
- The plant then raises dozens of alarms. **Raqeeb must work out from the data alone** which faults were planted, separate root causes from symptoms, apply **safe, reversible fixes** automatically, ask a human for risky ones, and **verify** that output returned to the expected line.
- A **benchmark** grades Raqeeb against the answer key over many random scenarios, and compares it with a simple threshold-alarm baseline.

**Hackathon challenge:** Challenge 1 — *Agentic Recovery of a Failing System*: identify, diagnose and help recover a system when multiple issues occur at once, using logs and system metrics, carrying out corrective actions and verifying recovery.
**Secondary (measured, not the entry):** Challenge 2 — *Deliver AI with Less Compute*: rules and a local model handle most work; a large API model is called only when needed. Report cost per incident.

**The key insight for the demo ("the dusty-sensor trap"):** if panels *and* the sunlight sensor are both dusty, the usual health check (actual ÷ expected-from-sensor) looks normal while the plant is actually losing ~20% of its energy. Raqeeb catches this by comparing the sensor with a clear-sky model and the weather data.

**Positioning rule:** AI agents for solar O&M already exist (SmartHelio, Areg.AI, Invertix; research such as an ICNCCom 2026 agentic SCADA paper). Never write "first", "novel" or "nobody does this" anywhere (README, UI, report). Our contribution is desert compound-fault scenarios + verified recovery + an open benchmark + low compute.

**Deadline:** hackathon runs Friday 30 Oct 2026 → submission before **Saturday 31 Oct, 8:00 pm (Asia/Riyadh)**. Submission = project overview, video, project URL. AI coding tools are allowed. Solo participant.

---

## 2. Environment

| Item | Value |
|---|---|
| OS | Windows (project folder on the Desktop) |
| Editor | VS Code + Claude Code (integrated terminal, PowerShell) |
| Python | 3.11+ via **uv** (`uv venv`, `uv add …`, `uv run …`) |
| Local LLM | Already running **in WSL2** as an OpenAI-compatible server (vLLM or Ollama). The project only calls its HTTP API. Do **not** install model servers in this project |
| GPU | RTX 3090 (24 GB), used only by the WSL2 model server |
| API LLM | Anthropic API (Claude Sonnet 5.5) for hard cases only |
| Deployment (later) | Docker Desktop (WSL2 backend) → small Linux VM |

**Windows rules for all code:**
- Use `pathlib.Path` for every path; never hard-code `\` or `/` separators.
- Add `.gitattributes` with `* text=auto eol=lf`; run `git config core.longpaths true`.
- No shell scripts that only work on Linux for core tasks; use Python entry points (`uv run python -m raqeeb.…`).
- Use `Asia/Riyadh` for displayed times; store timestamps in UTC.

---

## 3. Values the human must fill in (into `.env`, never committed)

```
LOCAL_LLM_BASE_URL=http://localhost:<PORT>/v1     # WSL2 server; test with: curl http://localhost:<PORT>/v1/models
LOCAL_LLM_MODEL=<exact model name the server reports>
LOCAL_LLM_API_KEY=<anything, or the server's key>
ANTHROPIC_API_KEY=<your key>
API_LLM_MODEL=anthropic/claude-sonnet-5-5         # LiteLLM model string
SITE_NAME=<e.g. Sudair area>
SITE_LAT=<latitude, check on a map>
SITE_LON=<longitude>
WEATHER_YEAR=2025
RAQEEB_DB=data/raqeeb.db
```

If `localhost` can't reach the WSL2 server: make it listen on `0.0.0.0`, or add `networkingMode=mirrored` under `[wsl2]` in `C:\Users\<you>\.wslconfig`, then `wsl --shutdown`.

---

## 4. Architecture

```
NASA POWER weather (CSV) ─► Plant twin (pvlib) ─► Telemetry store (SQLite)
                                   ▲                    │  metrics, events, commands
Fault injector + scenarios ────────┘                    ▼
(hidden answer key)                     LangGraph agent
                                        Triage → Investigate → Rank causes → Plan
                                        (rules)  (tools)       (causal graph) (MWh at risk)
                                                        │
                                        Safety gate → Executor → Verifier ─┐
                                        (auto vs human)  (twin controls)    │ reopen if no recovery
                                                        │                   │
                                        Streamlit console      Benchmark harness (scores vs answer key)
```

**Core principle:** code computes every number (expected power, residuals, scores, MWh, cost). The LLM proposes and prunes hypotheses when evidence conflicts, orders the plan when trade-offs exist, and writes explanations and the shift report. Executor actions change the twin's state, so the Verifier sees real recovery.

---

## 5. Folder layout

```
raqeeb/
  HANDOFF.md            # this file (do not edit except the status log at the end)
  CLAUDE.md             # short working rules for Claude Code, generated in Phase 0
  README.md
  pyproject.toml
  .env.example  .gitignore  .gitattributes
  configs/
    plant.yaml          # site, inverter count, ratings, layout
    alarm_codes.yaml    # our synthetic vendor-style alarm codes + short manual text
    actions.yaml        # action allowlist with reversible / approval flags
  scenarios/            # generated scenario YAML files (each with answer key)
  data/
    weather/            # NASA POWER CSV (downloaded once)
    raqeeb.db           # SQLite (gitignored)
  raqeeb/
    contracts.py        # ALL shared Pydantic models + enums (single source of truth)
    config.py           # loads .env + YAML
    twin/      weather.py  plant.py  simulator.py  controls.py
    faults/    base.py  catalogue.py  injector.py  generator.py
    telemetry/ store.py
    tools/     metrics.py  physics.py  peers.py  weather_tools.py  alarms.py
    agent/     state.py  graph.py  triage.py  investigate.py  rank.py  plan.py
               safety.py  execute.py  verify.py  report.py
    llm/       gateway.py  router.py  prompts/
    baseline/  threshold.py
    bench/     run.py  score.py
    ui/        app.py
  scripts/
    download_weather.py
  tests/
```

---

## 6. Shared contracts (Phase 0 — write these before anything else)

All modules import types from `raqeeb/contracts.py`. Changing a contract means updating every user of it and its tests in the same commit.

**Enums**
- `FaultType`: `SOILING`, `SENSOR_SOILING`, `INVERTER_DERATE`, `COMMS_DROPOUT`, `CURTAILMENT` (decoy, not a fault), `TRACKER_STUCK`*, `STRING_OPEN`*, `UNAUTHORIZED_CMD`* (*full scope only)
- `Severity`: `INFO`, `WARNING`, `CRITICAL`
- `ActionType`: see section 9

**Telemetry (SQLite tables, timestamps UTC, 5-minute steps)**
- `inverter_readings(ts, inverter_id, ac_kw, dc_kw, temp_c, status)` — a comms dropout means **rows are missing**, never zeros
- `weather_station(ts, ghi_sensor_wm2, ambient_c, wind_ms)` — the on-site sensor (can be dusty)
- `weather_reference(ts, ghi_wm2, dni_wm2, dhi_wm2, ambient_c, wind_ms, clearsky_ghi_wm2)` — NASA POWER + pvlib clear-sky (trusted)
- `meter(ts, plant_ac_kw)` — substation total
- `grid_setpoint(ts, limit_kw)` — curtailment signal
- `events(ts, device_id, code, severity, message)` — alarms, codes from `alarm_codes.yaml`
- `commands(ts, source, device_id, action, params)` — control actions, including unauthorized ones in full scope

**Scenario file (`scenarios/<id>.yaml`)**
```yaml
id: scn_0007
seed: 7
start: "2025-04-12T05:00:00Z"
duration_hours: 12
faults:
  - {type: SOILING, target: ALL, start: "07:00", severity: 0.20}
  - {type: SENSOR_SOILING, target: WS-01, start: "07:00", severity: 0.20}
  - {type: INVERTER_DERATE, target: INV-07, start: "10:30", severity: 0.38}
  - {type: COMMS_DROPOUT, target: INV-12, start: "11:15", end: "13:00"}
answer_key:            # hidden from the agent; used only by bench/score.py
  root_causes: [SOILING@ALL, SENSOR_SOILING@WS-01, INVERTER_DERATE@INV-07, COMMS_DROPOUT@INV-12]
```

**Agent models**
- `Symptom(id, kind, device_id, start, evidence)`
- `Hypothesis(id, fault_type, targets, explains: list[symptom_id], evidence: list[str], score: float)`
- `Diagnosis(root_causes: list[Hypothesis], unexplained: list[symptom_id], confidence)`
- `ActionRequest(action, target, params, reversible, needs_approval, rationale, expected_effect)`
- `Verification(action_id, recovered: bool, before_kw, after_kw, expected_kw, note)`
- `IncidentReport(summary_en, summary_ar, causes, actions, verifications, cost_usd, llm_calls)`

---

## 7. Plant twin

- **Weather:** `scripts/download_weather.py` downloads one year of **hourly NASA POWER** data (community `RE`) for `SITE_LAT/SITE_LON`: all-sky surface shortwave (GHI), DNI, diffuse, 2 m air temperature, 10 m wind. Check the API's time-standard option and store UTC. Resample to 5 minutes.
- **Physics (pvlib, keep it simple):** `Location` → solar position → clear-sky (Ineichen) for the reference → single-axis tracking (`pvlib.tracking`) → plane-of-array irradiance → cell temperature (SAPM model) → `pvwatts_dc` → `pvlib.inverter.pvwatts` with a clipping limit.
- **Plant:** core = 16 inverters (`INV-01`…`INV-16`), equal ratings, small random variation per inverter (±2%) so peers aren't identical; one weather station `WS-01`; one meter.
- **Twin state:** per-device multipliers and flags (soiling factor, derate cap, comms up/down, tracker angle override, sensor soiling). Faults and controls only change this state; the simulator computes readings from weather × state.
- **Controls (`twin/controls.py`):** functions the Executor calls, e.g. `reset_comms(device)`, `clean_panels(target)` (applies after a configurable delay), `set_power_limit(inverter, kw)`, `flag_sensor(ws)`.
- **Clock:** simulated time; runs faster than real time; supports "advance N minutes" for the demo and the benchmark.

---

## 8. Fault catalogue (what changes in the data, and the distinguishing evidence)

| Fault | Effect on data | Evidence that identifies it | Safe action |
|---|---|---|---|
| SOILING (dust storm) | All inverters −X% after the storm, persists when wind drops | Actual below expected from the **trusted** reference; plant-wide; started with high wind | `create_cleaning_work_order` (auto) |
| SENSOR_SOILING | `ghi_sensor` reads −X% | Sensor disagrees with clear-sky/NASA on a clear period; makes performance ratio look normal (**the trap**) | `flag_sensor_use_reference` (auto) |
| INVERTER_DERATE | One inverter flat-capped at midday, temp high, `DERATE_TEMP` alarm | Peers normal; cap is flat; temperature high | `set_power_limit` or inspection (**needs approval**) |
| COMMS_DROPOUT | Rows missing for one device, `COMM_LOSS` alarm | Blank not zero; meter total doesn't drop accordingly | `reset_comms_gateway` (auto) |
| CURTAILMENT (decoy) | All inverters capped together | `grid_setpoint` present; simultaneous | **No action** — mark expected |
| TRACKER_STUCK* | Morning/evening loss on a block | Flat angle after wind has dropped (stow during high wind is normal) | `resume_tracking` (auto) |
| STRING_OPEN* | One inverter's DC down by a fixed fraction | Single device, no weather link | `dispatch_technician` (auto ticket) |
| UNAUTHORIZED_CMD* | Inverter to zero after a command | Command from unexpected source/time, no prior electrical alarm | `isolate_device` (**needs approval**) |

The injector also emits **symptom alarms** (low string current, low PR warnings, tracker stow during wind) so a storm produces 30–50 alarms. Alarm codes and one-paragraph manual text live in `configs/alarm_codes.yaml` (synthetic, vendor-style; say so in the README).

**Causal graph (`agent/rank.py`, NetworkX):** nodes = fault types and symptom kinds; edges = "fault can cause symptom" with the conditions above. Scoring = symptoms explained − penalty for predicted-but-absent symptoms − penalty per extra cause (prefer the simplest explanation).

---

## 9. Safety gate and actions (`configs/actions.yaml`)

| Action | Reversible | Auto? |
|---|---|---|
| `reset_comms_gateway` | yes | auto |
| `flag_sensor_use_reference` | yes | auto |
| `create_cleaning_work_order` | yes (ticket) | auto |
| `resume_tracking`* | yes | auto |
| `dispatch_technician`* | yes (ticket) | auto |
| `set_power_limit` | yes | **human approval** |
| `isolate_device`* | no | **human approval** |
| change grid setpoint | — | **never** (not in allowlist) |

Anything not in the allowlist is rejected. Approval uses a LangGraph interrupt; the UI shows Approve / Reject with the rationale. Every action, approval and verification is written to an audit log table `audit(ts, step, actor, payload_json)`.

---

## 10. Models and routing (Challenge 2 layer)

- `llm/gateway.py`: LiteLLM wrapper with two named roles — `local` (WSL2 server via `LOCAL_LLM_BASE_URL`, OpenAI-compatible) and `api` (`API_LLM_MODEL`). Log every call: role, tokens, latency, cost.
- `llm/router.py`, rules first:
  1. Triage, grouping, numbers → **no LLM**.
  2. Hypothesis proposal and summaries → **local**.
  3. Escalate to **api** only if: top-2 hypothesis scores within a margin, unexplained symptoms above a threshold, or the local model's output fails Pydantic validation twice.
- Arabic + English shift report → **local** (fallback: api).
- Every LLM output is parsed into a Pydantic model; invalid output is retried once, then escalated.
- If the local server is down, fall back to api and record it.

---

## 11. Baseline and benchmark

- **Threshold baseline (`baseline/threshold.py`):** one ticket per alarm, alarm code mapped straight to a fault type, no correlation. Expected to over-count causes and act on symptoms.
- **Full scope extra baseline:** one large-model call given all the raw data at once.
- **Generator (`faults/generator.py`):** random scenarios with 1–4 faults, seeded; core = 20 scenarios, full = 50; always include several "trap" scenarios (sensor + panel soiling together, curtailment decoy).
- **Metrics (`bench/score.py`):** root-cause precision and recall, all-causes-found rate, false-action rate (actions on symptoms or decoys), time to verified recovery (sim minutes), MWh recovered vs no action, cost per incident (USD), share of calls handled locally.
- Output: `bench/results.csv` + a summary table the UI shows. **Report results honestly**, including where Raqeeb loses.

---

## 12. Console (`ui/app.py`, Streamlit + Plotly)

1. **Plant map:** 4×4 grid of inverters colored by status (healthy / degraded / no data).
2. **Inject faults:** buttons "Dust storm + 2 random faults" (random seed) and "Replay scenario".
3. **Alarm feed:** live count and list.
4. **Agent timeline:** each step with its evidence and decision.
5. **Approvals:** Approve / Reject for gated actions.
6. **Recovery chart:** actual vs expected (trusted) vs expected-from-sensor over time.
7. **Results tab:** benchmark table vs baseline.
8. **Report tab:** English and Arabic shift report; download as Markdown.
3D is out of scope unless the core is finished early (then a small three.js view as a separate page; the twin stays the source of truth).

---

## 13. Build phases and acceptance criteria

**Phase 0 — setup and contracts (do first, alone, ~1–2 h)**
- `uv init`, dependencies: `pvlib pandas numpy pydantic pyyaml networkx langgraph litellm streamlit plotly python-dotenv pytest`.
- Git init, `.gitignore` (`.env`, `data/*.db`, `.venv`), `.gitattributes`, `.env.example` from section 3.
- `contracts.py`, `config.py`, `configs/*.yaml` skeletons, folder layout, `CLAUDE.md` (≤60 lines: rules from sections 1, 2, 6, 14).
- `scripts/download_weather.py` and one smoke test that the local LLM endpoint answers.
- ✅ Done when: `uv run pytest` passes, the weather CSV exists, and the LLM smoke test passes.

**Phase 1 — parallel lanes (separate Claude Code sessions, each on its own git branch or worktree)**
| Lane | Builds | Done when |
|---|---|---|
| A | Twin (weather, plant, simulator, controls) | A clear day's expected vs simulated output plot looks right; controls change state |
| B | Fault catalogue, injector, generator | Each core fault produces the data effects in section 8; scenarios round-trip YAML |
| C | Telemetry store + tools | Tools return correct values on a fixture scenario |
| D | Console shell on fake data | All panels render with fixture data |
| E | Bench harness + threshold baseline on a fake agent | Scores computed for fixture answer keys |

**Phase 2 — agent core** (after A–C merge): LangGraph graph, triage, investigate, rank, plan, safety, execute, verify, report, router.
✅ Done when one trap scenario runs end to end and finds all root causes.

**Checkpoints (Asia/Riyadh)**
- **Fri 8:00 pm — integration:** core loop runs end to end on the 5 core faults; a hello-world deployment is live.
- **Sat 12:00 pm — go/no-go:** full-scope items not yet integrated are dropped.
- **Sat 2:00 pm — feature freeze.** Only fixes, benchmark run, report, video, overview after this.
- **Sat 6:00 pm — submit** (2 hours buffer before 8:00 pm).

**Fallbacks:** behind at Sat noon → recommend-only mode (diagnose and propose, no execution). Local model unreliable → route its steps to api. Deployment broken → recorded demo video.

---

## 14. Working rules for Claude Code

- Read this file before each session; follow the contracts; don't invent new shared types outside `contracts.py`.
- One module at a time, each with tests; run `uv run pytest` before every commit; commit after each working piece with a clear message.
- No network calls except: the LLM endpoints in `.env` and the one-time NASA POWER download.
- Never commit `.env` or keys; never print keys.
- Keep numbers in code, not in prompts; prompts live in `llm/prompts/` as files.
- Synthetic data must be labelled synthetic in the README and UI.
- No claims of novelty in any text.
- If something in this file is ambiguous or impossible, stop and ask instead of guessing.
- Append a dated line to the status log below after finishing each phase or lane.

---

## 15. Status log

- 2026-10-29 — Handoff written. Nothing built yet.
- 2026-10-29 — Phase 0 done except LLM smoke: uv + Python 3.12, contracts, config, YAML skeletons, Riyadh 2025 NASA POWER CSV (8760 h, no gaps), 18 tests pass. Local LLM not running yet; `test_llm_smoke` skips until `.env` points at it.
- 2026-10-29 — Phase 0 complete. Local LLM smoke passes: llama.cpp at 127.0.0.1:8080, Qwen3.8-27B IQ4_XS, ~84 tok/s, reasoning model (thinking can be turned off per call with `chat_template_kwargs: {enable_thinking: false}`). 19 tests pass.
- 2026-10-29 — Lane A (twin) done on branch `lane-a/twin`: weather (clear-sky-index resampling), plant physics, simulator (clock, scheduler, device alarms), controls for all 7 actions, `python -m raqeeb.twin.demo` clear-day plot. Contracts added: `PlantState` (+ inverter/station/atmosphere), `TelemetryBatch`, plant config sections (noise, controls, thresholds). 53 tests pass. Merged to main.
- 2026-10-29 — Lane A follow-up: `tracker_angle_deg` (nullable) added to `inverter_readings`; twin true irradiance now differs from the NASA reference (bias + AR(1) error, `reference_error` in plant.yaml) so the reference is not ground truth. Confirmed `temp_c` is inverter internal temperature. 56 tests pass.
- 2026-10-30 — Lane C (telemetry store + tools) done on branch `lane-c/telemetry`, not merged: `telemetry/store.py` (SQLite from `TABLE_DDL`, batch + audit writers, UTC-indexed DataFrame reads) and `tools/` weather_tools, physics, metrics, peers, alarms (numbers only, store only). Dusty-sensor check = sensor vs reference on clear steps + clear-sky shape + step drop + peers, explicit thresholds. Sweep over 26 days x 2 seeds: no false alarms on healthy days; sensor-only, panels-only and trap all classified right except one cloudy day (15 Feb, "inconclusive"). Note: erbs puts ~17% of midday GHI in diffuse vs NASA's ~32%, so expected-from-sensor runs 2-4% high per day (up to 10% in unclipped summer hours); the sensor check avoids erbs. No contract changes. 112 tests pass.
- 2026-10-30 — Lane B (faults) done on branch `lane-b/faults`: `faults/catalogue.py` (5 core + 3 full-scope faults, all via `twin.schedule`), `faults/injector.py` (`FaultInjector.for_scenario(s)`; drive the twin through `inj.advance()` so symptom alarms LOW_PR / LOW_STRING_CURRENT / DC_INPUT_LOW are raised from the on-site sensor, one step after the reading, not while clipping), `faults/generator.py` (seeded; 20 core scenarios in `scenarios/`, 5 traps in cool months, 6 with curtailment decoys). Dust storm = 49 alarms; with a dusty sensor only the 17 wind alarms remain. No contract changes. 91 tests pass.
- 2026-10-30 — Lane B merged to main. Contract fix: the agent gets `Scenario.agent_view()` -> `AgentScenario(id, start, duration_hours)`; `without_answer_key()` removed (it still listed the planted faults). `tests/test_agent_isolation.py` + `tests/leakcheck.py` fail if agent-side code (`agent/ tools/ llm/ baseline/`) can reach scenarios, the truth log or twin state, or if an agent-facing object leaks fault data. `Scenario.to_yaml` quotes "HH:MM" times. CLAUDE.md: agent isolation rule, drive the plant through `FaultInjector`. README: curtailment split noted as a known simplification. 117 tests pass.
- 2026-10-30 — Lane C update: `ToolResult` contract on main; every agent tool returns it (`raqeeb.tools.AGENT_TOOLS`); fixtures run through `FaultInjector`, tools get only the `agent_view()` window, outputs pass `assert_no_answer_leak`. `physics.expected_from_sensor()` is the shared erbs calculation, but the injector does NOT use it yet: with it, healthy days raise LOW_STRING_CURRENT/LOW_PR in 43/52 runs (erbs bias), so this waits on a decision. Sensor check sweep, 26 days x 2 runs, a different reference bias per run: 50/52 with reference error off, 50/52 at the current level, 39-48/52 at double (healthy: 2 false sensor and 11 false plant-loss findings). The 2 misses at current level are one cloudy day (inconclusive). 176 tests pass.
- 2026-10-30 — Lane C: expected-from-sensor unified (option A). `raqeeb.tools.physics.expected_from_sensor()` = reference scaled by sensor GHI / reference GHI; erbs removed; the injector's LOW_PR / LOW_STRING_CURRENT / DC_INPUT_LOW call it once per step. `scripts/sweep_detection.py` (fixed seeds, 26 days x 2 runs, ~6 min): sensor check 50/52 per scenario with reference error off and at the plant.yaml level (both misses = cloudy 15 Feb, "inconclusive"), 39-49/52 at double error (healthy: 2 false sensor, 11 false plant-loss findings); plant loss right 52/52 (41/52 at double); symptom alarms on 0/52 healthy, 0/52 dusty-sensor-only, 52/52 dusty-panels-only, 0/52 trap runs. Trap: PR vs sensor 100% while vs reference 83%.
- 2026-10-30 — Lane E (bench + baseline) done on branch `lane-e/bench`. Contract added (approved): `Agent` protocol (`start(view, telemetry)`, `step(now, outcomes, final)`), `AgentTurn`, `ActionOutcome`/`ActionStatus`, `LlmCall`/`LlmRole`, `TelemetryReader` (Lane C's `TelemetryStore.read` fits). The environment gates actions with `configs/actions.yaml`, approves and runs them with `apply_action`; outcomes return on the next poll. `bench/harness.py` (poll every 15 sim min; mitigation checked in twin state; energy from `twin.truth` vs no-action and decoy-only runs), `bench/score.py` (section 11 metrics; time to verified recovery = all causes mitigated in truth AND every applied action verified by the agent), `bench/fakes.py` (oracle, empty), `baseline/threshold.py`, `bench/memory.py` (in-memory telemetry until Lane C merges; swap the harness default to `TelemetryStore`). `uv run python -m raqeeb.bench.run` -> `bench/results.csv`, `summary.csv`, `summary.md`. Threshold on 20 core scenarios: precision 0.18, recall 0.56, all causes found 30%, never verifies. 142 tests pass.
- 2026-10-30 — Lane E review fixes: threshold baseline sets its power limit at the observed flat cap (median AC of DERATED readings; waits if none yet), harness counts a limit within 1% of the cap as mitigated; summary.md has no timestamp (stable unless results change); `git merge main` already up to date (ToolResult kept). Baseline now: precision 0.19, recall 0.56, 189.5 MWh recovered of 750.9 lost. 143 tests pass.
- 2026-10-30 — Lane D (console) done on branch `lane-d/console`: `ui/app.py` (Streamlit + Plotly, 8 panels: plant map, inject/replay, alarm feed, agent timeline, approvals, recovery chart + irradiance, results, EN/AR report with Markdown download; simulated clock slider; times in Asia/Riyadh; synthetic banner). All data goes through `ui/source.py` (runs scenarios in memory via `FaultInjector`; only `_telemetry()` changes when the Lane C store lands). `ui/samples.py` = SAMPLE agent scripted from the planted faults (its auto actions go through `apply_action`, so recovery is real twin behaviour), sample benchmark. Added `faults.generator.storm_scenario(seed)` for the inject button. No contract changes. 140 tests pass.
- 2026-10-30 — Lane D: merged `lane-c/telemetry` into `lane-d/console` (Lane C not yet on main). The console now writes each run into an in-memory `TelemetryStore` and reads every table back from it; actual / expected-from-reference / expected-from-sensor lines come from `raqeeb.tools.physics.expected_plant_ac` (same calculation as the agent tools and the symptom alarms). Planted faults are shown only after the simulated clock reaches the end of the run. 208 tests pass (local LLM server up).
- 2026-10-30 — Merged to main: `lane-c/telemetry`, `lane-e/bench` (contracts keep both `ToolResult` and the agent interface), `lane-d/console` (status-log conflicts only, both sides kept). Bench harness now defaults to an in-memory `TelemetryStore` per run; `python -m raqeeb.bench.run` output unchanged (threshold precision 0.19, recall 0.56, 189.5 of 750.9 MWh). Console starts (`streamlit run raqeeb/ui/app.py`). 233 tests pass.
