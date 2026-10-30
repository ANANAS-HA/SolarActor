"""Alarm and command tools. Numbers and lookups only; reads only the telemetry store.

Alarm text and related faults come from configs/alarm_codes.yaml (synthetic, vendor-style).
A storm raises many alarms; grouping them by code shows which are plant-wide (weather,
soiling, curtailment) and which sit on one device. Alarm severity is reported as
`alarm_severity` (a bare "severity" key is reserved for planted faults; see tests/leakcheck.py).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from raqeeb.config import load_alarm_codes
from raqeeb.contracts import VERDICT_OK, ActionType, PlantConfig, ToolResult
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.common import get_plant, local_time, window

PLANT_WIDE_SHARE = 0.75  # an alarm on at least this share of inverters is plant-wide
EXPECTED_SOURCES = frozenset({"raqeeb", "operator", "scada"})


def alarm_manual(code: str) -> ToolResult:
    """Title, alarm severity, related faults and manual text for an alarm code."""
    spec = load_alarm_codes()[code]
    return ToolResult(tool="alarm_manual", verdict=VERDICT_OK,
                      values={"code": code, "vendor_id": spec.vendor_id, "alarm_severity": spec.severity.value,
                              "title": spec.title, "related_faults": [f.value for f in spec.related_faults]},
                      evidence=f"[{spec.vendor_id}] {spec.title}: {spec.manual}")


def events_frame(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Events with title and related faults from the alarm catalogue."""
    events = store.events(start, end)
    codes = load_alarm_codes()
    events["title"] = [codes[c].title if c in codes else None for c in events["code"]]
    events["related_faults"] = [[f.value for f in codes[c].related_faults] if c in codes else [] for c in events["code"]]
    return events


def alarm_summary(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> ToolResult:
    """Counts by code and severity. Verdicts: "alarms" (devices = devices with alarms), ok."""
    start, end = window(store, start, end)
    events = store.events(start, end)
    if events.empty:
        return ToolResult(tool="alarm_summary", verdict=VERDICT_OK, start=start, end=end,
                          values={"total": 0, "by_code": {}, "by_alarm_severity": {}, "devices": 0},
                          evidence="no alarms in the window")
    by_code = events["code"].value_counts().to_dict()
    values = {
        "total": len(events),
        "by_code": by_code,
        "by_alarm_severity": events["severity"].value_counts().to_dict(),
        "devices": int(events["device_id"].nunique()),
        "first": events.index[0].to_pydatetime(),
        "last": events.index[-1].to_pydatetime(),
    }
    evidence = (f"{len(events)} alarms on {values['devices']} devices: "
                + ", ".join(f"{code} x{n}" for code, n in by_code.items()))
    return ToolResult(tool="alarm_summary", verdict="alarms", devices=sorted(events["device_id"].unique()),
                      start=start, end=end, values=values, evidence=evidence)


def alarm_groups(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> ToolResult:
    """One group per alarm code: devices, timing, catalogue info, plant-wide or not.

    Verdicts: "alarms" (devices = devices with alarms), ok. values["groups"] lists the groups.
    """
    plant = get_plant(plant)
    start, end = window(store, start, end)
    events = store.events(start, end)
    codes = load_alarm_codes()
    inverters = set(plant.inverters.ids)
    groups = []
    for code, group in events.groupby("code", sort=False):
        devices = sorted(group["device_id"].unique())
        n_inv = len(inverters.intersection(devices))
        spec = codes.get(code)
        groups.append({
            "code": code,
            "title": spec.title if spec else None,
            "alarm_severity": group["severity"].iloc[0],
            "related_faults": [f.value for f in spec.related_faults] if spec else [],
            "protective": bool(spec and spec.protective),
            "count": len(group),
            "devices": devices,
            "inverters": n_inv,
            "plant_wide": n_inv >= PLANT_WIDE_SHARE * len(inverters),
            "first": group.index[0].to_pydatetime(),
            "last": group.index[-1].to_pydatetime(),
        })
    groups.sort(key=lambda g: g["first"])

    def describe(g):
        where = f"{g['inverters']} inverters (plant-wide)" if g["plant_wide"] else ", ".join(g["devices"])
        normal = (", normal or protective behaviour" if g["protective"]
                  else "" if g["related_faults"] else ", no known fault linked to this code")
        return f"{g['code']} on {where} from {local_time(g['first'])}{normal}"

    return ToolResult(tool="alarm_groups", verdict="alarms" if groups else VERDICT_OK,
                      devices=sorted({d for g in groups for d in g["devices"]}), start=start, end=end,
                      values={"groups": groups}, thresholds={"plant_wide_share": PLANT_WIDE_SHARE},
                      evidence="; ".join(describe(g) for g in groups) if groups else "no alarms in the window")


def unexpected_commands(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                        expected_sources: frozenset[str] = EXPECTED_SOURCES) -> ToolResult:
    """Commands from a source outside `expected_sources` or with an action outside the allowlist.

    Verdicts: "unexpected_command" (devices = commanded devices), ok.
    """
    start, end = window(store, start, end)
    commands = store.commands(start, end)
    allowed = {a.value for a in ActionType}
    found = []
    for ts, row in commands.iterrows():
        reasons = []
        if row["source"] not in expected_sources:
            reasons.append(f"unexpected source {row['source']!r}")
        if row["action"] not in allowed:
            reasons.append(f"action {row['action']!r} not in allowlist")
        if reasons:
            found.append({"ts": ts.to_pydatetime(), "source": row["source"], "device_id": row["device_id"],
                          "action": row["action"], "params": row["params"], "reasons": reasons})
    evidence = "; ".join(f"{c['action']} on {c['device_id']} at {local_time(c['ts'])}: {', '.join(c['reasons'])}"
                         for c in found) if found else f"all {len(commands)} commands from expected sources"
    return ToolResult(tool="unexpected_commands", verdict="unexpected_command" if found else VERDICT_OK,
                      devices=sorted({c["device_id"] for c in found}), start=start, end=end,
                      values={"commands": found, "expected_sources": sorted(expected_sources)}, evidence=evidence)
