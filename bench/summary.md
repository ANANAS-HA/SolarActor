# Raqeeb benchmark results

Generated 2026-10-29 22:07 UTC.

All plant data, faults and alarms are **synthetic** (simulated plant twin, NASA POWER weather).

| Agent | Scenarios | Root-cause precision | Root-cause recall | All causes found | False-action rate | Approvals / incident | Verified recovery | Median time to verified recovery (min) | MWh recovered | Share of lost MWh recovered | Cost / incident (USD) | Local LLM calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| threshold | 20 | 0.18 | 0.56 | 30% | 5% | 0.6 | 0% | n/a | 164.3 | 22% | 0.0000 | n/a |
| oracle | 20 | 1.00 | 1.00 | 100% | 0% | 0.6 | 100% | 108 | 385.3 | 51% | 0.0000 | n/a |
| empty | 20 | n/a | 0.00 | 0% | n/a | 0.0 | 0% | n/a | 0.0 | 0% | 0.0000 | n/a |

- **oracle**: reference upper bound: reads the answer key
- **empty**: reference lower bound: does nothing

n/a: undefined (no predictions, no actions, no verified recovery or no LLM calls).
MWh recovered is measured against a no-action run of the same scenario and seed; it can be
negative when an action costs output (e.g. a power limit below what the inverter could give).
Lost MWh (no real fault vs no action) is not all recoverable: storm dimming, output before a
fix lands and derates that no core action repairs are lost to every agent.
A plant-wide cause (SOILING@ALL) counts an action on any single inverter as addressing it.

## Where threshold gets it wrong

- scn_0001: 1 wrong cause(s): SOILING@INV-11; 1 false action(s)
- scn_0002: 1 wrong cause(s): SOILING@INV-10; 1 false action(s)
- scn_0003: missed SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0004: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-10
- scn_0005: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-05; 1 false action(s)
- scn_0006: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0007: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-04
- scn_0008: 1 wrong cause(s): SOILING@INV-11; 1 false action(s)
- scn_0009: missed SENSOR_SOILING@WS-01
- scn_0010: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-10
- scn_0011: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-06
- scn_0012: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0013: 1 wrong cause(s): SOILING@INV-15; 1 false action(s)
- scn_0014: 1 wrong cause(s): SOILING@INV-05; 1 false action(s)
- scn_0015: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0016: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-16; 1 false action(s)
- scn_0017: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0019: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0020: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...

## Where oracle gets it wrong

- No missed causes, wrong causes or false actions.

## Where empty gets it wrong

- scn_0001: missed COMMS_DROPOUT@INV-08, INVERTER_DERATE@INV-11
- scn_0002: missed INVERTER_DERATE@INV-10
- scn_0003: missed COMMS_DROPOUT@INV-16, SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0004: missed COMMS_DROPOUT@INV-13, INVERTER_DERATE@INV-10, SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0005: missed INVERTER_DERATE@INV-05, SENSOR_SOILING@WS-01
- scn_0006: missed COMMS_DROPOUT@INV-11, INVERTER_DERATE@INV-04, SOILING@ALL
- scn_0007: missed COMMS_DROPOUT@INV-10, INVERTER_DERATE@INV-04, SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0008: missed INVERTER_DERATE@INV-11
- scn_0009: missed SENSOR_SOILING@WS-01
- scn_0010: missed COMMS_DROPOUT@INV-15, INVERTER_DERATE@INV-10, SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0011: missed COMMS_DROPOUT@INV-11, INVERTER_DERATE@INV-06, SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0012: missed COMMS_DROPOUT@INV-02, SOILING@ALL
- scn_0013: missed INVERTER_DERATE@INV-15
- scn_0014: missed COMMS_DROPOUT@INV-12, INVERTER_DERATE@INV-05
- scn_0015: missed SOILING@ALL
- scn_0016: missed COMMS_DROPOUT@INV-08, INVERTER_DERATE@INV-16, SENSOR_SOILING@WS-01
- scn_0017: missed SOILING@ALL
- scn_0018: missed COMMS_DROPOUT@INV-12
- scn_0019: missed COMMS_DROPOUT@INV-09, SOILING@ALL
- scn_0020: missed SOILING@ALL
