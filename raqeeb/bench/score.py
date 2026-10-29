"""Benchmark metrics (HANDOFF section 11), computed from harness RunRecords.

Per run (one agent on one scenario), `score_run` gives one results.csv row:
- root-cause precision / recall: exact "FAULT_TYPE@TARGET" labels of the final diagnosis vs the
  answer key. Precision is undefined (NaN) when nothing was predicted. `type_recall` is the
  looser check: share of answer-key fault types named at any target.
- all_found: every answer-key cause is in the diagnosis.
- false actions: requested actions that address no answer-key cause (actions on symptoms,
  decoys or healthy devices), whatever their outcome. See ACTION_FIXES.
- time to verified recovery (sim minutes from the first root cause's start): the first poll at
  which every cause is mitigated in the twin state AND the agent has reported recovered=True
  for every action it applied (at least one). NaN when that never happens; an agent that does
  not verify never gets a time.
- MWh recovered: twin.truth energy of the run minus the no-action run of the same seed.
  MWh lost: decoy-only run minus no-action run. Not all of it is recoverable: storm dimming,
  output before a fix lands and a derate no core action repairs are lost to every agent.
- cost (USD), LLM calls and how many were local.

`summarize` aggregates per agent (precision and recall are micro-averaged over all causes and
predictions; rates are shares of scenarios or actions).
"""

from __future__ import annotations

import math

import pandas as pd

from raqeeb.bench.harness import RunRecord
from raqeeb.contracts import (
    PLANT_TARGET,
    ActionRequest,
    ActionStatus,
    ActionType,
    FaultType,
    LlmRole,
    parse_cause_label,
)

# Which root causes each action addresses (HANDOFF section 8 "safe action", twin/controls.py effects)
ACTION_FIXES: dict[ActionType, frozenset[FaultType]] = {
    ActionType.RESET_COMMS_GATEWAY: frozenset({FaultType.COMMS_DROPOUT}),
    ActionType.FLAG_SENSOR_USE_REFERENCE: frozenset({FaultType.SENSOR_SOILING}),
    ActionType.CREATE_CLEANING_WORK_ORDER: frozenset({FaultType.SOILING}),
    ActionType.RESUME_TRACKING: frozenset({FaultType.TRACKER_STUCK}),
    ActionType.DISPATCH_TECHNICIAN: frozenset({FaultType.INVERTER_DERATE, FaultType.STRING_OPEN,
                                               FaultType.TRACKER_STUCK, FaultType.COMMS_DROPOUT,
                                               FaultType.SENSOR_SOILING}),
    ActionType.SET_POWER_LIMIT: frozenset({FaultType.INVERTER_DERATE}),
    ActionType.ISOLATE_DEVICE: frozenset({FaultType.UNAUTHORIZED_CMD}),
}
MWH_TIE = 0.01  # energy differences below this are a tie in head-to-head comparisons

NAN = float("nan")


def addresses(request: ActionRequest, label: str) -> bool:
    """True if `request` acts on root cause `label`. A plant-wide cause covers any single target."""
    fault_type, target = parse_cause_label(label)
    return fault_type in ACTION_FIXES[request.action] and (request.target == target or target == PLANT_TARGET)


def _minutes(delta) -> float:
    return delta.total_seconds() / 60.0


def time_to_verified_recovery(record: RunRecord) -> float:
    start = record.incident_start
    if start is None:
        return NAN
    for turn in record.turns:
        if not turn.mitigated or not all(turn.mitigated.values()):
            continue
        latest: dict[str, bool] = {}
        for ts, verification in record.verifications:
            if ts <= turn.ts:
                latest[verification.action_id] = verification.recovered
        applied = [a.request.id for a in record.actions if a.status == ActionStatus.APPLIED and a.ts <= turn.ts]
        if any(latest.values()) and all(latest.get(action_id) is True for action_id in applied):
            return _minutes(turn.ts - start)
    return NAN


def _ratio(num: float, den: float) -> float:
    return num / den if den else NAN


def score_run(record: RunRecord) -> dict:
    truth, predicted = set(record.root_causes), set(record.predicted)
    tp = len(truth & predicted)
    truth_types = {parse_cause_label(label)[0] for label in truth}
    predicted_types = {parse_cause_label(label)[0] for label in predicted}
    false_actions = [a for a in record.actions if not any(addresses(a.request, label) for label in truth)]
    final = record.turns[-1].mitigated if record.turns else {}
    ref = record.reference
    calls = record.llm_calls
    return {
        "agent": record.agent,
        "scenario_id": record.scenario_id,
        "root_causes": ";".join(sorted(truth)),
        "predicted": ";".join(sorted(predicted)),
        "n_causes": len(truth),
        "n_predicted": len(predicted),
        "tp": tp,
        "fp": len(predicted - truth),
        "fn": len(truth - predicted),
        "precision": _ratio(tp, len(predicted)),
        "recall": _ratio(tp, len(truth)),
        "type_recall": _ratio(len(truth_types & predicted_types), len(truth_types)),
        "all_found": truth <= predicted,
        "n_actions": len(record.actions),
        "false_actions": len(false_actions),
        "false_action_list": ";".join(f"{a.request.action.value}@{a.request.target}" for a in false_actions),
        "approvals": sum(a.gated for a in record.actions),
        "failed_actions": sum(a.status == ActionStatus.FAILED for a in record.actions),
        "mitigated_at_end": bool(final) and all(final.values()),
        "unmitigated_at_end": ";".join(sorted(label for label, ok in final.items() if not ok)),
        "ttvr_min": time_to_verified_recovery(record),
        "mwh": record.energy_mwh,
        "mwh_no_action": ref.no_action_mwh if ref else NAN,
        "mwh_recovered": record.energy_mwh - ref.no_action_mwh if ref else NAN,
        "mwh_lost": ref.decoy_only_mwh - ref.no_action_mwh if ref else NAN,
        "cost_usd": sum(c.cost_usd for c in calls),
        "llm_calls": len(calls),
        "local_calls": sum(c.role == LlmRole.LOCAL for c in calls),
    }


def results_frame(records: list[RunRecord]) -> pd.DataFrame:
    return pd.DataFrame([score_run(r) for r in records])


def summarize(results: pd.DataFrame, notes: dict[str, str] | None = None) -> pd.DataFrame:
    """One row per agent, in the order the agents first appear."""
    rows = []
    for agent, df in results.groupby("agent", sort=False):
        n = len(df)
        verified = df["ttvr_min"].notna()
        rows.append({
            "agent": agent,
            "note": (notes or {}).get(agent, ""),
            "scenarios": n,
            "precision": _ratio(df["tp"].sum(), df["n_predicted"].sum()),
            "recall": _ratio(df["tp"].sum(), df["n_causes"].sum()),
            "all_causes_found_rate": df["all_found"].mean(),
            "false_action_rate": _ratio(df["false_actions"].sum(), df["n_actions"].sum()),
            "actions_per_incident": df["n_actions"].mean(),
            "approvals_per_incident": df["approvals"].mean(),
            "verified_recovery_rate": verified.mean(),
            "ttvr_median_min": df.loc[verified, "ttvr_min"].median() if verified.any() else NAN,
            "mwh_recovered": df["mwh_recovered"].sum(),
            "mwh_lost": df["mwh_lost"].sum(),
            "recovered_share_of_lost": _ratio(df["mwh_recovered"].sum(), df["mwh_lost"].sum()),
            "cost_per_incident_usd": df["cost_usd"].mean(),
            "llm_calls_per_incident": df["llm_calls"].mean(),
            "local_call_share": _ratio(df["local_calls"].sum(), df["llm_calls"].sum()),
        })
    return pd.DataFrame(rows)


# -- honest comparison -----------------------------------------------------------------------


def _worse(a: dict, b: dict) -> list[str]:
    """Where run `a` is worse than run `b` on the same scenario."""
    out = []
    if a["recall"] < b["recall"]:
        out.append(f"recall {a['recall']:.2f} vs {b['recall']:.2f}")
    if not math.isnan(a["precision"]) and not math.isnan(b["precision"]) and a["precision"] < b["precision"]:
        out.append(f"precision {a['precision']:.2f} vs {b['precision']:.2f}")
    if a["false_actions"] > b["false_actions"]:
        out.append(f"false actions {a['false_actions']} vs {b['false_actions']}")
    if a["mwh_recovered"] < b["mwh_recovered"] - MWH_TIE:
        out.append(f"MWh recovered {a['mwh_recovered']:.2f} vs {b['mwh_recovered']:.2f}")
    if not math.isnan(b["ttvr_min"]) and (math.isnan(a["ttvr_min"]) or a["ttvr_min"] > b["ttvr_min"]):
        mine = "not verified" if math.isnan(a["ttvr_min"]) else f"{a['ttvr_min']:.0f} min"
        out.append(f"verified recovery {mine} vs {b['ttvr_min']:.0f} min")
    if a["cost_usd"] > b["cost_usd"]:
        out.append(f"cost ${a['cost_usd']:.4f} vs ${b['cost_usd']:.4f}")
    return out


def losses(results: pd.DataFrame, agent: str, versus: str) -> list[str]:
    """One line per scenario where `agent` does worse than `versus` on any metric."""
    mine = {r["scenario_id"]: r for r in results[results["agent"] == agent].to_dict("records")}
    theirs = {r["scenario_id"]: r for r in results[results["agent"] == versus].to_dict("records")}
    lines = []
    for scenario_id in sorted(mine.keys() & theirs.keys()):
        worse = _worse(mine[scenario_id], theirs[scenario_id])
        if worse:
            lines.append(f"{scenario_id}: " + "; ".join(worse))
    return lines


def failures(results: pd.DataFrame, agent: str) -> list[str]:
    """One line per scenario where `agent` missed or invented a cause, or acted falsely."""
    lines = []
    for r in results[results["agent"] == agent].sort_values("scenario_id").to_dict("records"):
        truth = set(filter(None, r["root_causes"].split(";")))
        predicted = set(filter(None, r["predicted"].split(";")))
        parts = []
        if truth - predicted:
            parts.append("missed " + ", ".join(sorted(truth - predicted)))
        if predicted - truth:
            extra = sorted(predicted - truth)
            parts.append(f"{len(extra)} wrong cause(s): " + ", ".join(extra[:4]) + (" ..." if len(extra) > 4 else ""))
        if r["false_actions"]:
            parts.append(f"{r['false_actions']} false action(s)")
        if parts:
            lines.append(f"{r['scenario_id']}: " + "; ".join(parts))
    return lines


# -- output ----------------------------------------------------------------------------------

_DISPLAY = [  # (column, header, format)
    ("agent", "Agent", "{}"),
    ("scenarios", "Scenarios", "{:d}"),
    ("precision", "Root-cause precision", "{:.2f}"),
    ("recall", "Root-cause recall", "{:.2f}"),
    ("all_causes_found_rate", "All causes found", "{:.0%}"),
    ("false_action_rate", "False-action rate", "{:.0%}"),
    ("approvals_per_incident", "Approvals / incident", "{:.1f}"),
    ("verified_recovery_rate", "Verified recovery", "{:.0%}"),
    ("ttvr_median_min", "Median time to verified recovery (min)", "{:.0f}"),
    ("mwh_recovered", "MWh recovered", "{:.1f}"),
    ("recovered_share_of_lost", "Share of lost MWh recovered", "{:.0%}"),
    ("cost_per_incident_usd", "Cost / incident (USD)", "{:.4f}"),
    ("local_call_share", "Local LLM calls", "{:.0%}"),
]


def _fmt(value, fmt: str) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "n/a"
    return fmt.format(int(value) if fmt == "{:d}" else value)


def summary_markdown(results: pd.DataFrame, summary: pd.DataFrame) -> str:
    """No timestamp: the file changes only when results change."""
    lines = ["# Raqeeb benchmark results", ""]
    lines += [
        "All plant data, faults and alarms are **synthetic** (simulated plant twin, NASA POWER weather).",
        "",
        "| " + " | ".join(h for _, h, _ in _DISPLAY) + " |",
        "|" + "---|" * len(_DISPLAY),
    ]
    for row in summary.to_dict("records"):
        lines.append("| " + " | ".join(_fmt(row[c], f) for c, _, f in _DISPLAY) + " |")
    noted = [f"- **{r['agent']}**: {r['note']}" for r in summary.to_dict("records") if r["note"]]
    if noted:
        lines += ["", *noted]
    lines += [
        "",
        "n/a: undefined (no predictions, no actions, no verified recovery or no LLM calls).",
        "MWh recovered is measured against a no-action run of the same scenario and seed; it can be",
        "negative when an action costs output (e.g. a power limit below what the inverter could give).",
        "Lost MWh (no real fault vs no action) is not all recoverable: storm dimming, output before a",
        "fix lands and derates that no core action repairs are lost to every agent.",
        "A plant-wide cause (SOILING@ALL) counts an action on any single inverter as addressing it.",
        "",
    ]
    agents = list(summary["agent"])
    for agent in agents:
        rows = failures(results, agent)
        lines += [f"## Where {agent} gets it wrong", ""]
        lines += [f"- {line}" for line in rows] if rows else ["- No missed causes, wrong causes or false actions."]
        lines.append("")
    compared = [a for a in agents if not summary.set_index("agent").loc[a, "note"]]
    for agent in compared:
        for other in compared:
            if other == agent:
                continue
            rows = losses(results, agent, other)
            lines += [f"## Where {agent} loses to {other}", ""]
            lines += [f"- {line}" for line in rows] if rows else [f"- {agent} is never worse than {other}."]
            lines.append("")
    return "\n".join(lines)
