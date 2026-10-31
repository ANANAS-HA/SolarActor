"""Unknown faults (SYNTHETIC): fault types deliberately absent from the agent's causal graph and planner.

The agent can learn about them only from alarm manual text (configs/alarm_codes.yaml) and from
technician notes, which these faults post as they develop (indirect: what staff saw, never the
fault's name). Rules-only Raqeeb should report their symptoms as unexplained and escalate.

- TRANSFORMER_TRIP (target T1..T4): the transformer's 4 inverters lose the grid at `start`, drop
  to zero together with a FAULT status and no alarm of their own; the plant controller raises
  MV_PROTECTION_OP. No allowed action fixes it (a high-voltage crew must).
- NEW_SHADING (target EDGE-W / EDGE-E): from `start`, new construction shades the inverters along
  one fence when the sun is low on that side (afternoon for west, morning for east). Up to
  `severity` of their irradiance is lost. No allowed action fixes it.
- FIRMWARE_ALARM (target an inverter): from `start`, after a controller update, the inverter is held
  at (1 - severity) x rating with normal temperature and raises CTRL_PARAM_LIMIT every hour.
  A technician reloads the parameters (dispatch_technician fixes it).

Variant "quiet" (spec.variant): no telltale alarm code (no MV_PROTECTION_OP, no CTRL_PARAM_LIMIT), so the
numbers alone fit more than one cause; the deciding note is always posted, plus one misleading note that
points at a plausible other cause (a comms reboot, a noisy fan, dust after grass cutting).
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from raqeeb.contracts import FaultType, PlantConfig, PlantState
from raqeeb.faults.base import Fault, InjectionContext, step_times
from raqeeb.faults.notes import local_hhmm, note

TRIP_CODE = "MV_PROTECTION_OP"
FIRMWARE_CODE = "CTRL_PARAM_LIMIT"
FIRMWARE_REPEAT = timedelta(hours=1)
DEFAULT_SHADING = 0.40
SHADE_FULL_ZENITH = 80.0  # shading reaches `severity` with the sun this low on the shaded side
SHADE_START_ZENITH = 55.0  # and starts once the sun is lower than this
DEFAULT_FIRMWARE_DERATE = 0.20


class TransformerTrip(Fault):
    fault_type = FaultType.TRANSFORMER_TRIP

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.layout.transformers)

    def arm(self, ctx: InjectionContext) -> None:
        target, layout = self.spec.target, ctx.plant.layout
        inverters, group = layout.transformers[target], layout.groups.get(target, "?")

        quiet = self.spec.variant == "quiet"

        def trip(state: PlantState) -> None:
            for inv_id in inverters:
                state.inverters[inv_id].grid_lost = True
            if not quiet:
                ctx.twin.emit_event(ctx.plant.plant_controller_id, TRIP_CODE, "feeder protection relay operated")

        def restore(state: PlantState) -> None:
            for inv_id in inverters:
                state.inverters[inv_id].grid_lost = False

        self.schedule(ctx, self.start, "trips", trip)
        if self.end is not None:
            self.schedule(ctx, self.end, "restored", restore)
        at = local_hhmm(self.start)
        texts = [
            (f"group {group}", f"Group {group} lost power around {at}, burning smell near the {target} enclosure."),
            (target, f"Heard a loud bang from the {target} side at about {at}. All the inverter screens in "
                     f"group {group} are dark; the other rows look normal."),
            (f"group {group}", f"Walked group {group}: no inverter shows an error on its own panel, they just say "
                               "waiting for grid. Smell of hot oil near the enclosure, kept people away."),
        ]
        picks = sorted(ctx.rng.choice(len(texts), size=2, replace=False))
        for k, delay in zip(picks, (int(ctx.rng.integers(15, 40)), int(ctx.rng.integers(50, 110)))):
            area, text = texts[k]
            self.notes.append(note(self.start + timedelta(minutes=delay), area, ctx.rng, text))
        if quiet:  # misleading: points at a comms fault in the same group
            self.notes.append(note(self.start + timedelta(minutes=int(ctx.rng.integers(5, 25))), f"group {group}", ctx.rng,
                                   f"IT rebooted the network switch in the group {group} comms cabinet this "
                                   "morning; some screens there may show stale values."))


class NewShading(Fault):
    fault_type = FaultType.NEW_SHADING

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.layout.edges)

    def shading(self, weather: pd.DataFrame) -> pd.Series:
        """Fraction of irradiance lost on the shaded edge at each step (sun low on that side)."""
        west = self.spec.target.endswith("W")
        on_side = weather["azimuth"] > 180.0 if west else weather["azimuth"] < 180.0
        low = ((weather["zenith"] - SHADE_START_ZENITH) / (SHADE_FULL_ZENITH - SHADE_START_ZENITH)).clip(0.0, 1.0)
        return (self.severity_or(DEFAULT_SHADING) * low).where(on_side, 0.0)

    def arm(self, ctx: InjectionContext) -> None:
        inverters = ctx.plant.layout.edges[self.spec.target]
        loss = self.shading(ctx.weather)
        end = self.end or ctx.scenario.end
        for ts in step_times(self.start, end):
            value = float(loss.get(pd.Timestamp(ts), 0.0))

            def shade(state: PlantState, value: float = value) -> None:
                for inv_id in inverters:
                    state.inverters[inv_id].shading_loss = value

            self.schedule(ctx, ts, "shade", shade)
        if self.end is not None:
            self.schedule(ctx, self.end, "removed",
                          lambda s: [setattr(s.inverters[i], "shading_loss", 0.0) for i in inverters])
        side = "west" if self.spec.target.endswith("W") else "east"
        when = "late afternoon" if side == "west" else "early morning"
        texts = [
            (f"{side} fence", f"Contractor started putting up a steel frame for the new warehouse just past the "
                              f"{side} fence this morning, crane on site."),
            (f"{side} row", f"The panels along the {side} fence look darker than the rest in the {when}, "
                            "strings on that row tested fine yesterday."),
            (f"{side} fence", f"Neighbour's building site past the {side} fence is now two storeys high."),
        ]
        picks = sorted(ctx.rng.choice(len(texts), size=2, replace=False))
        for k, delay in zip(picks, (int(ctx.rng.integers(20, 60)), int(ctx.rng.integers(120, 300)))):
            area, text = texts[k]
            self.notes.append(note(self.start + timedelta(minutes=delay), area, ctx.rng, text))
        if self.spec.variant == "quiet":  # misleading: points at dust on that row
            self.notes.append(note(self.start + timedelta(minutes=int(ctx.rng.integers(60, 200))), f"{side} row",
                                   ctx.rng, f"Grass cutting along the {side} fence threw up a lot of dust onto the "
                                            f"{side} row this morning."))


class FirmwareAlarm(Fault):
    fault_type = FaultType.FIRMWARE_ALARM

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.inverters.ids)

    def arm(self, ctx: InjectionContext) -> None:
        target = self.spec.target
        cap = ctx.plant.inverters.ac_kw * (1.0 - self.severity_or(DEFAULT_FIRMWARE_DERATE))

        def begin(state: PlantState) -> None:
            state.inverters[target].firmware_cap_kw = cap

        quiet = self.spec.variant == "quiet"

        def repeat(state: PlantState) -> None:
            if state.inverters[target].firmware_cap_kw is not None and not quiet:  # until the parameters are reloaded
                ctx.twin.emit_event(target, FIRMWARE_CODE, "active power held by control parameter")

        self.schedule(ctx, self.start, "begins", begin)
        end = self.end or ctx.scenario.end
        ts = self.start
        while ts < end:
            self.schedule(ctx, ts, "alarm", repeat)
            ts += FIRMWARE_REPEAT
        if self.end is not None:
            self.schedule(ctx, self.end, "ends", lambda s: setattr(s.inverters[target], "firmware_cap_kw", None))
        before = self.start - timedelta(minutes=int(ctx.rng.integers(30, 120)))
        texts = [
            (target, f"Vendor remote session on {target} from about {local_hhmm(before)}, they said they were "
                     "loading the new controller package. No site work needed."),
            (target, f"{target} display shows a parameter warning I have not seen before. Unit is running, "
                     "cabinet is not hot."),
        ]
        if quiet:  # no warning on the display; misleading: points at a cooling problem
            texts[1] = (target, f"{target} cooling fan sounds rough on the afternoon round, may need a look.")
        for (area, text), delay in zip(texts, (int(ctx.rng.integers(5, 30)), int(ctx.rng.integers(60, 150)))):
            self.notes.append(note(self.start + timedelta(minutes=delay), area, ctx.rng, text))


UNKNOWN_CATALOGUE = {cls.fault_type: cls for cls in (TransformerTrip, NewShading, FirmwareAlarm)}
