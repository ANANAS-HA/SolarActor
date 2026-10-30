# Handoff: held-out faults (2026-10-30)

For the agent session ("Session A"). These faults are in the simulator and the injector only. There are no detection rules for them; detection is yours.

## Done (each with an injector entry, an answer-key label, a telemetry-signature test and a recovery test in `tests/test_heldout_faults.py`)

| Fault | Challenge wording | Signature | Recovery |
|---|---|---|---|
| `HUNG_INVERTER@INV-xx` | a workflow that hangs forever | AC output zero (meter drops by its share); rows keep arriving, frozen at the last values, status RUNNING; no COMM_LOSS or REMOTE_STOP | `reset_comms_gateway` (back after 5 min) or `dispatch_technician`; otherwise never |
| `COMMS_DROPOUT_LOAD@INV-xx` | loses data under load | rows missing only while true GHI > 1000 x (1 - severity) W/m2 (default 700); COMM_LOSS on each drop; output unchanged | `dispatch_technician`; `reset_comms_gateway` does not hold |
| `SENSOR_DRIFT@WS-01` | slowly corrupts data | sensor reads x (1 + bias), bias += severity per hour (default 0.03), sign drawn per run; plant unchanged | `dispatch_technician` recalibrates; `flag_sensor_use_reference` counts as mitigated in the bench but the sensor keeps drifting |
| `TRACKER_STUCK_SILENT@INV-xx` | stuck tracker | tracker angle frozen at its angle at `start` (not stow), no TRACKER_FAULT; loss grows as the sun moves away | `resume_tracking` or `dispatch_technician` |

Each fault also keeps a hidden `record` (start, end, threshold / drift rate / frozen angle) for the bench and tests.

## Not done

- No detection rules, causal-graph nodes or fix ladders (on purpose).
- No generated scenario set uses them. The committed sets are unchanged (`FULL_SCOPE_FAULTS` excludes them), and no `--heldout` option exists in the generator yet.
- `bench/score.unknown_counts` still counts only `UNKNOWN_FAULTS`.
- `configs/actions.yaml` descriptions are unchanged. `reset_comms_gateway` now also restarts a hung controller, but its description does not say so.
- The weather station cannot have a load dropout (inverters only).
- Decision for you: `llm/router.PROPOSABLE` is built from every `FaultType`, so the model can already propose the four new types by name. I left it unchanged. If they should stay fully unknown to the model, exclude `HELDOUT_FAULTS` there. The live local-model test (`test_live_local_model_proposes_and_reports`) failed once in a full run and passed on rerun; I did not check whether the new types play a part.

## Contract changes (`raqeeb/contracts.py`)

- `FaultType`: `HUNG_INVERTER`, `COMMS_DROPOUT_LOAD`, `SENSOR_DRIFT`, `TRACKER_STUCK_SILENT`. New `HELDOUT_FAULTS`; `FULL_SCOPE_FAULTS` now excludes it.
- `InverterState.hung`, `InverterState.comms_load_limit_wm2`, `WeatherStationState.sensor_bias`, `WeatherStationState.sensor_drift_per_h`. All defaults leave the twin byte-identical.
- Twin: a hung inverter repeats its last sent reading; a device is offline while GHI is above `comms_load_limit_wm2`; `sensor_bias` grows by `sensor_drift_per_h` each step.
- Controls: `reset_comms_gateway` clears `hung`; `dispatch_technician` clears `hung`, the load limit and the sensor bias and drift.
- Bench: `MITIGATED`, `ACTION_FIXES` and the oracle's `FIX` cover the four types. `tests/test_agent.py` coverage test excludes `HELDOUT_FAULTS`.
