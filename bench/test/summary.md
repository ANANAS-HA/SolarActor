# Raqeeb benchmark results

All plant data, faults and alarms are **synthetic** (simulated plant twin, NASA POWER weather).

| Agent | Scenarios | Root-cause precision | Root-cause recall | All causes found | False-action rate | Approvals / incident | Wasted washes | Verified recovery | Median time to verified recovery (min) | MWh recovered | Share of lost MWh recovered | Cost / incident (USD) | Local LLM calls |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| raqeeb | 50 | 0.93 | 0.98 | 96% | 4% | 1.0 | 1 | 92% | 190 | 747.9 | 45% | 0.0000 | n/a |
| threshold | 50 | 0.31 | 0.53 | 24% | 11% | 0.6 | 2 | 0% | n/a | 259.4 | 16% | 0.0000 | n/a |
| oracle | 50 | 1.00 | 1.00 | 100% | 0% | 0.7 | 0 | 100% | 132 | 806.6 | 48% | 0.0000 | n/a |
| empty | 50 | n/a | 0.00 | 0% | n/a | 0.0 | 0 | 0% | n/a | 0.0 | 0% | 0.0000 | n/a |

- **oracle**: reference upper bound: reads the answer key
- **empty**: reference lower bound: does nothing

n/a: undefined (no predictions, no actions, no verified recovery or no LLM calls).
MWh recovered is measured against a no-action run of the same scenario and seed; it can be
negative when an action costs output (e.g. a power limit below what the inverter could give).
Lost MWh (no real fault vs no action) is not all recoverable: storm dimming, output before a
fix lands and derates that no core action repairs are lost to every agent.
A plant-wide cause (SOILING@ALL) counts an action on any single inverter as addressing it.
Wasted washes: cleaning orders that finish while the dust storm still blows (dust settles again).

## Where raqeeb gets it wrong

- tst_0006: 1 wrong cause(s): STRING_OPEN@INV-10
- tst_0007: missed STRING_OPEN@INV-16
- tst_0008: 1 wrong cause(s): STRING_OPEN@INV-15
- tst_0011: 1 wrong cause(s): STRING_OPEN@INV-05
- tst_0025: missed STRING_OPEN@INV-04
- tst_0026: 1 wrong cause(s): SOILING@ALL; 1 false action(s)
- tst_0031: 1 wrong cause(s): STRING_OPEN@INV-03
- tst_0032: 1 wrong cause(s): STRING_OPEN@INV-05
- tst_0036: 1 wrong cause(s): SOILING@ALL; 2 false action(s)
- tst_0040: 1 wrong cause(s): SOILING@ALL; 1 false action(s)
- tst_0045: 1 wrong cause(s): SOILING@ALL; 2 false action(s)

## Where threshold gets it wrong

- tst_0002: 1 wrong cause(s): SOILING@INV-15; 1 false action(s)
- tst_0003: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0004: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0005: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0006: missed STRING_OPEN@INV-12; 1 wrong cause(s): SOILING@INV-12; 1 false action(s)
- tst_0007: missed INVERTER_DERATE@INV-10, SENSOR_SOILING@WS-01, SOILING@ALL; 1 wrong cause(s): SOILING@INV-16
- tst_0008: 1 wrong cause(s): SOILING@INV-07; 1 false action(s)
- tst_0009: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0010: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0011: 1 wrong cause(s): SOILING@INV-14; 1 false action(s)
- tst_0013: 1 wrong cause(s): SOILING@INV-08; 1 false action(s)
- tst_0015: 1 wrong cause(s): SOILING@INV-15; 1 false action(s)
- tst_0016: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0017: missed SENSOR_SOILING@WS-01, SOILING@ALL, STRING_OPEN@INV-13; 1 wrong cause(s): SOILING@INV-13
- tst_0018: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0019: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0020: missed SOILING@ALL; 15 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0021: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0022: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0023: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-11; 1 false action(s)
- tst_0024: missed SENSOR_SOILING@WS-01, STRING_OPEN@INV-01; 1 wrong cause(s): SOILING@INV-01; 1 false action(s)
- tst_0025: missed SOILING@ALL, STRING_OPEN@INV-04; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0026: missed STRING_OPEN@INV-13; 2 wrong cause(s): SOILING@INV-06, SOILING@INV-13; 2 false action(s)
- tst_0028: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-06; 1 false action(s)
- tst_0029: missed SENSOR_SOILING@WS-01
- tst_0030: missed SOILING@ALL; 16 wrong cause(s): SOILING@INV-01, SOILING@INV-02, SOILING@INV-03, SOILING@INV-04 ...
- tst_0031: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0033: missed SENSOR_SOILING@WS-01
- tst_0034: missed STRING_OPEN@INV-14; 1 wrong cause(s): SOILING@INV-14; 1 false action(s)
- tst_0035: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0036: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-09; 1 false action(s)
- tst_0037: missed STRING_OPEN@INV-10; 1 wrong cause(s): SOILING@INV-10; 1 false action(s)
- tst_0038: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-04; 1 false action(s)
- tst_0039: missed SENSOR_SOILING@WS-01; 1 wrong cause(s): SOILING@INV-06; 1 false action(s)
- tst_0040: missed STRING_OPEN@INV-06; 1 wrong cause(s): SOILING@INV-06; 1 false action(s)
- tst_0041: missed STRING_OPEN@INV-09; 1 wrong cause(s): SOILING@INV-09; 1 false action(s)
- tst_0042: missed STRING_OPEN@INV-12; 1 wrong cause(s): SOILING@INV-12; 1 false action(s)
- tst_0043: missed STRING_OPEN@INV-03; 1 wrong cause(s): SOILING@INV-03; 1 false action(s)
- tst_0044: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0046: missed SENSOR_SOILING@WS-01
- tst_0047: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0048: missed SENSOR_SOILING@WS-01
- tst_0049: missed SENSOR_SOILING@WS-01, STRING_OPEN@INV-09; 2 wrong cause(s): SOILING@INV-09, SOILING@INV-12; 2 false action(s)

## Where oracle gets it wrong

- No missed causes, wrong causes or false actions.

## Where empty gets it wrong

- tst_0001: missed COMMS_DROPOUT@INV-15, TRACKER_STUCK@INV-09
- tst_0002: missed INVERTER_DERATE@INV-15
- tst_0003: missed SENSOR_SOILING@WS-01, SOILING@ALL, UNAUTHORIZED_CMD@INV-06
- tst_0004: missed COMMS_DROPOUT@INV-10, INVERTER_DERATE@INV-13, SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0005: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0006: missed STRING_OPEN@INV-12, TRACKER_STUCK@INV-10
- tst_0007: missed INVERTER_DERATE@INV-10, SENSOR_SOILING@WS-01, SOILING@ALL, STRING_OPEN@INV-16
- tst_0008: missed INVERTER_DERATE@INV-07, TRACKER_STUCK@INV-15
- tst_0009: missed SOILING@ALL, TRACKER_STUCK@INV-16
- tst_0010: missed SOILING@ALL
- tst_0011: missed INVERTER_DERATE@INV-14, TRACKER_STUCK@INV-05
- tst_0012: missed COMMS_DROPOUT@INV-10, UNAUTHORIZED_CMD@INV-01
- tst_0013: missed COMMS_DROPOUT@INV-15, INVERTER_DERATE@INV-08
- tst_0014: missed COMMS_DROPOUT@INV-07
- tst_0015: missed INVERTER_DERATE@INV-15
- tst_0016: missed SOILING@ALL
- tst_0017: missed SENSOR_SOILING@WS-01, SOILING@ALL, STRING_OPEN@INV-13, UNAUTHORIZED_CMD@INV-07
- tst_0018: missed SOILING@ALL
- tst_0019: missed INVERTER_DERATE@INV-05, SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0020: missed SOILING@ALL, UNAUTHORIZED_CMD@INV-07
- tst_0021: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0022: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0023: missed SENSOR_SOILING@WS-01, TRACKER_STUCK@INV-11, UNAUTHORIZED_CMD@INV-08
- tst_0024: missed COMMS_DROPOUT@INV-16, SENSOR_SOILING@WS-01, STRING_OPEN@INV-01
- tst_0025: missed INVERTER_DERATE@INV-15, SOILING@ALL, STRING_OPEN@INV-04
- tst_0026: missed COMMS_DROPOUT@INV-03, INVERTER_DERATE@INV-06, STRING_OPEN@INV-13, UNAUTHORIZED_CMD@INV-11
- tst_0027: missed TRACKER_STUCK@INV-08
- tst_0028: missed INVERTER_DERATE@INV-06, SENSOR_SOILING@WS-01, TRACKER_STUCK@INV-15, UNAUTHORIZED_CMD@INV-14
- tst_0029: missed SENSOR_SOILING@WS-01, TRACKER_STUCK@INV-05
- tst_0030: missed SOILING@ALL, TRACKER_STUCK@INV-03
- tst_0031: missed COMMS_DROPOUT@INV-07, SENSOR_SOILING@WS-01, SOILING@ALL, TRACKER_STUCK@INV-03
- tst_0032: missed COMMS_DROPOUT@INV-03, TRACKER_STUCK@INV-05
- tst_0033: missed SENSOR_SOILING@WS-01, UNAUTHORIZED_CMD@INV-04
- tst_0034: missed COMMS_DROPOUT@INV-13, STRING_OPEN@INV-14
- tst_0035: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0036: missed INVERTER_DERATE@INV-09, SENSOR_SOILING@WS-01, TRACKER_STUCK@INV-08, UNAUTHORIZED_CMD@INV-14
- tst_0037: missed COMMS_DROPOUT@INV-08, STRING_OPEN@INV-10
- tst_0038: missed INVERTER_DERATE@INV-04, SENSOR_SOILING@WS-01
- tst_0039: missed INVERTER_DERATE@INV-06, SENSOR_SOILING@WS-01, TRACKER_STUCK@INV-14, UNAUTHORIZED_CMD@INV-13
- tst_0040: missed COMMS_DROPOUT@INV-01, STRING_OPEN@INV-06, UNAUTHORIZED_CMD@INV-09
- tst_0041: missed STRING_OPEN@INV-09
- tst_0042: missed STRING_OPEN@INV-12, UNAUTHORIZED_CMD@INV-05
- tst_0043: missed STRING_OPEN@INV-03
- tst_0044: missed SENSOR_SOILING@WS-01, SOILING@ALL
- tst_0045: missed UNAUTHORIZED_CMD@INV-05
- tst_0046: missed SENSOR_SOILING@WS-01, UNAUTHORIZED_CMD@INV-13
- tst_0047: missed SENSOR_SOILING@WS-01, SOILING@ALL, TRACKER_STUCK@INV-13, UNAUTHORIZED_CMD@INV-12
- tst_0048: missed COMMS_DROPOUT@INV-11, INVERTER_DERATE@INV-09, SENSOR_SOILING@WS-01
- tst_0049: missed INVERTER_DERATE@INV-12, SENSOR_SOILING@WS-01, STRING_OPEN@INV-09, UNAUTHORIZED_CMD@INV-03
- tst_0050: missed COMMS_DROPOUT@INV-16, UNAUTHORIZED_CMD@INV-09

## Where raqeeb loses to threshold

- tst_0001: MWh recovered 1.01 vs 1.03
- tst_0009: MWh recovered 24.61 vs 24.74
- tst_0016: MWh recovered 39.82 vs 40.83
- tst_0018: MWh recovered 33.89 vs 35.21
- tst_0027: MWh recovered 1.56 vs 1.60
- tst_0029: MWh recovered 1.54 vs 1.57
- tst_0030: MWh recovered 19.11 vs 19.80
- tst_0031: precision 0.80 vs 1.00
- tst_0032: precision 0.67 vs 1.00; MWh recovered 1.52 vs 1.71
- tst_0036: false actions 2 vs 1
- tst_0045: precision 0.50 vs 1.00; false actions 2 vs 0
- tst_0047: wasted washes 1 vs 0

## Where threshold loses to raqeeb

- tst_0001: verified recovery not verified vs 175 min
- tst_0002: precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 2.19; verified recovery not verified vs 140 min
- tst_0003: recall 0.33 vs 1.00; MWh recovered 0.00 vs 25.69; verified recovery not verified vs 225 min
- tst_0004: recall 0.50 vs 1.00; MWh recovered -0.02 vs 45.63; verified recovery not verified vs 230 min
- tst_0005: recall 0.00 vs 1.00; MWh recovered 0.00 vs 32.05; verified recovery not verified vs 250 min
- tst_0006: recall 0.50 vs 1.00; precision 0.50 vs 0.67; false actions 1 vs 0; MWh recovered 1.84 vs 3.24; verified recovery not verified vs 395 min
- tst_0007: recall 0.25 vs 0.75; precision 0.50 vs 1.00; MWh recovered 4.47 vs 50.99
- tst_0008: false actions 1 vs 0; MWh recovered 2.11 vs 5.78; verified recovery not verified vs 190 min
- tst_0009: recall 0.50 vs 1.00; precision 0.06 vs 1.00; verified recovery not verified vs 145 min
- tst_0010: recall 0.00 vs 1.00; precision 0.00 vs 1.00; verified recovery not verified vs 120 min
- tst_0011: false actions 1 vs 0; MWh recovered 1.36 vs 2.82; verified recovery not verified vs 200 min
- tst_0012: verified recovery not verified vs 145 min
- tst_0013: precision 0.67 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 1.89; verified recovery not verified vs 185 min
- tst_0014: verified recovery not verified vs 30 min
- tst_0015: precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered -0.03 vs 2.72; verified recovery not verified vs 140 min
- tst_0016: recall 0.00 vs 1.00; precision 0.00 vs 1.00; wasted washes 1 vs 0; verified recovery not verified vs 150 min
- tst_0017: recall 0.25 vs 1.00; precision 0.50 vs 1.00; wasted washes 1 vs 0; MWh recovered 1.68 vs 39.18; verified recovery not verified vs 190 min
- tst_0018: recall 0.00 vs 1.00; precision 0.00 vs 1.00; verified recovery not verified vs 160 min
- tst_0019: recall 0.33 vs 1.00; MWh recovered -0.00 vs 25.79; verified recovery not verified vs 255 min
- tst_0020: recall 0.50 vs 1.00; precision 0.06 vs 1.00; MWh recovered 23.86 vs 26.34; verified recovery not verified vs 120 min
- tst_0021: recall 0.00 vs 1.00; MWh recovered 0.00 vs 40.38; verified recovery not verified vs 140 min
- tst_0022: recall 0.00 vs 1.00; MWh recovered 0.00 vs 54.55; verified recovery not verified vs 125 min
- tst_0023: recall 0.67 vs 1.00; precision 0.67 vs 1.00; false actions 1 vs 0
- tst_0024: recall 0.33 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 1.88; verified recovery not verified vs 450 min
- tst_0025: recall 0.33 vs 0.67; precision 0.06 vs 1.00; MWh recovered 41.04 vs 43.89
- tst_0026: recall 0.75 vs 1.00; precision 0.60 vs 0.80; false actions 2 vs 1; MWh recovered -0.01 vs 4.20; verified recovery not verified vs 310 min
- tst_0027: verified recovery not verified vs 45 min
- tst_0028: recall 0.75 vs 1.00; precision 0.75 vs 1.00; false actions 1 vs 0; MWh recovered 1.96 vs 4.39; verified recovery not verified vs 150 min
- tst_0029: recall 0.50 vs 1.00; verified recovery not verified vs 75 min
- tst_0030: recall 0.50 vs 1.00; precision 0.06 vs 1.00; verified recovery not verified vs 235 min
- tst_0031: recall 0.50 vs 1.00; MWh recovered 2.16 vs 36.55; verified recovery not verified vs 260 min
- tst_0032: verified recovery not verified vs 230 min
- tst_0033: recall 0.50 vs 1.00; verified recovery not verified vs 275 min
- tst_0034: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 1.94; verified recovery not verified vs 190 min
- tst_0035: recall 0.00 vs 1.00; MWh recovered 0.00 vs 49.12; verified recovery not verified vs 125 min
- tst_0036: recall 0.75 vs 1.00; precision 0.75 vs 0.80; MWh recovered 1.14 vs 2.45; verified recovery not verified vs 360 min
- tst_0037: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 1.11; verified recovery not verified vs 395 min
- tst_0038: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 0.90; verified recovery not verified vs 230 min
- tst_0039: recall 0.75 vs 1.00; precision 0.75 vs 1.00; false actions 1 vs 0; MWh recovered 1.42 vs 2.51; verified recovery not verified vs 335 min
- tst_0040: recall 0.67 vs 1.00; precision 0.67 vs 0.75; MWh recovered 0.00 vs 1.12; verified recovery not verified vs 390 min
- tst_0041: recall 0.00 vs 1.00; precision 0.00 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 0.71; verified recovery not verified vs 165 min
- tst_0042: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 4.10; verified recovery not verified vs 165 min
- tst_0043: recall 0.00 vs 1.00; precision 0.00 vs 1.00; false actions 1 vs 0; MWh recovered 0.00 vs 2.05; verified recovery not verified vs 165 min
- tst_0044: recall 0.00 vs 1.00; MWh recovered 0.00 vs 20.31; verified recovery not verified vs 225 min
- tst_0045: verified recovery not verified vs 40 min
- tst_0046: recall 0.50 vs 1.00; verified recovery not verified vs 215 min
- tst_0047: recall 0.50 vs 1.00; MWh recovered 0.95 vs 31.08
- tst_0048: recall 0.67 vs 1.00; MWh recovered 0.00 vs 0.60; verified recovery not verified vs 160 min
- tst_0049: recall 0.50 vs 1.00; precision 0.50 vs 1.00; false actions 2 vs 0; MWh recovered -0.04 vs 7.64; verified recovery not verified vs 280 min
- tst_0050: verified recovery not verified vs 225 min
