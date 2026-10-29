from datetime import timedelta

import pytest

from raqeeb.tools import peers
from tests.tool_scenarios import at


def _outliers(store):
    return [k for k, v in peers.peer_outliers(store).items() if v["outlier"]]


def test_healthy_peers_agree(scenario):
    store, _ = scenario("healthy")
    ratios = peers.peer_ratio(store)
    assert ratios.stack().between(0.95, 1.05).all()
    assert _outliers(store) == []
    assert peers.flat_caps(store) == [] and peers.plant_flat_cap(store) == []
    assert not any(v["hot"] for v in peers.temperature_check(store).values())


def test_plant_wide_soiling_is_not_a_peer_outlier(scenario):
    assert _outliers(scenario("soiling_only")[0]) == []


def test_derate_outlier_flat_cap_and_heat(scenario):
    store, twin = scenario("trap")
    out = peers.peer_outliers(store)
    assert _outliers(store) == ["INV-07"]
    assert abs(out["INV-07"]["onset"] - at(twin, "08:30")) <= timedelta(minutes=15)
    assert out["INV-07"]["status_counts"].get("DERATED", 0) > 0

    (cap,) = peers.flat_caps(store)
    assert cap["inverter_id"] == "INV-07"
    assert cap["level_kw"] == pytest.approx(1400.0, rel=0.02)
    assert cap["ratio_to_peers"] < 0.95 and cap["derated_status_steps"] >= cap["steps"] - 2
    assert abs(cap["start"] - at(twin, "08:30")) <= timedelta(minutes=15)

    hot = {k: v for k, v in peers.temperature_check(store).items() if v["hot"]}
    assert list(hot) == ["INV-07"]
    assert hot["INV-07"]["max_temp_c"] >= twin.plant.inverters.derate_temp_c
    assert hot["INV-07"]["first_hot"] == at(twin, "08:30")


def test_missing_rows_do_not_count_as_low(scenario):
    store, _ = scenario("trap")
    out = peers.peer_outliers(store)["INV-12"]
    assert not out["outlier"]
    assert out["steps"] < peers.peer_outliers(store)["INV-01"]["steps"]


def test_curtailment_is_plant_wide_flat_cap_with_grid_limit(scenario):
    store, twin = scenario("curtailment")
    (cap,) = peers.plant_flat_cap(store)
    assert (cap["start"], cap["end"]) == (at(twin, "07:30"), at(twin, "09:30"))
    assert cap["grid_limit_kw"] == 24000.0 and cap["grid_limit_steps"] == 24
    assert cap["plant_kw"] == pytest.approx(24000.0, rel=0.01)
    assert cap["curtailed_status_share"] == 1.0
    assert peers.flat_caps(store) == [] and _outliers(store) == []


def test_stuck_tracker_is_a_peer_outlier(scenario):
    store, twin = scenario("tracker_stuck")
    out = peers.peer_outliers(store)
    assert _outliers(store) == ["INV-03"]
    assert out["INV-03"]["ratio_since_onset"] < 0.9
    assert peers.flat_caps(store) == []
