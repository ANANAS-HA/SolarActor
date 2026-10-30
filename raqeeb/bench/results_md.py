"""bench/results.md: the three result tables of Milestone 3 from the bench outputs.

    uv run python -m raqeeb.bench.results_md [--paragraph notes.md]

Table 1, known faults: practice2 and the final set's known-fault scenarios (the final set's
unknown-fault scenarios and held-out slice get their own rows, so known-fault numbers are not mixed
with them). Table 2, unknown faults: the ablation set (bench/unknown), the final set's unknown-fault
slice and the practice_unknown tuning set. Table 3, the safety set. Everything is computed from
results.csv files the bench wrote; nothing is typed in by hand except the closing paragraph.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd

from raqeeb import contracts
from raqeeb.bench.safety_cases import SAFETY_CASES
from raqeeb.bench.score import summarize
from raqeeb.config import BENCH_DIR
from raqeeb.contracts import UNKNOWN_FAULTS, parse_cause_label

AGENT_ORDER = ["raqeeb_rules", "raqeeb_local", "threshold", "oracle", "empty"]
HELDOUT = getattr(contracts, "HELDOUT_FAULTS", frozenset())


def _f(value, fmt: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return fmt.format(value)


def _types(causes: str) -> set:
    return {parse_cause_label(label)[0] for label in str(causes).split(";") if label and label != "nan"}


def _read(set_name: str) -> pd.DataFrame | None:
    path = BENCH_DIR / set_name / "results.csv"
    return pd.read_csv(path) if path.exists() else None


def _ordered(df: pd.DataFrame) -> pd.DataFrame:
    order = {a: k for k, a in enumerate(AGENT_ORDER)}
    return df[df["agent"].isin(order)].sort_values("agent", key=lambda s: s.map(order), kind="stable")


def table1(rows: list[tuple[str, pd.DataFrame]]) -> list[str]:
    head = ("| Set | Agent | Scenarios | Precision | Recall | All causes found | False actions | Wasted washes | "
            "Verified recovery | Median time to verified recovery (min) | MWh recovered | LLM calls / incident | "
            "LLM time total (s) | Cost / incident (USD) |")
    out = [head, "|" + "---|" * 14]
    for label, results in rows:
        if results is None or results.empty:
            out.append(f"| {label} | (not run) |" + " |" * 12)
            continue
        summary = summarize(_ordered(results))
        for r in summary.to_dict("records"):
            out.append(
                f"| {label} | {r['agent']} | {r['scenarios']} | {_f(r['precision'], '{:.2f}')} | {_f(r['recall'], '{:.2f}')} "
                f"| {_f(r['all_causes_found_rate'], '{:.0%}')} | {_f(r['false_action_rate'], '{:.0%}')} | {r['wasted_washes']} "
                f"| {_f(r['verified_recovery_rate'], '{:.0%}')} | {_f(r['ttvr_median_min'], '{:.0f}')} "
                f"| {_f(r['mwh_recovered'], '{:.1f}')} | {_f(r['llm_calls_per_incident'], '{:.1f}')} "
                f"| {_f(r['llm_time_s_total'], '{:.0f}')} | {_f(r['cost_per_incident_usd'], '{:.4f}')} |")
    return out


def slice_rows(results: pd.DataFrame | None, label: str, fault_types: frozenset) -> list[str]:
    """Per agent on scenarios with an answer-key cause of these types: named, escalated, harmful actions."""
    if results is None or not fault_types:
        return []
    mask = results["root_causes"].map(lambda c: bool(_types(c) & fault_types))
    df = _ordered(results[mask])
    out = []
    for agent, d in df.groupby("agent", sort=False):
        n = len(d)
        named = sum(all(lab in str(p).split(";") for lab in str(c).split(";")
                        if lab and parse_cause_label(lab)[0] in fault_types)
                    for c, p in zip(d["root_causes"], d["predicted"]))
        escalated = int((d["escalations"] > 0).sum())
        out.append(f"| {label} | {agent} | {n} | {named} of {n} | {escalated} of {n} | {int(d['false_actions'].sum())} "
                   f"| {int(d.get('unsafe_actions', pd.Series([0])).fillna(0).sum())} | {d['llm_calls'].mean():.1f} "
                   f"| {d['llm_time_s'].sum():.0f} |")
    return out


def table2(ablation: pd.DataFrame | None, final: pd.DataFrame | None, practice_unknown: pd.DataFrame | None) -> list[str]:
    out = ["| Set | Agent | Scenarios | Cause found | Escalated | Proposals accepted | Proposals rejected | "
           "False actions | LLM calls / incident | LLM time total (s) |", "|" + "---|" * 10]
    if ablation is not None:
        for agent, d in _ordered(ablation).groupby("agent", sort=False):
            n = len(d)
            out.append(f"| ablation (rules switched off) | {agent} | {n} | {int(d['found'].sum())} of {n} "
                       f"| {int(d['escalated'].sum())} of {n} | {int(d['proposals_accepted'].sum())} "
                       f"| {int(d['proposals_rejected'].sum())} | {int(d['false_actions'].sum())} "
                       f"| {d['llm_calls'].mean():.1f} | {d['llm_time_s'].sum():.0f} |")
    for label, results in (("final, unknown-fault slice", final), ("practice_unknown (tuning set)", practice_unknown)):
        if results is None:
            continue
        mask = results["root_causes"].map(lambda c: bool(_types(c) & UNKNOWN_FAULTS))
        for agent, d in _ordered(results[mask]).groupby("agent", sort=False):
            n = len(d)
            out.append(f"| {label} | {agent} | {n} | {int((d['unknown_named'] == d['n_unknown']).sum())} of {n} "
                       f"| {int((d['unknown_escalated'] == d['n_unknown']).sum())} of {n} | | "
                       f"| {int(d['false_actions'].sum())} | {d['llm_calls'].mean():.1f} | {d['llm_time_s'].sum():.0f} |")
    return out


def table3(safety: pd.DataFrame | None) -> list[str]:
    out = ["| Agent | Runs | Allowlist held | Escalated | Retries (failed fixes) | Injection ignored (unsafe actions = 0) "
           "| Approvals refused | LLM calls / incident |", "|" + "---|" * 8]
    if safety is None:
        return out + ["| (not run) |" + " |" * 7]
    for agent, d in _ordered(safety).groupby("agent", sort=False):
        injected = d[d["scenario_id"].str.startswith("saf_")]
        clean = int((injected["unsafe_actions"].fillna(1) == 0).sum())
        out.append(f"| {agent} | {len(d)} | {int(d['allowlist_held'].sum())} of {len(d)} | {int(d['escalated'].sum())} of {len(d)} "
                   f"| {int(d['retries'].sum())} | {clean} of {len(injected)} | {int(d['refused'].sum())} "
                   f"| {d['llm_calls'].mean():.1f} |")
    out += ["", "Per safety case:", "",
            "| Case | Scenario | Agent | Allowlist held | Escalated | Retries | Approvals refused | Audit entries |",
            "|---|---|---|---|---|---|---|---|"]
    for sid, case in SAFETY_CASES.items():
        for r in _ordered(safety[safety["scenario_id"] == sid]).to_dict("records"):
            out.append(f"| ({case.case}) {case.what} | {sid} | {r['agent']} | {'yes' if r['allowlist_held'] else 'NO'} "
                       f"| {'yes' if r['escalated'] else 'no'} | {r['retries']} | {r['refused']} | {r['audit_entries']} |")
    return out


def build(paragraph: str = "") -> str:
    practice2, final, safety = _read("practice2"), _read("final"), _read("safety")
    ablation, practice_unknown = _read("unknown"), _read("practice_unknown")
    rows1 = [("practice2", practice2)]
    if final is not None:
        special = UNKNOWN_FAULTS | HELDOUT
        known = final[~final["root_causes"].map(lambda c: bool(_types(c) & special))]
        rows1.append((f"final, known-fault scenarios", known))
    lines = ["# Raqeeb results (Milestone 3)", "",
             "All plant data, faults, alarms, technician notes and injected instructions are **synthetic**. "
             "Fully local: no API model was used (`raqeeb_full` is not run). raqeeb_rules = no language model; "
             "raqeeb_local = rules plus the local model. oracle reads the answer key and empty does nothing: "
             "reference bounds, not competitors.", "",
             "## Table 1. Known faults", "", *table1(rows1), ""]
    held = slice_rows(final, "final, held-out slice", HELDOUT)
    if held:
        lines += ["Held-out slice of the final set (fault types with no rules; never tuned on):", "",
                  "| Set | Agent | Scenarios | Diagnosed correctly | Escalated | False actions | Unsafe actions "
                  "| LLM calls / incident | LLM time total (s) |", "|" + "---|" * 9, *held, ""]
    lines += ["## Table 2. Unknown faults", "", *table2(ablation, final, practice_unknown), "",
              "## Table 3. Safety set", "", *table3(safety), ""]
    if paragraph:
        lines += ["## What the local model added, and what it did not", "", paragraph.strip(), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paragraph", type=Path, default=None)
    args = parser.parse_args(argv)
    text = build(args.paragraph.read_text(encoding="utf-8") if args.paragraph else "")
    (BENCH_DIR / "results.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
