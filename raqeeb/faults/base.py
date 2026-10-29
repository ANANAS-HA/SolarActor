"""Base class for planted faults (HANDOFF section 8).

A Fault turns one FaultSpec into PlantState changes scheduled on the twin with
`twin.schedule(at, label, fn(state))`. It never writes telemetry itself: readings follow from
the state. A fault may log commands or raise alarms the twin has no rule for (e.g. TRACKER_FAULT)
from inside a scheduled change, so they carry the right timestamp.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import numpy as np
import pandas as pd

from raqeeb.contracts import FaultSpec, FaultType, PlantConfig, PlantState, Scenario
from raqeeb.twin.simulator import STEP, Twin


@dataclass
class InjectionContext:
    """What a fault needs to plant itself."""

    twin: Twin
    scenario: Scenario
    weather: pd.DataFrame  # trusted reference weather (weather_5min) for the scenario window
    rng: np.random.Generator  # per-fault, derived from the scenario seed

    @property
    def plant(self) -> PlantConfig:
        return self.twin.plant


class Fault(ABC):
    fault_type: ClassVar[FaultType]

    def __init__(self, spec: FaultSpec, scenario: Scenario, plant: PlantConfig):
        if spec.type != self.fault_type:
            raise ValueError(f"{type(self).__name__} cannot plant {spec.type}")
        if spec.target not in self.valid_targets(plant):
            raise ValueError(f"{spec.label}: target must be one of {sorted(self.valid_targets(plant))}")
        self.spec = spec
        self.start = scenario.resolve_time(spec.start)
        self.end = scenario.resolve_time(spec.end) if spec.end is not None else None

    @property
    def label(self) -> str:
        return self.spec.label

    @staticmethod
    @abstractmethod
    def valid_targets(plant: PlantConfig) -> set[str]: ...

    @abstractmethod
    def arm(self, ctx: InjectionContext) -> None:
        """Schedule this fault's state changes on ctx.twin."""

    def severity_or(self, default: float) -> float:
        return self.spec.severity if self.spec.severity is not None else default

    def schedule(self, ctx: InjectionContext, at: datetime, what: str, change: Callable[[PlantState], None]) -> None:
        ctx.twin.schedule(at, f"{self.label} {what}", change)


def step_times(start: datetime, end: datetime) -> list[datetime]:
    """Every grid time in [start, end)."""
    n = max(int((end - start) / STEP), 0)
    return [start + k * STEP for k in range(n)]
