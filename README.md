# Raqeeb

Raqeeb is an AI agent that helps recover a solar power plant when several faults happen at once. It reads plant telemetry and alarms, works out which faults are root causes and which alarms are only symptoms, applies safe and reversible fixes, asks a human before risky ones, and checks that output has returned to the expected level.

Built for the Misk x HUMAIN "Future AI Builders" hackathon, Challenge 1: *Agentic Recovery of a Failing System*. AI agents for solar operations already exist. This project adds compound-fault desert scenarios, recovery that is verified against a digital twin, an open benchmark against a threshold-alarm baseline, and low compute use (a local model by default, a large API model only when needed).

> **Synthetic data notice.** The plant, its telemetry, the faults, the alarm codes and the alarm manual text are all **synthetic**. Weather comes from NASA POWER for a real Saudi location. Plant output is simulated from that weather with pvlib.

## Known simplifications

- **Curtailment split.** When the grid operator limits the plant, the twin gives every running inverter an equal share of the limit. A real plant controller would hand a derated inverter's unused share to the others, so a scenario with both curtailment and a derated inverter sits slightly below the grid limit.

## Benchmark

```powershell
uv run python -m raqeeb.bench.run            # all scenarios; --agents threshold,oracle,empty --scenarios scn_0001
```

Each synthetic scenario runs through the twin and fault injector. The agent sees only the incident window and the telemetry written so far. The harness gates, approves (a simulated operator approves every gated action and the count is reported) and executes the agent's actions on the twin, then scores the result against the hidden answer key. Output: `bench/results.csv` (one row per agent and scenario), `bench/summary.csv` and `bench/summary.md` (the table, every wrong or missed cause, and where each agent loses head-to-head).

- **threshold**: the baseline. It opens one ticket per alarm, maps the alarm code to its primary fault type, acts on the alarm's device and does no correlation.
- **oracle** and **empty**: reference bounds, not competitors. The oracle reads the answer key; the empty agent does nothing.

The baseline catches comms dropouts and derates from their own alarms. It names plant-wide soiling as 16 separate per-inverter causes, and it misses soiling entirely when the irradiance sensor is dusty too (the trap). MWh recovered is measured against a no-action run of the same seed; not all lost energy is recoverable (storm dimming, time before a fix lands).

## Status

Phase 0 (project setup and shared contracts). See `handoffs/HANDOFF.md` for the full plan.

## Quick start (Windows, PowerShell)

```powershell
uv sync
copy .env.example .env   # then fill in the values
uv run python scripts/download_weather.py
uv run pytest
uv run python -m raqeeb.llm.smoke   # needs the local LLM server running
```
