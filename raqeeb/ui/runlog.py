"""What the console records about one run, and the replay JSON format.

A live run (ui/live.py) fills a RunLog poll by poll; a replay loads one from a JSON file. The
console draws both the same way: from the log and a telemetry store, cut at the current sim time.

Replay file (format "raqeeb-replay", version 1), one JSON object:

    format, version      "raqeeb-replay", 1
    label                how it was recorded, shown on screen, e.g. "recorded with test model"
    mode                 "rules" | "local" | "test" (test = scripted stand-in for the model, no LLM)
    agent, model         agent name; model name ("" for rules only)
    recorded_at          UTC, ISO 8601
    poll_min             sim minutes between agent polls
    incident             {id, start, duration_hours}: all the agent was told (AgentScenario)
    telemetry            {table: [row, ...]} for every table in contracts.TABLE_MODELS, audit included
    polls                one per agent poll, in order: ts, outcomes the agent received, timeline entries,
                         diagnosis (when it changed), actions requested, verifications, escalations,
                         llm_calls, tool findings (when they changed), report (when written), agent_s
    actions              every action request: gate, decision (who, when, note), status, effective_at
    finished             true once the run reached the end of its window
    answer_key           planted faults and root causes; null until the run has finished

Model types are the contracts' (ActionRequest, Diagnosis, Verification, LlmCall, ...); the types
here are the console's own record of a run, used only by raqeeb/ui.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from raqeeb.contracts import (
    TABLE_MODELS,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    AgentScenario,
    Diagnosis,
    Escalation,
    FaultSpec,
    IncidentReport,
    LlmCall,
    ToolResult,
    UtcDatetime,
    Verification,
)
from raqeeb.telemetry.store import TelemetryStore

REPLAY_FORMAT = "raqeeb-replay"
REPLAY_VERSION = 1


class Mode(StrEnum):
    RULES = "rules"
    LOCAL = "local"
    TEST = "test"  # the scripted test model (ui/testmodel.py): exercises the LLM path, no language model


MODE_LABEL = {Mode.RULES: "Live: rules only", Mode.LOCAL: "Live: rules + local model",
              Mode.TEST: "Live: rules + test model (scripted, not a language model)"}
RECORDED_LABEL = {Mode.RULES: "recorded with rules only", Mode.LOCAL: "recorded with the local model",
                  Mode.TEST: "recorded with test model"}


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimelineItem(_Record):
    ts: UtcDatetime
    node: str  # agent graph node ("triage", "rank", ...) or "approval" / "execute" for console entries
    actor: str = "agent"  # "agent", "operator", "console"
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class PollRecord(_Record):
    ts: UtcDatetime
    final: bool = False
    outcomes: list[ActionOutcome] = Field(default_factory=list)  # what the agent was told at this poll
    timeline: list[TimelineItem] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None  # None = unchanged
    actions: list[ActionRequest] = Field(default_factory=list)
    verifications: list[Verification] = Field(default_factory=list)
    escalations: list[Escalation] = Field(default_factory=list)
    llm_calls: list[LlmCall] = Field(default_factory=list)
    findings: list[ToolResult] | None = None  # tool results with a finding; None = same as the last poll
    report: IncidentReport | None = None
    agent_s: float = 0.0  # wall-clock seconds the agent took for this poll


class ActionEntry(_Record):
    request: ActionRequest
    requested_at: UtcDatetime
    gated: bool  # needs human approval per configs/actions.yaml
    status: ActionStatus | None = None  # None = waiting for a decision
    decided_by: str = ""  # "auto" (allowlisted, no approval needed) or "operator"
    decided_at: UtcDatetime | None = None
    note: str = ""  # operator note, or why it failed
    effective_at: UtcDatetime | None = None  # when a delayed action takes effect in the plant


class AnswerKey(_Record):
    faults: list[FaultSpec]
    root_causes: list[str]


class RunLog(_Record):
    format: str = REPLAY_FORMAT
    version: int = REPLAY_VERSION
    label: str
    mode: Mode
    agent: str
    model: str = ""
    recorded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0))
    poll_min: int
    incident: AgentScenario
    telemetry: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # filled when saved
    polls: list[PollRecord] = Field(default_factory=list)
    actions: list[ActionEntry] = Field(default_factory=list)
    finished: bool = False
    answer_key: AnswerKey | None = None  # set only when the run has finished

    # -- views ------------------------------------------------------------------

    def polls_until(self, now: datetime) -> list[PollRecord]:
        return [p for p in self.polls if p.ts <= now]

    def timeline_until(self, now: datetime) -> list[TimelineItem]:
        items = [t for p in self.polls_until(now) for t in p.timeline]
        items += [t for a in self.actions if a.decided_by == "operator" and a.decided_at and a.decided_at <= now
                  for t in [_decision_item(a)]]
        return sorted(items, key=lambda t: t.ts)

    def diagnosis_until(self, now: datetime) -> Diagnosis | None:
        return next((p.diagnosis for p in reversed(self.polls_until(now)) if p.diagnosis is not None), None)

    def findings_until(self, now: datetime) -> list[ToolResult]:
        return next((p.findings for p in reversed(self.polls_until(now)) if p.findings is not None), [])

    def report_until(self, now: datetime) -> IncidentReport | None:
        return next((p.report for p in reversed(self.polls_until(now)) if p.report is not None), None)

    def llm_calls_until(self, now: datetime) -> list[LlmCall]:
        return [c for p in self.polls_until(now) for c in p.llm_calls]

    def verifications_until(self, now: datetime) -> list[tuple[datetime, Verification]]:
        return [(p.ts, v) for p in self.polls_until(now) for v in p.verifications]

    def escalations_until(self, now: datetime) -> list[Escalation]:
        return [e for p in self.polls_until(now) for e in p.escalations]

    def actions_until(self, now: datetime) -> list[ActionEntry]:
        """Requests made by `now`; a decision made later is not shown yet (status None)."""
        out = []
        for a in self.actions:
            if a.requested_at > now:
                continue
            if a.decided_at is not None and a.decided_at > now:
                a = a.model_copy(update={"status": None, "decided_by": "", "decided_at": None, "note": "",
                                         "effective_at": None})
            out.append(a)
        return out

    # -- files ------------------------------------------------------------------

    def to_json(self) -> str:
        return self.model_dump_json()

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8", newline="\n")
        return path

    @classmethod
    def from_json(cls, text: str | bytes) -> RunLog:
        data = json.loads(text)
        if data.get("format") != REPLAY_FORMAT:
            raise ValueError("not a Raqeeb replay file")
        if data.get("version") != REPLAY_VERSION:
            raise ValueError(f"replay version {data.get('version')!r} not supported (expected {REPLAY_VERSION})")
        return cls.model_validate(data)

    @classmethod
    def load(cls, path: Path) -> RunLog:
        return cls.from_json(path.read_text(encoding="utf-8"))


def _decision_item(a: ActionEntry) -> TimelineItem:
    verb = {ActionStatus.APPLIED: "approved", ActionStatus.REJECTED: "rejected"}.get(a.status, "decided")
    return TimelineItem(ts=a.decided_at, node="approval", actor="operator",
                        summary=f"operator {verb} {a.request.action.value} on {a.request.target} ({a.request.id})",
                        details={"note": a.note} if a.note else {})


# -- telemetry in and out of a store ----------------------------------------------


def dump_telemetry(store: TelemetryStore) -> dict[str, list[dict[str, Any]]]:
    """Every table as JSON-ready rows of its contract model (ts as "YYYY-MM-DDTHH:MM:SSZ")."""
    out = {}
    for table, model in TABLE_MODELS.items():
        frame = store.read(table).reset_index()
        fields = list(model.model_fields)
        out[table] = [model.model_validate({k: _cell(row[k]) for k in fields}).model_dump(mode="json")
                      for row in frame[fields].to_dict("records")]
    return out


def load_telemetry(rows: dict[str, list[dict[str, Any]]], store: TelemetryStore | None = None) -> TelemetryStore:
    store = store or TelemetryStore()
    for table, records in rows.items():
        model = TABLE_MODELS[table]
        store.write_rows(table, [model.model_validate(r) for r in records])
    return store


def _cell(value: Any) -> Any:
    """pandas -> plain Python (NaN -> None, Timestamp -> datetime)."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, float) and value != value:
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def plain(value: Any) -> Any:
    """Agent timeline details as JSON-safe values (anything unusual becomes its string)."""
    return json.loads(json.dumps(value, default=str))
