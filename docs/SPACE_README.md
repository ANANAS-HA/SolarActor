---
title: Solar Actor console
emoji: ☀️
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Solar plant fault recovery agent on a synthetic plant
---

# Solar Actor console

An agent that diagnoses and recovers a simulated solar plant when several faults happen at once
(Misk x HUMAIN "Future AI Builders" hackathon, Challenge 1).

**Synthetic plant and faults; weather data from NASA POWER (2025, Riyadh).** The plant, its telemetry,
the faults, the alarm codes and all log and note text are simulated.

Modes in this demo:

- **Live: rules only.** The real agent runs step by step on a practice scenario or a random dust storm.
  Actions that need a human (power limits, isolation) pause the run for Approve / Reject.
- **Replay.** Four runs of the agent with a local language model, replayed from the frozen final set
  (labelled "replayed from the frozen set from commit final-trace, agent unchanged"), with the model's
  prompts and replies. The Trace tab shows every turn: checks, symptoms, candidate causes, model calls,
  each proposal and how the agent tested it, actions and the safety gate's decision.

The local-model mode is switched off here: this Space has no model server and no API keys.
The planted faults (answer key) are shown only after a run has ended.

Source and benchmark: see the project repository.
