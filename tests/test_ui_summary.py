"""Summary tab (ui/summary.py): key moments with their turns, the causes table, the energy figures, the
jump state the Trace tab reads, and the tab rendering in a replay and a live run."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT
from raqeeb.contracts import ActionStatus
from raqeeb.ui import live, source, summary, trace
from raqeeb.ui.runlog import Mode

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"


@pytest.fixture(scope="module")
def showcase():
    run = source.start_replay(source.showcase_files()[0])  # fin_0029: storm, dusty sensor, a derate with approvals
    run.run_to_end()
    yield run
    run.close()


def test_key_moments_cover_the_record_and_point_at_turns(showcase):
    log, now = showcase.log, showcase.now
    moments = summary.key_moments(log, now)
    kinds = {m.kind for m in moments}
    assert {summary.SYMPTOM, summary.CAUSE, summary.ACTION, summary.VERIFICATION} <= kinds
    assert [m for m in moments if m.kind == summary.SYMPTOM] == [moments[0]]  # exactly one, and it comes first
    assert moments == sorted(moments, key=lambda m: m.ts)
    turns = len(log.polls)
    assert all(1 <= m.turn <= turns and log.polls[m.turn - 1].ts >= m.ts for m in moments)
    causes = {m.text.split(" (score")[0] for m in moments if m.kind == summary.CAUSE}
    assert causes == {h.labels[0] for p in log.polls if p.diagnosis for h in p.diagnosis.root_causes}
    gated = [a for a in log.actions if a.gated and a.status is not None]
    approvals = [m for m in moments if m.kind == summary.APPROVAL]
    assert len(approvals) == len(gated) and all("simulated operator" in m.text for m in approvals)
    actions = [m for m in moments if m.kind == summary.ACTION]
    assert len(actions) == len(log.actions) and all(m.outcome for m in actions)
    for p in log.proposals_until(now):
        assert any(m.kind == summary.PROPOSAL and p.label in m.text and m.outcome == p.decision for m in moments)


def test_cause_rows_follow_the_trace_statuses(showcase):
    log, now = showcase.log, showcase.now
    rows = summary.cause_rows(log, now)
    statuses = trace.cause_statuses(log, now)
    assert {r.cause for r in rows} == {c for v in statuses.values() for c in v}
    by_cause = {r.cause: r for r in rows}
    for a in log.actions:
        if a.cause in by_cause:
            assert a.request.action.value in by_cause[a.cause].actions
    resolved = statuses[trace.RESOLVED]
    assert all(by_cause[c].verified == "recovered" for c in resolved)
    assert all(r.turn is None or 1 <= r.turn <= len(log.polls) for r in rows)


def test_energy_splits_the_shortfall_at_the_first_applied_action(showcase):
    power = source.power_lines(showcase)
    actions = showcase.log.actions_until(showcase.now)
    e = summary.energy(power, actions, showcase.now)
    assert e.expected_mwh > e.actual_mwh > 0 and e.lost_mwh > 0
    assert e.lost_before_mwh + e.lost_after_mwh == pytest.approx(e.lost_mwh)
    assert any(a.status == ActionStatus.APPLIED for a in actions) and e.first_effective is not None
    assert e.gap_worst_pct is not None and e.gap_worst_pct >= (e.gap_last_pct or 0)
    early = summary.energy(power, [], showcase.incident.start)  # nothing shown yet
    assert early.lost_mwh == 0 and early.gap_worst_pct is None and early.first_effective is None


def test_jump_sets_the_trace_state():
    state: dict = {summary.FOLLOW_KEY: True}
    summary.jump(state, 7)
    assert state == {summary.CLOCK_KEY: 6, summary.FOCUS_KEY: 7, summary.FOLLOW_KEY: False, summary.SWITCH_KEY: True}
    summary.jump(state, 1)
    assert state[summary.CLOCK_KEY] == 0


def _text(at: AppTest) -> str:
    return " ".join(str(e.value) for k in ("markdown", "caption", "info", "subheader") for e in getattr(at, k))


def test_summary_tab_renders_a_replay_and_jumps_to_a_turn():
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state.run = source.start_replay(source.showcase_files()[0])
    at.session_state.mode_choice = "Replay"
    at.run()
    at.button(key="to_end").click().run()
    assert at.session_state.nav == "Summary"
    text = _text(at)
    assert "Incident summary" in text and "Key moments" in text and "Shift report" in text
    labels = [m.label for m in at.metric]
    assert "Energy below expected" in labels and "Model calls" in labels
    assert len(at.dataframe) >= 2  # the causes table, and the raw moments and causes behind Show data
    assert "العربية" in text  # the bilingual report, in the first tab
    go = [b for b in at.button if b.key and b.key.startswith("summary_go_")]
    assert go
    go[0].click().run()
    focus = at.session_state[summary.FOCUS_KEY]
    assert focus >= 1 and at.session_state[summary.CLOCK_KEY] == focus - 1
    assert at.session_state[summary.FOLLOW_KEY] is False
    assert f"Turn {focus}" in _text(at) and not at.exception
    # Moving the clock by hand ends the focus
    at.select_slider(key=summary.CLOCK_KEY).set_value(len(at.session_state.run.log.polls) - 1).run()
    assert summary.FOCUS_KEY not in at.session_state and not at.exception


def test_summary_tab_in_a_live_run(tmp_path):
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    at.radio(key="mode_choice").set_value(live.MODE_LABEL[Mode.RULES]).run()
    at.selectbox(key="scenario_id").set_value("scn_0002").run()
    at.button(key="start_live").click().run()
    assert "No turns yet" in _text(at)
    at.button(key="step").click().run()
    at.button(key="step").click().run()
    run = at.session_state.run
    assert len(run.log.polls) >= 2 and "Incident summary" in _text(at) and not at.exception
    at.radio(key="nav_main").set_value("Trace").run()
    assert at.select_slider(key=summary.CLOCK_KEY)  # the clock is offered while a live run is paused
    run.close()
