"""What is recorded about one agent run: the console's replay file and the bench's trajectory file.

A live run (ui/live.py) or a bench run (bench/harness.py) fills a RunLog poll by poll through a
Recorder; a replay loads one from a JSON file. The console draws both the same way: from the log
and a telemetry store, cut at the current sim time. `python -m raqeeb.bench.trajectory <file>`
prints one turn by turn.

Replay / trajectory file (format "raqeeb-replay", version 3), one JSON object:

    format, version      "raqeeb-replay", 3
    label                how it was recorded, shown on screen, e.g. "recorded with test model"
    mode                 "rules" | "local" | "test" | "bench" (test = scripted stand-in for the model, no LLM;
                         bench = written by the bench harness, any agent, a simulated operator decides)
    agent, model         agent name; model name ("" for rules only)
    recorded_at          UTC, ISO 8601
    poll_min             sim minutes between agent polls
    incident             {id, start, duration_hours}: all the agent was told (AgentScenario)
    telemetry            {table: [row, ...]} for every table in contracts.TABLE_MODELS, audit included
    polls                one per agent poll, in order (agent-side data only: what the agent saw, computed and
                         decided; nothing from the answer key):
                           ts, final, outcomes the agent received, timeline entries, diagnosis (when it
                           changed), actions requested, verifications, escalations, llm_calls, findings
                           (tool results with a finding, when they changed), report (when written), agent_s,
                           notes (technician notes since the last poll), proposals (model proposals and how the
                           agent's test decided them)                                          -- v1 / v2
                           checks (every tool result of the poll: tool, devices, window, verdict, values,
                           thresholds, evidence), symptoms (active: id, kind, device, since), candidates (the
                           ranker's candidates with scores, selected or not), gate (each planned request with
                           the safety gate's decision: auto / needs_approval / rejected, and the reason),
                           transcripts ({purpose, prompt, reply} per model call; empty unless enabled)  -- v3
    actions              every action request: gate, decision (who, when, note), status, effective_at, cause
    finished             true once the run reached the end of its window
    answer_key           planted faults and root causes; null until the run has finished   (not agent-side)
    bench                bench-only truth: per-poll mitigation state from the twin, energy, reference energies,
                         storm windows; null unless the bench harness wrote the file     (not agent-side)

Version 1 files (no notes, proposals, action causes or v3 fields) and version 2 files (no v3 fields)
still load: the fields they lack are empty. The gzip form (<name>.json.gz) loads the same way.

Model types are the contracts' (ActionRequest, Diagnosis, Verification, LlmCall, ToolResult, ...); the
types here are the record of a run, used by raqeeb/ui and raqeeb/bench.
"""

from __future__ import annotations

import gzip
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
    AgentTurn,
    Diagnosis,
    Escalation,
    FaultSpec,
    IncidentReport,
    LlmCall,
    LlmRole,
    ToolResult,
    UtcDatetime,
    Verification,
)
from raqeeb.telemetry.store import TelemetryStore

REPLAY_FORMAT = "raqeeb-replay"
REPLAY_VERSION = 3
READABLE_VERSIONS = (1, 2, 3)
GZIP_MAGIC = b"\x1f\x8b"


class Mode(StrEnum):
    RULES = "rules"
    LOCAL = "local"
    TEST = "test"  # the scripted test model (ui/testmodel.py): exercises the LLM path, no language model
    BENCH = "bench"  # written by the bench harness (any agent); gated actions decided by the simulated operator


LIVE_MODES = (Mode.RULES, Mode.LOCAL, Mode.TEST)  # modes the console can start a run in
MODE_LABEL = {Mode.RULES: "Live: rules only", Mode.LOCAL: "Live: rules + local model",
              Mode.TEST: "Live: rules + test model (scripted, not a language model)",
              Mode.BENCH: "Benchmark run"}
RECORDED_LABEL = {Mode.RULES: "recorded with rules only", Mode.LOCAL: "recorded with the local model",
                  Mode.TEST: "recorded with test model", Mode.BENCH: "recorded by the bench harness"}


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimelineItem(_Record):
    ts: UtcDatetime
    node: str  # agent graph node ("triage", "rank", ...) or "approval" / "execute" for console entries
    actor: str = "agent"  # "agent", "operator", "console"
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


# -- v2 records (console, phase2/lane-c): kept so version 2 files load; filled by the console ----------


class NoteRecord(_Record):
    """A note from site staff (technician_notes table) or an operator's approval note. Untrusted text."""

    ts: UtcDatetime
    source: str  # "technician" or "operator"
    author: str = ""
    about: str = ""  # area or device the note is about (technician), or the action decided (operator)
    text: str


class ProposalCheck(_Record):
    check: str  # what was checked: the agent's test, or a tool whose result covers the target
    result: str  # computed by code
    passed: bool | None = None  # None = context (a tool result), not a pass/fail test


class ProposalRecord(_Record):
    """One cause proposed by a model and what the agent's tool test made of it (agent/consult.py)."""

    ts: UtcDatetime  # poll at which the agent decided
    label: str  # FAULT_TYPE@TARGET
    fault_type: str
    target: str
    decision: str  # "accepted" (added to the diagnosis), "rejected" (nothing changed), "dropped" (later re-test failed)
    reason: str = ""  # the test's reason for rejecting or dropping
    explains: list[str] = Field(default_factory=list)  # symptom ids it was accepted to explain
    checks: list[ProposalCheck] = Field(default_factory=list)
    model_evidence: list[str] = Field(default_factory=list)  # the model's own words (untrusted); accepted only
    model: str = ""
    llm_s: float = 0.0  # time of the model call(s) that proposed it


# -- v3 records: the agent's reasoning per poll ---------------------------------------------------


class SymptomRecord(_Record):
    """A symptom the tools report as active at this poll (agent/investigate.py)."""

    id: str  # "KIND:device" (or "KIND:code:device" for alarms)
    kind: str  # contracts.SymptomKind value
    device: str  # device id, or PLANT_TARGET for plant-wide
    since: UtcDatetime  # onset the tools computed (kept from the first poll that saw it)
    summary: str = ""  # the tool's one-line summary of it, computed by code


class CandidateRecord(_Record):
    """A cause the ranker considered this poll (agent/rank.py): selected into the diagnosis or not."""

    id: str
    label: str  # FAULT_TYPE@TARGET (the first target for a multi-target cause)
    fault_type: str
    targets: list[str]
    score: float  # rank.py score: selected causes in the simplest explanation, alternatives standalone
    selected: bool
    explains: list[str] = Field(default_factory=list)  # symptom ids
    evidence: list[str] = Field(default_factory=list)


class GateDecision(StrEnum):
    AUTO = "auto"  # allowlisted, no approval needed: sent to the environment to run
    NEEDS_APPROVAL = "needs_approval"  # allowlisted but gated: sent, the environment asks a human
    REJECTED = "rejected"  # held back by the agent's safety gate: never sent


class GateRecord(_Record):
    """One planned ActionRequest and what the agent's safety gate (agent/safety.py) decided."""

    request: ActionRequest
    decision: GateDecision
    reason: str = ""  # why it was held back ("" when sent)
    cause: str = ""  # label of the cause the plan requested it for


class Transcript(_Record):
    """The text of one model call; recorded only when the gateway keeps transcripts (opt-in)."""

    purpose: str  # LlmCall.purpose, in the order of the poll's llm_calls
    prompt: str  # the messages sent, one "[role]" block each
    reply: str  # the model's reply text ("" when the call failed)


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
    # v2 (filled by the console)
    notes: list[NoteRecord] = Field(default_factory=list)  # technician notes posted since the last poll
    proposals: list[ProposalRecord] = Field(default_factory=list)  # model proposals decided at this poll
    # v3 (from the agent's memory after the poll; empty for agents without one)
    checks: list[ToolResult] = Field(default_factory=list)  # every tool result of this poll, finding or not
    symptoms: list[SymptomRecord] = Field(default_factory=list)  # active symptoms after this poll
    candidates: list[CandidateRecord] = Field(default_factory=list)  # the ranker's candidates, best first
    gate: list[GateRecord] = Field(default_factory=list)  # planned requests and the safety gate's decisions
    transcripts: list[Transcript] = Field(default_factory=list)  # one per llm_call when enabled, else empty


class ActionEntry(_Record):
    request: ActionRequest
    requested_at: UtcDatetime
    gated: bool  # needs human approval per configs/actions.yaml
    status: ActionStatus | None = None  # None = waiting for a decision
    decided_by: str = ""  # "auto" (allowlisted, no approval needed), "operator", or the bench's simulated human
    decided_at: UtcDatetime | None = None
    note: str = ""  # operator note, or why it failed
    effective_at: UtcDatetime | None = None  # when a delayed action takes effect in the plant
    cause: str = ""  # label of the cause the agent requested it for


class AnswerKey(_Record):
    faults: list[FaultSpec]
    root_causes: list[str]


class MitigationRecord(_Record):
    ts: UtcDatetime
    mitigated: dict[str, bool]  # answer-key label -> mitigated in the twin state after this poll


class BenchTruth(_Record):
    """BENCH-ONLY TRUTH, read from the twin by the harness. Never shown to the agent, never part of a
    poll's agent-side data (tests/leakcheck applies to the polls, not to this section)."""

    polls: list[MitigationRecord] = Field(default_factory=list)
    energy_mwh: float | None = None  # plant energy of this run (twin.truth)
    no_action_mwh: float | None = None  # the same faults, nobody fixes anything
    decoy_only_mwh: float | None = None  # only the decoys: output with no real fault at all
    storms: list[tuple[UtcDatetime, UtcDatetime]] = Field(default_factory=list)  # dust-storm windows [start, end)
    control: bool = False  # this is the neutral-text control run of an injected-instruction scenario


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
    bench: BenchTruth | None = None  # bench-only truth (see BenchTruth); None for console runs

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
        """Write the log; a ".gz" suffix writes it gzip-compressed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_json()
        if path.suffix == ".gz":
            with gzip.open(path, "wt", encoding="utf-8", newline="\n") as f:
                f.write(text)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
        return path

    @classmethod
    def from_json(cls, text: str | bytes) -> RunLog:
        data = json.loads(text)
        if data.get("format") != REPLAY_FORMAT:
            raise ValueError("not a Raqeeb replay file")
        if data.get("version") not in READABLE_VERSIONS:
            raise ValueError(f"replay version {data.get('version')!r} not supported (expected one of "
                             f"{', '.join(map(str, READABLE_VERSIONS))})")
        log = cls.model_validate(data)
        # An older file becomes a current-version object (its missing fields are empty), so saving it again
        # writes a file that a reader of this version accepts
        return log if log.version == REPLAY_VERSION else log.model_copy(update={"version": REPLAY_VERSION})

    @classmethod
    def load(cls, path: Path) -> RunLog:
        """Load a plain or gzip-compressed file (detected by content, whatever the suffix)."""
        raw = path.read_bytes()
        if raw[:2] == GZIP_MAGIC:
            raw = gzip.decompress(raw)
        return cls.from_json(raw.decode("utf-8"))


def _decision_item(a: ActionEntry) -> TimelineItem:
    verb = {ActionStatus.APPLIED: "approved", ActionStatus.REJECTED: "rejected"}.get(a.status, "decided")
    return TimelineItem(ts=a.decided_at, node="approval", actor="operator",
                        summary=f"operator {verb} {a.request.action.value} on {a.request.target} ({a.request.id})",
                        details={"note": a.note} if a.note else {})


# -- filling a log poll by poll (shared by ui/live.py and bench/harness.py) -------------------------


class Recorder:
    """Fills `log` from what the environment hands the agent and what the agent returns, plus (for an agent
    with a `memory`, i.e. RaqeebAgent) its timeline, tool results, symptoms, candidates and gate decisions,
    read after each poll. Reads only; the agent is never changed.

        rec = Recorder(log, agent)
        rec.poll(now, final, outcomes, turn, agent_s)          # one PollRecord
        entry = rec.request(request, now, gated)               # the environment's gate: an ActionEntry
        rec.settle(entry, status, by="auto", at=now, note="", effective_at=None)
        rec.finish(faults, root_causes, store)                 # answer key + telemetry, once the run has ended
    """

    def __init__(self, log: RunLog, agent: Any):
        self.log = log
        self.agent = agent
        self._timeline_seen = 0
        self._last_findings: list[dict] | None = None
        self._transcripts_seen = 0

    @property
    def memory(self) -> Any | None:
        return getattr(self.agent, "memory", None)

    @property
    def gateway(self) -> Any | None:
        return getattr(getattr(self.agent, "router", None), "gateway", None)

    def poll(self, now: datetime, final: bool, outcomes: list[ActionOutcome], turn: AgentTurn,
             agent_s: float = 0.0) -> PollRecord:
        record = PollRecord(ts=now, final=final, outcomes=list(outcomes), diagnosis=turn.diagnosis,
                            actions=list(turn.actions), verifications=list(turn.verifications),
                            escalations=list(turn.escalations), llm_calls=list(turn.llm_calls), report=turn.report,
                            agent_s=agent_s)
        record.timeline = self._take_timeline()
        record.findings = self._take_findings()
        memory = self.memory
        if memory is not None:
            record.checks = list(memory.results.values())
            record.symptoms = active_symptoms(memory)
            record.candidates = rank_candidates(memory)
            record.gate = gate_records(memory, record.timeline, turn.actions)
        record.transcripts = self._take_transcripts()
        self.log.polls.append(record)
        return record

    def request(self, request: ActionRequest, requested_at: datetime, gated: bool) -> ActionEntry:
        """The environment received `request`; `gated` per configs/actions.yaml. Returns the new entry."""
        entry = ActionEntry(request=request, requested_at=requested_at, gated=gated,
                            cause=cause_of(self.memory, request.id))
        self.log.actions.append(entry)
        return entry

    @staticmethod
    def settle(entry: ActionEntry, status: ActionStatus, by: str, at: datetime, note: str = "",
               effective_at: datetime | None = None) -> None:
        entry.status, entry.decided_by, entry.decided_at, entry.note = status, by, at, note
        entry.effective_at = effective_at

    def finish(self, faults: list[FaultSpec], root_causes: list[str], store: Any | None) -> None:
        """End of run: the answer key goes into the log (shown only now) and the telemetry is attached."""
        if self.log.finished:
            return
        self.log.answer_key = AnswerKey(faults=list(faults), root_causes=list(root_causes))
        self.log.finished = True
        if store is not None:
            self.log.telemetry = dump_telemetry(store)

    # -- what the agent did ---------------------------------------------------------

    def _take_timeline(self) -> list[TimelineItem]:
        memory = self.memory
        if memory is None:
            return []
        new = memory.timeline[self._timeline_seen:]
        self._timeline_seen = len(memory.timeline)
        return [TimelineItem(ts=e.ts, node=e.node, summary=e.summary, details=plain(e.details)) for e in new]

    def _take_findings(self) -> list[ToolResult] | None:
        memory = self.memory
        findings = [r for r in (memory.results.values() if memory else []) if r.finding]
        dumped = [r.model_dump(mode="json") for r in findings]
        if dumped == self._last_findings:
            return None
        self._last_findings = dumped
        return findings

    def _take_transcripts(self) -> list[Transcript]:
        gateway = self.gateway
        kept = getattr(gateway, "transcripts", None)
        if not kept:
            return []
        new = kept[self._transcripts_seen:]
        self._transcripts_seen = len(kept)
        return [Transcript(purpose=t["purpose"], prompt=t["prompt"], reply=t["reply"]) for t in new]


def active_symptoms(memory: Any) -> list[SymptomRecord]:
    out = []
    for sid in sorted(memory.active):
        s = memory.symptoms.get(sid)
        if s is None:
            continue
        summary = s.evidence.get("summary", "") if isinstance(s.evidence, dict) else ""
        out.append(SymptomRecord(id=s.id, kind=str(s.kind.value if hasattr(s.kind, "value") else s.kind),
                                 device=s.device_id, since=s.start, summary=str(summary)))
    return out


def rank_candidates(memory: Any) -> list[CandidateRecord]:
    """The ranker's selection (memory.hypotheses, best first) then the candidates it left out
    (memory.alternatives, standalone scores)."""
    def record(h, selected: bool) -> CandidateRecord:
        return CandidateRecord(id=h.id, label=h.labels[0] if h.targets else h.id, fault_type=h.fault_type.value,
                               targets=list(h.targets), score=h.score, selected=selected,
                               explains=list(h.explains), evidence=list(h.evidence))
    return [record(h, True) for h in memory.hypotheses] + [record(h, False) for h in memory.alternatives]


def gate_records(memory: Any, timeline: list[TimelineItem], sent: list[ActionRequest]) -> list[GateRecord]:
    """Sent requests (auto or needs_approval, from the request's allowlist flags) followed by the requests
    the safety gate held back this poll (its "held" details on the timeline, with the reason)."""
    out = [GateRecord(request=r, decision=GateDecision.NEEDS_APPROVAL if r.needs_approval else GateDecision.AUTO,
                      cause=cause_of(memory, r.id)) for r in sent]
    for item in timeline:
        if item.node != "safety":
            continue
        for held in item.details.get("held", []) or []:
            try:
                request = ActionRequest.model_validate(held["request"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append(GateRecord(request=request, decision=GateDecision.REJECTED, reason=str(held.get("reason", "")),
                                  cause=str(held.get("cause", ""))))
    return out


def cause_of(memory: Any, action_id: str) -> str:
    pending = getattr(memory, "pending", None) or {}
    p = pending.get(action_id)
    return getattr(p, "cause", "") or ""


def model_label(agent: Any) -> str:
    """The model name for the log header: "" for an agent without a router."""
    gateway = getattr(getattr(agent, "router", None), "gateway", None)
    if gateway is None:
        return ""
    try:
        return gateway.model_label(LlmRole.LOCAL)
    except Exception:  # noqa: BLE001 - a header string only
        return ""


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
