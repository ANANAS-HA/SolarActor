# Raqeeb results by category

## Results by category (correlated)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| held-out 2 | raqeeb_rules | 3 | 3 of 3 (100%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | raqeeb_local | 3 | 3 of 3 (100%) | 0 | 3 | 3 | 0 | 7 | 5.7 | 152 | 0 |
| held-out 2 | threshold | 3 | 0 of 3 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | oracle | 3 | 3 of 3 (100%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | empty | 3 | 0 of 3 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_rules | 15 | 6 of 15 (40%) | 0 | 6 | 6 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_local | 15 | 6 of 15 (40%) | 0 | 6 | 6 | 0 | 24 | 4.0 | 513 | 0 |
| all scenarios | threshold | 15 | 0 of 15 (0%) | 6 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | oracle | 15 | 15 of 15 (100%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | empty | 15 | 0 of 15 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb_rules | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 0.0 |
| raqeeb_local | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 2.4 |
| threshold | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| oracle | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| empty | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 0 | 0 | 0.0 |
