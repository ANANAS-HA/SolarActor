import ast
from pathlib import Path

from raqeeb.config import REPO_ROOT
from raqeeb.contracts import Command
from raqeeb.tools import alarms
from tests.tool_scenarios import at, build


def test_alarm_groups_trap(scenario):
    store, twin = scenario("trap")
    groups = {g["code"]: g for g in alarms.alarm_groups(store)}
    assert set(groups) == {"HIGH_WIND", "TRACKER_STOW_WIND", "DERATE_TEMP", "COMM_LOSS"}
    stow = groups["TRACKER_STOW_WIND"]
    assert stow["plant_wide"] and stow["inverters"] == 16 and stow["related_faults"] == []
    assert groups["DERATE_TEMP"]["devices"] == ["INV-07"] and not groups["DERATE_TEMP"]["plant_wide"]
    assert groups["DERATE_TEMP"]["related_faults"] == ["INVERTER_DERATE"]
    assert groups["COMM_LOSS"]["devices"] == ["INV-12"]
    assert groups["COMM_LOSS"]["first"] == at(twin, "09:15")
    assert groups["HIGH_WIND"]["devices"] == ["WS-01"]


def test_alarm_summary(scenario):
    store, _ = scenario("trap")
    summary = alarms.alarm_summary(store)
    assert summary["total"] == len(store.events())
    assert summary["by_code"]["TRACKER_STOW_WIND"] == 16
    assert alarms.alarm_summary(scenario("healthy")[0])["total"] == 0


def test_events_frame_and_manual(scenario):
    events = alarms.events_frame(scenario("curtailment")[0])
    assert list(events["code"]) == ["GRID_LIMIT_ACTIVE"]
    assert events["related_faults"].iloc[0] == ["CURTAILMENT"]
    manual = alarms.alarm_manual("COMM_LOSS")
    assert manual["vendor_id"] == "A-1203" and "meter" in manual["manual"]


def test_unexpected_commands():
    store, twin = build("sensor_flagged")
    assert alarms.unexpected_commands(store) == []  # raqeeb's flag_sensor is expected
    store.write_rows("commands", [
        Command(ts=at(twin, "10:00"), source="10.0.0.99", device_id="INV-04", action="set_active_power",
                params={"kw": 0}),
        Command(ts=at(twin, "10:05"), source="operator", device_id="INV-04", action="reset_comms_gateway"),
    ])
    (odd,) = alarms.unexpected_commands(store)
    assert odd["device_id"] == "INV-04" and len(odd["reasons"]) == 2


def test_tools_never_read_twin_state_truth_or_answer_key():
    """Tools read only the store: no simulator import, no twin state, truth or answer key."""
    for path in sorted((REPO_ROOT / "raqeeb" / "tools").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
        assert not any(m and (m.startswith("raqeeb.twin.simulator") or m.startswith("raqeeb.twin.controls")
                              or m.startswith("raqeeb.faults") or m.startswith("raqeeb.bench")) for m in imported), path
        for word in (".truth", "answer_key", ".state", "PlantState"):
            assert word not in source, f"{Path(path).name} mentions {word}"
