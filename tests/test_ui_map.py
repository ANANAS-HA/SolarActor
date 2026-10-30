"""Plant map (ui/plantmap.py): statuses come from telemetry and the agent's record only; a missing row is
"no data", never zero; badges follow the agent's actions, escalations and accepted proposals."""

from __future__ import annotations

import ast
import json
import math
from datetime import timedelta
from pathlib import Path

import pytest

from raqeeb.config import load_plant
from raqeeb.ui import live, plantmap, source
from raqeeb.ui.live import ReplayRun
from raqeeb.ui.runlog import Mode, RunLog

PLANT = load_plant()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(live.NO_LLM_ENV, "1")
    monkeypatch.setenv(live.RUN_DIR_ENV, str(tmp_path))


def _replay(name: str, minutes: int | None = None) -> ReplayRun:
    run = source.start_replay(source.replay_dir() / name)
    if minutes is None:
        run.run_to_end()
    else:
        run.advance(minutes)
    return run


def _states(view: plantmap.MapView) -> dict[str, str]:
    return {t.id: t.state for t in view.inverters}


def test_silent_inverter_is_no_data_not_zero():
    run = source.start_live("scn_0018", Mode.RULES)  # comms dropout on INV-12 from 07:00
    run.advance(4 * 60 + 15)  # 03:00 -> 07:15 UTC
    view = plantmap.map_view(run)
    tile = next(t for t in view.inverters if t.id == "INV-12")
    assert tile.state == plantmap.NO_DATA and tile.ac_kw is None
    assert not any("MW" in line for line in tile.lines) and "no data" in tile.lines
    assert sum(s == plantmap.NORMAL for s in _states(view).values()) == PLANT.inverters.count - 1
    run.close()


def test_missing_rows_are_no_data_and_zero_rows_are_zero():
    """Edit a recorded run's telemetry: drop INV-05's last row, set INV-06's to 0 kW."""
    log = RunLog.load(source.replay_dir() / source.DEFAULT_REPLAY)
    rows = log.telemetry["inverter_readings"]
    last = max(r["ts"] for r in rows)
    log.telemetry["inverter_readings"] = [r for r in rows if not (r["ts"] == last and r["inverter_id"] == "INV-05")]
    for r in log.telemetry["inverter_readings"]:
        if r["ts"] == last and r["inverter_id"] == "INV-06":
            r["ac_kw"], r["status"] = 0.0, "STOPPED"
    run = ReplayRun(log)
    run.run_to_end()
    tiles = {t.id: t for t in plantmap.map_view(run).inverters}
    assert tiles["INV-05"].state == plantmap.NO_DATA and tiles["INV-05"].ac_kw is None
    assert tiles["INV-06"].state != plantmap.NO_DATA and tiles["INV-06"].ac_kw == 0.0
    assert "0.00 MW" in tiles["INV-06"].lines
    history = plantmap.inverter_history(run, "INV-05")
    assert math.isnan(history["inverter_kw"].iloc[-1])  # a gap in the line, not a drop to zero


def test_map_reads_no_scenario_or_answer_key():
    """The same telemetry and agent record give the same map with the answer key removed; the module
    never names the scenario, its faults, the answer key or the twin's truth."""
    full = _replay(source.DEFAULT_REPLAY)
    data = json.loads(full.log.to_json())
    data["answer_key"] = None
    blind = ReplayRun(RunLog.model_validate(data))
    blind.run_to_end()
    assert plantmap.map_view(blind) == plantmap.map_view(full)

    names = {n.id if isinstance(n, ast.Name) else n.attr
             for n in ast.walk(ast.parse(Path(plantmap.__file__).read_text(encoding="utf-8")))
             if isinstance(n, (ast.Name, ast.Attribute))}
    assert not names & {"answer_key", "_scenario", "faults", "truth", "_inj", "twin", "Scenario"}


def test_trap_storm_in_the_scn_0007_replay():
    """At the storm's worst poll: trackers stowed, the meter low against the reference but on the sensor's line."""
    run = _replay(source.DEFAULT_REPLAY, 4 * 60 + 30)  # 07:30 UTC
    bands = plantmap.storm_bands(plantmap.sky_strip(run))
    assert bands and bands[0][0] <= run.now
    view = plantmap.map_view(run)
    assert all("tracker stowed (wind)" in t.lines for t in view.inverters if t.state != plantmap.NO_DATA)
    assert view.meter.state == plantmap.LOW
    ref, sensor = (next(line for line in view.meter.lines if f"({name})" in line)
                   for name in ("reference", "on-site sensor"))
    assert int(ref.split("%")[0]) < 90 <= int(sensor.split("%")[0])
    assert "cleaning ordered" in view.plant
    assert view.station.state == plantmap.NORMAL  # the agent has not flagged the sensor yet


def test_badges_follow_the_agent_record():
    end = plantmap.map_view(_replay(source.DEFAULT_REPLAY))
    tiles = {t.id: t for t in end.inverters}
    assert tiles["INV-10"].state == plantmap.RECOVERED and "reset sent" in tiles["INV-10"].badges
    assert "cleaned" in end.plant and end.station.state == plantmap.FLAGGED

    unknown = _replay("pru_0001_test.json")
    view = plantmap.map_view(unknown)
    assert view.blocks["T3"] == ["TRANSFORMER_TRIP proposed → accepted → escalated"]
    assert not view.blocks["T1"]  # the rejected proposal changed nothing, so no badge
    assert {_states(view)[i] for i in PLANT.layout.transformers["T3"]} == {plantmap.LOW}
    before = plantmap.map_view(_replay("pru_0001_test.json", 5 * 60 + 45))  # 08:45, before the proposal
    assert not before.blocks["T3"]


def test_pending_approval_shows_on_the_tile():
    run = source.start_live("scn_0002", Mode.RULES)  # a derate whose power limit needs approval
    run.run_to_end()
    target = run.waiting[0].request.target
    tile = next(t for t in plantmap.map_view(run).inverters if t.id == target)
    assert tile.state == plantmap.PENDING and any(b.startswith("awaiting approval") for b in tile.badges)
    run.close()


def test_every_shipped_replay_draws_at_every_poll():
    for path in source.replay_files():
        run = source.start_replay(path)
        while not run.finished:
            run.advance(60)
            view = plantmap.map_view(run)
            assert len(view.inverters) == PLANT.inverters.count
            assert set(_states(view).values()) <= set(plantmap.STATES)
            fig = plantmap.map_figure(view, PLANT)
            assert list(fig.data[0].customdata) == PLANT.inverters.ids  # click targets, in inverter order
            text = " ".join(a.text for a in fig.layout.annotations)
            assert all(f"{plantmap.ICON[t.state]} {t.state}" in text for t in view.inverters)
        history = plantmap.inverter_history(run, "INV-01")
        assert len(history) == int(run.incident.duration_hours * 60 / 5)
        assert run.now - run.incident.start <= timedelta(hours=run.incident.duration_hours)
