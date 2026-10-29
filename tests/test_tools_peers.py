from datetime import timedelta

import pytest

from raqeeb.tools import peers
from tests.conftest import call
from tests.tool_scenarios import at


def test_healthy_peers_agree(run):
    store, s, _ = run("healthy")
    assert peers.peer_ratio(store).stack().between(0.95, 1.05).all()
    for tool in (peers.peer_outliers, peers.flat_caps, peers.plant_flat_cap, peers.temperature_check):
        result = call(tool, store, s)
        assert result.verdict == "ok" and result.devices == [], tool.__name__


def test_plant_wide_soiling_is_not_a_peer_outlier(run):
    assert call(peers.peer_outliers, *run("soiling_only")[:2]).verdict == "ok"


def test_derate_outlier_flat_cap_and_heat(run):
    store, s, inj = run("trap")
    out = call(peers.peer_outliers, store, s)
    assert out.verdict == "peer_outlier" and out.devices == ["INV-07"]
    inv7 = out.values["inverters"]["INV-07"]
    assert abs(inv7["onset"] - at(s, "08:30")) <= timedelta(minutes=15)
    assert inv7["status_counts"].get("DERATED", 0) > 0

    caps = call(peers.flat_caps, store, s)
    assert caps.verdict == "device_flat_cap" and caps.devices == ["INV-07"]
    (cap,) = caps.values["caps"]
    assert cap["level_kw"] == pytest.approx(1400.0, rel=0.02)
    assert cap["ratio_to_peers"] < 0.95 and cap["derated_status_steps"] >= cap["steps"] - 2
    assert abs(cap["start"] - at(s, "08:30")) <= timedelta(minutes=15)

    hot = call(peers.temperature_check, store, s)
    assert hot.verdict == "hot" and hot.devices == ["INV-07"]
    inv7 = hot.values["inverters"]["INV-07"]
    assert inv7["max_temp_c"] >= inj.twin.plant.inverters.derate_temp_c
    assert inv7["first_hot"] == at(s, "08:30")


def test_missing_rows_do_not_count_as_low(run):
    store, s, _ = run("trap")
    rows = call(peers.peer_outliers, store, s).values["inverters"]
    assert not rows["INV-12"]["outlier"]
    assert rows["INV-12"]["steps"] < rows["INV-01"]["steps"]


def test_curtailment_is_plant_wide_flat_cap_with_grid_limit(run):
    store, s, _ = run("curtailment")
    result = call(peers.plant_flat_cap, store, s)
    assert result.verdict == "plant_flat_cap_grid_limit" and result.devices == ["ALL"]
    (cap,) = result.values["periods"]
    assert (cap["start"], cap["end"]) == (at(s, "07:30"), at(s, "09:30"))
    assert cap["grid_limit_kw"] == 24000.0 and cap["grid_limit_steps"] == 24
    assert cap["plant_kw"] == pytest.approx(24000.0, rel=0.01)
    assert cap["curtailed_status_share"] == 1.0
    assert call(peers.flat_caps, store, s).verdict == "ok"
    assert call(peers.peer_outliers, store, s).verdict == "ok"


def test_stuck_tracker_is_a_peer_outlier(run):
    store, s, _ = run("tracker_stuck")
    out = call(peers.peer_outliers, store, s)
    assert out.devices == ["INV-03"]
    assert out.values["inverters"]["INV-03"]["ratio_since_onset"] < 0.9
    assert call(peers.flat_caps, store, s).verdict == "ok"
