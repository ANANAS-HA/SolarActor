"""Plain language layer (ui/plain.py): every FaultType, ActionType, tool and verdict has a non-fallback entry;
devices come from plant.yaml; numbers read as a plant manager would say them; no markdown is produced."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from raqeeb.config import REPO_ROOT, load_plant
from raqeeb.contracts import VERDICT_INCONCLUSIVE, VERDICT_OK, ActionType, FaultType, SymptomKind, ToolResult
from raqeeb.tools import AGENT_TOOLS
from raqeeb.ui import plain
from raqeeb.ui.runlog import ProposalCheck, ProposalRecord, RunLog

SHOWCASE = REPO_ROOT / "raqeeb" / "ui" / "replays" / "final"
TS = datetime(2025, 12, 21, 6, 0, tzinfo=timezone.utc)


def test_every_fault_type_has_a_name_and_meaning():
    for ft in FaultType:
        f = plain.fault(ft)
        assert f.known and f.name and f.meaning and plain.UNFAMILIAR not in f.name, ft
        assert f.name[0].isupper() and not f.name.isupper()  # sentence case, never all caps
    assert plain.fault("SOILING").name == "Dust on the panels"
    assert plain.fault("CURTAILMENT").name.endswith("(not a fault)")
    unknown = plain.fault("GRID_WOBBLE_X")
    assert not unknown.known and unknown.name == f"Grid wobble x ({plain.UNFAMILIAR})"


def test_every_action_type_has_words_in_both_tenses():
    for a in ActionType:
        assert a in plain.ACTIONS and a in plain.ACTION_SHORT, a
        text = plain.action_text(a, "INV-07", {"kw": 1200.0})
        assert "_" not in text and "INV-07" not in text and "Inverter 7" in text or a == ActionType.FLAG_SENSOR_USE_REFERENCE
    assert plain.action_text(ActionType.SET_POWER_LIMIT, "INV-03", {"kw": 1624.0}) == "Lower Inverter 3's output limit to 1.6 MW"
    assert plain.action_text(ActionType.SET_POWER_LIMIT, "INV-03", {"kw": None}) == "Remove Inverter 3's output limit"
    assert plain.action_text(ActionType.CREATE_CLEANING_WORK_ORDER, "ALL", past=True) == "Ordered cleaning of all panels"
    assert plain.action(ActionType.RESET_COMMS_GATEWAY) == "Reset the inverter's communications"


def test_every_agent_tool_and_verdict_has_an_entry():
    for name in AGENT_TOOLS:
        assert name in plain.TOOLS and not plain.tool(name).startswith("Ran the"), name
    assert plain.tool("peer_outliers") == "Compared each inverter with its peers"
    # every finding code the tools emit (grepped from raqeeb/tools) has a noun and a sentence
    codes = ("sensor_low", "sensor_low_panels_too", "hot", "device_flat_cap", "peer_outlier", "underperforming",
             "inverters_low", "data_gap", "curtailment", "plant_flat_cap", "plant_flat_cap_grid_limit", "tracker_off",
             "stow_wind", "high_wind", "unexpected_command", "alarms")
    for code in codes:
        assert code in plain.FINDING_NOUNS, code
    assert plain.verdict(ToolResult(tool="x", verdict=VERDICT_OK)) == "Nothing unusual"
    assert plain.verdict(ToolResult(tool="x", verdict=VERDICT_INCONCLUSIVE)) == "Not enough data yet"


def test_verdict_sentences_carry_the_numbers_from_the_showcase_record():
    log = RunLog.load(SHOWCASE / "fin_0029__raqeeb_local.json.gz")
    seen = {}
    for p in log.polls:
        for c in p.checks:
            if c.finding:
                seen.setdefault(c.verdict, c)
    sensor = plain.verdict(seen["sensor_low_panels_too"])
    assert sensor.startswith("Sun sensor reads 28% below the reference on a clear sky") and "trap" in sensor
    hot = plain.verdict(seen["hot"])
    assert hot.startswith("Inverter 3 at 71 °C") and "above its peers" in hot
    assert plain.verdict(seen["device_flat_cap"]).startswith("Inverter 3 held flat at 1.6 MW (78% of its peers)")
    assert plain.verdict(seen["peer_outlier"]).startswith("Inverter 3 at 77% of its peers")
    under = plain.verdict(seen["underperforming"])
    assert under.startswith("Plant output") and "below expected from the weather reference" in under and "MWh short" in under
    assert plain.verdict(seen["stow_wind"]).startswith("Wind up to 20.6 m/s")
    assert "alarms on" in plain.verdict(seen["alarms"]) or "alarm groups" in plain.verdict(seen["alarms"])
    for c in seen.values():  # no raw identifier leaks through
        text = plain.verdict(c)
        assert "INV-" not in text and "_" not in text.replace("°", ""), text
    # the fallback for a code this module does not know keeps the tool's evidence
    odd = ToolResult(tool="x", verdict="odd_code", evidence="computed text")
    assert plain.verdict(odd) == "Found odd code: computed text"


def test_devices_come_from_the_plant_layout():
    plant = load_plant()
    assert plain.device("INV-07") == "Inverter 7"
    assert plain.device("T4") == "Transformer T4 (inverters 13-16)"
    assert plain.device(plant.weather_station_id) == "Sun sensor"
    assert plain.device("ALL") == "all panels"
    assert plain.device("EDGE-W") == "West fence edge (inverters 1, 5, 9, 13)"
    assert plain.device(plant.meter_id) == "Substation meter" and plain.device(plant.plant_controller_id) == "Plant controller"
    assert plain.device("XYZ-99") == "XYZ-99"  # unknown ids stay as they are
    assert plain.devices(["INV-13", "INV-14", "INV-15", "INV-16"]) == "Inverters 13, 14, 15 and 16"
    assert plain.devices(["INV-05", "WS-01"]) == "Inverter 5 and Sun sensor"
    assert plain.cause("TRANSFORMER_TRIP@T4") == "Transformer trip (Transformer T4 (inverters 13-16))".replace(
        "(Transformer T4 (inverters 13-16))", "(Transformer T4 (inverters 13-16))")
    assert plain.cause("SOILING@ALL") == "Dust on the panels (all panels)"
    assert plain.cause("unexplained") == "Unexplained"
    for ft in FaultType:
        assert "_" not in plain.cause(f"{ft.value}@INV-01")


def test_numbers_read_as_power_and_percent():
    assert plain.kw(6992) == "7.0 MW" and plain.kw(650.4) == "650 kW" and plain.kw(24528) == "24.5 MW"
    assert plain.power_change(6992, 24528, 25364) == "7.0 MW -> 24.5 MW, 3% below expected"
    assert plain.power_change(0, 1624.9, 0) == "0 kW -> 1.6 MW"
    assert plain.pct(0.7157) == "72%" and plain.below(1.0) == "on expected" and plain.below(1.04) == "4% above expected"


def test_gate_origin_and_status_words():
    assert plain.gate("auto") == "Safe to do automatically"
    assert plain.gate("needs_approval") == "Needs your approval" == plain.gate("needs approval")
    assert plain.gate("rejected") == "Not allowed" and plain.gate("held back: no fix left") == "Not allowed: no fix left"
    assert plain.origin("SOILING@ALL") == "Found by the rules"
    accepted = ProposalRecord(ts=TS, label="TRANSFORMER_TRIP@T4", fault_type="TRANSFORMER_TRIP", target="T4",
                              decision="accepted", checks=[ProposalCheck(check="a", result="b", passed=True),
                                                           ProposalCheck(check="c", result="d")])
    rejected = ProposalRecord(ts=TS, label="NEW_SHADING@EDGE-W", fault_type="NEW_SHADING", target="EDGE-W",
                              decision="rejected", reason="footprint does not match")
    assert plain.origin("TRANSFORMER_TRIP@T4", [accepted]) == "Proposed by the model, confirmed by 2 checks"
    assert plain.origin("NEW_SHADING@EDGE-W", [rejected]) == "Proposed by the model, rejected: footprint does not match"
    assert "score" not in plain.origin("TRANSFORMER_TRIP@T4", [accepted]).lower()
    for status in ("open", "acting", "resolved", "escalated", "expected", "flagged"):
        assert plain.cause_status(status) and "_" not in plain.cause_status(status)
    for kind in SymptomKind:
        assert "_" not in plain.symptom(kind, "INV-03")
    assert plain.symptom_from_id("ALARM:DERATE_TEMP:INV-03") == "Alarm: derate temp: Inverter 3"
    assert plain.symptom_from_id("HIGH_TEMPERATURE:INV-03") == "Running hot: Inverter 3"


def test_timeline_sentences_are_one_line_active_voice():
    assert plain.found("SOILING@ALL") == "Found dust on the panels (all panels), by the rules."
    accepted = ProposalRecord(ts=TS, label="TRANSFORMER_TRIP@T4", fault_type="TRANSFORMER_TRIP", target="T4",
                              decision="accepted", checks=[ProposalCheck(check="a", result="b", passed=True)])
    assert plain.found("TRANSFORMER_TRIP@T4", [accepted]).startswith("The model proposed transformer trip (Transformer T4")
    assert plain.acted(ActionType.CREATE_CLEANING_WORK_ORDER, "ALL") == "Ordered cleaning of all panels automatically."
    assert plain.approval_needed(ActionType.SET_POWER_LIMIT, "INV-03", {"kw": 1624}) == \
        "Needs your approval: lower Inverter 3's output limit to 1.6 MW."
    assert plain.decided(ActionType.SET_POWER_LIMIT, "INV-03", True, "operator", "fan replaced", {"kw": 1624}) == \
        "You approved: lower Inverter 3's output limit to 1.6 MW (fan replaced)."
    assert plain.verified(ActionType.CREATE_CLEANING_WORK_ORDER, "ALL", True, 7181, 31502, 32431) == \
        "Verified: ordered cleaning of all panels and output recovered (7.2 MW -> 31.5 MW, 3% below expected)."
    assert plain.verified(ActionType.RESET_COMMS_GATEWAY, "INV-12", False, 0, 0, 100).startswith("Not recovered yet")
    assert plain.escalated("TRANSFORMER_TRIP@T4", ["INV-13"], "no automatic fix for this fault") == \
        "Handed to a human: transformer trip (Transformer T4 (inverters 13-16)): no automatic fix for this fault."
    assert plain.escalated("unexplained", ["INV-05", "INV-09"]) == "Handed to a human: an unexplained loss on Inverters 5 and 9."
    assert plain.proposal("NEW_SHADING@EDGE-W", "rejected", "footprint does not match") == \
        "The model proposed new shading on one edge (West fence edge (inverters 1, 5, 9, 13)); rejected: footprint does not match. Nothing changed."
    assert plain.outcome(ActionType.DISPATCH_TECHNICIAN, "INV-03", "APPLIED") == "The plant reports: send a technician to Inverter 3 done."
    for text in (plain.found("SOILING@ALL"), plain.acted(ActionType.ISOLATE_DEVICE, "INV-02", gated=True),
                 plain.escalated("unexplained", ["ALL"])):
        assert "\n" not in text and "*" not in text and "_" not in text and "[" not in text  # no markdown, one line


def test_checks_headline_counts_and_names_findings():
    log = RunLog.load(SHOWCASE / "fin_0029__raqeeb_local.json.gz")
    poll = next(p for p in log.polls if any(c.verdict == "hot" for c in p.checks))
    text = plain.checks_headline(poll.checks, named=["INVERTER_DERATE@INV-03"])
    assert text.startswith(f"Ran {len(poll.checks)} checks; ") and "found something: " in text and "a hot inverter" in text
    assert text.endswith("Named: inverter overheating and holding back.")
    assert plain.checks_headline([ToolResult(tool="x", verdict=VERDICT_OK)]) == "Ran 1 check; nothing unusual found."
