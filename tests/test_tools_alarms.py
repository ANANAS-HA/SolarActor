from raqeeb.contracts import Command
from raqeeb.tools import AGENT_TOOLS, alarms
from tests.conftest import call, checked
from tests.tool_scenarios import SCENARIOS, at, build


def test_alarm_groups_trap(run):
    store, s, _ = run("trap")
    result = call(alarms.alarm_groups, store, s)
    groups = {g["code"]: g for g in result.values["groups"]}
    # device alarms from the twin + symptom alarms from the injector (LOW_PR family)
    assert {"HIGH_WIND", "TRACKER_STOW_WIND", "DERATE_TEMP", "COMM_LOSS", "LOW_PR"} <= set(groups)
    stow = groups["TRACKER_STOW_WIND"]
    assert stow["plant_wide"] and stow["inverters"] == 16 and stow["related_faults"] == []
    assert groups["DERATE_TEMP"]["devices"] == ["INV-07"] and not groups["DERATE_TEMP"]["plant_wide"]
    assert groups["DERATE_TEMP"]["related_faults"] == ["INVERTER_DERATE"]
    assert groups["COMM_LOSS"]["devices"] == ["INV-12"]
    assert groups["COMM_LOSS"]["first"] == at(s, "09:15")
    assert groups["HIGH_WIND"]["devices"] == ["WS-01"]
    assert "severity" not in groups["HIGH_WIND"] and groups["HIGH_WIND"]["alarm_severity"] == "INFO"


def test_symptom_alarms_come_from_the_injector(run):
    store, s, _ = run("tracker_stuck")
    codes = set(call(alarms.alarm_summary, store, s).values["by_code"])
    assert {"TRACKER_FAULT", "LOW_PR", "LOW_STRING_CURRENT"} <= codes


def test_alarm_summary(run):
    store, s, _ = run("trap")
    summary = call(alarms.alarm_summary, store, s)
    assert summary.values["total"] == len(store.events())
    assert summary.values["by_code"]["TRACKER_STOW_WIND"] == 16
    assert call(alarms.alarm_summary, *run("healthy")[:2]).values["total"] == 0


def test_events_frame_and_manual(run):
    events = alarms.events_frame(run("curtailment")[0])
    assert list(events["code"]) == ["GRID_LIMIT_ACTIVE"]
    assert events["related_faults"].iloc[0] == ["CURTAILMENT"]
    manual = checked(alarms.alarm_manual("COMM_LOSS"), run("healthy")[1])
    assert manual.values["vendor_id"] == "A-1203" and "meter" in manual.evidence


def test_unexpected_commands(run):
    store, s, _ = run("dead_and_silent")
    result = call(alarms.unexpected_commands, store, s)
    assert result.verdict == "unexpected_command" and result.devices == ["INV-05"]
    assert len(result.values["commands"][0]["reasons"]) == 2  # odd source and action outside the allowlist

    store, s, _ = build("sensor_flagged")
    assert call(alarms.unexpected_commands, store, s).verdict == "ok"  # raqeeb's flag_sensor is expected
    store.write_rows("commands", [Command(ts=at(s, "10:05"), source="operator", device_id="INV-04",
                                          action="reset_comms_gateway")])
    assert call(alarms.unexpected_commands, store, s).verdict == "ok"


def test_every_agent_tool_returns_a_clean_tool_result(run):
    """All AGENT_TOOLS on every fixture: ToolResult, JSON-safe, no answer leak."""
    for name in SCENARIOS:
        store, s, _ = run(name)
        for tool_name, tool in AGENT_TOOLS.items():
            result = call(tool, store, s)
            assert result.tool == tool_name
            assert (result.start, result.end) == (s.start, s.end)
            assert result.finding == bool(result.devices), (name, tool_name)
