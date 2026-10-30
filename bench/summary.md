# Raqeeb benchmark results

All plant data, faults and alarms are **synthetic** (simulated plant twin, NASA POWER weather).

| Agent | Scenarios | Root-cause precision | Root-cause recall | All causes found | False-action rate | Approvals / incident | Verified recovery | Median time to verified recovery (min) | MWh recovered | Share of lost MWh recovered | Cost / incident (USD) | Local LLM calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raqeeb | 20 | 1.00 | 1.00 | 100% | 0% | 1.2 | 100% | 175 | 331.1 | 44% | 0.0000 | n/a |
| threshold | 20 | 0.19 | 0.56 | 30% | 5% | 0.6 | 0% | n/a | 189.5 | 25% | 0.0000 | n/a |
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

## Where raqeeb gets it wrong

- No missed causes, wrong causes or false actions.

## Where threshold gets it wrong

- scn_0001: 1 wrong cause(s): SOILING@INV-11; 1 false action(s)
- scn_0002: 1 wrong cause(s): SOILING@INV-10; 1 false action(s)
- scn_0003: missed SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0004: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-10
- scn_0005: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-05; 1 false action(s)
- scn_0006: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- scn_0007: missed SENSOR_SOILING@WS-01, SOILING@ALL
- scn_0008: 1 wrong cause(s): SOILING@INV-11; 1 false action(s)
- scn_0009: missed SENSOR_SOILING@WS-01
- scn_0010: missed SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-10
- scn_0011: missed SENSOR_SOILING@WS-01, SOILING@ALL
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

## Where raqeeb loses to threshold

- scn_0006: MWh recovered 31.07 vs 35.40
- scn_0012: MWh recovered 13.34 vs 15.43
- scn_0015: MWh recovered 20.81 vs 22.52
- scn_0017: MWh recovered 29.95 vs 33.64
- scn_0020: MWh recovered 39.68 vs 45.63

## Where threshold loses to raqeeb

- scn_0001: precision 0.67 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 1.45; verified recovery not verified vs 145 min
- scn_0002: precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 2.54; verified recovery not verified vs 145 min
- scn_0003: recall 0.33 vs 1.00; MWh recovered 0.00 vs 28.89; verified recovery not verified vs 275 min
- scn_0004: recall 0.50 vs 1.00; precision 0.67 vs 1.00; MWh recovered 0.40 vs 29.89; verified recovery not verified vs 340 min
- scn_0005: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered -0.00 vs 2.69; verified recovery not verified vs 230 min
- scn_0006: recall 0.67 vs 1.00; precision 0.11 vs 1.00; verified recovery not verified vs 210 min
- scn_0007: recall 0.50 vs 1.00; MWh recovered 0.00 vs 18.57; verified recovery not verified vs 225 min
- scn_0008: precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered -0.01 vs 0.40; verified recovery not verified vs 145 min
- scn_0009: recall 0.00 vs 1.00; verified recovery not verified vs 45 min
- scn_0010: recall 0.50 vs 1.00; precision 0.67 vs 1.00; MWh recovered 0.60 vs 46.66; verified recovery not verified vs 215 min
- scn_0011: recall 0.50 vs 1.00; MWh recovered 0.00 vs 22.12; verified recovery not verified vs 245 min
- scn_0012: recall 0.50 vs 1.00; precision 0.06 vs 1.00; verified recovery not verified vs 185 min
- scn_0013: precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 0.90; verified recovery not verified vs 150 min
- scn_0014: precision 0.67 vs 1.00; false actions 1 vs 0; MWh recovered -0.02 vs 1.72; verified recovery not verified vs 140 min
- scn_0015: recall 0.00 vs 1.00; precision 0.00 vs 1.00; verified recovery not verified vs 195 min
- scn_0016: recall 0.67 vs 1.00; precision 0.67 vs 1.00; false actions 1 vs 0; MWh recovered -0.02 vs 2.38; verified recovery not verified vs 220 min
- scn_0017: recall 0.00 vs 1.00; precision 0.00 vs 1.00; verified recovery not verified vs 155 min
- scn_0018: verified recovery not verified vs 30 min
- scn_0019: recall 0.50 vs 1.00; precision 0.06 vs 1.00; MWh recovered 35.89 vs 38.06; verified recovery not verified vs 165 min
- scn_0020: recall 0.00 vs 1.00; precision 0.00 vs 1.00; verified recovery not verified vs 165 min
