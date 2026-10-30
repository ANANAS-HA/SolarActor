# Raqeeb results by category

## Results by category (safety)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all scenarios | raqeeb_rules | 15 | 15 of 15 (100%) | 0 | 6 | 7 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | raqeeb_local | 15 | 15 of 15 (100%) | 0 | 6 | 7 | 0 | 3 | 2.4 | 330 | 0 |
| all scenarios | threshold | 15 | 1 of 15 (7%) | 6 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | oracle | 15 | 15 of 15 (100%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |
| all scenarios | empty | 15 | 0 of 15 (0%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |

## Safety set

(bench/safety not run)

Results from commit fe522d08ffb33eb8f482767fa233ff5695fdb319, set safety, written 2026-10-30T20:27:55Z.
