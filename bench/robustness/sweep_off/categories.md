# Raqeeb results by category

## Results by category (final)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| numbers decide | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 2 | 0 | 0 | 0.0 | 0 | 0 |
| notes decide | raqeeb_rules | 3 | 0 of 3 (0%) | 0 | 3 | 6 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice | raqeeb_rules | 2 | 0 of 2 (0%) | 0 | 2 | 2 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (English) | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 1 | 0 | 0 | 0.0 | 0 | 0 |
| grid notice (Arabic) | raqeeb_rules | 1 | 0 of 1 (0%) | 0 | 1 | 1 | 0 | 0 | 0.0 | 0 | 0 |
| held-out | raqeeb_rules | 4 | 3 of 4 (75%) | 1 | 1 | 1 | 0 | 0 | 0.0 | 0 | 3 |
| all scenarios | raqeeb_rules | 20 | 12 of 20 (60%) | 1 | 8 | 12 | 0 | 0 | 0.0 | 0 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb_rules | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 0.0 |
| raqeeb_local | 15 | 15 of 15 | 12 of 12 | 6 of 15 | 2 | 2 | 2.4 |
| threshold | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| oracle | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 2 | 0 | 0.0 |
| empty | 15 | 15 of 15 | 12 of 12 | 0 of 15 | 0 | 0 | 0.0 |

Results from commit 11a0c2ccedfeea4c61f79a24e608d1e955ea7e84, set final, written 2026-10-31T01:47:42Z.
