"""Seeded random scenarios (HANDOFF section 11).

    uv run python -m raqeeb.faults.generator [--n 20] [--seed 2025] [--full] [--out scenarios] [--prefix scn]
    uv run python -m raqeeb.faults.generator --test     # the held-out test set (scenarios/test/tst_*.yaml)

Each scenario has 1-4 faults on a fairly clear day of the weather year, 06:00-18:00 Riyadh.
The mix always includes "trap" scenarios (dust storm that soils panels AND the sensor by about
the same amount, in a cooler month so midday clipping does not hide the loss) and curtailment
decoys (a grid limit plus at least one real fault). The answer key is
Scenario.expected_answer_key(): every planted fault except the decoy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
from pathlib import Path

import numpy as np

from raqeeb.config import SCENARIO_DIR, TEST_SCENARIO_DIR, load_plant
from raqeeb.contracts import (
    CORE_FAULTS,
    FULL_SCOPE_FAULTS,
    PLANT_TARGET,
    STEP_MINUTES,
    FaultSpec,
    FaultType,
    Scenario,
)
from raqeeb.twin.weather import clear_days

DEFAULT_SEED = 2025
# The practice set is generate(20, DEFAULT_SEED) in scenarios/ (thresholds and timings are tuned on it, then
# frozen). The test set is held out: new seed, full-scope faults, ids tst_*, never used for tuning.
PRACTICE = {"n": 20, "seed": DEFAULT_SEED, "full_scope": False, "prefix": "scn"}
TEST = {"n": 50, "seed": 7777, "full_scope": True, "prefix": "tst"}
START_UTC = time(3, 0)  # 06:00 Riyadh
DURATION_HOURS = 12.0
COOL_MONTHS = (1, 2, 3, 11, 12)
CLEAR_DAYS_PER_MONTH = 8  # pick dates among the clearest days of each month

SHARE_TRAP = 0.25
SHARE_DECOY = 0.20

# (low, high) ranges; times are minutes after midnight UTC
STORM_START_MIN = (4 * 60 + 30, 6 * 60 + 30)
STORM_LENGTH_MIN = (45, 120)
SOILING_SEVERITY = (0.12, 0.30)
TRAP_SENSOR_DELTA = 0.03  # trap: sensor soiling within this of the panel soiling
SENSOR_START_MIN = (4 * 60, 8 * 60)
SENSOR_SEVERITY = (0.10, 0.25)  # >= 0.10: the reference itself is only good to a few percent
DERATE_START_MIN = (5 * 60, 8 * 60)
DERATE_SEVERITY = (0.25, 0.45)
COMMS_START_MIN = (4 * 60, 10 * 60)
COMMS_SELF_RECOVERS = 0.4  # share of dropouts with an end time
COMMS_LENGTH_MIN = (60, 180)
CURTAIL_START_MIN = (7 * 60 + 30, 10 * 60)  # around midday, when the limit binds
CURTAIL_LENGTH_MIN = (60, 120)
CURTAIL_SEVERITY = (0.30, 0.50)
CURTAIL_AFTER_STORM_MIN = 30
FULL_START_MIN = (4 * 60, 9 * 60)
STRING_SEVERITY = (0.15, 0.35)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _grid_minute(rng: np.random.Generator, bounds: tuple[int, int]) -> int:
    low, high = bounds
    return int(rng.integers(low // STEP_MINUTES, high // STEP_MINUTES + 1)) * STEP_MINUTES


def _severity(rng: np.random.Generator, bounds: tuple[float, float]) -> float:
    return round(float(rng.uniform(*bounds)), 2)


def candidate_days(months: tuple[int, ...] | None = None) -> list[date]:
    """The clearest days of each month (from the weather year), in date order."""
    by_month: dict[int, list[date]] = {}
    for day in clear_days(400):
        by_month.setdefault(day.month, []).append(day)
    chosen = [d for m, days in by_month.items() if months is None or m in months
              for d in days[:CLEAR_DAYS_PER_MONTH]]
    return sorted(chosen)


class _Builder:
    """Builds one scenario's fault list, keeping device targets distinct."""

    def __init__(self, rng: np.random.Generator):
        self.rng = rng
        self.plant = load_plant()
        self.free_inverters = list(self.plant.inverters.ids)
        self.storm_end: int | None = None

    def inverter(self) -> str:
        return self.free_inverters.pop(int(self.rng.integers(len(self.free_inverters))))

    def soiling(self) -> tuple[FaultSpec, int, float]:
        start = _grid_minute(self.rng, STORM_START_MIN)
        self.storm_end = start + _grid_minute(self.rng, STORM_LENGTH_MIN)
        severity = _severity(self.rng, SOILING_SEVERITY)
        spec = FaultSpec(type=FaultType.SOILING, target=PLANT_TARGET, start=_hhmm(start),
                         end=_hhmm(self.storm_end), severity=severity)
        return spec, start, severity

    def build(self, types: list[FaultType], trap: bool) -> list[FaultSpec]:
        rng, faults = self.rng, []
        storm_start, panel_soiling = None, None
        if FaultType.SOILING in types:
            spec, storm_start, panel_soiling = self.soiling()
            faults.append(spec)
        for fault_type in types:
            if fault_type == FaultType.SOILING:
                continue
            if fault_type == FaultType.SENSOR_SOILING:
                if trap:
                    severity = panel_soiling + float(rng.uniform(-TRAP_SENSOR_DELTA, TRAP_SENSOR_DELTA))
                    start, severity = storm_start, round(max(severity, SENSOR_SEVERITY[0]), 2)
                else:
                    start, severity = _grid_minute(rng, SENSOR_START_MIN), _severity(rng, SENSOR_SEVERITY)
                faults.append(FaultSpec(type=fault_type, target=self.plant.weather_station_id,
                                        start=_hhmm(start), severity=severity))
            elif fault_type == FaultType.INVERTER_DERATE:
                faults.append(FaultSpec(type=fault_type, target=self.inverter(),
                                        start=_hhmm(_grid_minute(rng, DERATE_START_MIN)),
                                        severity=_severity(rng, DERATE_SEVERITY)))
            elif fault_type == FaultType.COMMS_DROPOUT:
                start = _grid_minute(rng, COMMS_START_MIN)
                end = _hhmm(start + _grid_minute(rng, COMMS_LENGTH_MIN)) if rng.random() < COMMS_SELF_RECOVERS else None
                faults.append(FaultSpec(type=fault_type, target=self.inverter(), start=_hhmm(start), end=end))
            elif fault_type == FaultType.CURTAILMENT:
                start = _grid_minute(rng, CURTAIL_START_MIN)
                if self.storm_end is not None:  # the limit must bind, so not under storm dimming
                    start = max(start, self.storm_end + CURTAIL_AFTER_STORM_MIN)
                end = start + _grid_minute(rng, CURTAIL_LENGTH_MIN)
                faults.append(FaultSpec(type=fault_type, target=PLANT_TARGET, start=_hhmm(start), end=_hhmm(end),
                                        severity=_severity(rng, CURTAIL_SEVERITY)))
            elif fault_type == FaultType.TRACKER_STUCK:
                faults.append(FaultSpec(type=fault_type, target=self.inverter(),
                                        start=_hhmm(_grid_minute(rng, FULL_START_MIN))))
            elif fault_type == FaultType.STRING_OPEN:
                faults.append(FaultSpec(type=fault_type, target=self.inverter(),
                                        start=_hhmm(_grid_minute(rng, FULL_START_MIN)),
                                        severity=_severity(rng, STRING_SEVERITY)))
            elif fault_type == FaultType.UNAUTHORIZED_CMD:
                faults.append(FaultSpec(type=fault_type, target=self.inverter(),
                                        start=_hhmm(_grid_minute(rng, FULL_START_MIN))))
        return sorted(faults, key=lambda f: f.start)


def _pick(rng: np.random.Generator, pool: list[FaultType], k: int) -> list[FaultType]:
    return [pool[i] for i in rng.choice(len(pool), size=k, replace=False)]


def _kinds(n: int, rng: np.random.Generator) -> list[str]:
    n_trap = max(1, round(n * SHARE_TRAP)) if n >= 3 else 0
    n_decoy = max(1, round(n * SHARE_DECOY)) if n >= 3 else 0
    kinds = ["trap"] * n_trap + ["decoy"] * n_decoy + ["random"] * (n - n_trap - n_decoy)
    rng.shuffle(kinds)
    return kinds


def _fault_types(kind: str, rng: np.random.Generator, full_scope: bool) -> list[FaultType]:
    real = sorted(CORE_FAULTS - {FaultType.CURTAILMENT} | (FULL_SCOPE_FAULTS if full_scope else set()))
    if kind == "trap":
        others = [t for t in real if t not in (FaultType.SOILING, FaultType.SENSOR_SOILING)]
        return [FaultType.SOILING, FaultType.SENSOR_SOILING] + _pick(rng, others, int(rng.integers(0, 3)))
    if kind == "decoy":
        return [FaultType.CURTAILMENT] + _pick(rng, real, int(rng.integers(1, 4)))
    pool = real + [FaultType.CURTAILMENT]
    while True:  # at least one real fault; panel + sensor soiling together only in designed traps
        types = _pick(rng, pool, int(rng.integers(1, 5)))
        if set(types) - {FaultType.CURTAILMENT} and not {FaultType.SOILING, FaultType.SENSOR_SOILING} <= set(types):
            return types


def generate(n: int = 20, seed: int = DEFAULT_SEED, full_scope: bool = False, prefix: str = "scn") -> list[Scenario]:
    """n scenarios; the same (n, seed, full_scope) always gives the same scenarios, with ids <prefix>_0001..."""
    rng = np.random.default_rng(seed)
    any_day, cool_day = candidate_days(), candidate_days(COOL_MONTHS)
    scenarios = []
    for i, kind in enumerate(_kinds(n, rng), start=1):
        types = _fault_types(kind, rng, full_scope)
        trap = kind == "trap"
        day = (cool_day if trap else any_day)[int(rng.integers(len(cool_day if trap else any_day)))]
        faults = _Builder(rng).build(types, trap)
        scenario = Scenario(
            id=f"{prefix}_{i:04d}", seed=seed * 1000 + i,
            start=datetime.combine(day, START_UTC, tzinfo=timezone.utc),
            duration_hours=DURATION_HOURS, faults=faults,
        )
        scenarios.append(scenario.model_copy(update={"answer_key": scenario.expected_answer_key()}))
    return scenarios


def storm_scenario(seed: int, n_extra: int = 2, full_scope: bool = False) -> Scenario:
    """A dust storm plus `n_extra` other random faults on a cool clear day (console "inject" button).

    A dusty sensor drawn alongside the storm makes it a trap (sensor soiled about as much as the panels).
    """
    rng = np.random.default_rng([seed, 7])
    others = sorted(CORE_FAULTS - {FaultType.SOILING} | (FULL_SCOPE_FAULTS if full_scope else set()))
    types = [FaultType.SOILING] + _pick(rng, others, n_extra)
    days = candidate_days(COOL_MONTHS)
    day = days[int(rng.integers(len(days)))]
    faults = _Builder(rng).build(types, trap=FaultType.SENSOR_SOILING in types)
    scenario = Scenario(
        id=f"storm_{seed}", seed=seed, start=datetime.combine(day, START_UTC, tzinfo=timezone.utc),
        duration_hours=DURATION_HOURS, faults=faults,
    )
    return scenario.model_copy(update={"answer_key": scenario.expected_answer_key()})


def is_trap(scenario: Scenario) -> bool:
    types = {f.type for f in scenario.faults}
    return {FaultType.SOILING, FaultType.SENSOR_SOILING} <= types


def has_decoy(scenario: Scenario) -> bool:
    return any(f.type == FaultType.CURTAILMENT for f in scenario.faults)


HEADER = ("# SYNTHETIC scenario from raqeeb.faults.generator. Fault times are UTC on the start date.\n"
          "# Faults and answer_key are for the injector and bench only; the agent gets Scenario.agent_view().\n")


def write_scenarios(scenarios: list[Scenario], out_dir: Path = SCENARIO_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for scenario in scenarios:
        path = out_dir / f"{scenario.id}.yaml"
        scenario.to_yaml(path, header=HEADER)
        paths.append(path)
    return paths


def load_scenarios(directory: Path = SCENARIO_DIR, prefix: str = "scn") -> list[Scenario]:
    return [Scenario.from_yaml(p) for p in sorted(directory.glob(f"{prefix}_*.yaml"))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--full", action="store_true", help="include full-scope faults")
    parser.add_argument("--out", type=Path, default=SCENARIO_DIR)
    parser.add_argument("--prefix", default="scn", help="scenario id prefix")
    parser.add_argument("--test", action="store_true", help="write the held-out test set (overrides the others)")
    args = parser.parse_args()
    if args.test:
        args.n, args.seed, args.full, args.prefix, args.out = (TEST["n"], TEST["seed"], TEST["full_scope"],
                                                                TEST["prefix"], TEST_SCENARIO_DIR)
    scenarios = generate(args.n, args.seed, args.full, args.prefix)
    for path, scenario in zip(write_scenarios(scenarios, args.out), scenarios):
        tags = [t for t, on in (("trap", is_trap(scenario)), ("decoy", has_decoy(scenario))) if on]
        print(f"{path.name}: {scenario.start.date()} {', '.join(f.label for f in scenario.faults)}"
              + (f"  [{', '.join(tags)}]" if tags else ""))


if __name__ == "__main__":
    main()
