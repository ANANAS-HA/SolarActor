import pandas as pd
import pytest

from raqeeb.tools import metrics
from tests.conftest import call
from tests.tool_scenarios import at


def test_energy_and_runs():
    idx = pd.date_range("2025-01-15 06:00", periods=6, freq="5min", tz="UTC")
    assert metrics.energy_kwh(pd.Series([1200.0] * 6, index=idx)) == pytest.approx(600.0)
    assert metrics.energy_kwh(pd.Series([1200.0, None, 1200.0], index=idx[:3])) == pytest.approx(200.0)
    mask = pd.Series([True, True, False, True, False, False], index=idx)
    assert metrics.runs(mask) == [(idx[0], idx[2], 2), (idx[3], idx[4], 1)]
    gappy = pd.Series(True, index=idx.delete(2))  # a missing step breaks a run
    assert [r[2] for r in metrics.runs(gappy)] == [2, 3]


def test_loss_onset():
    idx = pd.date_range("2025-01-15 06:00", periods=30, freq="5min", tz="UTC")
    ratio = pd.Series([1.0] * 10 + [0.8] * 20, index=idx)
    assert metrics.loss_onset(ratio, 0.9) == idx[10]
    assert metrics.loss_onset(pd.Series(1.0, index=idx), 0.9) is None


def test_healthy_plant_performance(run):
    store, s, _ = run("healthy")
    perf = call(metrics.plant_performance, store, s)
    assert perf.verdict == "ok" and perf.values["onset"] is None
    assert abs(perf.values["vs_reference"]["ratio"] - 1) < 0.06
    assert abs(perf.values["vs_sensor"]["ratio"] - 1) < 0.06
    assert (perf.start, perf.end) == (s.start, s.end)


def test_dust_storm_soiling_shows_against_reference_and_sensor(run):
    store, s, _ = run("soiling_only")
    perf = call(metrics.plant_performance, store, s)
    assert perf.verdict == "underperforming" and perf.devices == ["ALL"]
    assert at(s, "06:00") <= perf.values["onset"] <= at(s, "07:00")  # soiling builds during the storm
    assert perf.values["vs_reference"]["ratio"] < 0.90 and perf.values["vs_sensor"]["ratio"] < 0.90
    after = metrics.performance_series(store, at(s, "08:00"), at(s, "13:00"))
    assert after["ratio"].median() == pytest.approx(0.82, abs=0.04)


def test_trap_health_check_looks_better_than_normal(run):
    """The trap: PR against the dusty sensor reads above 1; against the reference it does not."""
    store, s, _ = run("trap")
    perf = call(metrics.plant_performance, store, s)
    assert perf.values["vs_sensor"]["ratio"] >= 1.0
    assert perf.values["vs_reference"]["ratio"] <= 0.88
    assert perf.verdict == "underperforming"
    shortfall = perf.values["vs_reference"]
    assert shortfall["shortfall_kwh"] > 0.1 * shortfall["expected_kwh"]


def test_dusty_sensor_alone_makes_plant_look_too_good(run):
    store, s, _ = run("sensor_only")
    perf = call(metrics.plant_performance, store, s)
    assert perf.verdict == "ok"
    assert perf.values["vs_sensor"]["ratio"] > 1.15


def test_flag_reported(run):
    assert call(metrics.plant_performance, *run("sensor_flagged")[:2]).values["sensor_flagged"]
    assert not call(metrics.plant_performance, *run("sensor_only")[:2]).values["sensor_flagged"]


def test_inverter_performance(run):
    store, s, _ = run("soiling_only")
    soiled = call(metrics.inverter_performance, store, s)
    rows = soiled.values["inverters"]
    assert len(rows) == 16 and soiled.verdict == "inverters_low" and len(soiled.devices) == 16
    assert all(0.78 <= r["ratio_unclipped"] <= 0.92 for r in rows.values())
    trap = call(metrics.inverter_performance, *run("trap")[:2]).values["inverters"]
    assert trap["INV-12"]["missing_daylight_steps"] == 21
    assert all(r["missing_daylight_steps"] == 0 for k, r in trap.items() if k != "INV-12")
    assert trap["INV-07"]["ratio"] < min(r["ratio"] for k, r in trap.items() if k != "INV-07")
    assert call(metrics.inverter_performance, *run("healthy")[:2]).verdict == "ok"


def test_comms_gap_checked_against_meter(run):
    store, s, _ = run("trap")
    result = call(metrics.data_gaps, store, s)
    assert result.verdict == "data_gap" and result.devices == ["INV-12"]
    (gap,) = result.values["gaps"]
    assert (gap["start"], gap["end"], gap["steps"]) == (at(s, "09:15"), at(s, "11:00"), 21)
    assert gap["still_producing"] and gap["producing_fraction"] == pytest.approx(1.0, abs=0.1)
    assert not gap["ongoing"]
    assert "kept producing" in result.evidence


def test_dead_and_silent_device_is_not_producing(run):
    store, s, _ = run("dead_and_silent")
    result = call(metrics.data_gaps, store, s)
    (gap,) = result.values["gaps"]
    assert gap["device_id"] == "INV-05" and gap["ongoing"]
    assert gap["start"] == at(s, "08:00")
    assert gap["still_producing"] is False and abs(gap["producing_fraction"]) < 0.1


def test_station_gap(run):
    store, s, _ = run("station_dropout")
    (gap,) = call(metrics.data_gaps, store, s).values["gaps"]
    assert gap["device_id"] == "WS-01" and gap["steps"] == 6
    assert gap["start"] == at(s, "07:00") and gap["end"] == at(s, "07:30")


def test_no_gaps_when_healthy(run):
    result = call(metrics.data_gaps, *run("healthy")[:2])
    assert result.verdict == "ok" and result.values["gaps"] == []


def test_curtailment_period(run):
    store, s, _ = run("curtailment")
    result = call(metrics.curtailment_periods, store, s)
    assert result.verdict == "curtailment" and result.devices == ["PPC-01"]
    (period,) = result.values["periods"]
    assert (period["start"], period["end"]) == (at(s, "07:30"), at(s, "09:30"))
    assert period["limit_kw"] == 24000.0 and period["meter_at_limit"]
    assert period["expected_median_kw"] > period["limit_kw"]
    perf = call(metrics.plant_performance, store, s)
    assert perf.values["curtailed_steps"] == 24 and perf.verdict == "ok"
    assert call(metrics.curtailment_periods, *run("healthy")[:2]).verdict == "ok"


def test_window_arguments(run):
    store, s, _ = run("soiling_only")
    assert metrics.plant_performance(store, s.start, at(s, "06:00")).verdict == "ok"
    assert metrics.plant_performance(store, at(s, "07:00"), s.end).verdict == "underperforming"
    empty = metrics.plant_performance(store, s.end, s.end + pd.Timedelta(hours=1))
    assert empty.verdict == "inconclusive"
