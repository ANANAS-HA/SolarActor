"""Alarm and command tools. Numbers and lookups only; reads only the telemetry store.

Alarm text and related faults come from configs/alarm_codes.yaml (synthetic, vendor-style).
A storm raises many alarms; grouping them by code shows which are plant-wide (weather,
soiling, curtailment) and which sit on one device.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from raqeeb.config import load_alarm_codes
from raqeeb.contracts import ActionType, PlantConfig
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.tools.weather_tools import get_plant

PLANT_WIDE_SHARE = 0.75  # an alarm on at least this share of inverters is plant-wide
EXPECTED_SOURCES = frozenset({"raqeeb", "operator", "scada"})


def alarm_manual(code: str) -> dict[str, Any]:
    """Title, severity, related faults and manual text for an alarm code."""
    spec = load_alarm_codes()[code]
    return {"code": code, "vendor_id": spec.vendor_id, "severity": spec.severity.value, "title": spec.title,
            "related_faults": [f.value for f in spec.related_faults], "manual": spec.manual}


def events_frame(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Events with title and related faults from the alarm catalogue."""
    events = store.events(start, end)
    codes = load_alarm_codes()
    events["title"] = [codes[c].title if c in codes else None for c in events["code"]]
    events["related_faults"] = [[f.value for f in codes[c].related_faults] if c in codes else [] for c in events["code"]]
    return events


def alarm_summary(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    events = store.events(start, end)
    if events.empty:
        return {"total": 0, "by_code": {}, "by_severity": {}, "devices": 0, "first": None, "last": None}
    return {
        "total": len(events),
        "by_code": events["code"].value_counts().to_dict(),
        "by_severity": events["severity"].value_counts().to_dict(),
        "devices": int(events["device_id"].nunique()),
        "first": events.index[0].to_pydatetime(),
        "last": events.index[-1].to_pydatetime(),
    }


def alarm_groups(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                 plant: PlantConfig | None = None) -> list[dict[str, Any]]:
    """One group per alarm code: devices, timing, catalogue info, plant-wide or not."""
    plant = get_plant(plant)
    events = store.events(start, end)
    codes = load_alarm_codes()
    inverters = set(plant.inverters.ids)
    out = []
    for code, group in events.groupby("code", sort=False):
        devices = sorted(group["device_id"].unique())
        n_inv = len(inverters.intersection(devices))
        spec = codes.get(code)
        out.append({
            "code": code,
            "title": spec.title if spec else None,
            "severity": group["severity"].iloc[0],
            "related_faults": [f.value for f in spec.related_faults] if spec else [],
            "count": len(group),
            "devices": devices,
            "inverters": n_inv,
            "plant_wide": n_inv >= PLANT_WIDE_SHARE * len(inverters),
            "first": group.index[0].to_pydatetime(),
            "last": group.index[-1].to_pydatetime(),
        })
    return sorted(out, key=lambda g: g["first"])


def unexpected_commands(store: TelemetryStore, start: datetime | None = None, end: datetime | None = None,
                        expected_sources: frozenset[str] = EXPECTED_SOURCES) -> list[dict[str, Any]]:
    """Commands from a source outside `expected_sources` or with an action outside the allowlist."""
    commands = store.commands(start, end)
    allowed = {a.value for a in ActionType}
    out = []
    for ts, row in commands.iterrows():
        reasons = []
        if row["source"] not in expected_sources:
            reasons.append(f"unexpected source {row['source']!r}")
        if row["action"] not in allowed:
            reasons.append(f"action {row['action']!r} not in allowlist")
        if reasons:
            out.append({"ts": ts.to_pydatetime(), "source": row["source"], "device_id": row["device_id"],
                        "action": row["action"], "params": row["params"], "reasons": reasons})
    return out
