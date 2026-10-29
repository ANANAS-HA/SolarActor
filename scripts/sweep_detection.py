"""Year-long detection sweep for the agent tools (synthetic plant, real Riyadh 2025 weather).

    uv run python scripts/sweep_detection.py [--step-days 14] [--runs-per-day 2]

Four scenarios, run through the FaultInjector on a day every --step-days days of 2025
(07:00-18:00 Riyadh), each run with its own fixed seed, so each run gets a different
reference bias. The tools see only the telemetry store and the agent's window.

Table 1, at three levels of the twin's reference error (true sunlight vs the NASA reference
the agent uses: off, the plant.yaml value, doubled): how often sensor_check gives the right
verdict and plant_performance the right underperforming / ok call.
Table 2, at the plant.yaml level: symptom alarms (LOW_PR, LOW_STRING_CURRENT, DC_INPUT_LOW)
from the injector, which compare inverters with expected-from-sensor as a plant SCADA would.
Results are deterministic: the same arguments print the same tables.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from raqeeb.config import load_plant
from raqeeb.contracts import FaultSpec, PlantConfig, ReferenceErrorConfig, Scenario
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools import metrics, weather_tools

YEAR_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
FIRST_DAY = 3  # 4 Jan
START_H, HOURS = 4, 11  # 04:00-15:00 UTC = 07:00-18:00 Riyadh
SEED_BASE = 1000
SYMPTOMS = ("LOW_PR", "LOW_STRING_CURRENT", "DC_INPUT_LOW")
PLANT_WIDE = 12  # symptom alarm on at least this many inverters

STORM = {"type": "SOILING", "target": "ALL", "start": "06:00", "end": "07:00", "severity": 0.20}
SENSOR_DUST = {"type": "SENSOR_SOILING", "target": "WS-01", "start": "07:00", "severity": 0.20}
# name: (faults, expected sensor_check verdict, plant should be underperforming)
SCENARIOS = {
    "healthy": ([], "ok", False),
    "dusty sensor only": ([SENSOR_DUST], "sensor_low", False),
    "dusty panels only": ([STORM], "ok", True),
    "trap (both dusty)": ([STORM, SENSOR_DUST | {"start": STORM["start"]}], "sensor_low_panels_too", True),
}


def error_levels(plant: PlantConfig) -> dict[str, ReferenceErrorConfig]:
    cfg = plant.reference_error
    return {
        "off": ReferenceErrorConfig(bias_sigma=0.0, sigma=0.0, tau_min=cfg.tau_min, max_abs=0.01),
        f"plant.yaml (bias sd {cfg.bias_sigma:.0%}, slow sd {cfg.sigma:.1%}, max {cfg.max_abs:.0%})": cfg,
        "doubled": ReferenceErrorConfig(bias_sigma=2 * cfg.bias_sigma, sigma=2 * cfg.sigma, tau_min=cfg.tau_min,
                                        max_abs=min(2 * cfg.max_abs, 0.5)),
    }


def run_one(faults: list[dict], day: datetime, seed: int, plant: PlantConfig):
    scenario = Scenario(id="sweep", seed=seed, start=day + timedelta(hours=START_H), duration_hours=HOURS,
                        faults=[FaultSpec(**f) for f in faults])
    inj = FaultInjector.for_scenario(scenario, plant=plant)
    store = TelemetryStore()
    store.write_batch(inj.advance(HOURS * 60))
    view = scenario.agent_view()
    sensor = weather_tools.sensor_check(store, view.start, view.end)
    perf = metrics.plant_performance(store, view.start, view.end)
    events = store.events()
    symptoms = events[events["code"].isin(SYMPTOMS)]
    widest = int(symptoms.groupby("code")["device_id"].nunique().max()) if len(symptoms) else 0
    return sensor, perf, len(symptoms), widest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--step-days", type=int, default=14, help="one day every N days of 2025 (default 14)")
    parser.add_argument("--runs-per-day", type=int, default=2, help="runs (seeds) per day (default 2)")
    args = parser.parse_args()

    base = load_plant()
    days = [YEAR_START + timedelta(days=d) for d in range(FIRST_DAY, 365, args.step_days)]
    n = len(days) * args.runs_per_day
    print(f"{len(days)} days x {args.runs_per_day} runs = {n} runs per scenario; seeds from {SEED_BASE}\n")
    alarms: dict[str, Counter] = {}
    t0 = time.time()

    print("Table 1: right verdicts")
    print("| reference error | scenario | sensor_check | plant_performance | sensor_check misses |")
    print("|---|---|---|---|---|")
    for level_name, err in error_levels(base).items():
        plant = base.model_copy(update={"reference_error": err})
        is_config_level = err == base.reference_error
        for name, (faults, verdict, underperforming) in SCENARIOS.items():
            sensor_ok = perf_ok = 0
            misses: Counter = Counter()
            alarm_counts: Counter = Counter()
            for i, day in enumerate(days):
                for k in range(args.runs_per_day):
                    sensor, perf, n_symptoms, widest = run_one(faults, day, SEED_BASE + args.runs_per_day * i + k, plant)
                    sensor_ok += sensor.verdict == verdict
                    perf_ok += (perf.verdict == "underperforming") == underperforming
                    if sensor.verdict != verdict:
                        misses[f"{sensor.verdict} {day:%d %b}"] += 1
                    alarm_counts["runs with symptom alarms"] += n_symptoms > 0
                    alarm_counts["runs with plant-wide symptom alarms"] += widest >= PLANT_WIDE
                    alarm_counts["symptom alarms"] += n_symptoms
            if is_config_level:
                alarms[name] = alarm_counts
            miss_text = ", ".join(f"{m} x{c}" if c > 1 else m for m, c in sorted(misses.items())) or "-"
            print(f"| {level_name} | {name} | {sensor_ok}/{n} | {perf_ok}/{n} | {miss_text} |", flush=True)

    print("\nTable 2: symptom alarms from the injector (reference error at the plant.yaml level)")
    print(f"| scenario | runs with symptom alarms | runs with plant-wide (>= {PLANT_WIDE} inverters) | alarms per run |")
    print("|---|---|---|---|")
    for name, c in alarms.items():
        print(f"| {name} | {c['runs with symptom alarms']}/{n} | {c['runs with plant-wide symptom alarms']}/{n} "
              f"| {c['symptom alarms'] / n:.1f} |")
    print(f"\n{time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
