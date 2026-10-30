"""Held-out faults (SYNTHETIC): fault types with no detection rules, kept out of the agent's causal graph and
planner and out of every generated scenario set, so they can serve as unknowns.

Each maps to a failure from the challenge page:

- HUNG_INVERTER (target an inverter), "a workflow that hangs forever": from `start` the controller hangs. The
  inverter produces nothing (the meter drops by its share) but keeps reporting its last reading, status
  included, with a new timestamp each step: no COMM_LOSS, no REMOTE_STOP. It lasts until a controlled restart
  (reset_comms_gateway, or a technician) or `end`.
- COMMS_DROPOUT_LOAD (target an inverter), "loses data under load": from `start` the inverter's link fails
  whenever the true on-site GHI is above 1000 x (1 - severity) W/m2 (default 700). Its rows go missing and
  COMM_LOSS is raised each time; below the threshold data returns on its own. The inverter keeps producing.
  A gateway reset does not help (the next high-irradiance step drops again); a technician replaces the module.
- SENSOR_DRIFT (target the weather station), "slowly corrupts data": from `start` the GHI sensor's bias grows
  linearly by `severity` per hour (default 3 %/h), up or down (drawn per run). Plant output is unchanged.
  A technician recalibrates it; flag_sensor_use_reference only stops the tools from trusting it.
- TRACKER_STUCK_SILENT (target an inverter's block), stuck tracker: at `start` the tracker freezes at the angle
  it had then, not at stow, and raises no TRACKER_FAULT (unlike TRACKER_STUCK). Readings report the true
  frozen angle. resume_tracking (or a technician) frees it.

`record` holds the exact parameters each fault planted (bench and tests only, like the answer key).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from raqeeb.contracts import FaultType, PlantConfig, PlantState
from raqeeb.faults.base import Fault, InjectionContext
from raqeeb.twin import plant as physics

LOAD_DROPOUT_FULL_WM2 = 1000.0  # threshold = this x (1 - severity)
DEFAULT_LOAD_DROPOUT = 0.30
DEFAULT_DRIFT_PER_H = 0.03


class HeldoutFault(Fault):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.record: dict[str, Any] = {}


class HungInverter(HeldoutFault):
    fault_type = FaultType.HUNG_INVERTER

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.inverters.ids)

    def arm(self, ctx: InjectionContext) -> None:
        target = self.spec.target
        self.record = {"start": self.start, "end": self.end}
        self.schedule(ctx, self.start, "hangs", lambda s: setattr(s.inverters[target], "hung", True))
        if self.end is not None:
            self.schedule(ctx, self.end, "recovers", lambda s: setattr(s.inverters[target], "hung", False))


class CommsDropoutUnderLoad(HeldoutFault):
    fault_type = FaultType.COMMS_DROPOUT_LOAD

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.inverters.ids)

    def arm(self, ctx: InjectionContext) -> None:
        target = self.spec.target
        limit = LOAD_DROPOUT_FULL_WM2 * (1.0 - self.severity_or(DEFAULT_LOAD_DROPOUT))
        self.record = {"start": self.start, "end": self.end, "ghi_threshold_wm2": limit}

        def begin(state: PlantState) -> None:
            state.inverters[target].comms_load_limit_wm2 = limit

        self.schedule(ctx, self.start, "begins", begin)
        if self.end is not None:
            self.schedule(ctx, self.end, "ends",
                          lambda s: setattr(s.inverters[target], "comms_load_limit_wm2", None))


class SensorDrift(HeldoutFault):
    fault_type = FaultType.SENSOR_DRIFT

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return {plant.weather_station_id}

    def arm(self, ctx: InjectionContext) -> None:
        rate = self.severity_or(DEFAULT_DRIFT_PER_H) * (1 if ctx.rng.random() < 0.5 else -1)
        self.record = {"start": self.start, "end": self.end, "drift_per_h": rate}

        def begin(state: PlantState) -> None:
            state.station.sensor_drift_per_h = rate

        def finish(state: PlantState) -> None:
            state.station.sensor_drift_per_h = 0.0
            state.station.sensor_bias = 0.0

        self.schedule(ctx, self.start, "begins", begin)
        if self.end is not None:
            self.schedule(ctx, self.end, "ends", finish)


class SilentTrackerStuck(HeldoutFault):
    fault_type = FaultType.TRACKER_STUCK_SILENT

    @staticmethod
    def valid_targets(plant: PlantConfig) -> set[str]:
        return set(plant.inverters.ids)

    def arm(self, ctx: InjectionContext) -> None:
        target = self.spec.target
        theta = physics.prepare(ctx.weather.loc[[pd.Timestamp(self.start)]], ctx.plant)["tracker_theta"]
        angle = float(theta.iloc[0])
        self.record = {"start": self.start, "end": self.end, "angle_deg": angle}
        self.schedule(ctx, self.start, "freezes",
                      lambda s: setattr(s.inverters[target], "tracker_override_deg", angle))
        if self.end is not None:
            self.schedule(ctx, self.end, "ends", lambda s: setattr(s.inverters[target], "tracker_override_deg", None))


HELDOUT_CATALOGUE = {cls.fault_type: cls for cls in (HungInverter, CommsDropoutUnderLoad, SensorDrift,
                                                     SilentTrackerStuck)}
