# Unknown faults by ablation

Ablation set: a known fault with its rules switched off for the run (see README.md). Synthetic data.

| Agent | Scenarios | Ablated cause found | Escalated | Proposals accepted | Proposals rejected | Precision | Recall (all causes) | False actions | LLM calls / incident | LLM time (s) |
|---|---|---|---|---|---|---|---|---|---|---|
| raqeeb_rules | 12 | 0 of 12 | 12 of 12 | 0 | 0 | 0.57 | 0.52 | 7 | 0.0 | 0 |
| raqeeb_local | 12 | 12 of 12 | 7 of 12 | 13 | 24 | 0.74 | 1.00 | 9 | 4.4 | 2032 |

## Per scenario

| Scenario | Ablated cause | Agent | Found | Escalated | Accepted | Rejected (reason) |
|---|---|---|---|---|---|---|
| abl_0001 | SOILING@ALL | raqeeb_rules | no | yes | - | - |
| abl_0001 | SOILING@ALL | raqeeb_local | yes | yes | SOILING@ALL | TRACKER_STUCK@INV-05: support 0.33, explains 1 unexplained symptoms |
| abl_0002 | SOILING@ALL | raqeeb_rules | no | yes | - | - |
| abl_0002 | SOILING@ALL | raqeeb_local | yes | yes | SOILING@ALL | TRACKER_STUCK@INV-05: decoys and multi-target proposals are left to the rules | TRACKER_STUCK@INV-09: the causal graph finds no key symptom for it, or a symptom rules it out |
| abl_0003 | INVERTER_DERATE@INV-01 | raqeeb_rules | no | yes | - | - |
| abl_0003 | INVERTER_DERATE@INV-01 | raqeeb_local | yes | no | INVERTER_DERATE@INV-01 | SOILING@ALL: the causal graph finds no key symptom for it, or a symptom rules it out | SENSOR_SOILING@WS-01: the causal graph finds no key symptom for it, or a symptom rules it out | TRACKER_STUCK@INV-01: the causal graph finds no key symptom for it, or a symptom rules it out | STRING_OPEN@INV-01: the causal graph finds no key symptom for it, or a symptom rules it out |
| abl_0004 | INVERTER_DERATE@INV-14 | raqeeb_rules | no | yes | - | - |
| abl_0004 | INVERTER_DERATE@INV-14 | raqeeb_local | yes | yes | INVERTER_DERATE@INV-14 | - |
| abl_0005 | COMMS_DROPOUT@INV-02 | raqeeb_rules | no | yes | - | - |
| abl_0005 | COMMS_DROPOUT@INV-02 | raqeeb_local | yes | no | COMMS_DROPOUT@INV-02 | - |
| abl_0006 | COMMS_DROPOUT@INV-12 | raqeeb_rules | no | yes | - | - |
| abl_0006 | COMMS_DROPOUT@INV-12 | raqeeb_local | yes | yes | FIRMWARE_ALARM@INV-16;COMMS_DROPOUT@INV-12 | TRACKER_STUCK@INV-16: support 0.33, explains 1 unexplained symptoms | UNAUTHORIZED_CMD@INV-16: the causal graph finds no key symptom for it, or a symptom rules it out |
| abl_0007 | TRACKER_STUCK@INV-02 | raqeeb_rules | no | yes | - | - |
| abl_0007 | TRACKER_STUCK@INV-02 | raqeeb_local | yes | no | TRACKER_STUCK@INV-02 | SOILING@ALL: the causal graph finds no key symptom for it, or a symptom rules it out | SENSOR_SOILING@WS-01: the causal graph finds no key symptom for it, or a symptom rules it out | INVERTER_DERATE@INV-02: the causal graph finds no key symptom for it, or a symptom rules it out | COMMS_DROPOUT@INV-02: the causal graph finds no key symptom for it, or a symptom rules it out | CURTAILMENT@ALL: decoys and multi-target proposals are left to the rules |
| abl_0008 | TRACKER_STUCK@INV-14 | raqeeb_rules | no | yes | - | - |
| abl_0008 | TRACKER_STUCK@INV-14 | raqeeb_local | yes | no | TRACKER_STUCK@INV-14 | SOILING@ALL: the causal graph finds no key symptom for it, or a symptom rules it out | INVERTER_DERATE@INV-14: the causal graph finds no key symptom for it, or a symptom rules it out | COMMS_DROPOUT@INV-14: the causal graph finds no key symptom for it, or a symptom rules it out | CURTAILMENT@ALL: decoys and multi-target proposals are left to the rules |
| abl_0009 | STRING_OPEN@INV-16 | raqeeb_rules | no | yes | - | - |
| abl_0009 | STRING_OPEN@INV-16 | raqeeb_local | yes | yes | STRING_OPEN@INV-16 | TRACKER_STUCK@INV-16: the causal graph finds no key symptom for it, or a symptom rules it out | INVERTER_DERATE@INV-16: the causal graph finds no key symptom for it, or a symptom rules it out |
| abl_0010 | STRING_OPEN@INV-12 | raqeeb_rules | no | yes | - | - |
| abl_0010 | STRING_OPEN@INV-12 | raqeeb_local | yes | no | STRING_OPEN@INV-12 | SOILING@ALL: the causal graph finds no key symptom for it, or a symptom rules it out | TRACKER_STUCK@INV-12: the causal graph finds no key symptom for it, or a symptom rules it out | INVERTER_DERATE@INV-12: the causal graph finds no key symptom for it, or a symptom rules it out | COMMS_DROPOUT@INV-12: the causal graph finds no key symptom for it, or a symptom rules it out |
| abl_0011 | UNAUTHORIZED_CMD@INV-04 | raqeeb_rules | no | yes | - | - |
| abl_0011 | UNAUTHORIZED_CMD@INV-04 | raqeeb_local | yes | yes | UNAUTHORIZED_CMD@INV-04 | - |
| abl_0012 | UNAUTHORIZED_CMD@INV-09 | raqeeb_rules | no | yes | - | - |
| abl_0012 | UNAUTHORIZED_CMD@INV-09 | raqeeb_local | yes | yes | UNAUTHORIZED_CMD@INV-09 | - |
