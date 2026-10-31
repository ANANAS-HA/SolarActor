---
title: Raqeeb console
emoji: ☀️
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Solar plant fault recovery agent on a synthetic plant
---

# Raqeeb console

An agent that diagnoses and recovers a simulated solar plant when several faults happen at once
(Misk x HUMAIN "Future AI Builders" hackathon, Challenge 1).

**Synthetic plant and faults; weather data from NASA POWER (2025, Riyadh).** The plant, its telemetry,
the faults, the alarm codes and all log and note text are simulated.

Modes in this demo:

- **Live: rules only.** The real agent runs step by step on a practice scenario or a random dust storm.
  Actions that need a human (power limits, isolation) pause the run for Approve / Reject.
- **Replay.** Runs recorded earlier, including runs with a language model. Each replay is labelled
  with how it was recorded; "recorded with test model" means a scripted stand-in, not a language model
  (its cause proposals come from a fixed rule, to show how the agent tests a proposal before using it).

The local-model mode is switched off here: this Space has no model server and no API keys.
The planted faults (answer key) are shown only after a run has ended.

Source and benchmark: see the project repository.
