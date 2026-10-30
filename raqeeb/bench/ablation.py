"""Unknown faults by ablation: a known fault whose rules are switched off for the run.

    uv run python -m raqeeb.bench.ablation [--write] [--agents raqeeb_rules,raqeeb_local] [--out bench/unknown]

The practice sets hold no fault the rules do not know, so this set makes one: for each ablated fault
type, 2 scenarios where that fault is planted and Raqeeb runs with that type removed from its causal
graph and its fix ladders (RaqeebAgent(disabled={type})). Everything else, the model included, is
unchanged. The scenarios come from the existing generator (new seed, no new fault physics) and live in
scenarios/ablation/abl_*.yaml with ablation.yaml naming the ablated type of each; that map is bench
configuration, handed to the agent as "these rules are off", never where or whether the fault is.

Per scenario and agent it records whether the ablated cause was named exactly, whether its devices were
escalated to a human, each model proposal accepted or rejected (with the test's reason), LLM calls and
the usual scores. Output: <out>/results.csv, <out>/proposals.csv, <out>/summary.md, <out>/README.md.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import yaml

from raqeeb.agent import RaqeebAgent
from raqeeb.bench.harness import run_scenario
from raqeeb.bench.score import score_run
from raqeeb.config import BENCH_DIR, BENCH_LOCK, SCENARIO_DIR, load_plant
from raqeeb.contracts import FaultType, Scenario
from raqeeb.faults.generator import generate, write_scenarios
from raqeeb.llm.router import Router

ABLATION_DIR = SCENARIO_DIR / "ablation"
MAP_FILE = ABLATION_DIR / "ablation.yaml"
# Six of the seven rule types, two scenarios each (12, the top of the 8-12 target). SENSOR_SOILING is left
# out to stay within 12: with its rules off it only removes the flag, the other six change the diagnosis.
ABLATED = (FaultType.SOILING, FaultType.INVERTER_DERATE, FaultType.COMMS_DROPOUT, FaultType.TRACKER_STUCK,
           FaultType.STRING_OPEN, FaultType.UNAUTHORIZED_CMD)
PER_TYPE = 2
SEED = 4242
POOL = 200
MAX_FAULTS = 3


def build() -> tuple[list[Scenario], dict[str, FaultType]]:
    """The ablation scenarios (abl_0001...) and the fault type ablated in each."""
    pool = generate(POOL, SEED, full_scope=True, prefix="pool", share_trap=0.0, share_decoy=0.0, random_decoys=False)
    chosen: list[tuple[Scenario, FaultType]] = []
    used: set[str] = set()
    for fault in ABLATED:
        picks = [s for s in pool if s.id not in used and len(s.faults) <= MAX_FAULTS
                 and fault in {f.type for f in s.faults}][:PER_TYPE]
        used |= {s.id for s in picks}
        chosen += [(s, fault) for s in picks]
    scenarios, ablated = [], {}
    for k, (s, fault) in enumerate(chosen, start=1):
        sid = f"abl_{k:04d}"
        scenarios.append(s.model_copy(update={"id": sid}))
        ablated[sid] = fault
    return scenarios, ablated


def write() -> list[Path]:
    scenarios, ablated = build()
    paths = write_scenarios(scenarios, ABLATION_DIR)
    MAP_FILE.write_text("# Ablation set: the fault type whose rules are switched off for each scenario (bench only).\n"
                        + yaml.safe_dump({sid: f.value for sid, f in ablated.items()}, sort_keys=True),
                        encoding="utf-8", newline="\n")
    return paths


def load() -> tuple[list[Scenario], dict[str, FaultType]]:
    ablated = {k: FaultType(v) for k, v in yaml.safe_load(MAP_FILE.read_text(encoding="utf-8")).items()}
    return [Scenario.from_yaml(ABLATION_DIR / f"{sid}.yaml") for sid in sorted(ablated)], ablated


def make_agent(name: str, disabled: FaultType) -> RaqeebAgent:
    router = Router(api=False) if name == "raqeeb_local" else None
    return RaqeebAgent(router=router, name=name, disabled=frozenset({disabled}))


def run(agents: list[str], out: Path) -> pd.DataFrame:
    plant = load_plant()
    scenarios, ablated = load()
    rows, proposals = [], []
    for s in scenarios:
        fault = ablated[s.id]
        label = next(f.label for f in s.faults if f.type == fault)
        devices = set(plant.layout.area_of(label.split("@")[1]) or [label.split("@")[1]])
        for name in agents:
            agent = make_agent(name, fault)
            started = time.perf_counter()
            record = run_scenario(s, agent, plant=plant)
            row = score_run(record)
            timeline = agent.memory.timeline
            accepted = [e.summary.split(" ")[3] for e in timeline if e.summary.startswith("accepted model proposal")]
            rejected = [e.summary[len("rejected model proposal "):] for e in timeline
                        if e.summary.startswith("rejected model proposal")]
            for e in timeline:
                if e.summary.startswith(("accepted model proposal", "rejected model proposal")):
                    proposals.append({"scenario_id": s.id, "agent": name, "ablated": fault.value,
                                      "ts": e.ts.isoformat(), "decision": e.summary.split(" ")[0],
                                      "detail": e.summary})
            rows.append(row | {
                "ablated": fault.value, "ablated_cause": label, "found": label in record.predicted,
                "escalated": any(e.subject == label or set(e.devices) & devices for e in record.escalations),
                "proposals_accepted": len(accepted), "proposals_rejected": len(rejected),
                "accepted": ";".join(accepted), "rejected": " | ".join(rejected)[:600],
                "llm_time_s": round(sum(c.latency_s for c in record.llm_calls), 1),
                "wall_s": round(time.perf_counter() - started, 1),
            })
            print(f"{s.id} {name}: ablated {label}, found={label in record.predicted}, "
                  f"accepted={accepted}, rejected={len(rejected)}")
    results = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False, float_format="%.4f", lineterminator="\n")
    pd.DataFrame(proposals).to_csv(out / "proposals.csv", index=False, lineterminator="\n")
    (out / "summary.md").write_text(summary_markdown(results), encoding="utf-8", newline="\n")
    (out / "README.md").write_text(README, encoding="utf-8", newline="\n")
    return results


README = """# Unknown faults by ablation (bench/unknown)

**This is an ablation set, not a set of new faults.** Each scenario plants faults the simulator already
has (synthetic data). For each scenario one known fault type has its rules switched off for the run:
removed from Raqeeb's causal graph and from its fix ladders. To the agent that fault is unknown. The
language model and everything else are unchanged.

- Scenarios: `scenarios/ablation/abl_*.yaml`, generated by `raqeeb.bench.ablation` (seed 4242) from the
  existing fault generator; `scenarios/ablation/ablation.yaml` names the ablated type per scenario.
- Ablated types: SOILING, INVERTER_DERATE, COMMS_DROPOUT, TRACKER_STUCK, STRING_OPEN, UNAUTHORIZED_CMD,
  2 scenarios each. SENSOR_SOILING is left out to stay within 12 scenarios.
- `found`: the ablated cause is named exactly (type and target) in the final diagnosis.
- `escalated`: its devices were handed to a human.
- Model proposals are accepted only after testing on tool data (footprint test for a type whose rules
  are off); `proposals.csv` lists every accepted and rejected proposal with the reason.
- An accepted proposal for a type whose rules are off gets only safe actions: a technician ticket for
  one inverter, otherwise escalation.

Run: `uv run python -m raqeeb.bench.ablation --agents raqeeb_rules,raqeeb_local`.
"""


def summary_markdown(results: pd.DataFrame) -> str:
    lines = ["# Unknown faults by ablation", "",
             "Ablation set: a known fault with its rules switched off for the run (see README.md). Synthetic data.", "",
             "| Agent | Scenarios | Ablated cause found | Escalated | Proposals accepted | Proposals rejected | "
             "Precision | Recall (all causes) | False actions | LLM calls / incident | LLM time (s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for agent, df in results.groupby("agent", sort=False):
        predicted, causes = df["n_predicted"].sum(), df["n_causes"].sum()
        lines.append(f"| {agent} | {len(df)} | {int(df['found'].sum())} of {len(df)} | {int(df['escalated'].sum())} of {len(df)} "
                     f"| {int(df['proposals_accepted'].sum())} | {int(df['proposals_rejected'].sum())} "
                     f"| {df['tp'].sum() / predicted if predicted else float('nan'):.2f} | {df['tp'].sum() / causes:.2f} "
                     f"| {int(df['false_actions'].sum())} | {df['llm_calls'].mean():.1f} | {df['llm_time_s'].sum():.0f} |")
    lines += ["", "## Per scenario", "",
              "| Scenario | Ablated cause | Agent | Found | Escalated | Accepted | Rejected (reason) |", "|---|---|---|---|---|---|---|"]
    for r in results.to_dict("records"):
        lines.append(f"| {r['scenario_id']} | {r['ablated_cause']} | {r['agent']} | {'yes' if r['found'] else 'no'} "
                     f"| {'yes' if r['escalated'] else 'no'} | {r['accepted'] or '-'} | {r['rejected'] or '-'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="(re)write scenarios/ablation/ and exit")
    parser.add_argument("--agents", default="raqeeb_rules,raqeeb_local")
    parser.add_argument("--out", type=Path, default=BENCH_DIR / "unknown")
    args = parser.parse_args(argv)
    if args.write:
        for path in write():
            print(path.name)
        return
    BENCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    BENCH_LOCK.write_text("ablation\n", encoding="utf-8")
    try:
        results = run([a for a in args.agents.split(",") if a], args.out)
    finally:
        BENCH_LOCK.unlink(missing_ok=True)
    print(summary_markdown(results).split("## Per scenario")[0])


if __name__ == "__main__":
    main()
