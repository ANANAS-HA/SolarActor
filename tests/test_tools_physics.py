from datetime import timedelta

import pytest

from raqeeb.tools import physics, weather_tools
from raqeeb.twin import plant as twin_physics
from raqeeb.twin.weather import weather_5min
from tests.conftest import call
from tests.tool_scenarios import FLAG_AT, at


def test_expected_from_store_matches_twin_expected(run):
    """The tools rebuild the twin's fault-free line from stored reference rows alone."""
    store, s, inj = run("healthy")
    from_store = physics.expected_plant_ac(store)
    direct = twin_physics.expected_plant_ac(weather_5min(s.start, s.end, inj.twin.plant), inj.twin.plant)
    assert from_store.to_numpy() == pytest.approx(direct.to_numpy(), rel=1e-6, abs=1e-6)


def test_expected_vs_truth_within_reference_error(run):
    store, _, inj = run("healthy")
    exp = physics.expected_plant_ac(store)
    truth = {t.ts: t.plant_ac_kw for t in inj.twin.truth}
    day = exp[exp > 0.2 * inj.twin.plant.rated_ac_kw]
    assert all(abs(truth[ts.to_pydatetime()] / kw - 1) < 0.1 for ts, kw in day.items())


def test_expected_columns(run):
    store, _, inj = run("healthy")
    exp = physics.expected(store)
    assert set(exp.columns.get_level_values("inverter_id")) == set(inj.twin.plant.inverters.ids)
    assert physics.expected_inverter(store, "ac_kw").max().max() <= inj.twin.plant.inverters.ac_kw + 1e-6


def test_expected_from_sensor_is_the_shared_calculation(run):
    """basis='sensor' is expected_from_sensor() on the stored station rows, step by step too."""
    store, _, inj = run("healthy")
    ws = store.weather_station()
    geo = weather_tools.solar_geometry(ws.index)
    whole = physics.expected_from_sensor(ws["ghi_sensor_wm2"], ws["ambient_c"], ws["wind_ms"], geo)
    assert physics.expected(store, basis="sensor").to_numpy() == pytest.approx(whole.to_numpy())
    k = 60
    one = physics.expected_from_sensor(ws["ghi_sensor_wm2"].iloc[[k]], ws["ambient_c"].iloc[[k]],
                                       ws["wind_ms"].iloc[[k]], geo.iloc[[k]])
    assert one.to_numpy() == pytest.approx(whole.iloc[[k]].to_numpy())


def test_expected_from_dusty_sensor_is_low(run):
    store, s, _ = run("sensor_only")
    late = slice(at(s, "08:00"), at(s, "13:00"))
    ref = physics.expected_plant_ac(store, basis="reference").loc[late].sum()
    sen = physics.expected_plant_ac(store, basis="sensor").loc[late].sum()
    assert sen / ref < 0.85


def test_auto_basis_switches_when_flagged(run):
    store, s, _ = run("sensor_flagged")
    flag = at(s, FLAG_AT)
    basis = physics.expected_basis(store, store.meter().index)
    assert set(basis[basis.index < flag]) == {"sensor"}
    assert set(basis[basis.index >= flag]) == {"reference"}
    auto = physics.expected_plant_ac(store, basis="auto")
    ref = physics.expected_plant_ac(store, basis="reference")
    sen = physics.expected_plant_ac(store, basis="sensor")
    assert auto[auto.index >= flag].to_numpy() == pytest.approx(ref[ref.index >= flag].to_numpy())
    assert auto[auto.index < flag].to_numpy() == pytest.approx(sen[sen.index < flag].to_numpy())


def test_auto_basis_uses_reference_where_station_missing(run):
    store, s, _ = run("station_dropout")
    basis = physics.expected_basis(store, store.meter().index)
    gap = (basis.index >= at(s, "07:00")) & (basis.index < at(s, "07:30"))
    assert set(basis[gap]) == {"reference"} and set(basis[~gap]) == {"sensor"}
    assert physics.expected_plant_ac(store, basis="auto").notna().all()


def test_tracker_stuck(run):
    store, s, _ = run("tracker_stuck")
    result = call(physics.tracker_check, store, s)
    assert result.verdict == "tracker_off" and result.devices == ["INV-03"]
    inv = result.values["inverters"]["INV-03"]
    assert inv["held_at_deg"] == pytest.approx(0.0, abs=0.5)
    assert abs(inv["first_off"] - at(s, "05:00")) <= timedelta(minutes=30)


def test_wind_stow_is_not_a_stuck_tracker(run):
    store, s, _ = run("trap")
    assert call(physics.tracker_check, store, s).verdict == "ok"
    angle = physics.expected_tracker_angle(store)
    stow = store.weather_station()["wind_ms"] > 15
    assert (angle[stow] == 0).all() and stow.sum() == 12
