"""Plant tab panels (ui/plantpanels.py): the stat row, legend tables, numbered markers, the HTML map and the
equipment cards are built from telemetry and the agent's record, read as words (no raw ids outside Show data)
and escape plant free text."""

from __future__ import annotations

import html
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from raqeeb.config import load_plant
from raqeeb.contracts import ActionStatus
from raqeeb.ui import live, plain, plantmap, plantpanels, source
from raqeeb.ui.runlog import Mode

PLANT = load_plant()
APP = Path(__file__).resolve().parents[1] / "raqeeb" / "ui" / "app.py"
HOSTILE = "<img src=x onerror=alert(1)> **bold** [link](http://x)"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(live.NO_LLM_ENV, "1")
    monkeypatch.setenv(live.RUN_DIR_ENV, str(tmp_path))


def _replay(name: str, minutes: int | None = None):
    run = source.start_replay(source.replay_dir() / name)
    if minutes is None:
        run.run_to_end()
    else:
        run.advance(minutes)
    return run


def test_stats_read_as_words_with_a_state():
    run = _replay(source.DEFAULT_REPLAY, 6 * 60)  # midday of the trap storm
    power = source.power_lines(run)
    events = source.tables(run)["events"]
    stats = plantpanels.stats(run, power, events, PLANT)
    assert [s.label for s in stats] == ["Plant output now", "Energy below expected", "Alarms so far",
                                        "Waiting for your approval"]
    assert stats[0].value.endswith("MW") and "against expected" in stats[0].note or stats[0].note == "on expected"
    assert stats[1].value.endswith("MWh") and stats[2].value.isdigit()
    assert stats[3].value == "0" and stats[3].note == "Nothing to approve" and stats[3].state == "ok"
    assert all(s.state in plantpanels.STATE_INK for s in stats)
    assert len(stats[0].spark) > 2 and len(stats[1].spark) == len(stats[0].spark)
    page = plantpanels.stats_html(stats)
    assert page.count('class="rq-stat"') == 4 and "rq-spark" in page
    assert "INV-" not in page and "METER-" not in page


def test_legend_rows_markers_and_takeaways():
    run = _replay(source.DEFAULT_REPLAY, 6 * 60)
    power = source.power_lines(run)
    dropped = plantpanels.sensor_dropped_at(run)
    rows = plantpanels.legend_output(power, dropped)
    assert [r.name for r in rows] == ["Actual", "Expected, satellite weather", "Expected, plant sun sensor"]
    assert all(r.now.endswith("MW") and r.low.endswith("MW") and r.high.endswith("MW") for r in rows)
    if dropped is not None:
        assert "stopped using it at" in rows[2].meaning
    table = plantpanels.legend_html(rows)
    assert table.count("<tr>") == 4 and "stroke-dasharray" in table  # header + 3 rows; dashed samples
    marks = plantpanels.markers(run)
    applied = [a for a in run.log.actions_until(run.now) if a.status == ActionStatus.APPLIED]
    assert len(marks) == len(applied) and [m.n for m in marks] == list(range(1, len(marks) + 1))
    assert marks == sorted(marks, key=lambda m: m.ts)
    if marks:
        assert "①" in plantpanels.markers_html(marks) and "_" not in marks[0].text
    sky = plantmap.sky_strip(run, PLANT)
    assert [r.name for r in plantpanels.legend_sky(sky, PLANT)] == ["Sun height", "Wind on site"]
    text, state = plantpanels.takeaway_sky(sky, PLANT)
    assert text.startswith("Wind ") and state in plantpanels.STATE_INK
    text, state = plantpanels.takeaway_output(power, PLANT)
    assert text.startswith("Now ") and state in plantpanels.STATE_INK


def test_map_and_equipment_html_use_words_and_escape_free_text():
    run = _replay(source.DEFAULT_REPLAY)
    view = plantmap.map_view(run, PLANT)
    view.inverters[0].badges.append(HOSTILE)
    view.station.badges.append(HOSTILE)
    page = plantpanels.map_html(view, PLANT)
    assert "Transformer 1" in page and "Group A" in page and plain.device("INV-01") in page
    assert "INV-01" not in page and "<img" not in page and html.escape(HOSTILE) in page
    assert "Plant-wide:" in page or "No plant-wide action" in page
    assert page.count('class="rq-tile') == PLANT.inverters.count
    equipment = plantpanels.equipment_html(view, PLANT)
    for name in ("Grid connection", "Substation meter", "Weather station"):
        assert name in equipment
    assert "WS-01" not in equipment and "METER-01" not in equipment and "<img" not in equipment
    rows = plantpanels.kv_rows(view.station)
    assert rows and all(len(r) == 2 for r in rows) and rows[0][0] in ("Sun sensor", "Reading")
    assert any(k == "Agent" for k, _ in rows)  # the flagged sensor's badge stays visible, escaped
    raw = plantpanels.raw_rows(view)
    assert raw[0]["Device"] == "INV-01" and len(raw) == PLANT.inverters.count + 3


def test_pending_tile_says_needs_approval():
    run = source.start_live("scn_0002", Mode.RULES)  # a derate whose power limit needs approval
    run.run_to_end()
    target = run.waiting[0].request.target
    view = plantmap.map_view(run, PLANT)
    page = plantpanels.map_html(view, PLANT)
    assert "Needs approval" in page and plain.device(target) in page
    assert plantpanels.pending_inverters(view) == [target]
    stats = plantpanels.stats(run, source.power_lines(run), source.tables(run)["events"], PLANT)
    assert stats[3].value == "1" and stats[3].state == "warn" and plain.device(target) in stats[3].note
    run.close()


def test_plant_page_renders_the_panels():
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state.run = _replay(source.DEFAULT_REPLAY, 6 * 60)
    at.session_state.mode_choice = "Replay"
    at.session_state.nav = at.session_state.nav_main = "Plant"
    at.session_state.nav_judge = None
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    for needle in ("Plant output now", "Sun and wind", "Plant output against expected", "Transformer 1",
                   "Weather station", "Series"):
        assert needle in text, needle
    assert "Plant map" in {s.value for s in at.subheader}
    assert "INV-01" not in text  # raw ids only under Show data
    assert not [b for b in at.button if b.key and b.key.startswith("map_review_")]  # a replay has nothing waiting
