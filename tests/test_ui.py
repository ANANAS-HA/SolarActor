"""Console: every panel renders on fixture data, the buttons work, the adapter's numbers are right."""

import json
from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from raqeeb.config import REPO_ROOT
from raqeeb.contracts import FaultType
from raqeeb.faults.generator import is_trap, load_scenarios
from raqeeb.ui import samples, source
from tests.leakcheck import assert_no_answer_leak

APP = REPO_ROOT / "raqeeb" / "ui" / "app.py"
PANELS = {"Plant map", "Alarm feed", "Recovery", "Agent timeline (sample)", "Approvals (sample)",
          "Benchmark: Raqeeb vs threshold-alarm baseline", "Shift report (sample)"}
SIDEBAR = {"Inject faults", "Simulated clock"}


def _scenario_with(*types: FaultType) -> str:
    return next(s.id for s in load_scenarios() if set(types) <= {f.type for f in s.faults})


@pytest.fixture
def app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def test_every_panel_renders(app):
    assert PANELS <= {s.value for s in app.subheader}
    assert SIDEBAR <= {h.value for h in app.sidebar.header}
    assert [t.label for t in app.tabs] == ["Operations", "Agent", "Results", "Report"]
    assert any("Synthetic data" in i.value for i in app.info)
    assert len(app.get("plotly_chart")) == 3
    assert len(app.get("download_button")) == 1


def test_inject_storm_and_replay(app):
    app.button(key="inject_storm").click().run()
    assert not app.exception
    assert app.session_state.run_key[0] == "storm"
    other = source.scenario_ids()[-1]
    app.selectbox(key="replay_id").select(other)
    app.button(key="replay").click().run()
    assert not app.exception
    assert app.session_state.run_key == ("scenario", other)


def test_clock_moves_and_panels_follow(app):
    start = app.session_state.clock
    run = source.replay(source.default_scenario_id())
    assert run.times[start] >= max(e.ts for e in run.agent.timeline)  # opens with the whole incident shown
    app.button(key="clock_back").click().run()
    assert not app.exception and app.session_state.clock == start - 6
    app.select_slider(key="clock").set_value(0).run()
    assert not app.exception
    app.button(key="clock_end").click().run()
    assert not app.exception and app.session_state.clock == len(run.times) - 1


def _reveal_shown(at: AppTest) -> bool:
    return any("revealed after the run" in e.label and "never sees" in e.label for e in at.sidebar.expander)


def test_answer_key_revealed_only_after_the_run(app):
    run = source.replay(source.default_scenario_id())
    assert app.session_state.clock < len(run.times) - 1 and not _reveal_shown(app)
    page = " ".join(m.value for m in app.sidebar.markdown)
    assert not any(f.label in page for f in run.planted.faults)
    app.button(key="clock_end").click().run()
    assert _reveal_shown(app)
    page = " ".join(m.value for m in app.sidebar.markdown)
    assert all(f.label in page for f in run.planted.faults)
    app.button(key="clock_back").click().run()
    assert not _reveal_shown(app)


@pytest.mark.parametrize("scenario_id", source.scenario_ids()[:3])
def test_incident_view_carries_no_answer(scenario_id):
    run = source.replay(scenario_id)
    assert_no_answer_leak(run.incident, run.planted, strict=True)


def test_approve_and_reject():
    scenario_id = _scenario_with(FaultType.INVERTER_DERATE)
    first, second = (source.replay(scenario_id).agent.approvals * 2)[:2]
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.session_state.run_key = ("scenario", scenario_id)
    at.run()
    action_id = first.request.id
    at.text_input(key=f"note_{action_id}").input("cooling fan checked")
    at.button(key=f"approve_{action_id}").click().run()
    assert not at.exception
    assert at.session_state.decisions[action_id] == {"decision": "Approved", "note": "cooling fan checked",
                                                     "ts": at.session_state.now}
    assert any("Approved" in s.value for s in at.success)
    if second is not first:
        at.button(key=f"reject_{second.request.id}").click().run()
        assert at.session_state.decisions[second.request.id]["decision"] == "Rejected"


def test_map_cell_for_missing_inverter():
    scenario_id = _scenario_with(FaultType.COMMS_DROPOUT)
    run = source.replay(scenario_id)
    comms = next(f for f in run.planted.faults if f.type == FaultType.COMMS_DROPOUT)
    at = run.planted.resolve_time(comms.start) + timedelta(minutes=10)
    at_ = AppTest.from_file(str(APP), default_timeout=120)
    at_.session_state.run_key = ("scenario", scenario_id)
    at_.session_state.clock = int(run.times.get_loc(at))
    at_.run()
    assert not at_.exception
    cells = {a["text"].split("</b>")[0].removeprefix("<b>"): a["text"]
             for a in json.loads(at_.get("plotly_chart")[0].proto.spec)["layout"]["annotations"]}
    assert cells[comms.target].endswith("<br>—<br>NO DATA")
    assert not any("nan" in text for text in cells.values())


# -- adapter ------------------------------------------------------------------


@pytest.fixture(scope="module")
def trap() -> source.PlantRun:
    return source.replay(next(s.id for s in load_scenarios() if is_trap(s)))


def test_tables_follow_the_store_shape(trap):
    assert set(trap.tables) >= {"inverter_readings", "weather_station", "weather_reference", "meter", "events"}
    for frame in trap.tables.values():
        assert str(frame["ts"].dt.tz) == "UTC"
    assert len(trap.lines) == len(trap.tables["meter"]) == 144


def test_trap_hides_loss_from_sensor_line(trap):
    """With panels and sensor equally dusty, actual meets the sensor line but not the reference line."""
    soiling = next(f for f in trap.planted.faults if f.type == FaultType.SOILING)
    clean_at = next(p.at for p in trap.agent.executed if p.fault.type == FaultType.SOILING)
    t0 = trap.planted.resolve_time(soiling.end) + timedelta(minutes=15)
    window = trap.lines[(trap.lines.index >= t0) & (trap.lines.index < clean_at)]
    window = window[window["expected_ref_kw"] > 5000]
    vs_ref = (window["actual_kw"] / window["expected_ref_kw"]).mean()
    vs_sensor = (window["actual_kw"] / window["expected_sensor_kw"]).mean()
    assert vs_ref < 0.92 and abs(vs_sensor - 1) < 0.06


def test_sample_cleaning_recovers_the_twin(trap):
    clean = next(p for p in trap.agent.executed if p.request.action.value == "create_cleaning_work_order")
    verification = next(v for v in trap.agent.verifications if v.action_id == clean.request.id)
    assert verification.recovered and verification.after_kw > verification.before_kw


def test_map_shows_no_data_and_degraded():
    run = source.replay(_scenario_with(FaultType.INVERTER_DERATE, FaultType.COMMS_DROPOUT))
    comms = next(f for f in run.planted.faults if f.type == FaultType.COMMS_DROPOUT)
    derate = next(f for f in run.planted.faults if f.type == FaultType.INVERTER_DERATE)
    reset = next(p.at for p in run.agent.executed if p.fault is comms)
    at = max(run.planted.resolve_time(comms.start), run.planted.resolve_time(derate.start)) + timedelta(minutes=10)
    assert at < reset
    states = source.inverter_status(run, at).set_index("inverter_id")["state"]
    assert states[comms.target] == source.NO_DATA
    if run.lines.loc[at, "expected_ref_kw"] > 0.5 * run.lines["expected_ref_kw"].max():
        assert states[derate.target] == source.DEGRADED
    assert (states == source.HEALTHY).sum() >= len(states) - 2


def test_samples_are_labelled_and_gated(trap):
    report = trap.agent.report
    assert "SAMPLE" in report.summary_en and "نموذجي" in report.summary_ar
    for p in samples.scripted_plan(trap.planted, source.load_plant()):
        assert p.request.rationale.startswith("SAMPLE")
    run = source.replay(_scenario_with(FaultType.INVERTER_DERATE))
    assert run.agent.approvals and all(p.request.needs_approval for p in run.agent.approvals)
    assert not any(p.request.needs_approval for p in run.agent.executed)
    table = source.benchmark()
    assert "Baseline" in set(table["Winner"])  # the sample shows where the baseline does better
