"""What Raqeeb remembers between polls (Memory) and what one poll carries through the graph (PollState).

These types are internal to the agent; everything it exchanges with its environment uses the
contracts (Symptom, Hypothesis, Diagnosis, ActionRequest, Verification, AgentTurn).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, TypedDict

from raqeeb.contracts import (
    ActionOutcome,
    ActionRequest,
    ActionSpec,
    ActionStatus,
    ActionType,
    AgentScenario,
    AlarmCode,
    Diagnosis,
    Escalation,
    Hypothesis,
    IncidentReport,
    LlmCall,
    PlantConfig,
    Symptom,
    ToolResult,
    Verification,
)
from raqeeb.telemetry.store import TelemetryStore


class CauseStatus(StrEnum):
    OPEN = "open"  # diagnosed, nothing requested yet
    WAITING = "waiting"  # a fix is planned but its conditions are not met yet (e.g. the storm still blows)
    ACTING = "acting"  # fix requested or applied, waiting to verify
    RESOLVED = "resolved"  # verified recovered, or cleared on its own
    REOPENED = "reopened"  # a fix did not bring recovery: plan the next one
    EXPECTED = "expected"  # normal behaviour (grid curtailment): no action
    FLAGGED = "flagged"  # a device log line: flagged for security review, never a root cause, no action
    BLOCKED = "blocked"  # approval refused or the fix failed: left to the operators
    ESCALATED = "escalated"  # fixes used up (or none fits): handed to a human with a reason, no more retries


PASSIVE = frozenset({CauseStatus.EXPECTED, CauseStatus.FLAGGED})  # decoys: reported, never a root cause or an action


@dataclass
class Cause:
    """A root cause the agent has confirmed (selected by rank at least once)."""

    hypothesis: Hypothesis
    first_seen: datetime
    last_selected: datetime
    status: CauseStatus = CauseStatus.OPEN
    attempts: int = 0  # fixes that failed verification
    action_ids: list[str] = field(default_factory=list)
    note: str = ""
    misses: int = 0  # consecutive polls rank did not select it
    confidence: float = 0.0  # support for this cause alone (rank.cause_support); gates its irreversible actions
    source: str = "rules"  # "rules" (causal graph) or "llm" (proposed by the model, accepted after testing)

    @property
    def label(self) -> str:
        return self.hypothesis.labels[0]


@dataclass
class PendingAction:
    """An action the agent requested, through approval, execution and verification."""

    request: ActionRequest
    cause: str  # cause label it addresses
    requested_at: datetime
    status: ActionStatus | None = None  # None until the environment reports an outcome
    applied_at: datetime | None = None
    verify_after: datetime | None = None
    give_up_at: datetime | None = None
    before_kw: float | None = None
    verification: Verification | None = None


@dataclass
class TimelineEntry:
    ts: datetime
    node: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Memory:
    view: AgentScenario
    telemetry: TelemetryStore
    plant: PlantConfig
    action_specs: dict[ActionType, ActionSpec]
    alarm_codes: dict[str, AlarmCode]
    alarm_groups: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, ToolResult] = field(default_factory=dict)
    symptoms: dict[str, Symptom] = field(default_factory=dict)  # every symptom seen, by id
    active: set[str] = field(default_factory=set)  # ids the tools report in the latest poll
    hypotheses: list[Hypothesis] = field(default_factory=list)  # last ranking, best first
    causes: dict[str, Cause] = field(default_factory=dict)  # label -> cause (incl. expected decoys)
    pending: dict[str, PendingAction] = field(default_factory=dict)  # action id -> pending
    timeline: list[TimelineEntry] = field(default_factory=list)
    diagnosis: Diagnosis | None = None
    report: IncidentReport | None = None
    action_seq: int = 0
    known_losses: list[str] = field(default_factory=list)  # inverters left out of the plant-wide check
    revised: list[tuple[str, str]] = field(default_factory=list)  # (old label, new label) diagnosis revisions
    escalations: list[Escalation] = field(default_factory=list)  # hand-overs to a human, in order
    unexplained_polls: dict[str, int] = field(default_factory=dict)  # symptom id -> polls in a row unexplained
    escalated: set[str] = field(default_factory=set)  # cause labels and symptom ids already handed over
    router: Any = None  # raqeeb.llm.router.Router, or None for rules only
    alternatives: list[Hypothesis] = field(default_factory=list)  # candidates rank did not select, standalone scores
    consulted: set[frozenset[str]] = field(default_factory=set)  # questions already put to the model
    hypothesis_calls: int = 0  # hypothesis questions asked this incident
    llm_calls: list[LlmCall] = field(default_factory=list)  # every model call of the incident, in order

    def log(self, ts: datetime, node: str, summary: str, **details: Any) -> None:
        self.timeline.append(TimelineEntry(ts=ts, node=node, summary=summary, details=details))

    def next_action_id(self) -> str:
        self.action_seq += 1
        return f"raqeeb-{self.action_seq}"


class PollState(TypedDict, total=False):
    """One pass of the graph (one environment poll)."""

    memory: Memory
    now: datetime
    final: bool
    outcomes: list[ActionOutcome]
    proposals: list[tuple[ActionRequest, str]]  # (request, cause label) from plan
    approved: list[tuple[ActionRequest, str]]  # after safety
    actions: list[ActionRequest]  # sent to the environment this poll
    verifications: list[Verification]  # new this poll
    reopened: list[str]  # cause labels reopened this poll
    replanned: bool
    diagnosis_changed: bool
