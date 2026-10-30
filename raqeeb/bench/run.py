"""Run the benchmark over the scenarios and write the results.

    uv run python -m raqeeb.bench.run [--set practice|test|safety] [--agents threshold,oracle,empty]
                                      [--scenarios scn_0001,scn_0007] [--poll-min 15] [--out bench/<set>]

Scenario sets, reported separately: "practice" (scenarios/scn_*.yaml; thresholds and timings
are tuned on it, then frozen), "test" (scenarios/test/tst_*.yaml; held out, never tuned on) and
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
from raqeeb.bench.score import results_frame, summarize, summary_markdown
from raqeeb.config import BENCH_DIR, SAFETY_SCENARIO_DIR, SCENARIO_DIR, TEST_SCENARIO_DIR, load_plant
from raqeeb.contracts import Agent, Scenario
from raqeeb.twin.weather import weather_5min


@dataclass(frozen=True)
class AgentEntry:
    make: Callable[[Scenario], Agent]  # bench-side factory; only reference agents may use the scenario
    note: str = ""  # non-empty = reference bound, not a competitor


AGENTS: dict[str, AgentEntry] = {
    "raqeeb": AgentEntry(lambda s: RaqeebAgent()),  # gets only agent_view() and telemetry, like the baseline
    "threshold": AgentEntry(lambda s: ThresholdBaseline()),
    "oracle": AgentEntry(lambda s: OracleAgent(s), "reference upper bound: reads the answer key"),
    "empty": AgentEntry(lambda s: EmptyAgent(), "reference lower bound: does nothing"),
}
DEFAULT_AGENTS = ["raqeeb", "threshold", "oracle", "empty"]
SETS: dict[str, tuple[Path, str]] = {"practice": (SCENARIO_DIR, "scn"), "test": (TEST_SCENARIO_DIR, "tst"),
                                   "safety": (SAFETY_SCENARIO_DIR, "saf")}


def load_scenarios(ids: list[str] | None = None, scenario_set: str = "practice") -> list[Scenario]:
    folder, prefix = SETS[scenario_set]
    paths = sorted(folder.glob(f"{prefix}_*.yaml"))
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
            records.append(run_with_control(scenario, lambda: make(scenario), poll_min=poll_min,
                                            reference=reference, plant=plant))
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
    return results, summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help=f"comma-separated, from {sorted(AGENTS)}")
    parser.add_argument("--set", dest="scenario_set", choices=sorted(SETS), default="practice",
                        help="practice (tune on it), test (held out) or safety (injected instructions)")
    parser.add_argument("--scenarios", default="", help="comma-separated ids (default: the whole set)")
    parser.add_argument("--poll-min", type=int, default=POLL_MIN)
    parser.add_argument("--out", type=Path, default=None, help="default: bench/<set>")
    args = parser.parse_args(argv)
    agents = [a for a in args.agents.split(",") if a]
    unknown = set(agents) - set(AGENTS)
    if unknown:
        raise SystemExit(f"unknown agents: {sorted(unknown)}; choose from {sorted(AGENTS)}")
    args.out = args.out or BENCH_DIR / args.scenario_set
    scenarios = load_scenarios([s for s in args.scenarios.split(",") if s] or None, args.scenario_set)
    records = run_bench(scenarios, agents, args.poll_min)
    results, summary = write_outputs(records, args.out)
    with pd.option_context("display.width", 200, "display.max_columns", 30, "display.float_format", "{:.2f}".format):
        print(summary.drop(columns="note").to_string(index=False))
    print(f"wrote {args.out / 'results.csv'}, summary.csv and summary.md ({len(results)} runs)")


if __name__ == "__main__":
    main()
