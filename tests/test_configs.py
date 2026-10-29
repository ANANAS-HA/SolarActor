from raqeeb.config import load_actions, load_alarm_codes, load_plant, load_settings
from raqeeb.contracts import ActionType


def test_actions_match_allowlist_exactly():
    actions = load_actions()
    assert set(actions) == set(ActionType)
    gated = {a for a, spec in actions.items() if spec.needs_approval}
    assert gated == {ActionType.SET_POWER_LIMIT, ActionType.ISOLATE_DEVICE}
    assert not actions[ActionType.ISOLATE_DEVICE].reversible


def test_grid_setpoint_change_is_never_allowed():
    assert not any("setpoint" in a.value or "grid" in a.value for a in ActionType)


def test_alarm_codes_load():
    codes = load_alarm_codes()
    assert {"COMM_LOSS", "DERATE_TEMP", "LOW_PR", "GRID_LIMIT_ACTIVE"} <= set(codes)
    assert all(spec.manual.strip() for spec in codes.values())


def test_plant_config():
    plant = load_plant()
    settings = load_settings()
    assert plant.inverters.ids[0] == "INV-01"
    assert plant.inverters.ids[-1] == "INV-16"
    assert plant.site.lat == settings.site_lat
    assert plant.site.lon == settings.site_lon
    assert plant.weather_station_id == "WS-01"


def test_settings_repr_hides_keys():
    settings = load_settings().model_copy(update={"anthropic_api_key": "sk-secret"})
    assert "sk-secret" not in repr(settings)
    assert "sk-secret" not in str(settings)
