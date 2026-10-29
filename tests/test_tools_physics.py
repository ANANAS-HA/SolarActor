from datetime import timedelta

import pytest

from raqeeb.tools import physics
from raqeeb.twin import plant as twin_physics
from raqeeb.twin.weather import weather_5min
from tests.tool_scenarios import FLAG_AT, at


def test_expected_from_store_matches_twin_expected(scenario):
    """The tools rebuild the twin's fault-free line from stored reference rows alone."""
    store, twin = scenario("healthy")
    from_store = physics.expected_plant_ac(store)
    direct = twin_physics.expected_plant_ac(weather_5min(twin.start, twin.end, twin.plant), twin.plant)
    assert from_store.to_numpy() == pytest.approx(direct.to_numpy(), rel=1e-6, abs=1e-6)


def test_expected_vs_truth_within_reference_error(scenario):
    store, twin = scenario("healthy")
    exp = physics.expected_plant_ac(store)
    truth = {t.ts: t.plant_ac_kw for t in twin.truth}
    day = exp[exp > 0.2 * twin.plant.rated_ac_kw]
    ratio = [truth[ts.to_pydatetime()] / kw for ts, kw in day.items()]
    assert all(abs(r - 1) < 0.1 for r in ratio)


def test_expected_columns(scenario):
    store, twin = scenario("healthy")
    exp = physics.expected(store)
    assert set(exp.columns.get_level_values("inverter_id")) == set(twin.plant.inverters.ids)
    ac = physics.expected_inverter(store, "ac_kw")
    assert ac.max().max() <= twin.plant.inverters.ac_kw + 1e-6


def test_expected_from_dusty_sensor_is_low(scenario):
    store, twin = scenario("sensor_only")
    late = slice(at(twin, "08:00"), at(twin, "13:00"))
    ref = physics.expected_plant_ac(store, basis="reference").loc[late].sum()
    sen = physics.expected_plant_ac(store, basis="sensor").loc[late].sum()
    assert sen / ref < 0.85


def test_auto_basis_switches_when_flagged(scenario):
    store, twin = scenario("sensor_flagged")
    flag = at(twin, FLAG_AT)
    basis = physics.expected_basis(store, store.meter().index)
    assert set(basis[basis.index < flag]) == {"sensor"}
    assert set(basis[basis.index >= flag]) == {"reference"}
    auto = physics.expected_plant_ac(store, basis="auto")
    ref = physics.expected_plant_ac(store, basis="reference")
    sen = physics.expected_plant_ac(store, basis="sensor")
    assert auto[auto.index >= flag].to_numpy() == pytest.approx(ref[ref.index >= flag].to_numpy())
    assert auto[auto.index < flag].to_numpy() == pytest.approx(sen[sen.index < flag].to_numpy())


def test_auto_basis_uses_reference_where_station_missing(scenario):
    store, twin = scenario("station_dropout")
    basis = physics.expected_basis(store, store.meter().index)
    gap = (basis.index >= at(twin, "07:00")) & (basis.index < at(twin, "07:30"))
    assert set(basis[gap]) == {"reference"} and set(basis[~gap]) == {"sensor"}
    assert physics.expected_plant_ac(store, basis="auto").notna().all()


def test_tracker_stuck(scenario):
    store, twin = scenario("tracker_stuck")
    result = physics.tracker_check(store)
    assert [k for k, v in result.items() if v["stuck"]] == ["INV-03"]
    assert result["INV-03"]["held_at_deg"] == pytest.approx(0.0, abs=0.5)
    assert abs(result["INV-03"]["first_off"] - at(twin, "05:00")) <= timedelta(minutes=30)


def test_wind_stow_is_not_a_stuck_tracker(scenario):
    store, _ = scenario("trap")
    assert not any(v["stuck"] for v in physics.tracker_check(store).values())
    angle = physics.expected_tracker_angle(store)
    stow = store.weather_station()["wind_ms"] > 15
    assert (angle[stow] == 0).all() and stow.sum() == 12
