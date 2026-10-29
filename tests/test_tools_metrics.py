
import pandas as pd
import pytest

from raqeeb.tools import metrics
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


def test_healthy_plant_performance(scenario):
    perf = metrics.plant_performance(scenario("healthy")[0])
    assert not perf["underperforming"] and perf["onset"] is None
    assert abs(perf["vs_reference"]["ratio"] - 1) < 0.06
    assert abs(perf["vs_sensor"]["ratio"] - 1) < 0.06


def test_soiling_shows_against_reference_and_sensor(scenario):
    store, twin = scenario("soiling_only")
    perf = metrics.plant_performance(store)
    assert perf["underperforming"]
    assert perf["onset"] == at(twin, "07:00")
    assert perf["vs_reference"]["ratio"] < 0.90 and perf["vs_sensor"]["ratio"] < 0.90
    after = metrics.performance_series(store, at(twin, "08:00"), at(twin, "13:00"))
    assert after["ratio"].median() == pytest.approx(0.82, abs=0.04)


def test_trap_health_check_looks_normal(scenario):
    """The trap: PR against the dusty sensor looks fine; against the reference it does not."""
    store, twin = scenario("trap")
    perf = metrics.plant_performance(store)
    assert perf["vs_sensor"]["ratio"] >= 0.95
    assert perf["vs_reference"]["ratio"] <= 0.88
    assert perf["underperforming"]
    assert perf["vs_reference"]["shortfall_kwh"] > 0.1 * perf["vs_reference"]["expected_kwh"]


def test_dusty_sensor_alone_makes_plant_look_too_good(scenario):
    perf = metrics.plant_performance(scenario("sensor_only")[0])
    assert not perf["underperforming"]
    assert perf["vs_sensor"]["ratio"] > 1.15


def test_flag_reported(scenario):
    assert metrics.plant_performance(scenario("sensor_flagged")[0])["sensor_flagged"]
    assert not metrics.plant_performance(scenario("sensor_only")[0])["sensor_flagged"]


def test_inverter_performance(scenario):
    df = metrics.inverter_performance(scenario("soiling_only")[0])
    assert len(df) == 16
    assert df["ratio_unclipped"].between(0.78, 0.92).all()
    trap = metrics.inverter_performance(scenario("trap")[0])
    assert trap.loc["INV-12", "missing_daylight_steps"] == 21
    assert trap.drop(index="INV-12")["missing_daylight_steps"].eq(0).all()
    assert trap.loc["INV-07", "ratio"] < trap.drop(index="INV-07")["ratio"].min()


def test_comms_gap_checked_against_meter(scenario):
    store, twin = scenario("trap")
    gaps = metrics.data_gaps(store)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["device_id"] == "INV-12"
    assert (gap["start"], gap["end"], gap["steps"]) == (at(twin, "09:15"), at(twin, "11:00"), 21)
    assert gap["still_producing"] and gap["producing_fraction"] == pytest.approx(1.0, abs=0.1)
    assert not gap["ongoing"]


def test_dead_and_silent_device_is_not_producing(scenario):
    store, twin = scenario("dead_and_silent")
    (gap,) = metrics.data_gaps(store)
    assert gap["device_id"] == "INV-05" and gap["ongoing"]
    assert gap["start"] == at(twin, "08:00")
    assert gap["still_producing"] is False and abs(gap["producing_fraction"]) < 0.1


def test_station_gap(scenario):
    store, twin = scenario("station_dropout")
    (gap,) = metrics.data_gaps(store)
    assert gap["device_id"] == "WS-01" and gap["steps"] == 6
    assert gap["start"] == at(twin, "07:00") and gap["end"] == at(twin, "07:30")


def test_no_gaps_when_healthy(scenario):
    assert metrics.data_gaps(scenario("healthy")[0]) == []


def test_curtailment_period(scenario):
    store, twin = scenario("curtailment")
    (period,) = metrics.curtailment_periods(store)
    assert (period["start"], period["end"]) == (at(twin, "07:30"), at(twin, "09:30"))
    assert period["limit_kw"] == 24000.0 and period["meter_at_limit"]
    assert period["expected_median_kw"] > period["limit_kw"]
    perf = metrics.plant_performance(store)
    assert perf["curtailed_steps"] == 24 and not perf["underperforming"]
    assert metrics.curtailment_periods(scenario("healthy")[0]) == []


def test_window_arguments(scenario):
    store, twin = scenario("soiling_only")
    before = metrics.plant_performance(store, twin.start, at(twin, "07:00"))
    assert not before["underperforming"]
    assert metrics.plant_performance(store, at(twin, "07:00"), twin.end)["underperforming"]
