# Raqeeb results by category

## Results by category (heldout2)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| held-out | raqeeb_rules | 24 | 11 of 24 (46%) | 13 | 10 | 18 | 0 | 0 | 0.0 | 0 | 15 |
| held-out | raqeeb_local | 24 | 11 of 24 (46%) | 13 | 9 | 16 | 1 | 19 | 2.8 | 611 | 15 |
| held-out | threshold | 24 | 0 of 24 (0%) | 91 | 0 | 0 | 0 | 0 | 0.0 | 0 | 6 |
| held-out | oracle | 24 | 24 of 24 (100%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| held-out | empty | 24 | 0 of 24 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | raqeeb_rules | 12 | 9 of 12 (75%) | 0 | 9 | 10 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | raqeeb_local | 12 | 9 of 12 (75%) | 0 | 9 | 10 | 0 | 17 | 4.9 | 327 | 0 |
| held-out 2 | threshold | 12 | 0 of 12 (0%) | 4 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | oracle | 12 | 12 of 12 (100%) | 0 | 12 | 12 | 0 | 0 | 0.0 | 0 | 0 |
| held-out 2 | empty | 12 | 0 of 12 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| grid transient | raqeeb_rules | 6 | 6 of 6 (100%) | 0 | 3 | 3 | 0 | 0 | 0.0 | 0 | 0 |
| grid transient | raqeeb_local | 6 | 6 of 6 (100%) | 0 | 3 | 3 | 0 | 5 | 2.8 | 142 | 0 |
| grid transient | threshold | 6 | 5 of 6 (83%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| grid transient | oracle | 6 | 6 of 6 (100%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| grid transient | empty | 6 | 6 of 6 (100%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_rules | 42 | 6 of 42 (14%) | 13 | 22 | 31 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_local | 42 | 6 of 42 (14%) | 13 | 21 | 29 | 1 | 41 | 3.4 | 1080 | 0 |
| all scenarios | threshold | 42 | 3 of 42 (7%) | 95 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | oracle | 42 | 42 of 42 (100%) | 0 | 12 | 12 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | empty | 42 | 0 of 42 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb_rules | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 0.0 |
| raqeeb_local | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 2.4 |
| threshold | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| oracle | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| empty | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 0 | 0 | 0.0 |
