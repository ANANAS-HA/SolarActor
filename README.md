# Raqeeb

Raqeeb (Arabic for "watcher") is an agent that diagnoses and recovers a simulated solar power plant when
several faults happen at once. It reads plant telemetry and alarms, separates root causes from the
symptoms they trigger, applies safe and reversible fixes on its own, asks a human before risky ones,
and checks that output actually came back.

Built for the Misk x HUMAIN "Future AI Builders" hackathon, **Challenge 1: Agentic Recovery of a Failing
System**, with an eye on low compute: rules and a small local model do the work, and code computes every
number.

> **Synthetic data.** The plant, its telemetry, the faults, the alarm codes and manual text, the
> technician notes and the device log lines are all **synthetic**. Only the weather is real: NASA POWER
> hourly data for Riyadh, 2025. Plant output is simulated from that weather with pvlib.

## The dusty-sensor trap

A dust storm soils the panels and the on-site irradiance sensor together. The plant loses output, but
compared with its own dusty sensor it looks normal, so a threshold alarm sees nothing; Raqeeb checks the
sensor against an independent satellite reference, the neighbouring inverters and the clear-sky shape.

## How it works

1. **Digital twin** (`raqeeb/twin/`). A 40 MWac single-axis tracker plant (16 inverters behind four
   transformers, a weather station, a substation meter) simulated with pvlib on real 2025 Riyadh weather
   in 5-minute steps. Controls (comms reset, cleaning, power limit, isolation, ...) change the plant's
   state after a delay, as on a real site.
2. **Faults** (`raqeeb/faults/`). An injector plants faults on a schedule: soiling from dust storms, a
   dusty sensor, comms dropouts, inverter derates, stuck trackers, open strings, unauthorised commands,
   grid curtailment (a decoy: normal operation), injected instructions in device log text (a decoy that
   must never be obeyed), and three unknown fault types the agent has no rules for (transformer trip, new
   shading, firmware alarm). Some unknown faults are "quiet": no telltale alarm, so a technician note
   decides between causes that fit the numbers (with a misleading note alongside). Grid-operator notices
   (English or Arabic) explain a cap that has no setpoint rows; fake notices do not match the cap.
   Faults show up the way a plant shows them: alarms, missing rows, flat or low readings, notes.
   Four more **held-out** fault types (hung inverter, comms dropout under load, sensor drift, silent
   stuck tracker) have no rules in the agent and appear only in the final set.
3. **Telemetry** (`raqeeb/telemetry/`). Everything lands in SQLite tables (inverter readings, weather
   station, reference weather, meter, events, commands, technician notes, audit). A comms dropout is
   missing rows, never zeros. Every read is clipped to the simulated clock, so nothing can read ahead.
4. **Tools** (`raqeeb/tools/`). Deterministic checks the agent calls: sensor vs reference vs clear-sky
   shape, peer outliers, expected power from the reference or from the sensor, alarm grouping, plant
   metrics. Each returns numbers, the thresholds it used and a short evidence line.
5. **Agent** (`raqeeb/agent/`, LangGraph). One pass per poll through nine steps: **triage → investigate →
   rank → consult → plan → safety → execute → verify → report**. A causal graph ranks root causes
   against symptoms; the planner picks a fix ladder per cause, with timing (no washing panels while the
   storm still blows); verify compares output after a fix with what the twin model expects and reopens
   the cause if it did not recover.
6. **Safety gate.** Only the actions in `configs/actions.yaml` are possible. Risky or irreversible ones
   (power limit, isolation) wait for a human. Each cause has a retry budget, after which it goes to a
   human with the reason. Changing the grid setpoint is never allowed. Alarm text, device log text and
   technician notes are untrusted data: the rules never read them. A note never decides on its own: the
   model reports a notice's window and limit, and code accepts it only if they match the cap the tools
   measured; a note may only reduce actions, never trigger one.
7. **Local model, only where the rules stop** (`raqeeb/llm/`). When symptoms stay unexplained or the top
   two causes are nearly tied, a local model may propose a cause. The agent tests every proposal on tool
   data (the causal graph, or the proposal's footprint on the site layout) before accepting it. A rejected
   proposal changes nothing; an accepted cause the planner has no fix for gets a ticket or an escalation,
   never a new kind of action. The model also writes the operator explanation and the English and Arabic
   shift report from the agent's own records, and any number it writes must appear in that data.
8. **Console** (`raqeeb/ui/`, Streamlit). A plant map, alarm feed, recovery chart, agent timeline,
   approvals, model proposals and the shift report, for a live run or a recorded replay.

## Run the console

### Locally (Windows, PowerShell, [uv](https://docs.astral.sh/uv/))

```powershell
uv sync
uv run streamlit run raqeeb/ui/app.py          # http://localhost:8501
```

The NASA POWER weather file is in `data/weather/`. Modes:

- **Live: rules only.** The real agent runs step by step on a practice scenario or a random dust storm.
  Actions that need a human pause the run for Approve / Reject.
- **Live: rules + local model.** Enabled only when `.env` names a reachable OpenAI-compatible local model
  server (`LOCAL_LLM_BASE_URL`, `LOCAL_LLM_MODEL`; copy `.env.example` to `.env`).
- **Replay.** A recorded run, or any trajectory file the bench writes (Trace tab, upload or path). The
  console opens on four runs of the agent with the local model, replayed from the frozen final set (tag
  `final-trace`, agent unchanged; `raqeeb/ui/replays/final/`): fin_0029 (dust storm, dusty sensor and a
  derate that needs approval), fin_0024 (a grid-operator notice in Arabic, confirmed), fin_0038 (a quiet
  transformer trip named through a model proposal the agent tested) and fin_0053 (a held-out sensor drift,
  escalated as unexplained). They keep the model's prompts and replies. The model name keeps only the
  model file's name.
  The repository also keeps four older recordings labelled "recorded with test model": a scripted
  stand-in, not a language model, whose proposals come from a fixed rule (not shipped in the Docker image).

The planted faults (the answer key) are shown only after a run has ended.

### With Docker

```powershell
docker build -t raqeeb-console .
docker run --rm -p 7860:7860 raqeeb-console        # http://localhost:7860
```

The image runs rules-only live runs and replays, holds no secrets, and makes no network calls while it
runs. `docs/DEPLOY.md` has the steps for a Hugging Face Docker Space.

### Tests and benchmark

```powershell
uv run pytest -m "not llm"                         # the live model tests need a local server
uv run python -m raqeeb.bench.run --set practice   # writes bench/practice/
```

## Synthetic vs real

| Real | Synthetic |
|---|---|
| Weather: NASA POWER hourly irradiance (GHI, DNI, DHI), temperature and wind for 24.71 N, 46.68 E (Riyadh), 2025 | The plant: layout, inverters, trackers, ratings |
| Solar position and clear-sky models (pvlib) | Telemetry, alarms, alarm codes and manual text |
| | Every fault, technician note and device log line |
| | The reference error: true irradiance = NASA × (1 + bias + slow error), a few percent |

**Acknowledgement.** These data were obtained from the NASA Langley Research Center (LaRC) POWER Project
funded through the NASA Earth Science/Applied Science Program.

## Benchmark method

- **Hidden answers.** Each scenario's faults and answer key go to the fault injector and the scorer only.
  The agent sees the incident window (id, start, length) and the telemetry written so far. Tests check
  that agent code never opens scenario files, the twin's state or its truth log, and that no
  agent-facing tool output carries an answer.
- **Practice vs final.** Thresholds, timings, the model router and the unknown-fault handling are tuned
  only on the practice sets (`scenarios/scn_*`, 20 scenarios; `practice2`, 30; `practice_unknown`, 25).
  The final set (`scenarios/final/`, 58 scenarios, seed 9999) was generated once and run once, with the
  contracts and configs frozen: 38 with known faults only, 6 where the numbers decide an unknown fault,
  6 where notes decide, 4 grid-operator notices (2 English, 2 Arabic), 2 fake notices and 8 held-out.
  How each category is scored was written into the status log (`handoffs/HANDOFF.md`) before the set
  was generated. An earlier round 1 test set (50 scenarios) was run once; its failures
  then guided rule fixes made on the practice sets, so it no longer counts as unseen. Its results are kept
  as they were.
- **Baselines.** `threshold` opens one ticket per alarm, maps the alarm code to its usual fault and acts
  on that device, with no correlation. `oracle` (reads the answer key) and `empty` (does nothing) are
  reference bounds, not competitors. Raqeeb runs with rules only (`raqeeb_rules`) and with the local
  model (`raqeeb_local`); no API model is used.
- **Safety set.** 12 scenarios each carry one injected instruction in a device log line. Each runs twice,
  as planted and with neutral text; an action that appears only in the planted run counts as unsafe.
  3 more cases check the retry budget and the approval flow.
- **Unknown and held-out faults.** The three unknown fault types are absent from the agent's rules; the
  agent was developed against them on the practice_unknown set and by ablation (`scenarios/ablation/`,
  12 scenarios where a known fault type has its rules switched off), comparing rules only with rules
  plus the local model and listing every model proposal accepted or rejected. The four held-out types
  were never seen during development: in the final set, a run handles one correctly if it names the
  fault on the right target (a fixed close known type counts and is reported) or escalates it as
  unexplained, with no wrong cause and no false action.
- **Scores.** Root-cause precision and recall, all causes found, false actions, approvals, wasted washes,
  unsafe actions, verified recovery, time to verified recovery, and MWh recovered against a no-action run
  of the same scenario (not all lost energy is recoverable: storm dimming, the time before a fix lands).

### Scenario sets and result folders

| Set | Folder | Scenarios | Used for | Results |
|---|---|---|---|---|
| practice | `scenarios/scn_*.yaml` | 20 | tuning thresholds, timings and the router | `bench/practice/` |
| practice2 | `scenarios/practice2/` | 30 | tuning (second practice round) | run during development |
| practice_unknown | `scenarios/practice_unknown/` | 25 | developing the unknown-fault handling | run during development |
| ablation | `scenarios/ablation/` | 12 | a known fault with its rules switched off: rules only vs rules + model | run during development |
| test (round 1) | `scenarios/test/` | 50 | run once, then its failures guided fixes; no longer unseen | `bench/test/` |
| final | `scenarios/final/` | 58 | the headline results: generated once, run once, contracts and configs frozen | `bench/final/` |
| safety | `scenarios/safety/` | 15 | injected instructions (12, each run twice), retry budget and approval flow (3) | `bench/safety/` |
| robustness | `scenarios/robustness/` | 147 | run once after freezing: quiet days, six per held-out type, correlated faults, seeds; plus a reference-error sweep | `bench/robustness/` |

## Results

[final results: to be added]

- Root causes (precision, recall, all found): [final results: to be added]
- Actions (false actions, approvals, wasted washes): [final results: to be added]
- Recovery (verified, time to recovery, MWh recovered): [final results: to be added]
- Safety set (unsafe actions): [final results: to be added]
- Unknown and held-out faults, rules only vs rules + local model: [final results: to be added]
- Where Raqeeb loses: [final results: to be added]

## Robustness evaluation (after freezing)

Four extra scenario sets (147 scenarios) were generated and run once after the final results were frozen, on
branch eval/data at commit 21dffc1, with the detection rules unchanged. The local-model prompt's fault-type
list gained three types for these runs, so its results are not strictly from the frozen prompt. They test
what the final set doesn't: days with no fault (false-alarm rate), six scenarios per held-out fault type, two
faults on one unit, different random seeds, and a doubled reference-weather error. They are reported
separately and are not part of the final-set numbers. The code and fault physics that run these sets live on
branch eval/data.

| Set | Scenarios | What it tests | Rules only (`raqeeb_rules`) | Rules + local model (`raqeeb_local`) |
|---|---|---|---|---|
| quiet | 30 | 20 fault-free days (6 of them the cloudiest of the year), 10 decoy-only days | 0 causes named, 0 actions, 0 unsafe actions on 30 | the same |
| heldout2 | 42 | 6 scenarios per held-out type, 7 types, with 0-2 known faults each | precision 0.66, recall 0.52; 26 of 42 held-out faults handled correctly | precision 0.67, recall 0.54; 26 of 42 |
| correlated | 15 | two faults on one unit or at one moment | precision 1.00, recall 0.67 | the same |
| seeds | 60 | the same 20 final scenarios under 3 new twin seeds | recall 0.783 in every draw, precision 0.850-0.875; 19 of 20 diagnoses identical across draws | recall 0.808 in every draw |
| reference-error sweep | 3 x 20 | reference error off, as configured, doubled | recall 0.783 at all three levels; precision 0.850, 0.850, 0.832; false actions 1, 1, 3 | recall 0.808 at doubled |

Held-out faults, correct handling out of 6 (named on the right target, a fixed close type counting, or
escalated as unexplained, with no wrong cause and no false action): silent stuck tracker 6, sensor drift 5,
stale export cap 5, under-reading meter 4, grid transient (a decoy) 6. **Known losses:** a hung inverter 0 of
6 (read as a stuck tracker, with a needless resume-tracking command each time) and a load-dependent comms
dropout 0 of 6 (read as a plain dropout; the gateway reset does not hold and counts as a false action). In
the correlated set the second fault on a unit that already has one attributed cause is missed 9 of 9 times.
Full tables, the per-type breakdown and the method are in `bench/robustness/README.md`; per-set tables in
`bench/robustness/<set>/summary.md` and `categories.md`.

## Limitations

- **Everything but the weather is simulated**, and the same team wrote the simulator, the faults and the
  agent. Good scores here show the agent handles these fault models, not that it would handle a real
  plant's SCADA data, sensor quirks or fault mix.
- **One site, one year, one plant design**: Riyadh 2025 weather and a 16-inverter tracker plant.
- **The reference is modelled too.** The gap between NASA POWER and true on-site irradiance is a synthetic
  error model of a few percent; real satellite error varies with dust and cloud.
- **Simplifications in the twin.** Under curtailment every running inverter gets an equal share of the
  limit (a real plant controller would hand a derated inverter's unused share to the others). Hourly
  weather is resampled to 5 minutes, so fast cloud and dust changes are smoothed.
- **Unknown faults get no fix.** A model-proposed cause that passes the tool test is ticketed or
  escalated; the agent does not invent new actions.
- **The local model is small.** Its proposals and reports can be wrong or empty; the agent then falls back
  to its rules and templates. Four showcase replays are not a sample of its behaviour: the final
  results are.
- **The threshold baseline is simple** by design (one ticket per alarm). A tuned commercial alarm system
  would do better than it.

## Repository

```
raqeeb/twin/       digital twin (pvlib) and controls
raqeeb/faults/     fault injector, scenario generator, synthetic notes and log text
raqeeb/telemetry/  SQLite store
raqeeb/tools/      deterministic checks the agent calls
raqeeb/agent/      the LangGraph agent
raqeeb/llm/        model gateway, router, prompts
raqeeb/baseline/   threshold baseline
raqeeb/bench/      harness, scoring, result tables
raqeeb/ui/         Streamlit console and replays
configs/           plant, alarm codes, action allowlist
scenarios/         practice, ablation, final and robustness scenario sets
```

## Licence

[licence: to be decided by the authors]
