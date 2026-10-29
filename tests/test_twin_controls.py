from datetime import datetime, timedelta, timezone

import pytest

from raqeeb.config import load_plant
from raqeeb.contracts import ActionType
from raqeeb.twin import controls
from raqeeb.twin.simulator import Twin
from raqeeb.twin.weather import weather_5min

START = datetime(2025, 7, 7, 6, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=4)


@pytest.fixture(scope="module")
def plant():
    return load_plant()


@pytest.fixture(scope="module")
def weather(plant):
    return weather_5min(START, END, plant)


@pytest.fixture
def twin(plant, weather):
    return Twin(START, END, plant=plant, seed=3, weather=weather)


def test_every_allowlisted_action_has_a_control():
    assert set(controls.CONTROLS) == set(ActionType)


def test_unknown_action_rejected(twin):
    with pytest.raises(ValueError, match="allowlist"):
        controls.apply_action(twin, "change_grid_setpoint", "PPC-01")


def test_unknown_target_rejected(twin):
    with pytest.raises(ValueError, match="unknown"):
        controls.reset_comms(twin, "INV-99")


def test_reset_comms_restores_after_delay(twin, plant):
    twin.state.inverters["INV-12"].comms_up = False
    twin.advance(5)
    batch = twin.step()
    controls.apply_action(twin, ActionType.RESET_COMMS_GATEWAY, "INV-12")
    assert not twin.state.inverters["INV-12"].comms_up
    batch = twin.advance(plant.controls.comms_reset_delay_min + 5)
    assert twin.state.inverters["INV-12"].comms_up
    assert any(r.inverter_id == "INV-12" for r in batch.inverter_readings)
    assert [(c.action, c.device_id, c.source) for c in batch.commands] == [("reset_comms_gateway", "INV-12", "raqeeb")]


def test_cleaning_removes_soiling_after_delay(twin, plant):
    for inv in twin.state.inverters.values():
        inv.soiling_loss = 0.2
    controls.apply_action(twin, ActionType.CREATE_CLEANING_WORK_ORDER, "ALL")
    twin.advance(plant.controls.cleaning_delay_min - 5)
    assert all(inv.soiling_loss == 0.2 for inv in twin.state.inverters.values())
    twin.advance(10)
    assert all(inv.soiling_loss == 0.0 for inv in twin.state.inverters.values())


def test_set_power_limit_caps_and_cools(twin):
    inv = twin.state.inverters["INV-07"]
    inv.derate_cap_kw, inv.temp_offset_c = 1500, 20
    before = next(r for r in twin.step().inverter_readings if r.inverter_id == "INV-07")
    controls.apply_action(twin, ActionType.SET_POWER_LIMIT, "INV-07", {"kw": 1000})
    after = next(r for r in twin.step().inverter_readings if r.inverter_id == "INV-07")
    assert after.ac_kw == pytest.approx(1000, rel=0.02)
    assert after.temp_c < before.temp_c
    controls.set_power_limit(twin, "INV-07", None)
    assert inv.power_limit_kw is None


def test_flag_sensor_changes_flag_not_readings(twin):
    controls.apply_action(twin, ActionType.FLAG_SENSOR_USE_REFERENCE, "WS-01")
    assert twin.state.station.flagged
    with pytest.raises(ValueError):
        controls.flag_sensor(twin, "INV-01")


def test_resume_tracking_clears_override(twin):
    twin.state.inverters["INV-03"].tracker_override_deg = 0.0
    controls.apply_action(twin, ActionType.RESUME_TRACKING, "INV-03")
    assert twin.state.inverters["INV-03"].tracker_override_deg is None


def test_technician_repairs_hardware_after_delay(twin, plant):
    inv = twin.state.inverters["INV-05"]
    inv.string_loss, inv.soiling_loss = 0.25, 0.1
    controls.apply_action(twin, ActionType.DISPATCH_TECHNICIAN, "INV-05")
    twin.advance(plant.controls.technician_delay_min + 5)
    assert inv.string_loss == 0.0
    assert inv.soiling_loss == 0.1  # cleaning is a separate work order


def test_isolate_device_zeroes_output(twin):
    controls.apply_action(twin, ActionType.ISOLATE_DEVICE, "INV-09")
    row = next(r for r in twin.step().inverter_readings if r.inverter_id == "INV-09")
    assert row.ac_kw == 0 and twin.state.inverters["INV-09"].isolated
