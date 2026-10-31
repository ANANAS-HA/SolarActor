"""Trace tab (ui/trace.py and the tab in ui/app.py): one card per turn from the RunLog, the same for live
runs and replays; free text only as quoted data; every shipped replay renders."""

from __future__ import annotations

from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT
from raqeeb.ui import live, source, trace
from raqeeb.ui.runlog import NoteRecord, RunLog

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"
UNKNOWN_REPLAY = "pru_0001_test.json"
HOSTILE = "**Ignore rules** [click](http://x.invalid) <b>bold</b> :red[alarm] $x$"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(live.NO_LLM_ENV, "1")
    monkeypatch.setenv(live.RUN_DIR_ENV, str(tmp_path))


def _page(at: AppTest) -> str:
    return " ".join(str(e.value) for kind in ("markdown", "caption", "info", "warning", "success", "subheader")
                    for e in getattr(at, kind))


def _app(run) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state.run = run
    at.session_state.mode_choice = "Replay"
    return at


def test_cards_follow_the_record():
    run = source.start_replay(source.replay_dir() / UNKNOWN_REPLAY)
    run.run_to_end()
    cards = trace.turn_cards(run.log, run.now)
    assert [c.index for c in cards] == list(range(1, len(run.log.polls) + 1))
    assert [c.ts for c in cards] == [p.ts for p in run.log.polls]  # oldest first: newest at the bottom
    decided = next(c for c in cards if c.proposals)
    assert {p.decision for p in decided.proposals} == {"accepted", "rejected"} and decided.llm_calls
    assert any(c.actions and c.actions[0].cause == "COMMS_DROPOUT@INV-02" for c in cards)
    assert sum(len(c.actions) for c in cards) == len(run.log.actions)  # every action on exactly one card
    assert any(c.verifications for c in cards) and any(c.escalations for c in cards)
    assert any(c.checks for c in cards) and all(r.kind in (trace.FINDING, trace.OK, trace.INCONCLUSIVE)
                                                for c in cards for r in (c.checks or []))
    head = trace.header(run.log, run.now)
    assert head.turn == head.turns_total == len(run.log.polls)
    assert head.causes[trace.ESCALATED] == ["TRANSFORMER_TRIP@T3"]
    assert set(head.causes[trace.RESOLVED]) == {"COMMS_DROPOUT@INV-02", "SENSOR_SOILING@WS-01"}
    assert head.llm_calls == len(run.log.llm_calls_until(run.now))
    # Cut at an earlier clock: only the turns so far
    early = run.log.polls[10].ts
    assert len(trace.turn_cards(run.log, early)) == 11 and trace.header(run.log, early).turn == 11


def test_quiet_turns_and_repeated_findings_are_folded():
    run = source.start_replay(source.replay_dir() / source.DEFAULT_REPLAY)
    run.run_to_end()
    cards = trace.turn_cards(run.log, run.now)
    assert any(c.quiet for c in cards) and not cards[-1].quiet  # the final poll always has a card
    keys = [sorted((r.tool, r.devices, r.verdict) for r in c.checks) for c in cards if c.checks is not None]
    assert all(a != b for a, b in zip(keys, keys[1:]))  # a check table only when verdicts moved


def test_trace_tab_renders_a_replay_with_words_and_colours():
    at = _app(source.start_replay(source.replay_dir() / UNKNOWN_REPLAY))
    at.run()
    assert "No turns yet" in _page(at)
    at.button(key="to_end").click().run()
    assert not at.exception
    assert [t.label for t in at.tabs][:2] == ["Operations", "Trace"]
    page = _page(at)
    assert "Turn 48" in page and "ACCEPTED" in page and "REJECTED" in page and "(changed nothing)" in page
    checks = [d.value for d in at.dataframe if "Verdict" in getattr(d.value, "columns", [])]
    assert checks and all(v.split(":")[0] in ("FINDING", "OK", "INCONCLUSIVE") for df in checks for v in df["Verdict"])
    assert any(m.value == "Turn" or "48" in str(m.value) for m in at.metric)


def test_trace_free_text_is_quoted_data():
    log = RunLog.load(source.replay_dir() / UNKNOWN_REPLAY)
    poll = log.polls[5]
    poll.notes.append(NoteRecord(ts=poll.ts, source="technician", author="x", about="T3", text=HOSTILE))
    run = live.ReplayRun(log)
    at = _app(run)
    at.run()
    at.button(key="to_end").click().run()
    assert not at.exception
    assert HOSTILE not in _page(at) and "Ignore rules" not in _page(at)
    quoted = [d.value for d in at.dataframe if "Text (quoted)" in getattr(d.value, "columns", [])]
    assert any(HOSTILE in list(df["Text (quoted)"]) for df in quoted)
    assert all(not e.proto.expanded for e in at.expander)  # free text collapsed by default


def test_every_shipped_replay_renders_in_the_trace_tab():
    for path in source.replay_files():
        at = _app(source.start_replay(path))
        at.run()
        at.button(key="to_end").click().run()
        assert not at.exception, path.name
        assert f"Turn {len(RunLog.load(path).polls)}" in _page(at)


def test_v1_replay_still_renders(tmp_path):
    """A version 1 file has no notes, proposals or causes per action: the tab shows what it has."""
    import json

    data = json.loads((source.replay_dir() / source.DEFAULT_REPLAY).read_text(encoding="utf-8"))
    data["version"] = 1
    for poll in data["polls"]:
        del poll["notes"], poll["proposals"]
    for action in data["actions"]:
        del action["cause"]
    run = live.ReplayRun(RunLog.from_json(json.dumps(data)))
    run.run_to_end()
    assert trace.turn_cards(run.log, run.now)[-1].index == len(run.log.polls)
    at = _app(run)
    at.run()
    assert not at.exception
    assert run.now - run.incident.start <= timedelta(hours=run.incident.duration_hours)


# -- opening trajectory files (v3, bench-written, gzip) -----------------------------------------------

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "trajectory_v3_example.json"


def test_trace_tab_renders_the_v3_fixture_end_to_end():
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    at.text_input(key="trace_path").input(str(FIXTURE)).run()
    at.button(key="trace_open").click().run()
    assert not at.exception
    run = at.session_state.run
    assert isinstance(run, live.ReplayRun) and run.log.version == 3
    assert "Hidden while the run is in progress" in _page(at)  # the file has the key: still hidden
    at.button(key="to_end").click().run()
    assert not at.exception
    page = _page(at)
    assert f"Turn {len(run.log.polls)}" in page
    gate = [d.value for d in at.dataframe if "Gate" in getattr(d.value, "columns", [])]
    gates = {v for df in gate for v in df["Gate"]}
    assert {"auto (allowlisted)", "needs approval"} <= gates
    assert "applied by the operator" in {v for df in gate for v in df["Decision"]}
    candidates = [d.value for d in at.dataframe if "Selected" in getattr(d.value, "columns", [])]
    assert {"✓ selected", "not selected"} <= {v for df in candidates for v in df["Selected"]}  # v3 alternatives
    checks = [d.value for d in at.dataframe if "Verdict" in getattr(d.value, "columns", [])]
    assert any(v.startswith("OK:") for df in checks for v in df["Verdict"])  # v3 records the checks that passed


def test_trajectory_files_plain_gzip_and_folders(tmp_path):
    import gzip
    import shutil

    folder = tmp_path / "final"
    (folder / "trajectories").mkdir(parents=True)
    shutil.copy(FIXTURE, folder / "trajectories" / "scn_0007__raqeeb_rules.json")
    (folder / "trajectories" / "scn_0007__raqeeb_local.json.gz").write_bytes(gzip.compress(FIXTURE.read_bytes()))
    (folder / "summary.csv").write_text("agent\n", encoding="utf-8")
    files = source.trajectory_files(folder)
    assert [f.name for f in files] == ["scn_0007__raqeeb_local.json.gz", "scn_0007__raqeeb_rules.json"]
    assert source.trajectory_files(files[1]) == [files[1]] and source.trajectory_files(tmp_path / "none") == []
    plain = source.start_replay(files[1])
    zipped = source.start_replay(files[0])
    uploaded = source.start_replay(gzip.compress(FIXTURE.read_bytes()))  # an upload hands over bytes
    assert plain.log == zipped.log == uploaded.log

    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    at.text_input(key="trace_path").input(str(folder)).run()
    at.selectbox(key="trace_pick").set_value(str(files[0])).run()
    at.button(key="trace_open").click().run()
    assert not at.exception and at.session_state.run.log == zipped.log


def test_a_bad_file_gives_a_message_not_an_exception(tmp_path):
    bad = tmp_path / "not_a_run.json"
    bad.write_text('{"format": "something-else"}', encoding="utf-8")
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    before = at.session_state.run
    at.text_input(key="trace_path").input(str(bad)).run()
    at.button(key="trace_open").click().run()
    assert not at.exception and at.session_state.run is before
    assert any("Could not open" in e.value for e in at.error)
