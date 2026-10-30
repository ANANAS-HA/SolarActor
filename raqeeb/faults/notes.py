"""Technician notes (SYNTHETIC): free text from site staff, written into the technician_notes table.

Notes arrive over time. Relevant notes come from unknown faults (raqeeb.faults.unknown) and are
indirect: they say what staff saw, heard or did ("group B lost power around 11:20, burning smell
near the T2 enclosure"), never what the fault is. Every scenario also gets background notes that are
irrelevant or misleading (a smell that turns out to be a farm fire, a noisy fan on a healthy unit),
about devices and areas with no planted fault. The agent's rules never read note text; only the
LLM router sees it, as untrusted data. Times in the text are Riyadh local, as staff write them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from raqeeb.contracts import DISPLAY_TZ, PlantConfig, Scenario, TechnicianNote

AUTHORS = ("Khalid (O&M)", "Faisal (O&M)", "Noura (shift lead)", "Saad (security)", "Reem (HSE)", "Omar (contractor liaison)")
GRID_OPERATOR = "grid_operator"  # author of grid-operator notices (curtailment variants "notice" / "fake_notice")
BACKGROUND_PER_SCENARIO = (1, 3)  # inclusive range
_TZ = ZoneInfo(DISPLAY_TZ)


def local_hhmm(ts: datetime) -> str:
    return f"{ts.astimezone(_TZ):%H:%M}"


def note(ts: datetime, area: str, rng: np.random.Generator, text: str) -> TechnicianNote:
    return TechnicianNote(ts=ts, area_or_device=area, author=str(rng.choice(AUTHORS)), text=text)


# Background notes: {inv} = an inverter, {group} = a group letter, {t} = a transformer, {side} = a fence side,
# {hhmm} = a local time. Some are noise, some point at a cause that is not there.
BACKGROUND = (
    ("{inv}", "Morning round: checked {inv} DC connectors and combiner box, all tight, nothing found."),
    ("{inv}", "{inv} cooling fan sounds louder than its neighbours, may be nothing. Will listen again tomorrow."),
    ("{side} fence", "Grass and shrub cutting crew working along the {side} fence until noon."),
    ("site", "Smoke seen beyond the {side} boundary around {hhmm}; turned out to be a farm burning brush, not ours."),
    ("{t}", "Fence gate next to the {t} enclosure was found open on the night round and closed. Nothing missing."),
    ("control room", "Visitor group toured the control room in the morning. No changes made to any setting."),
    ("group {group}", "Drone survey flew over group {group} for the insurer. No hot spots reported."),
    ("site", "Water truck refilled; panel washing crew on standby if needed."),
    ("{inv}", "Replaced the door seal on the {inv} cabinet (cosmetic)."),
    ("site", "Contractor van parked near the substation road for a fibre survey, they did not open any cabinet."),
)
SIDES = ("north", "south", "east", "west")


def background_notes(scenario: Scenario, plant: PlantConfig, rng: np.random.Generator) -> list[TechnicianNote]:
    """1-3 irrelevant or misleading notes about devices and areas with no planted fault."""
    busy = {f.target for f in scenario.faults}
    for f in scenario.faults:
        busy |= set(plant.layout.area_of(f.target))
    inverters = [i for i in plant.inverters.ids if i not in busy] or list(plant.inverters.ids)
    transformers = [t for t, invs in plant.layout.transformers.items() if t not in busy and not set(invs) & busy] \
        or list(plant.layout.transformers)
    n = int(rng.integers(BACKGROUND_PER_SCENARIO[0], BACKGROUND_PER_SCENARIO[1] + 1))
    picks = rng.choice(len(BACKGROUND), size=n, replace=False)
    out = []
    for k in sorted(picks):
        area, text = BACKGROUND[k]
        t = str(rng.choice(transformers))
        names = {"inv": str(rng.choice(inverters)), "t": t, "group": plant.layout.groups.get(t, "A"),
                 "side": str(rng.choice(SIDES))}
        ts = scenario.start + timedelta(minutes=int(rng.integers(20, int(scenario.duration_hours * 60) - 60)))
        names["hhmm"] = local_hhmm(ts - timedelta(minutes=int(rng.integers(10, 40))))
        out.append(note(ts, area.format(**names), rng, text.format(**names)))
    return out
