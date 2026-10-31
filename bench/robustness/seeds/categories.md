# Raqeeb results by category

## Results by category (seeds)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers decide | raqeeb_rules | 3 | 0 of 3 (0%) | 0 | 3 | 9 | 0 | 0 | 0.0 | 0 | 0 |
| numbers decide | raqeeb_local | 3 | 3 of 3 (100%) | 0 | 3 | 4 | 3 | 0 | 3.0 | 94 | 0 |
| notes decide | raqeeb_rules | 9 | 0 of 9 (0%) | 0 | 9 | 17 | 0 | 0 | 0.0 | 0 | 0 |
| notes decide | raqeeb_local | 9 | 0 of 9 (0%) | 0 | 9 | 17 | 0 | 21 | 4.6 | 362 | 0 |
| grid notice | raqeeb_rules | 6 | 0 of 6 (0%) | 0 | 6 | 6 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice | raqeeb_local | 6 | 6 of 6 (100%) | 0 | 6 | 6 | 6 | 0 | 3.0 | 194 | 0 |
| grid notice (English) | raqeeb_rules | 3 | 0 of 3 (0%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (English) | raqeeb_local | 3 | 3 of 3 (100%) | 0 | 3 | 3 | 3 | 0 | 3.0 | 90 | 0 |
| grid notice (Arabic) | raqeeb_rules | 3 | 0 of 3 (0%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (Arabic) | raqeeb_local | 3 | 3 of 3 (100%) | 0 | 3 | 3 | 3 | 0 | 3.0 | 105 | 0 |
| held-out | raqeeb_rules | 12 | 9 of 12 (75%) | 3 | 5 | 5 | 0 | 0 | 0.0 | 0 | 7 |
| held-out | raqeeb_local | 12 | 9 of 12 (75%) | 3 | 5 | 5 | 0 | 6 | 2.7 | 277 | 7 |
| all scenarios | raqeeb_rules | 60 | 36 of 60 (60%) | 3 | 27 | 41 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_local | 60 | 39 of 60 (65%) | 3 | 27 | 36 | 9 | 32 | 2.8 | 1616 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb_rules | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 0.0 |
| raqeeb_local | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 2.4 |
| threshold | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| oracle | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| empty | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 0 | 0 | 0.0 |
