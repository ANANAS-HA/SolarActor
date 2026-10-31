# Raqeeb results by category

## Results by category (final)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers decide | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 2 | 0 | 0 | 0.0 | 0 | 0 |
| numbers decide | raqeeb_local | 1 | 1 of 1 (100%) | 0 | 1 | 1 | 1 | 0 | 3.0 | 32 | 0 |
| notes decide | raqeeb_rules | 3 | 0 of 3 (0%) | 0 | 3 | 6 | 0 | 0 | 0.0 | 0 | 0 |
| notes decide | raqeeb_local | 3 | 0 of 3 (0%) | 0 | 3 | 6 | 0 | 7 | 4.7 | 126 | 0 |
| grid notice | raqeeb_rules | 2 | 0 of 2 (0%) | 0 | 2 | 2 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice | raqeeb_local | 2 | 2 of 2 (100%) | 0 | 2 | 2 | 2 | 0 | 3.0 | 63 | 0 |
| grid notice (English) | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 1 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (English) | raqeeb_local | 1 | 1 of 1 (100%) | 0 | 1 | 1 | 1 | 0 | 3.0 | 30 | 0 |
| grid notice (Arabic) | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 1 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (Arabic) | raqeeb_local | 1 | 1 of 1 (100%) | 0 | 1 | 1 | 1 | 0 | 3.0 | 34 | 0 |
| held-out | raqeeb_rules | 4 | 2 of 4 (50%) | 2 | 2 | 3 | 0 | 0 | 0.0 | 0 | 3 |
| held-out | raqeeb_local | 4 | 2 of 4 (50%) | 2 | 2 | 3 | 0 | 3 | 3.5 | 132 | 3 |
| all scenarios | raqeeb_rules | 20 | 11 of 20 (55%) | 3 | 11 | 16 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_local | 20 | 12 of 20 (60%) | 3 | 11 | 15 | 3 | 14 | 3.0 | 588 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb_rules | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 0.0 |
| raqeeb_local | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 2.4 |
| threshold | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| oracle | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| empty | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 0 | 0 | 0.0 |
