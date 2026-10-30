"""Run the benchmark over the scenarios and write the results.

    uv run python -m raqeeb.bench.run [--set practice|practice2|test|safety] [--agents all|raqeeb_rules,oracle]
                                      [--scenarios scn_0001,scn_0007] [--poll-min 15] [--out bench/<set>]

Scenario sets, reported separately: "practice" (scenarios/scn_*.yaml) and "practice2"
(scenarios/practice2/pr2_*.yaml, full scope): thresholds and timings are tuned on these only;
"test" (scenarios/test/tst_*.yaml; round 1, held out, never tuned on) and
"safety" (scenarios/safety/saf_*.yaml; each has an injected instruction in a log line, and each
agent also runs a neutral-text control so unsafe actions can be counted; never tuned on).
Writes <out>/results.csv (one row per agent x scenario), <out>/summary.csv (one row per agent,
what the console shows) and <out>/summary.md (the same table plus where each agent is wrong
and where it loses head-to-head). Agents with a note in AGENTS are reference bounds, not
competitors: they are left out of head-to-head comparisons.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from raqeeb.agent import RaqeebAgent
from raqeeb.baseline.threshold import ThresholdBaseline
from raqeeb.bench.fakes import EmptyAgent, OracleAgent
from raqeeb.bench.harness import POLL_MIN, RunRecord, reference_energy, run_with_control
from raqeeb.bench.safety_cases import SAFETY_CASES
from raqeeb.bench.score import results_frame, summarize, summary_markdown
from raqeeb.config import (
    BENCH_DIR,
    BENCH_LOCK,
    FINAL_SCENARIO_DIR,
    PRACTICE2_SCENARIO_DIR,
    PRACTICE_UNKNOWN_SCENARIO_DIR,
    SAFETY_SCENARIO_DIR,
    SCENARIO_DIR,
    TEST_SCENARIO_DIR,
    load_plant,
)
from raqeeb.contracts import Agent, Scenario
from raqeeb.llm.router import Router
from raqeeb.twin.weather import weather_5min


@dataclass(frozen=True)
class AgentEntry:
    make: Callable[[Scenario], Agent]  # bench-side factory; only reference agents may use the scenario
    note: str = ""  # non-empty = reference bound, not a competitor


def _raqeeb(name: str, api: bool | None) -> Callable[[Scenario], Agent]:
    """Raqeeb gets only agent_view() and telemetry, like the baseline. api None = rules only (no router),
    False = local model only, True = local model plus the API model."""
    def make(scenario: Scenario) -> Agent:
        return RaqeebAgent(router=None if api is None else Router(api=api), name=name)
    return make


AGENTS: dict[str, AgentEntry] = {
    "raqeeb_rules": AgentEntry(_raqeeb("raqeeb_rules", None)),
    "raqeeb_local": AgentEntry(_raqeeb("raqeeb_local", False)),
    "raqeeb_full": AgentEntry(_raqeeb("raqeeb_full", True)),
    "raqeeb": AgentEntry(_raqeeb("raqeeb", None)),  # the rules-only agent under its Milestone 1-2 name
    "threshold": AgentEntry(lambda s: ThresholdBaseline()),
    "oracle": AgentEntry(lambda s: OracleAgent(s), "reference upper bound: reads the answer key"),
    "empty": AgentEntry(lambda s: EmptyAgent(), "reference lower bound: does nothing"),
}
DEFAULT_AGENTS = ["raqeeb_rules", "threshold", "oracle", "empty"]  # add raqeeb_local,raqeeb_full with --agents
# The project runs fully local (no API key): raqeeb_full is wired but left out of every default and "all" run
ALL_AGENTS = ["raqeeb_rules", "raqeeb_local", "threshold", "oracle", "empty"]
SETS: dict[str, tuple[Path, str]] = {"practice": (SCENARIO_DIR, "scn"), "practice2": (PRACTICE2_SCENARIO_DIR, "pr2"),
                                   "practice_unknown": (PRACTICE_UNKNOWN_SCENARIO_DIR, "pru"),
                                   "final": (FINAL_SCENARIO_DIR, "fin"),
                                   "test": (TEST_SCENARIO_DIR, "tst"), "safety": (SAFETY_SCENARIO_DIR, "saf|sfc")}


def load_scenarios(ids: list[str] | None = None, scenario_set: str = "practice") -> list[Scenario]:
    folder, prefixes = SETS[scenario_set]
    paths = sorted(p for prefix in prefixes.split("|") for p in folder.glob(f"{prefix}_*.yaml"))
    if ids:
        wanted = set(ids)
        paths = [p for p in paths if p.stem in wanted]
        missing = wanted - {p.stem for p in paths}
        if missing:
            raise SystemExit(f"unknown scenarios: {sorted(missing)}")
    return [Scenario.from_yaml(p) for p in paths]


def run_bench(scenarios: list[Scenario], agents: list[str], poll_min: int = POLL_MIN) -> list[RunRecord]:
    plant = load_plant()
    records = []
    for scenario in scenarios:
        reference = reference_energy(scenario, plant, weather_5min(scenario.start, scenario.end, plant))
        for name in agents:
            make = AGENTS[name].make
            case = SAFETY_CASES.get(scenario.id)
            records.append(run_with_control(scenario, lambda: make(scenario), poll_min=poll_min,
                                            reference=reference, plant=plant, **(case.run_options() if case else {})))
        print(f"{scenario.id}: done ({', '.join(agents)})")
    return records


def write_outputs(records: list[RunRecord], out: Path = BENCH_DIR / "practice") -> tuple[pd.DataFrame, pd.DataFrame]:
    results = results_frame(records)
    notes = {name: entry.note for name, entry in AGENTS.items()}
    summary = summarize(results, notes)
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False, float_format="%.4f", lineterminator="\n")
    summary.to_csv(out / "summary.csv", index=False, float_format="%.4f", lineterminator="\n")
    text = summary_markdown(results, summary)
    (out / "summary.md").write_text(text, encoding="utf-8", newline="\n")
    audit_dir = out / "audit"
    for record in records:
        if record.audit and (record.injections or record.scenario_id in SAFETY_CASES):
            audit_dir.mkdir(exist_ok=True)
            pd.DataFrame(record.audit).to_csv(audit_dir / f"{record.scenario_id}__{record.agent}.csv", index=False,
                                              lineterminator="\n")
    return results, summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help=f"comma-separated, from {sorted(AGENTS)}")
    parser.add_argument("--set", dest="scenario_set", choices=sorted(SETS), default="practice",
                        help="practice, practice2 (tune on them), test (round 1, held out) or safety")
    parser.add_argument("--scenarios", default="", help="comma-separated ids (default: the whole set)")
    parser.add_argument("--poll-min", type=int, default=POLL_MIN)
    parser.add_argument("--out", type=Path, default=None, help="default: bench/<set>")
    args = parser.parse_args(argv)
    agents = ALL_AGENTS if args.agents == "all" else [a for a in args.agents.split(",") if a]
    unknown = set(agents) - set(AGENTS)
    if unknown:
        raise SystemExit(f"unknown agents: {sorted(unknown)}; choose from {sorted(AGENTS)}")
    args.out = args.out or BENCH_DIR / args.scenario_set
    scenarios = load_scenarios([s for s in args.scenarios.split(",") if s] or None, args.scenario_set)
    BENCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    BENCH_LOCK.write_text(f"{args.scenario_set} {','.join(agents)}\n", encoding="utf-8")
    try:  # the lock keeps the live LLM tests off the local model server while the bench uses it
        records = run_bench(scenarios, agents, args.poll_min)
    finally:
        BENCH_LOCK.unlink(missing_ok=True)
    results, summary = write_outputs(records, args.out)
    with pd.option_context("display.width", 200, "display.max_columns", 30, "display.float_format", "{:.2f}".format):
        print(summary.drop(columns="note").to_string(index=False))
    print(f"wrote {args.out / 'results.csv'}, summary.csv and summary.md ({len(results)} runs)")


if __name__ == "__main__":
    main()
