# Raqeeb

Raqeeb is an AI agent that helps recover a solar power plant when several faults happen at once. It reads plant telemetry and alarms, works out which faults are root causes and which alarms are only symptoms, applies safe and reversible fixes, asks a human before risky ones, and checks that output has returned to the expected level.

Built for the Misk x HUMAIN "Future AI Builders" hackathon, Challenge 1: *Agentic Recovery of a Failing System*. AI agents for solar operations already exist. This project adds compound-fault desert scenarios, recovery that is verified against a digital twin, an open benchmark against a threshold-alarm baseline, and low compute use (a local model by default, a large API model only when needed).

> **Synthetic data notice.** The plant, its telemetry, the faults, the alarm codes and the alarm manual text are all **synthetic**. Weather comes from NASA POWER for a real Saudi location. Plant output is simulated from that weather with pvlib.

## Known simplifications

- **Curtailment split.** When the grid operator limits the plant, the twin gives every running inverter an equal share of the limit. A real plant controller would hand a derated inverter's unused share to the others, so a scenario with both curtailment and a derated inverter sits slightly below the grid limit.

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
