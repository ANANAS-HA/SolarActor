"""The text viewer (python -m raqeeb.bench.trajectory) prints a run turn by turn: the dusty-sensor turn shows
the check, its numbers and limits, the symptom, the candidates and the gate; quiet turns are one line; the
answer key comes last; --turn and --from select turns; old replay files and gzip files render too."""

from __future__ import annotations

import gzip
import re
from datetime import time
from pathlib import Path

import pytest

from raqeeb.bench import trajectory
from raqeeb.ui import source
from raqeeb.ui.runlog import RunLog

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "trajectory_v3_example.json"


@pytest.fixture(scope="module")
def log() -> RunLog:
    return RunLog.load(FIXTURE_LOG)


@pytest.fixture(scope="module")
def text(log) -> str:
    return trajectory.render(log, path=FIXTURE_LOG)


def _turn(text: str, n: int) -> str:
    start = text.index(f"== Turn {n} -")
    end = text.find("== ", start + 3)
    return text[start:end if end > 0 else None]


def test_header_names_the_incident_and_nothing_else_the_agent_did_not_see(text, log):
    head = text.split("== Turn 1 -")[0]
    assert "scn_0007" in head and "raqeeb_rules" in head and "rules only" in head
    assert "48 turns" in head and "every 15 min" in head
    assert "08:00 Riyadh (05:00 UTC)" in head
    for label in log.answer_key.root_causes:
        assert label not in head


def test_every_turn_is_printed_and_quiet_turns_are_one_line(text, log):
    assert [int(m) for m in re.findall(r"^== Turn (\d+) -", text, re.M)] == list(range(1, len(log.polls) + 1))
    assert "== Turn 3 - " in text and _turn(text, 3).count("\n") <= 2 and "nothing new" in _turn(text, 3)
    assert "final poll" in _turn(text, len(log.polls))


def test_the_dusty_sensor_turn_reads_like_a_script(text):
    turn = _turn(text, 15)  # 11:45 Riyadh: the sensor check turns from ok to a finding
    assert "11:45 Riyadh (08:45 UTC)" in turn
    assert re.search(r"sensor_check\s+08:00-11:45\s+ok -> sensor_low\s+WS-01\s+.*sensor_vs_reference 0\.784", turn)
    assert "sensor_check limits: sensor_low_ratio 0.9" in turn
    assert "sensor_check says: sensor reads 78% of the reference" in turn
    assert "+ SENSOR_DEVIATION:WS-01 since 11:15" in turn
    assert "* SENSOR_SOILING@WS-01 score 0.6 explains SENSOR_DEVIATION:WS-01" in turn
    assert "diagnosis: SOILING@ALL, SENSOR_SOILING@WS-01" in turn
    assert "flag_sensor_use_reference on WS-01 -> auto (for SENSOR_SOILING@WS-01)" in turn
    assert "raqeeb-2 flag_sensor_use_reference on WS-01 (auto)" in turn
    assert "raqeeb-2 flag_sensor_use_reference on WS-01: APPLIED by auto" in turn
    assert "standing findings:" in turn  # the storm findings are still there, named once


def test_sections_appear_in_order_and_the_answer_key_comes_last(text, log):
    first_cause = min(text.index(label) for label in log.answer_key.root_causes)
    footer = text.index("== After the run: what the agent never saw ==")
    assert first_cause < footer  # the agent names causes during the run (its own conclusions)
    tail = text[footer:]
    assert "ANSWER KEY (bench truth)" in tail and "SOILING on ALL from 07:00 UTC" in tail
    assert "root causes: SOILING@ALL, SENSOR_SOILING@WS-01, INVERTER_DERATE@INV-07, COMMS_DROPOUT@INV-12" in tail
    assert "missed: none" in tail and "named but not planted: none" in tail
    assert "BENCH TRUTH" not in tail  # a console recording has no bench section
    assert "severity 0.2" not in text[:footer]  # fault specs only in the footer
    order = [text.index("\n  " + k) for k in ("arrivals:", "checks (verdict changed):", "symptoms:", "candidates (",
                                               "plan:", "gate:", "requests sent:", "decisions (environment):")]
    assert order == sorted(order)
    gated = _turn(text, 24)  # 14:00 Riyadh: set_power_limit needs approval
    assert "-> needs_approval" in gated and "APPLIED by human:simulated" not in gated  # console: an operator decided
    assert "verifications:" in text and "report (English" in text


def test_turn_and_from_select_turns(log):
    one = trajectory.render(log, turn=15)
    assert one.count("== Turn ") == 1 and "== Turn 15 -" in one and "ANSWER KEY" not in one
    assert "ok -> sensor_low" in one  # the transition is judged against the earlier turns even when not printed
    late = trajectory.render(log, start=time(14, 0))  # 14:00 Riyadh = 11:00 UTC = turn 24
    assert "== Turn 23 -" not in late and "== Turn 24 -" in late and "ANSWER KEY" in late
    utc = trajectory.render(log, start=time(11, 0), utc=True)
    assert "== Turn 23 -" not in utc and "== Turn 24 -" in utc and "11:00 UTC" in utc
    # the viewer's own times are UTC only (tool evidence text may still say Riyadh)
    assert re.search(r"^== Turn 24 - 2025-04-12 11:00 UTC", utc, re.M) and not re.search(r"^== Turn .*Riyadh", utc, re.M)
    missing = trajectory.render(log, turn=99)
    assert "no turn 99" in missing


def test_all_checks_prints_every_tool(log):
    everything = trajectory.render(log, turn=3, all_checks=True)
    assert "checks:" in everything
    for tool in ("sensor_check", "plant_performance", "plant_performance#2", "wind_summary", "peer_outliers"):
        assert tool in everything


def test_cli_reads_plain_and_gzip_files(tmp_path, capsys, log):
    trajectory.main([str(FIXTURE_LOG), "--turn", "15"])
    out = capsys.readouterr().out
    assert "== Turn 15 -" in out and "sensor_vs_reference 0.784" in out
    packed = tmp_path / "t.json.gz"
    packed.write_bytes(gzip.compress(FIXTURE_LOG.read_bytes()))
    trajectory.main([str(packed), "--from", "19:45"])  # 16:45 UTC = turn 47
    out = capsys.readouterr().out
    assert "== Turn 48 -" in out and "== Turn 47 -" in out and "== Turn 46 -" not in out and "ANSWER KEY" in out


def test_old_replay_files_render():
    path = source.replay_files()[0]
    text = trajectory.render(RunLog.load(path), path=path)
    assert "An older file" in text and "== Turn 1 -" in text and "ANSWER KEY" in text
    assert "requests sent:" in text and "-> " in text  # gate derived from the sent requests
