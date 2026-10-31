"""Everything the console reads, behind one adapter.

    run = start_live(scenario_id, Mode.RULES)    # ui/live.py LiveRun: the real agent on the twin
    run = start_replay(path)                     # ui/live.py ReplayRun: a recorded run
    tables(run)                                  # telemetry up to run.now, one DataFrame per table
    power_lines(run)                             # actual vs expected (reference) vs expected (sensor)
    reveal(run)                                  # answer key vs the agent: None until the run has finished
    load_results(path)                           # benchmark output (bench/<set>/summary.csv, results.csv)

Scenarios offered for live runs are the practice sets (scenarios/scn_*, scenarios/practice2/pr2_*,
scenarios/practice_unknown/pru_*) and generated dust storms; the held-out sets are never listed or read here.
"""

from __future__ import annotations

import gzip
import html
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from raqeeb.bench.score import addresses
from raqeeb.config import BENCH_DIR, PRACTICE2_SCENARIO_DIR, PRACTICE_UNKNOWN_SCENARIO_DIR, SCENARIO_DIR, load_plant
from raqeeb.contracts import (
    DECOY_FAULTS,
    DISPLAY_TZ,
    STEP_MINUTES,
    TABLE_MODELS,
    FaultType,
    PlantConfig,
    Scenario,
)
from raqeeb.faults.generator import is_trap, load_scenarios, storm_scenario
from raqeeb.tools import physics as tool_physics
from raqeeb.ui.live import LiveRun, ReplayRun
from raqeeb.ui.runlog import GZIP_MAGIC, Mode, RunLog

Run = LiveRun | ReplayRun


REPLAY_DIR_ENV = "RAQEEB_REPLAY_DIR"
RESULTS_ENV = "RAQEEB_RESULTS"
REPLAY_DIR = Path(__file__).resolve().parent / "replays"
DEFAULT_REPLAY = "scn_0007_test.json"  # dusty panels + dusty sensor, comms loss, a derate that needs approval
DEFAULT_RESULTS = BENCH_DIR / "final"
STEP = timedelta(minutes=STEP_MINUTES)

FOOTER = "Synthetic plant and faults; weather data from NASA POWER (2025, Riyadh)."
NASA_CREDIT = ("Weather: NASA Langley Research Center (LaRC) POWER Project, funded through the NASA Earth "
               "Science/Applied Science Program (hourly, community RE, 2025, 24.71 N 46.68 E).")


# -- scenarios and replays ----------------------------------------------------------


def practice_scenarios() -> list[Scenario]:
    return (load_scenarios(SCENARIO_DIR, "scn") + load_scenarios(PRACTICE2_SCENARIO_DIR, "pr2")
            + load_scenarios(PRACTICE_UNKNOWN_SCENARIO_DIR, "pru"))


def scenario_ids() -> list[str]:
    return [s.id for s in practice_scenarios()]


def default_scenario_id() -> str:
    """The first trap scenario (dusty panels and a dusty sensor), else the first one."""
    scenarios = load_scenarios(SCENARIO_DIR, "scn")
    return next((s.id for s in scenarios if is_trap(s)), scenarios[0].id)


def scenario(scenario_id: str) -> Scenario:
    if scenario_id.startswith("storm_"):
        return storm_scenario(int(scenario_id.removeprefix("storm_")))
    return next(s for s in practice_scenarios() if s.id == scenario_id)


def start_live(scenario_id: str, mode: Mode = Mode.RULES, **kwargs) -> LiveRun:
    return LiveRun(scenario(scenario_id), mode, **kwargs)


def replay_dir() -> Path:
    return Path(os.environ.get(REPLAY_DIR_ENV) or REPLAY_DIR)


def replay_files() -> list[Path]:
    """Recorded runs, the trap demo (DEFAULT_REPLAY) first."""
    return sorted(replay_dir().glob("*.json"), key=lambda p: (p.name != DEFAULT_REPLAY, p.name))


def replay_title(path: Path) -> str:
    log = RunLog.load(path)
    return f"{log.incident.id} · {log.label}"


def start_replay(source: Path | str | bytes) -> ReplayRun:
    """A replay from a file path or the bytes of an uploaded file (plain or gzip-compressed JSON)."""
    if isinstance(source, (str, Path)):
        return ReplayRun(RunLog.load(Path(source)))
    raw = gzip.decompress(source) if source[:2] == GZIP_MAGIC else source
    return ReplayRun(RunLog.from_json(raw))


def trajectory_files(path: Path | str) -> list[Path]:
    """Replay / trajectory files at `path`: the file itself, or the *.json and *.json.gz in a folder (and in
    its trajectories/ subfolder, where the bench writes them), sorted by name."""
    path = Path(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    found = [p for folder in (path, path / "trajectories") if folder.is_dir()
             for pattern in ("*.json", "*.json.gz") for p in folder.glob(pattern)]
    return sorted(set(found), key=lambda p: (p.parent != path, p.name))


class RunAt:
    """A run seen at an earlier sim time (the Trace tab's clock slider once a run has ended). Read only:
    every view the console draws is cut at `now`, so this is the run as it stood then."""

    def __init__(self, run: Run, now: datetime):
        self.base, self.now = run, now
        self.log, self.store, self.incident, self.mode = run.log, run.store, run.incident, run.mode
        self.waiting: list = []
        self.finished = True


def mode_label(run: Run) -> str:
    if isinstance(run, ReplayRun):
        return f"Replay: {run.log.label}" + (f" ({run.log.model})" if run.log.model else "")
    return run.log.label + (f" ({run.log.model})" if run.log.model else "")


# -- telemetry views ----------------------------------------------------------------


def tables(run: Run) -> dict[str, pd.DataFrame]:
    """Every telemetry table up to and including `run.now`, with `ts` (UTC) as a column."""
    return {name: run.store.read(name, end=run.now + STEP).reset_index() for name in TABLE_MODELS}


def power_lines(run: Run, plant: PlantConfig | None = None) -> pd.DataFrame:
    """Plant AC in kW over the whole incident window, NaN after `run.now`: actual (meter), expected from
    the trusted reference, expected from the sensor; plus the irradiance behind them.

    Both expected lines come from raqeeb.tools.physics, the same calculation the agent's tools and
    the injector's symptom alarms use. The sensor line scales the reference by sensor GHI /
    reference GHI, as a plant SCADA's performance check does, so a dusty sensor pulls it down to
    meet a dusty plant (the trap).
    """
    start, end = run.incident.start, run.incident.end
    grid = pd.date_range(start, end, freq=f"{STEP_MINUTES}min", inclusive="left", name="ts")
    cut = min(run.now + STEP, end)
    store = run.store
    columns = ["actual_kw", "expected_ref_kw", "expected_sensor_kw", "sensor_ghi_wm2", "reference_ghi_wm2",
               "clearsky_ghi_wm2"]
    if store.span("meter") is None or cut <= start:
        return pd.DataFrame(index=grid, columns=columns, dtype=float)
    ref = store.weather_reference(start, cut).reindex(grid)
    frame = pd.DataFrame({
        "actual_kw": store.meter(start, cut)["plant_ac_kw"].reindex(grid),
        "expected_ref_kw": tool_physics.expected_plant_ac(store, start, cut, plant, basis="reference").reindex(grid),
        "expected_sensor_kw": tool_physics.expected_plant_ac(store, start, cut, plant, basis="sensor").reindex(grid),
        "sensor_ghi_wm2": store.weather_station(start, cut)["ghi_sensor_wm2"].reindex(grid),
        "reference_ghi_wm2": ref["ghi_wm2"],
        "clearsky_ghi_wm2": ref["clearsky_ghi_wm2"],
    }, index=grid)
    return frame[columns]


# -- answer key (only after the run) -----------------------------------------------------


@dataclass
class Reveal:
    faults: pd.DataFrame  # one row per planted fault: label, start, kind, named by the agent, actions on it
    extra_causes: list[str]  # causes the agent named that were not planted
    actions: pd.DataFrame  # every action the agent requested and which planted cause it addressed
    found: int
    root_causes: int


def reveal(run: Run) -> Reveal | None:
    """The answer key next to the agent's causes and actions; None while the run is in progress."""
    key = run.log.answer_key
    if not run.finished or key is None:
        return None
    diagnosis = run.log.diagnosis_until(run.now)
    predicted = {label for h in (diagnosis.root_causes if diagnosis else []) for label in h.labels}
    actions = run.log.actions_until(run.now)
    truth = set(key.root_causes)
    probe = Scenario(id="probe", seed=0, start=run.incident.start, duration_hours=24, faults=[])
    rows = []
    for f in key.faults:
        kind = ("decoy (expected behaviour)" if f.type == FaultType.CURTAILMENT
                else "decoy (injected instruction)" if f.type in DECOY_FAULTS
                else "root cause" if f.label in truth else "planted, not scored")
        acted = [f"{a.request.action.value} ({a.status.value.lower() if a.status else 'no decision'})"
                 for a in actions if f.label in truth and addresses(a.request, f.label)]
        rows.append({"Planted fault": f.label, "From": local_hhmm(probe.resolve_time(f.start)), "Kind": kind,
                     "Agent named it": "yes" if f.label in predicted else "no",
                     "Agent actions on it": ", ".join(acted) or "none"})
    action_rows = [{"Time": local_hhmm(a.requested_at), "Action": a.request.action.value, "Target": a.request.target,
                    "Status": a.status.value if a.status else "waiting",
                    "Addresses": ", ".join(label for label in key.root_causes if addresses(a.request, label))
                    or "no planted root cause"} for a in actions]
    return Reveal(faults=pd.DataFrame(rows), extra_causes=sorted(predicted - truth),
                  actions=pd.DataFrame(action_rows, columns=["Time", "Action", "Target", "Status", "Addresses"]),
                  found=len(truth & predicted), root_causes=len(truth))


def local_hhmm(ts: datetime) -> str:
    return pd.Timestamp(ts).tz_convert(DISPLAY_TZ).strftime("%H:%M")


# -- benchmark results ---------------------------------------------------------------


@dataclass
class BenchResults:
    path: Path
    summary: pd.DataFrame  # one row per agent (bench/score.summarize)
    results: pd.DataFrame | None  # one row per agent x scenario
    markdown: str  # summary.md, if present


def default_results_path() -> Path:
    return Path(os.environ.get(RESULTS_ENV) or DEFAULT_RESULTS)


def results_folders() -> list[Path]:
    """Bench output folders that have a summary (bench/<set>/summary.csv)."""
    return sorted(p.parent for p in BENCH_DIR.glob("*/summary.csv"))


def load_results(path: Path | str) -> BenchResults | None:
    """Bench output from a folder (summary.csv, results.csv, summary.md) or one summary file (.csv or .json).
    None when there is nothing there yet."""
    path = Path(path)
    if path.is_dir():
        summary = _table(path / "summary.csv") if (path / "summary.csv").exists() else _table(path / "summary.json")
        results = _table(path / "results.csv") if (path / "results.csv").exists() else _table(path / "results.json")
        md = path / "summary.md"
        markdown = md.read_text(encoding="utf-8") if md.exists() else ""
    else:
        summary, results, markdown = _table(path), None, ""
    if summary is None or summary.empty or "agent" not in summary.columns:
        return None
    return BenchResults(path=path, summary=summary, results=results, markdown=markdown)


def _table(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        return pd.read_json(path) if path.suffix == ".json" else pd.read_csv(path)
    except ValueError:
        return None


# -- untrusted text ------------------------------------------------------------------

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:&])")


def md_escape(text: object) -> str:
    """Plain text for use inside st.markdown: no markdown, links, images, HTML, colour or emoji codes."""
    escaped = _MD_SPECIAL.sub(r"\\\1", str(text))
    return escaped.replace("\r\n", "\n").replace("\n", "  \n")


def html_text(text: object) -> str:
    """Plain text for use inside an HTML snippet."""
    return html.escape(str(text)).replace("\n", "<br>")
