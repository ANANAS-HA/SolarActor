# Raqeeb results by category

## Results by category (practice)

Synthetic data. A scenario can be in several categories; each category judges its own element (see raqeeb/bench/categories.py for the definition of correct handling per category).

| Category | Agent | Scenarios | Correct handling | False actions | Runs escalated | Escalations | Proposals accepted | Proposals rejected | LLM calls / incident | LLM time total (s) | Close type counted as named |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all scenarios | raqeeb_rules | 20 | 20 of 20 (100%) | 0 | 0 | 0 | 0 | 0 | 0.0 | 0 | 0 |

## Safety set

| Agent | Runs | Allowlist held | Injection ignored (unsafe actions = 0) | Escalated | Approvals refused | Retries (failed fixes) | LLM calls / incident |
|---|---|---|---|---|---|---|---|
| raqeeb | 12 | n/a | 12 of 12 | n/a of 12 | n/a | n/a | 0.0 |
| threshold | 12 | n/a | 12 of 12 | n/a of 12 | n/a | n/a | 0.0 |
| oracle | 12 | n/a | 12 of 12 | n/a of 12 | n/a | n/a | 0.0 |
| empty | 12 | n/a | 12 of 12 | n/a of 12 | n/a | n/a | 0.0 |
