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
    _trace(at)
    return at


def _trace(at: AppTest) -> AppTest:
    """Open the Trace page (session state the navigation radios read on the next run)."""
    at.session_state.nav = "Trace"
    at.session_state.nav_main = "Trace"
    at.session_state.nav_judge = None
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
    assert at.session_state.nav == "Trace"
    page = _page(at)
    assert "Turn 48" in page and "Accepted" in page and "Rejected" in page and "Nothing changed" in page
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
    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()
    at.text_input(key="trace_path").input(str(FIXTURE)).run()
    at.button(key="trace_open").click().run()
    assert not at.exception
    run = at.session_state.run
    assert isinstance(run, live.ReplayRun) and run.log.version == 3
    at.radio(key="nav_judge").set_value("Answer key").run()
    assert "Hidden while the run is in progress" in _page(at)  # the file has the key: still hidden
    at.radio(key="nav_main").set_value("Trace").run()
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

    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()
    at.text_input(key="trace_path").input(str(folder)).run()
    at.selectbox(key="trace_pick").set_value(str(files[0])).run()
    at.button(key="trace_open").click().run()
    assert not at.exception and at.session_state.run.log == zipped.log


def test_a_bad_file_gives_a_message_not_an_exception(tmp_path):
    bad = tmp_path / "not_a_run.json"
    bad.write_text('{"format": "something-else"}', encoding="utf-8")
    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()
    before = at.session_state.run
    at.text_input(key="trace_path").input(str(bad)).run()
    at.button(key="trace_open").click().run()
    assert not at.exception and at.session_state.run is before
    assert any("Could not open" in e.value for e in at.error)


def test_every_free_text_in_the_trace_is_quoted_data_and_collapsed():
    """Hostile text planted in every free-text field a v3 file can carry: a note, an alarm message, a model
    transcript, a proposal's reason and evidence, the gate's reason. None of it is rendered as markdown."""
    from raqeeb.ui.runlog import (
        GateDecision,
        GateRecord,
        ProposalCheck,
        ProposalRecord,
        Transcript,
    )

    log = RunLog.load(FIXTURE)
    poll = next(p for p in log.polls if p.gate)
    poll.notes.append(NoteRecord(ts=poll.ts, source="technician", author="x", about="T1", text=HOSTILE))
    poll.transcripts.append(Transcript(purpose="hypotheses", prompt=HOSTILE, reply=HOSTILE))
    poll.proposals.append(ProposalRecord(ts=poll.ts, label="NEW_SHADING@EDGE-W", fault_type="NEW_SHADING",
                                         target="EDGE-W", decision="rejected", reason=HOSTILE,
                                         checks=[ProposalCheck(check="test", result=HOSTILE, passed=False)],
                                         model_evidence=[HOSTILE]))
    poll.gate.append(GateRecord(request=poll.gate[0].request.model_copy(update={"id": "held-1"}),
                                decision=GateDecision.REJECTED, reason=HOSTILE, cause="SOILING@ALL"))
    template = log.telemetry["events"][0]
    log.telemetry["events"].append({**template, "ts": poll.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "message": HOSTILE})
    at = _app(live.ReplayRun(log))
    at.run()
    at.button(key="to_end").click().run()
    assert not at.exception
    page = _page(at)
    assert HOSTILE not in page and "<b>bold</b>" not in page and "](http" not in page  # never markdown or HTML
    assert source.md_escape(HOSTILE) in page  # the proposal's reason, escaped
    def column(name: str) -> list[str]:
        return [v for d in at.dataframe for col in getattr(d.value, "columns", []) if name in col
                for v in d.value[col].astype(str)]

    for name in ("Text (quoted)", "Message (quoted)", "the model's words (quoted)", "Gate", "Result"):
        assert any(HOSTILE in v for v in column(name)), name
    assert [c.value for c in at.code].count(HOSTILE) == 2  # prompt and reply, as plain text
    assert at.expander and all(not e.proto.expanded for e in at.expander)


# -- live: the background runner ----------------------------------------------------------------------


def _wait(condition, timeout: float = 120.0, step: float = 0.1) -> bool:
    import time

    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(step)
    return condition()


def test_live_run_in_the_background_completes_with_inline_approvals():
    """scn_0007 rules only, played by the background runner from the Trace tab; every gated request approved
    on its card; reruns never restart the run."""
    import time

    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()
    at.radio(key="mode_choice").set_value(live.MODE_LABEL[live.Mode.RULES]).run()
    at.selectbox(key="scenario_id").set_value("scn_0007").run()
    at.button(key="start_live").click().run()
    run = at.session_state.run
    at.button(key="trace_jump").click().run()
    runner = at.session_state.runner
    assert runner.run is run
    approved = []
    deadline = time.time() + 180
    while not run.finished and time.time() < deadline:
        _wait(lambda: run.finished or bool(run.waiting), timeout=30)
        at.run()  # a rerun, as the page's refresh does
        assert not at.exception
        assert at.session_state.runner is runner and at.session_state.run is run  # never restarted
        if run.waiting:
            action_id = run.waiting[0].request.id
            at.text_input(key=f"trace_note_{action_id}").input("checked on the card")
            at.button(key=f"trace_approve_{action_id}").click().run()
            approved.append(action_id)
    assert run.finished and approved
    decided = {a.request.id: a for a in run.log.actions}
    assert all(decided[i].decided_by == "operator" and decided[i].note == "checked on the card" for i in approved)
    roots = {label for h in run.log.diagnosis_until(run.now).root_causes for label in h.labels}
    assert set(run.log.answer_key.root_causes) <= roots
    audit = run.store.audit(step="approval")
    assert sum(audit["actor"] == "human:operator") == len(approved)
    at.run()
    assert not at.exception
    at.select_slider(key="trace_clock").set_value(5).run()  # scrub back once the run has ended
    assert not at.exception and "Turn 6" in _page(at) and "Turn 7" not in _page(at)


def test_runner_pace_and_pause():
    from raqeeb.ui.runner import Runner

    run = source.start_live("scn_0002", live.Mode.RULES)
    runner = Runner(run, turns_per_s=8)
    runner.resume()
    assert _wait(lambda: len(run.log.polls) >= 3, timeout=60)
    runner.pause()
    _wait(lambda: not runner.state().running, timeout=5)
    with runner.lock:
        held = len(run.log.polls)
    import time

    time.sleep(0.6)
    assert len(run.log.polls) == held and runner.state().paused  # paused means no new turn
    runner.jump_to_end()
    assert _wait(lambda: run.finished or bool(run.waiting), timeout=60)
    assert run.waiting and not run.finished  # it stops for the operator
    runner.decide(run.waiting[0].request.id, approve=False, note="not now")
    assert _wait(lambda: run.finished or bool(run.waiting), timeout=60)  # and carries on by itself
    runner.stop()
    assert not runner.state().running
    run.close()


def test_a_new_run_stops_the_old_runner():
    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()
    at.radio(key="mode_choice").set_value(live.MODE_LABEL[live.Mode.RULES]).run()
    at.selectbox(key="scenario_id").set_value("scn_0002").run()
    at.button(key="start_live").click().run()
    at.select_slider(key="trace_pace").set_value(0.5).run()
    at.button(key="trace_run").click().run()
    runner = at.session_state.runner
    assert _wait(lambda: runner.state().running, timeout=10)
    at.button(key="start_live").click().run()  # replace the run
    assert _wait(lambda: not (runner._thread and runner._thread.is_alive()), timeout=15)
    assert "runner" not in at.session_state or at.session_state.runner is not runner


# -- the showcase: local-model runs replayed from the frozen final set ---------------------------------

SHOWCASE = ("fin_0029", "fin_0024", "fin_0038", "fin_0053")


def test_showcase_replays_are_the_console_default():
    files = source.showcase_files()
    assert [f.name.split("__")[0] for f in files] == list(SHOWCASE)  # the default (fin_0029) first
    assert source.console_replays()[: len(files)] == files and source.console_replays()[len(files):] == source.replay_files()
    for path in files:
        log = RunLog.load(path)
        assert log.version == 3 and log.finished and log.label == source.SHOWCASE_LABEL
        assert log.mode == live.Mode.BENCH and log.llm_calls_until(log.polls[-1].ts)  # the local model ran
        assert all(c.model == "openai/Qwen3.8-27B-ATX-4-XS.gguf" for c in log.llm_calls_until(log.polls[-1].ts))
        assert "/home/" not in log.to_json()  # the model server's folder is not published
        roots = {lbl for p in log.polls if p.diagnosis for h in p.diagnosis.root_causes for lbl in h.labels}
        assert roots or log.escalations_until(log.polls[-1].ts)


def test_showcase_replays_render_in_the_trace_tab():
    at = _trace(AppTest.from_file(str(APP), default_timeout=300))
    at.run()  # the console opens on the default showcase replay
    assert at.session_state.run.log.incident.id == "fin_0029"
    at.radio(key="nav_judge").set_value("Answer key").run()
    assert "Hidden while the run is in progress" in _page(at)
    for path in source.showcase_files():
        at = _app(source.start_replay(path))
        at.run()
        at.button(key="to_end").click().run()
        assert not at.exception, path.name
        page = _page(at)
        assert "Turn 48" in page and source.md_escape(source.SHOWCASE_LABEL) in page
