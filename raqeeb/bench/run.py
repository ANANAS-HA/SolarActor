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
import json
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from raqeeb.agent import RaqeebAgent
from raqeeb.baseline.threshold import ThresholdBaseline
from raqeeb.bench.fakes import EmptyAgent, OracleAgent
from raqeeb.bench.categories import write_category_report
from raqeeb.bench.harness import POLL_MIN, RunRecord, reference_energy, run_with_control
from raqeeb.bench.safety_cases import SAFETY_CASES
from raqeeb.bench.score import results_frame, summarize, summary_markdown
from raqeeb.config import (
    REPO_ROOT,
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


def source_commit() -> dict:
    """The commit the bench ran from: {"commit": sha or "unknown", "dirty": bool} (git, from REPO_ROOT)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                             check=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", ":!bench"],
                                cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=10).stdout
        return {"commit": sha, "dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": False}


def write_run_info(out: Path, set_name: str, n_scenarios: int, agents: list[str]) -> dict:
    """<out>/run_info.json: the commit (and whether the tree was dirty, bench/ outputs excepted), the set,
    the agents and the time the outputs were written; so a results folder names the code that produced it."""
    info = source_commit() | {"set": set_name, "scenarios": n_scenarios, "agents": agents,
                              "written": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out.mkdir(parents=True, exist_ok=True)
    (out / "run_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    return info


def write_outputs(records: list[RunRecord], out: Path = BENCH_DIR / "practice",
                  scenarios: list[Scenario] | None = None, set_name: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """`scenarios` (the set that was run) adds categories.csv / categories.md and the category section of
    summary.md (raqeeb.bench.categories)."""
    results = results_frame(records)
    notes = {name: entry.note for name, entry in AGENTS.items()}
    summary = summarize(results, notes)
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False, float_format="%.4f", lineterminator="\n")
    summary.to_csv(out / "summary.csv", index=False, float_format="%.4f", lineterminator="\n")
    info = write_run_info(out, set_name, len({r.scenario_id for r in records}), sorted({r.agent for r in records}))
    text = summary_markdown(results, summary) + category_section(results, scenarios, out, set_name, info)
    (out / "summary.md").write_text(text, encoding="utf-8", newline="\n")
    audit_dir = out / "audit"
    for record in records:
        if record.audit and (record.injections or record.scenario_id in SAFETY_CASES):
            audit_dir.mkdir(exist_ok=True)
            pd.DataFrame(record.audit).to_csv(audit_dir / f"{record.scenario_id}__{record.agent}.csv", index=False,
                                              lineterminator="\n")
    return results, summary


def category_section(results: pd.DataFrame, scenarios: list[Scenario] | None, out: Path, set_name: str,
                     info: dict | None = None) -> str:
    """The per-category tables appended to summary.md (also written to categories.csv / .md); "" without
    scenarios. The safety table comes from bench/safety when that set has been run. `info` (run_info) puts
    the commit the results came from under the tables."""
    if not scenarios:
        return ""
    safety_path = BENCH_DIR / "safety" / "results.csv"
    safety = pd.read_csv(safety_path) if safety_path.exists() and set_name != "safety" else None
    write_category_report(results, scenarios, out, set_name, safety, info)
    return "\n" + (out / "categories.md").read_text(encoding="utf-8").split("\n", 2)[2]


def merge_outputs(parts: list[Path], out: Path, scenarios: list[Scenario] | None = None,
                  set_name: str = "") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Combine bench outputs of the same set run in separate processes (e.g. the model-using agent alone,
    the rest in parallel) into one results.csv / summary.csv / summary.md (+ audit/) in `out`."""
    order = {name: k for k, name in enumerate(AGENTS)}
    results = pd.concat([pd.read_csv(p / "results.csv") for p in parts], ignore_index=True)
    text = [c for c in ("root_causes", "predicted", "false_action_list", "unsafe_action_list", "unmitigated_at_end")
            if c in results]
    results[text] = results[text].astype(object).fillna("").astype(str)  # empty strings come back from CSV as NaN
    results = results.sort_values("scenario_id", kind="stable")
    results = results.sort_values("agent", key=lambda s: s.map(order), kind="stable").reset_index(drop=True)
    summary = summarize(results, {name: entry.note for name, entry in AGENTS.items()})
    out.mkdir(parents=True, exist_ok=True)
    results.to_csv(out / "results.csv", index=False, float_format="%.4f", lineterminator="\n")
    summary.to_csv(out / "summary.csv", index=False, float_format="%.4f", lineterminator="\n")
    (out / "summary.md").write_text(summary_markdown(results, summary) + category_section(results, scenarios, out, set_name),
                                    encoding="utf-8", newline="\n")
    for part in parts:
        for audit in sorted((part / "audit").glob("*.csv")) if (part / "audit").exists() else []:
            (out / "audit").mkdir(exist_ok=True)
            (out / "audit" / audit.name).write_bytes(audit.read_bytes())
    return results, summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help=f"comma-separated, from {sorted(AGENTS)}")
    parser.add_argument("--set", dest="scenario_set", choices=sorted(SETS), default="practice",
                        help="practice, practice2 (tune on them), test (round 1, held out) or safety")
    parser.add_argument("--scenarios", default="", help="comma-separated ids (default: the whole set)")
    parser.add_argument("--poll-min", type=int, default=POLL_MIN)
    parser.add_argument("--out", type=Path, default=None, help="default: bench/<set>")
    parser.add_argument("--merge", default="", help="comma-separated output folders to combine into --out (no run)")
    args = parser.parse_args(argv)
    if args.merge:
        out = args.out or BENCH_DIR / args.scenario_set
        results, _ = merge_outputs([Path(p) for p in args.merge.split(",") if p], out,
                                   load_scenarios(None, args.scenario_set), args.scenario_set)
        print(f"merged {len(results)} runs into {out}")
        return
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
    results, summary = write_outputs(records, args.out, scenarios, args.scenario_set)
    with pd.option_context("display.width", 200, "display.max_columns", 30, "display.float_format", "{:.2f}".format):
        print(summary.drop(columns="note").to_string(index=False))
    print(f"wrote {args.out / 'results.csv'}, summary.csv and summary.md ({len(results)} runs)")


if __name__ == "__main__":
    main()
