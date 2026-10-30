"""Investigate: run every agent tool on the incident window and turn findings into Symptoms.

Tools compute all numbers; this module only maps their verdicts to SymptomKinds. A symptom id is
"KIND:device", so the same symptom keeps its id (and first start) from poll to poll.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from raqeeb.agent.state import Memory, PollState
from raqeeb.agent.triage import LOG_CODE, UNRECOGNISED
from raqeeb.contracts import PLANT_TARGET, Symptom, SymptomKind, ToolResult
from raqeeb.tools import AGENT_TOOLS

PLANT_WIDE_SHARE = 0.75  # inverters_low on at least this share of the fleet is a plant-wide loss
RECENT = timedelta(hours=2)  # plant_performance also runs on this recent window: a whole-day ratio reacts slowly


def symptom_id(kind: SymptomKind, device: str, code: str | None = None) -> str:
    return f"{kind.value}:{code}:{device}" if code else f"{kind.value}:{device}"


def _ts(value: Any, default: datetime) -> datetime:
    return value if isinstance(value, datetime) else default


def _make(kind: SymptomKind, device: str, start: datetime, evidence: dict, code: str | None = None) -> Symptom:
    return Symptom(id=symptom_id(kind, device, code), kind=kind, device_id=device, start=start, evidence=evidence)


def symptoms_from(results: dict[str, ToolResult], alarm_groups: list[dict], memory: Memory,
                  now: datetime) -> list[Symptom]:
    plant = memory.plant
    inverters = set(plant.inverters.ids)
    station = plant.weather_station_id
    out: list[Symptom] = []

    def found(name: str) -> ToolResult | None:
        r = results.get(name)
        return r if r is not None and r.finding else None

    for name in ("plant_performance", "plant_performance_recent"):
        if r := found(name):
            v = r.values
            out.append(_make(SymptomKind.PLANT_UNDERPERFORMANCE, PLANT_TARGET, _ts(v.get("onset"), now), {
                "tool": name, "vs_reference": v["vs_reference"]["ratio"],
                "vs_sensor": v.get("vs_sensor", {}).get("ratio"),
                "shortfall_kwh": v["vs_reference"].get("shortfall_kwh"), "summary": r.evidence}))
            break  # one plant symptom: the whole window if it shows the loss, else the recent window

    if r := found("inverter_performance"):
        low = [d for d in r.devices if d in inverters]
        if len(low) < PLANT_WIDE_SHARE * len(inverters):  # plant-wide lows are the plant symptom above
            for device in low:
                ratio = r.values["inverters"][device]["ratio"]
                out.append(_make(SymptomKind.DEVICE_UNDERPERFORMANCE, device, now,
                                 {"tool": r.tool, "ratio_vs_reference": ratio, "summary": f"{device} at {ratio:.0%} of expected"}))

    if r := found("peer_outliers"):
        for device in r.devices:
            info = r.values["inverters"][device]
            out.append(_make(SymptomKind.DEVICE_UNDERPERFORMANCE, device, _ts(info.get("onset"), now), {
                "tool": r.tool, "ratio_to_peers": info.get("ratio_since_onset"),
                "summary": f"{device} at {info.get('ratio_since_onset') or 0:.0%} of the peer median"}))

    if r := found("flat_caps"):
        for cap in r.values["caps"]:
            out.append(_make(SymptomKind.FLAT_CAP_DEVICE, cap["inverter_id"], cap["start"], {
                "tool": r.tool, "level_kw": cap["level_kw"], "peer_median_kw": cap["peer_median_kw"],
                "ratio_to_peers": cap["ratio_to_peers"], "end": cap["end"],
                "summary": f"{cap['inverter_id']} flat at {cap['level_kw']:.0f} kW ({cap['ratio_to_peers']:.0%} of peers)"}))

    if r := found("temperature_check"):
        for device in r.devices:
            info = r.values["inverters"][device]
            out.append(_make(SymptomKind.HIGH_TEMPERATURE, device, _ts(info.get("first_hot"), now), {
                "tool": r.tool, "max_temp_c": info.get("max_temp_c"), "max_excess_c": info.get("max_excess_c"),
                "summary": f"{device} up to {info.get('max_temp_c', 0):.0f} C"}))

    if r := found("plant_flat_cap"):
        for p in r.values["periods"]:
            grid = r.verdict == "plant_flat_cap_grid_limit"
            out.append(_make(SymptomKind.FLAT_CAP_PLANT, PLANT_TARGET, p["start"], {
                "tool": r.tool, "grid_limit": grid, "level_kw": p["level_kw"],
                "summary": f"all inverters flat at {p['level_kw']:.0f} kW" + (" under a grid limit" if grid else "")}))
            break  # one plant-wide cap symptom; its evidence is the first period
    if r := found("curtailment_periods"):
        first = r.values["periods"][0]
        out.append(_make(SymptomKind.FLAT_CAP_PLANT, PLANT_TARGET, first["start"], {
            "tool": r.tool, "grid_limit": True, "limit_kw": first["limit_kw"],
            "meter_at_limit": first["meter_at_limit"], "summary": r.evidence}))

    if r := found("data_gaps"):
        for gap in r.values["gaps"]:
            out.append(_make(SymptomKind.DATA_GAP, gap["device_id"], gap["start"], {
                "tool": r.tool, "ongoing": gap["ongoing"], "still_producing": gap.get("still_producing"),
                "end": gap["end"], "steps": gap["steps"],
                "summary": f"{gap['device_id']} silent for {gap['steps'] * 5} min" + (" (ongoing)" if gap["ongoing"] else "")}))

    if r := found("sensor_check"):
        v = r.values
        out.append(_make(SymptomKind.SENSOR_DEVIATION, station, _ts(v.get("onset"), now), {
            "tool": r.tool, "sensor_vs_reference": v["sensor_vs_reference"],
            "panels_share_loss": v["panels_share_loss"], "flagged": v["flagged"], "summary": r.evidence}))

    if r := found("wind_summary"):
        v = r.values
        out.append(_make(SymptomKind.HIGH_WIND, station, _ts(v.get("first_above_stow") or v.get("first_above_high_wind_alarm"), now), {
            "tool": r.tool, "max_wind_ms": v.get("max_wind_ms"), "stow": r.verdict == "stow_wind",
            "last_above_stow": v.get("last_above_stow"), "summary": r.evidence}))

    if r := found("tracker_check"):
        for device in r.devices:
            info = r.values["inverters"][device]
            out.append(_make(SymptomKind.TRACKER_ANGLE_FLAT, device, _ts(info.get("first_off"), now), {
                "tool": r.tool, "held_at_deg": info.get("held_at_deg"), "steps_off": info.get("steps_off"),
                "summary": f"{device} off its tracking angle"}))

    if r := found("unexpected_commands"):
        for c in r.values["commands"]:
            out.append(_make(SymptomKind.UNEXPECTED_COMMAND, c["device_id"], c["ts"], {
                "tool": r.tool, "reasons": c["reasons"], "summary": f"unexpected command on {c['device_id']}"}))

    # A device log line: only its code, device and time are used; the free text is untrusted and never read
    for g in alarm_groups:
        if g["code"] == LOG_CODE:
            out += [_make(SymptomKind.DEVICE_LOG, device, g["first"], {
                "code": g["code"], "count": g["count"], "summary": f"log line from {device}"}) for device in g["devices"]]

    # Alarms with a possible fault behind them; protective ones (wind stow) are weather, not faults
    for g in alarm_groups:
        if g["code"] == UNRECOGNISED or g["protective"]:
            continue
        devices = [PLANT_TARGET] if g["plant_wide"] else g["devices"]
        for device in devices:
            out.append(_make(SymptomKind.ALARM, device, g["first"], {
                "code": g["code"], "count": g["count"], "plant_wide": g["plant_wide"],
                "summary": f"{g['code']} on {'the whole plant' if g['plant_wide'] else device}"}, code=g["code"]))
    return out


def investigate(state: PollState) -> PollState:
    memory, now = state["memory"], state["now"]
    results = {name: tool(memory.telemetry, memory.view.start, now) for name, tool in AGENT_TOOLS.items()}
    recent_start = max(memory.view.start, now - RECENT)
    results["plant_performance_recent"] = AGENT_TOOLS["plant_performance"](memory.telemetry, recent_start, now)
    memory.results = results
    current = {s.id: s for s in symptoms_from(results, memory.alarm_groups, memory, now)}
    new = [sid for sid in current if sid not in memory.symptoms]
    for sid, symptom in current.items():
        old = memory.symptoms.get(sid)
        # keep the first start; refresh the evidence
        memory.symptoms[sid] = symptom if old is None else symptom.model_copy(update={"start": min(old.start, symptom.start)})
    memory.active = set(current)
    if new:
        memory.log(now, "investigate", f"{len(new)} new symptoms: " + "; ".join(
            current[s].evidence.get("summary", s) for s in new))
    return {}
