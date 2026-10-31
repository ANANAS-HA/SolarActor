"""Plain language for the console: every internal identifier as words a plant manager would use.

    fault("SENSOR_SOILING").name           -> "Dust on the sun sensor"
    device("INV-07")                        -> "Inverter 7";  device("T4") -> "Transformer T4 (inverters 13-16)"
    cause("TRANSFORMER_TRIP@T4")            -> "Transformer trip (Transformer T4, inverters 13-16)"
    tool("peer_outliers")                   -> "Compared each inverter with its peers"
    verdict(result)                         -> "Sun sensor reads 28% below the reference on a clear sky; ..."
    action_text(ActionType.SET_POWER_LIMIT, "INV-03", {"kw": 1624}) -> "Lower Inverter 3's output limit to 1.6 MW"
    gate("needs_approval")                  -> "Needs your approval"
    power_change(6992, 24528, 25364)        -> "7.0 MW -> 24.5 MW, 3% below expected"

Only dictionaries and formatting; no Streamlit, no telemetry, no agent code. Every function has a safe
fallback for a name it does not know (the identifier itself, in words). The strings returned here are
plain text: where a sentence carries free text from the record (a reason, a note, a model's words) the
page still escapes it before rendering (source.md_escape); nothing here is markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from raqeeb.contracts import (
    PLANT_TARGET,
    VERDICT_INCONCLUSIVE,
    VERDICT_OK,
    ActionType,
    FaultType,
    SymptomKind,
    ToolResult,
    parse_cause_label,
)

UNFAMILIAR = "unfamiliar fault"

# -- faults ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fault:
    name: str  # short noun phrase, sentence case
    meaning: str  # one line
    known: bool = True  # False = fallback for a name this module does not know


FAULTS: dict[str, Fault] = {
    FaultType.SOILING: Fault("Dust on the panels", "A dust storm left dust on the panels; the whole plant produces less until they are cleaned."),
    FaultType.SENSOR_SOILING: Fault("Dust on the sun sensor", "The on-site sunlight sensor reads low, so the plant's usual health check looks normal while output is lost."),
    FaultType.INVERTER_DERATE: Fault("Inverter overheating and holding back", "One inverter runs hot and caps its own output to protect itself."),
    FaultType.COMMS_DROPOUT: Fault("Inverter stopped reporting", "One inverter's data stopped arriving; it may still be producing."),
    FaultType.CURTAILMENT: Fault("Grid-ordered output limit (not a fault)", "The grid operator capped the plant's export; every inverter is held back together. Expected, no action."),
    FaultType.TRACKER_STUCK: Fault("Tracker stuck", "One block's sun tracker no longer follows the sun, so it loses output in the morning and evening."),
    FaultType.STRING_OPEN: Fault("Open string", "One inverter lost a group of panels (an open circuit or blown fuse); its output drops by a fixed share."),
    FaultType.UNAUTHORIZED_CMD: Fault("Unauthorised stop command", "An inverter was stopped by a command from a source that should not be sending commands."),
    FaultType.PROMPT_INJECTION: Fault("Instruction planted in a log line (not a fault)", "A device log line tells the agent what to do; it is untrusted text with no physical effect. Obeying it would be unsafe."),
    FaultType.TRANSFORMER_TRIP: Fault("Transformer trip", "A medium-voltage protection opened; all inverters on that transformer dropped to zero together."),
    FaultType.NEW_SHADING: Fault("New shading on one edge", "Something new near the fence shades one edge of the field at low sun."),
    FaultType.FIRMWARE_ALARM: Fault("Firmware parameter limit", "After a firmware update one inverter holds its output a few percent low and raises a new alarm code."),
    FaultType.HUNG_INVERTER: Fault("Inverter controller hung", "One inverter's controller froze: output zero, telemetry stuck at its last values, still shown as online."),
    FaultType.COMMS_DROPOUT_LOAD: Fault("Inverter stops reporting under load", "One inverter's data goes missing only when the sun is strong."),
    FaultType.SENSOR_DRIFT: Fault("Sun sensor drifting", "The sunlight sensor's error grows slowly over hours."),
    FaultType.TRACKER_STUCK_SILENT: Fault("Tracker stuck, no alarm", "One block's tracker froze at its current angle and raised no alarm."),
}


def _words(identifier: str) -> str:
    """SOME_IDENTIFIER / some_identifier -> "Some identifier"."""
    text = re.sub(r"[_\-]+", " ", str(identifier)).strip()
    return text[:1].upper() + text[1:].lower() if text else ""


def fault(name: str | FaultType) -> Fault:
    key = str(getattr(name, "value", name))
    if key in FAULTS:
        return FAULTS[key]
    return Fault(f"{_words(key)} ({UNFAMILIAR})", "A fault type this console has no description for.", known=False)


def fault_name(name: str | FaultType) -> str:
    return fault(name).name


# -- devices -----------------------------------------------------------------------------------------------

_INV = re.compile(r"^INV-(\d+)$")


def _plant():
    from raqeeb.config import load_plant

    return load_plant()


def _inverter_number(device_id: str) -> int | None:
    match = _INV.match(device_id)
    return int(match.group(1)) if match else None


def _inverter_range(ids: Iterable[str]) -> str:
    numbers = sorted(n for n in (_inverter_number(i) for i in ids) if n is not None)
    if not numbers:
        return ""
    if numbers == list(range(numbers[0], numbers[-1] + 1)) and len(numbers) > 2:
        return f"inverters {numbers[0]}-{numbers[-1]}"
    return "inverters " + ", ".join(str(n) for n in numbers)


def device(device_id: str, plant=None) -> str:
    """A device or target id in words. Unknown ids come back as they are."""
    if device_id is None or device_id == "":
        return ""
    if device_id == PLANT_TARGET:
        return "all panels"
    n = _inverter_number(device_id)
    if n is not None:
        return f"Inverter {n}"
    if device_id == "GRID":
        return "Grid connection point"
    try:
        plant = plant or _plant()
    except Exception:  # noqa: BLE001 - names still work without the plant file
        plant = None
    if plant is not None:
        if device_id == plant.weather_station_id:
            return "Sun sensor"
        if device_id == plant.meter_id:
            return "Substation meter"
        if device_id == plant.plant_controller_id:
            return "Plant controller"
        members = plant.layout.transformers.get(device_id)
        if members:
            return f"Transformer {device_id} ({_inverter_range(members)})"
        members = plant.layout.edges.get(device_id)
        if members:
            side = {"EDGE-W": "West", "EDGE-E": "East", "EDGE-N": "North", "EDGE-S": "South"}.get(device_id, device_id)
            return f"{side} fence edge ({_inverter_range(members)})"
    if device_id.startswith("WS-"):
        return "Sun sensor"
    return device_id


def devices(ids: Iterable[str], plant=None) -> str:
    """Several devices, as one phrase: "Inverter 5 and Inverter 9", "Inverters 13, 14, 15 and 16"."""
    ids = [i for i in ids if i]
    if not ids:
        return ""
    numbers = [_inverter_number(i) for i in ids]
    if len(ids) > 1 and all(n is not None for n in numbers):
        head, last = ", ".join(str(n) for n in numbers[:-1]), numbers[-1]
        return f"Inverters {head} and {last}"
    names = [device(i, plant) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def cause(label: str, plant=None) -> str:
    """FAULT_TYPE@TARGET -> "Fault name (device)". A label that is not one comes back in words."""
    try:
        fault_type, target = parse_cause_label(label)
    except ValueError:
        return _words(label) if label else ""
    where = device(target, plant)
    return f"{fault(fault_type).name} ({where})" if where else fault(fault_type).name


def cause_short(label: str) -> str:
    """The fault name only, lower case, for use inside a sentence."""
    try:
        fault_type, _ = parse_cause_label(label)
    except ValueError:
        return label.lower()
    name = fault(fault_type).name
    return name[:1].lower() + name[1:]


# -- tools and verdicts ------------------------------------------------------------------------------------

TOOLS: dict[str, str] = {
    "sensor_check": "Checked the sun sensor against the reference",
    "wind_summary": "Checked the wind at the weather station",
    "tracker_check": "Checked that the trackers follow the sun",
    "plant_performance": "Compared plant output with the weather reference",
    "inverter_performance": "Compared each inverter's output with the weather reference",
    "data_gaps": "Looked for inverters that stopped reporting",
    "curtailment_periods": "Checked for grid output limits",
    "peer_outliers": "Compared each inverter with its peers",
    "flat_caps": "Looked for inverters held at a flat limit",
    "plant_flat_cap": "Looked for the whole plant held at a flat limit",
    "temperature_check": "Checked inverter temperatures",
    "alarm_summary": "Counted the alarms",
    "alarm_groups": "Grouped the alarms",
    "unexpected_commands": "Checked for commands from unexpected sources",
    "alarm_manual": "Read the alarm manual",
}

# What a finding is, as a short noun phrase for headlines ("2 found something: a hot inverter and ...")
FINDING_NOUNS: dict[str, str] = {
    "sensor_low": "a sun sensor reading low",
    "sensor_low_panels_too": "a dusty sun sensor hiding a plant loss",
    "hot": "a hot inverter",
    "device_flat_cap": "an inverter held at a flat limit",
    "peer_outlier": "an inverter below its peers",
    "underperforming": "plant output below expected",
    "inverters_low": "inverters below expected",
    "data_gap": "an inverter not reporting",
    "stow_wind": "high wind (trackers stowed)",
    "high_wind": "high wind",
    "unexpected_command": "a command from an unexpected source",
    "curtailment": "a grid output limit",
    "plant_flat_cap": "the whole plant held at a flat limit",
    "plant_flat_cap_grid_limit": "the plant held at the grid limit",
    "tracker_off": "a tracker off its angle",
    "alarms": "alarms",
}
ALARM_TOOLS = ("alarm_summary", "alarm_groups", "alarm_manual")  # context, not findings, in headlines


def tool(name: str) -> str:
    return TOOLS.get(name, f"Ran the {_words(name).lower()} check")


def kw(value: float | None) -> str:
    """kW -> "7.0 MW" above 1000, else "650 kW"."""
    if value is None:
        return "no value"
    value = float(value)
    return f"{value / 1000:.1f} MW" if abs(value) >= 1000 else f"{value:.0f} kW"


def pct(ratio: float | None, digits: int = 0) -> str:
    return "no value" if ratio is None else f"{float(ratio) * 100:.{digits}f}%"


def below(ratio: float | None) -> str:
    """A ratio to expected as "3% below expected" / "on expected" / "4% above expected"."""
    if ratio is None:
        return "no expected value"
    gap = round((1 - float(ratio)) * 100)
    if gap == 0:
        return "on expected"
    return f"{gap}% below expected" if gap > 0 else f"{-gap}% above expected"


def power_change(before_kw: float, after_kw: float, expected_kw: float | None = None) -> str:
    """"7.0 MW -> 24.5 MW, 3% below expected"; without an expected value, the change alone."""
    text = f"{kw(before_kw)} -> {kw(after_kw)}"
    if expected_kw and float(expected_kw) > 0:
        text += f", {below(float(after_kw) / float(expected_kw))}"
    return text


def hhmm(value: Any) -> str:
    """A UTC timestamp (datetime or ISO string) as HH:MM in the display zone; other values unchanged."""
    from raqeeb.ui.source import local_hhmm

    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    try:
        return local_hhmm(value)
    except Exception:  # noqa: BLE001 - a label only
        return str(value)


def _ratio_phrase(ratio: float | None, against: str) -> str:
    if ratio is None:
        return ""
    gap = round((1 - float(ratio)) * 100)
    if gap > 0:
        return f"{gap}% below {against}"
    if gap < 0:
        return f"{-gap}% above {against}"
    return f"on {against}"


def verdict(result: ToolResult, plant=None) -> str:
    """One sentence with the numbers for a tool result: "Nothing unusual", "Not enough data yet", or what
    was found. Every number comes from the tool's values; the fallback is the tool's own evidence line."""
    v, code = result.values or {}, result.verdict
    if code == VERDICT_OK:
        return "Nothing unusual"
    if code == VERDICT_INCONCLUSIVE:
        return "Not enough data yet"
    try:
        text = _verdict_text(result, v, code, plant)
    except (KeyError, TypeError, ValueError, AttributeError):
        text = None
    if text:
        return text
    noun = FINDING_NOUNS.get(code, _words(code).lower())
    return f"Found {noun}: {result.evidence}" if result.evidence else f"Found {noun}"


def _verdict_text(r: ToolResult, v: Mapping[str, Any], code: str, plant) -> str | None:
    if code in ("sensor_low", "sensor_low_panels_too"):
        ratio = v.get("sensor_vs_reference")
        text = f"Sun sensor reads {_ratio_phrase(ratio, 'the reference')} on a clear sky"
        if v.get("onset"):
            text += f" since {hhmm(v['onset'])}"
        if code == "sensor_low_panels_too":
            text += "; the panels lose about the same, so the usual health check hides the loss (the dusty-sensor trap)"
        else:
            text += "; the panels do not see this loss, so the sensor itself is suspect"
        return text
    if code == "hot":
        parts = []
        for inv in r.devices:
            info = v.get("inverters", {}).get(inv, {})
            if info.get("max_temp_c") is None:
                continue
            part = f"{device(inv, plant)} at {info['max_temp_c']:.0f} °C"
            if info.get("max_excess_c") is not None:
                part += f", {info['max_excess_c']:.0f} °C above its peers"
            if info.get("first_above_derate"):
                part += f", above the derate limit since {hhmm(info['first_above_derate'])}"
            parts.append(part)
        return "; ".join(parts) or None
    if code == "device_flat_cap":
        parts = [f"{device(c['inverter_id'], plant)} held flat at {kw(c['level_kw'])} ({pct(c['ratio_to_peers'])} of its peers) "
                 f"from {hhmm(c['start'])} to {hhmm(c['end'])}" for c in v.get("caps", [])]
        return "; ".join(parts) or None
    if code == "peer_outlier":
        parts = []
        for inv in r.devices:
            info = v.get("inverters", {}).get(inv, {})
            part = f"{device(inv, plant)} at {pct(info.get('ratio_since_onset'))} of its peers"
            if info.get("onset"):
                part += f" since {hhmm(info['onset'])}"
            parts.append(part)
        return "; ".join(parts) or None
    if code == "underperforming":
        ref = v.get("vs_reference", {})
        text = f"Plant output {_ratio_phrase(ref.get('ratio'), 'expected from the weather reference')}"
        if ref.get("shortfall_kwh") is not None:
            text += f", {ref['shortfall_kwh'] / 1000:.1f} MWh short"
        sensor = v.get("vs_sensor", {})
        if sensor.get("ratio") is not None:
            text += f"; against the on-site sensor it looks {_ratio_phrase(sensor['ratio'], 'expected')}"
        if v.get("onset"):
            text += f"; below the limit since {hhmm(v['onset'])}"
        return text
    if code == "inverters_low":
        rows = v.get("inverters", {})
        named = [f"{device(d, plant)} ({pct(rows.get(d, {}).get('ratio_unclipped'))})" for d in r.devices]
        return f"{len(r.devices)} of {len(rows)} inverters below expected: " + ", ".join(named) if named else None
    if code == "data_gap":
        parts = []
        for g in v.get("gaps", []):
            part = f"{device(g['device_id'], plant)} sent no data for {g['steps'] * 5} min from {hhmm(g['start'])}"
            if g.get("ongoing"):
                part += " (still missing)"
            if g.get("still_producing") is True:
                part += "; the meter shows it kept producing"
            elif g.get("still_producing") is False:
                part += "; the meter shows it is not producing"
            parts.append(part)
        return "; ".join(parts) or None
    if code == "curtailment":
        parts = [f"Grid export limit {kw(p['limit_kw'])} from {hhmm(p['start'])} to {hhmm(p['end'])}"
                 for p in v.get("periods", [])]
        return ("; ".join(parts) + " (ordered by the grid operator: expected, no action)") if parts else None
    if code in ("plant_flat_cap", "plant_flat_cap_grid_limit"):
        parts = [f"all producing inverters held flat together at {kw(p['level_kw'])} each (plant {kw(p['plant_kw'])}) "
                 f"from {hhmm(p['start'])} to {hhmm(p['end'])}" for p in v.get("periods", [])]
        if not parts:
            return None
        tail = ", matching the grid limit" if code == "plant_flat_cap_grid_limit" else ", with no grid limit active"
        return "; ".join(parts).capitalize() + tail
    if code == "tracker_off":
        parts = []
        for inv in r.devices:
            info = v.get("inverters", {}).get(inv, {})
            part = f"{device(inv, plant)}'s tracker off its angle for {info.get('steps_off', 0) * 5} min"
            if info.get("first_off"):
                part += f" since {hhmm(info['first_off'])}"
            if info.get("held_at_deg") is not None:
                part += f", held at {info['held_at_deg']:.0f}°"
            parts.append(part)
        return ("; ".join(parts) + " (wind below the stow limit, so this is not a wind stow)") if parts else None
    if code in ("stow_wind", "high_wind"):
        text = f"Wind up to {v['max_wind_ms']:.1f} m/s at {hhmm(v['max_at'])}"
        if code == "stow_wind":
            text += (f", above the {r.thresholds.get('stow_wind_ms', 0):.0f} m/s stow limit for "
                     f"{v.get('steps_above_stow', 0) * 5} min (trackers stow flat: normal in a storm)")
        else:
            text += f", above the {r.thresholds.get('high_wind_alarm_ms', 0):.0f} m/s warning level"
        return text
    if code == "unexpected_command":
        parts = [f"{_words(c['action']).lower()} on {device(c['device_id'], plant)} at {hhmm(c['ts'])} "
                 f"from '{c['source']}'" for c in v.get("commands", [])]
        return (f"{len(parts)} command(s) from an unexpected source: " + "; ".join(parts)) if parts else None
    if code == "alarms":
        if r.tool == "alarm_summary":
            codes = ", ".join(f"{_words(c).lower()} x{n}" for c, n in list(v.get("by_code", {}).items())[:4])
            return f"{v.get('total', 0)} alarms on {v.get('devices', 0)} devices: {codes}"
        groups = v.get("groups", [])
        parts = []
        for g in groups[:4]:
            where = "the whole plant" if g.get("plant_wide") else devices(g.get("devices", []), plant)
            kind = " (normal protective behaviour)" if g.get("protective") else ""
            parts.append(f"{g.get('title') or _words(g['code'])} on {where}{kind}")
        more = f"; +{len(groups) - 4} more groups" if len(groups) > 4 else ""
        return f"{len(groups)} alarm groups: " + "; ".join(parts) + more if parts else None
    return None


def finding_noun(result: ToolResult) -> str:
    return FINDING_NOUNS.get(result.verdict, _words(result.verdict).lower())


def checks_headline(results: Iterable[ToolResult], named: Iterable[str] = ()) -> str:
    """"Ran 14 checks; 2 found something: a hot inverter and a sun sensor reading low." Alarm tools count as
    checks but are summarised as context, not as findings. `named` = cause labels named this turn."""
    results = list(results)
    findings = [r for r in results if r.finding and r.tool not in ALARM_TOOLS]
    nouns = list(dict.fromkeys(finding_noun(r) for r in findings))
    text = f"Ran {len(results)} check{'s' if len(results) != 1 else ''}"
    if findings:
        text += f"; {len(nouns)} found something: {_join(nouns)}"
    else:
        text += "; nothing unusual found"
    alarms = next((r for r in results if r.tool == "alarm_summary" and r.finding), None)
    if alarms is not None and alarms.values.get("total"):
        text += f"; {alarms.values['total']} alarms so far"
    named = [cause_short(label) for label in named]
    if named:
        text += f". Named: {_join(named)}"
    return text + "."


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


# -- symptoms -----------------------------------------------------------------------------------------------

SYMPTOMS: dict[str, str] = {
    SymptomKind.PLANT_UNDERPERFORMANCE: "Plant output below expected",
    SymptomKind.DEVICE_UNDERPERFORMANCE: "Output below its peers",
    SymptomKind.FLAT_CAP_DEVICE: "Held at a flat limit",
    SymptomKind.FLAT_CAP_PLANT: "Whole plant held at a flat limit",
    SymptomKind.HIGH_TEMPERATURE: "Running hot",
    SymptomKind.DATA_GAP: "Stopped reporting",
    SymptomKind.SENSOR_DEVIATION: "Sun sensor disagrees with the reference",
    SymptomKind.HIGH_WIND: "High wind",
    SymptomKind.TRACKER_ANGLE_FLAT: "Tracker not following the sun",
    SymptomKind.DC_STEP_LOSS: "Sudden drop in panel-side power",
    SymptomKind.UNEXPECTED_COMMAND: "Command from an unexpected source",
    SymptomKind.DEVICE_LOG: "Device log line",
    SymptomKind.ALARM: "Alarm",
}


def symptom(kind: str, device_id: str = "", code: str | None = None, plant=None) -> str:
    """"Running hot: Inverter 3"; an alarm symptom names its code in words."""
    name = SYMPTOMS.get(str(kind), _words(kind))
    if str(kind) == SymptomKind.ALARM and code:
        name = f"Alarm: {_words(code).lower()}"
    where = device(device_id, plant) if device_id else ""
    return f"{name}: {where}" if where else name


def symptom_from_id(symptom_id: str, plant=None) -> str:
    """"HIGH_TEMPERATURE:INV-03" or "ALARM:DERATE_TEMP:INV-03" -> words."""
    parts = symptom_id.split(":")
    if len(parts) == 3:
        return symptom(parts[0], parts[2], parts[1], plant)
    if len(parts) == 2:
        return symptom(parts[0], parts[1], None, plant)
    return _words(symptom_id)


# -- actions and the gate ----------------------------------------------------------------------------------

# (imperative, past tense); {d} = device, {kw} = the limit
ACTIONS: dict[str, tuple[str, str]] = {
    ActionType.CREATE_CLEANING_WORK_ORDER: ("Order cleaning of {d}", "Ordered cleaning of {d}"),
    ActionType.FLAG_SENSOR_USE_REFERENCE: ("Stop trusting the sun sensor; use the reference",
                                           "Stopped trusting the sun sensor; now using the reference"),
    ActionType.RESET_COMMS_GATEWAY: ("Reset {d}'s communications", "Reset {d}'s communications"),
    ActionType.SET_POWER_LIMIT: ("Lower {d}'s output limit to {kw}", "Lowered {d}'s output limit to {kw}"),
    ActionType.DISPATCH_TECHNICIAN: ("Send a technician to {d}", "Sent a technician to {d}"),
    ActionType.RESUME_TRACKING: ("Restart the tracker of {d}", "Restarted the tracker of {d}"),
    ActionType.ISOLATE_DEVICE: ("Isolate {d}", "Isolated {d}"),
}
ACTION_SHORT: dict[str, str] = {  # the action alone, for table cells and badges
    ActionType.CREATE_CLEANING_WORK_ORDER: "Order cleaning",
    ActionType.FLAG_SENSOR_USE_REFERENCE: "Stop trusting the sun sensor; use the reference",
    ActionType.RESET_COMMS_GATEWAY: "Reset the inverter's communications",
    ActionType.SET_POWER_LIMIT: "Lower the inverter's output limit",
    ActionType.DISPATCH_TECHNICIAN: "Send a technician",
    ActionType.RESUME_TRACKING: "Restart the tracker",
    ActionType.ISOLATE_DEVICE: "Isolate the device",
}


def action(action_type: str | ActionType) -> str:
    key = str(getattr(action_type, "value", action_type))
    return ACTION_SHORT.get(key, _words(key))


def action_text(action_type: str | ActionType, target: str = "", params: Mapping[str, Any] | None = None,
                past: bool = False, plant=None) -> str:
    """The action on its target as one sentence, imperative or past tense. A power limit of None reads as
    removing the limit."""
    key = str(getattr(action_type, "value", action_type))
    params = params or {}
    d = device(target, plant) if target else "the device"
    if key == ActionType.SET_POWER_LIMIT and params.get("kw") is None:
        return (f"Remove {d}'s output limit" if not past else f"Removed {d}'s output limit")
    template = ACTIONS.get(key)
    if template is None:
        verb = _words(key)
        return f"{verb} on {d}" if target else verb
    text = template[1 if past else 0]
    return text.format(d=d, kw=kw(params.get("kw")) if "kw" in params else "a lower level")


GATES: dict[str, str] = {
    "auto": "Safe to do automatically",
    "needs_approval": "Needs your approval",
    "rejected": "Not allowed",
    # the Trace tab's older gate words
    "auto (allowlisted)": "Safe to do automatically",
    "needs approval": "Needs your approval",
}


def gate(decision: str) -> str:
    key = str(getattr(decision, "value", decision))
    if key in GATES:
        return GATES[key]
    if key.startswith("held back"):
        return "Not allowed" + key[len("held back"):]
    return _words(key)


DECIDED_BY: dict[str, str] = {"auto": "automatically", "operator": "by you", "console": "by the console",
                              "human:simulated": "by the simulated operator", "": ""}


def decided_by(who: str) -> str:
    return DECIDED_BY.get(who, f"by {who}")


STATUS_WORDS: dict[str, str] = {  # action status
    "APPLIED": "done", "REJECTED": "refused", "FAILED": "failed", "waiting": "waiting for your decision",
}


def action_status(status: Any) -> str:
    key = str(getattr(status, "value", status)) if status is not None else "waiting"
    return STATUS_WORDS.get(key, key.lower())


# -- cause status and origin -------------------------------------------------------------------------------

CAUSE_STATUS: dict[str, str] = {
    "open": "Found, fix not started", "acting": "Fix under way", "resolved": "Fixed and verified",
    "escalated": "Handed to a human", "expected": "Expected behaviour, no action", "flagged": "Flagged for security review",
}


def cause_status(status: str) -> str:
    return CAUSE_STATUS.get(status, _words(status))


def origin(label: str, proposals: Iterable[Any] = ()) -> str:
    """How a cause came to be named: "Found by the rules", or "Proposed by the model, confirmed by N checks" /
    "Proposed by the model, rejected: <reason>" when a ProposalRecord with that label exists. The score is
    never part of it."""
    mine = [p for p in proposals if getattr(p, "label", None) == label]
    if not mine:
        return "Found by the rules"
    p = mine[-1]
    if p.decision == "accepted":
        n = len(p.checks)
        return f"Proposed by the model, confirmed by {n} check{'s' if n != 1 else ''}"
    if p.decision == "dropped":
        return "Proposed by the model, dropped later: " + (p.reason or "its re-test failed")
    return "Proposed by the model, rejected: " + (p.reason or "the agent's test failed")


MODEL_PURPOSES: dict[str, str] = {
    "hypotheses": "asked the model for possible causes",
    "explain": "asked the model to explain the diagnosis",
    "report": "asked the model to write the shift report",
    "plan_order": "asked the model to order the plan",
    "notice": "asked the model to read a site notice",
    "ask_raqeeb": "asked the model to word an answer",
}


def model_purpose(purpose: str) -> str:
    return MODEL_PURPOSES.get(purpose, f"asked the model: {_words(purpose).lower()}")


# -- timeline sentences ------------------------------------------------------------------------------------


def found(label: str, proposals: Iterable[Any] = (), plant=None) -> str:
    """"Found dust on the panels (all panels), by the rules." or "The model proposed a transformer trip (...);
    the agent's checks confirmed it." """
    how = origin(label, proposals)
    if how.startswith("Proposed by the model, confirmed"):
        return f"The model proposed {cause_short(label)} ({device(_target(label), plant)}); the agent's checks confirmed it."
    return f"Found {cause_short(label)} ({device(_target(label), plant)}), by the rules."


def _target(label: str) -> str:
    try:
        return parse_cause_label(label)[1]
    except ValueError:
        return ""


def acted(action_type, target: str, params: Mapping[str, Any] | None = None, gated: bool = False, plant=None) -> str:
    text = action_text(action_type, target, params, past=True, plant=plant)
    return text + (" (after your approval)." if gated else " automatically.")


def approval_needed(action_type, target: str, params: Mapping[str, Any] | None = None, plant=None) -> str:
    text = action_text(action_type, target, params, plant=plant)
    return f"Needs your approval: {text[:1].lower()}{text[1:]}."


def decided(action_type, target: str, approved: bool, who: str = "operator", note: str = "",
            params: Mapping[str, Any] | None = None, plant=None) -> str:
    text = action_text(action_type, target, params, plant=plant)
    verb = "approved" if approved else "refused"
    actor = {"operator": "You", "human:simulated": "The simulated operator", "auto": "The console"}.get(who, who.capitalize())
    sentence = f"{actor} {verb}: {text[:1].lower()}{text[1:]}"
    return sentence + (f" ({note})." if note else ".")


def verified(action_type, target: str, recovered: bool, before_kw: float, after_kw: float,
             expected_kw: float | None = None, plant=None) -> str:
    what = action_text(action_type, target, past=True, plant=plant)
    change = power_change(before_kw, after_kw, expected_kw)
    if recovered:
        return f"Verified: {what[:1].lower()}{what[1:]} and output recovered ({change})."
    return f"Not recovered yet after: {what[:1].lower()}{what[1:]} ({change})."


def escalated(subject: str, device_ids: Iterable[str] = (), reason: str = "", plant=None) -> str:
    """Handed to a human. `reason` is the agent's text (code-built); the page escapes it anyway."""
    ids = list(device_ids)
    if subject == "unexplained":
        where = devices(ids, plant) if ids else "the plant"
        head = f"Handed to a human: an unexplained loss on {where}"
    else:
        head = f"Handed to a human: {cause_short(subject)} ({device(_target(subject), plant)})"
    return head + (f": {reason}." if reason else ".")


def proposal(label: str, decision: str, reason: str = "", n_checks: int = 0, plant=None) -> str:
    what = f"{cause_short(label)} ({device(_target(label), plant)})"
    if decision == "accepted":
        return f"The model proposed {what}; confirmed by {n_checks} check{'s' if n_checks != 1 else ''}."
    if decision == "dropped":
        return f"The model's proposal {what} was dropped when its re-test failed; nothing had been done about it."
    return f"The model proposed {what}; rejected" + (f": {reason}" if reason else "") + ". Nothing changed."


def outcome(action_type, target: str, status: Any, note: str = "", plant=None) -> str:
    text = action_text(action_type, target, plant=plant)
    word = action_status(status)
    return f"The plant reports: {text[:1].lower()}{text[1:]} {word}" + (f" ({note})" if note else "") + "."
