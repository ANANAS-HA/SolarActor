"""The Trace tab's view of a run: one card per agent turn (poll), built from the RunLog alone.

    cards = turn_cards(run.log, run.now)     # oldest first; the tab shows the newest at the bottom
    head = header(run.log, run.now)          # sim time, turn, causes by status, model calls, model time

The same RunLog feeds live runs and replays, so a card is the same whether the turn just happened or
was recorded. Each card holds what the agent did in that turn, as recorded: tool checks (numbers,
thresholds, verdict), new symptoms, the candidate causes it selected with their scores, model calls with
the proposals they led to, actions with the gate's decision, verifications, escalations, notes and the
agent's own step lines. Version 3 files (live runs now, and bench trajectory files) add every check,
the active symptoms, all candidates (selected or not), the safety gate's decision per planned request
and, when kept, model transcripts. Older files show what they have (v1/v2: the tool results with a
finding, the selected candidates, no gate records, no transcripts). The bench's own truth section
(RunLog.bench) is never shown here.
Free text (notes, alarm and log messages, model data and replies) is data: the tab shows it escaped.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from raqeeb.contracts import (
    VERDICT_INCONCLUSIVE,
    VERDICT_OK,
    ActionStatus,
    Escalation,
    LlmCall,
    Verification,
)
from raqeeb.ui.runlog import (
    ActionEntry,
    GateDecision,
    NoteRecord,
    PollRecord,
    ProposalRecord,
    RunLog,
    TimelineItem,
    Transcript,
)

# Cause statuses shown in the header (derived from the record, oldest to newest)
OPEN, ACTING, RESOLVED, ESCALATED, EXPECTED, FLAGGED = "open", "acting", "resolved", "escalated", "expected", "flagged"
CAUSE_STATUSES = (OPEN, ACTING, RESOLVED, ESCALATED, EXPECTED, FLAGGED)
FINDING, OK, INCONCLUSIVE = "finding", "ok", "inconclusive"


@dataclass
class CheckRow:
    tool: str
    devices: str
    numbers: str  # the tool's values, as computed by code
    threshold: str  # the limits its verdict used
    verdict: str  # the tool's verdict code
    kind: str  # FINDING / OK / INCONCLUSIVE (shown as a word and a colour)


@dataclass
class Candidate:
    label: str
    score: float
    selected: bool
    explains: int


@dataclass
class GateRow:
    action: str
    target: str
    cause: str
    gate: str  # the agent's safety gate: "auto", "needs approval", "held back: <reason>"
    decision: str  # what happened after: applied / rejected / waiting (from the environment's record)
    entry: ActionEntry | None  # None when the gate held it back (never sent)


@dataclass
class TurnCard:
    index: int  # 1-based turn number
    ts: datetime
    since: datetime | None  # the previous turn's time (None for the first): this turn covers (since, ts]
    final: bool
    checks: list[CheckRow] | None  # None = same verdicts on the same devices as the last turn
    symptoms: list[str]  # new symptoms this turn (the agent's summaries)
    unexplained: list[str]  # symptom ids the diagnosis leaves unexplained (when it changed)
    candidates: list[Candidate] | None  # None = same candidates and scores as the last turn
    llm_calls: list[LlmCall]
    proposals: list[ProposalRecord]
    transcripts: list[Transcript]  # model data sent and replies, when the file records them
    actions: list[ActionEntry]
    gate: list[GateRow]  # planned requests with the gate's decision (v3; v1/v2: built from the actions)
    verifications: list[Verification]
    escalations: list[Escalation]
    notes: list[NoteRecord]
    steps: list[TimelineItem]  # the agent's step lines this turn
    outcomes: list[str] = field(default_factory=list)  # what the environment told the agent at this turn

    @property
    def quiet(self) -> bool:
        """Nothing new: no checks change, symptom, diagnosis change, model call, action, verification,
        escalation or note."""
        return not (self.checks is not None or self.symptoms or self.candidates is not None or self.llm_calls
                    or self.proposals or self.actions or self.gate or self.verifications or self.escalations
                    or self.notes or self.outcomes or self.final)


@dataclass
class Header:
    ts: datetime
    turn: int
    turns_total: int | None  # None while a live run is still going
    causes: dict[str, list[str]]  # status -> cause labels
    llm_calls: int
    llm_s: float


# -- building ---------------------------------------------------------------------------------------------


def _kind(verdict: str) -> str:
    return OK if verdict == VERDICT_OK else INCONCLUSIVE if verdict == VERDICT_INCONCLUSIVE else FINDING


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value[:6]) + (", …" if len(value) > 6 else "") + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in list(value.items())[:6]) + "}"
    return str(value)


def _checks(poll: PollRecord) -> list[CheckRow] | None:
    results = poll.checks or poll.findings  # v3 records every check of the poll; v1/v2 the findings
    if results is None:
        return None
    return [CheckRow(tool=r.tool, devices=", ".join(r.devices),
                     numbers="; ".join(f"{k} {_fmt(v)}" for k, v in r.values.items() if k != "summary"),
                     threshold="; ".join(f"{k} {_fmt(v)}" for k, v in r.thresholds.items()),
                     verdict=r.verdict, kind=_kind(r.verdict)) for r in results]


def _candidates(poll: PollRecord) -> list[Candidate] | None:
    if poll.candidates:  # v3: the ranker's selection and the alternatives it left out
        return [Candidate(label=c.label, score=c.score, selected=c.selected, explains=len(c.explains))
                for c in poll.candidates]
    if poll.diagnosis is None:
        return None
    return [Candidate(label=h.labels[0], score=h.score, selected=True, explains=len(h.explains))
            for h in poll.diagnosis.root_causes]


def _symptoms(poll: PollRecord, previous: PollRecord | None) -> list[str]:
    if poll.symptoms:  # v3: active symptoms; the chips are the ones new since the last turn
        before = {s.id for s in previous.symptoms} if previous is not None else set()
        return [s.summary or s.id for s in poll.symptoms if s.id not in before]
    out = []
    for item in poll.timeline:
        if item.node == "investigate" and ": " in item.summary and "new symptom" in item.summary:
            out += [s.strip() for s in item.summary.split(": ", 1)[1].split("; ") if s.strip()]
    return out


def _outcomes(poll: PollRecord) -> list[str]:
    return [f"{o.action_id}: {o.status.value.lower()}" + (f" ({o.note})" if o.note else "") for o in poll.outcomes]


def _gate_rows(poll: PollRecord, entries: list[ActionEntry]) -> list[GateRow]:
    by_id = {a.request.id: a for a in entries}
    if not poll.gate:  # v1/v2: only the requests that were sent
        return [GateRow(a.request.action.value, a.request.target, a.cause, *gate(a), entry=a) for a in entries]
    rows = []
    for g in poll.gate:
        entry = by_id.get(g.request.id)
        if g.decision == GateDecision.REJECTED:
            rows.append(GateRow(g.request.action.value, g.request.target, g.cause, f"held back: {g.reason}",
                                "never sent", None))
            continue
        word = "needs approval" if g.decision == GateDecision.NEEDS_APPROVAL else "auto (allowlisted)"
        rows.append(GateRow(g.request.action.value, g.request.target, g.cause or (entry.cause if entry else ""),
                            word, gate(entry)[1] if entry else "not received", entry))
    return rows


def turn_cards(log: RunLog, now: datetime) -> list[TurnCard]:
    polls = log.polls_until(now)
    actions = log.actions_until(now)
    cards = []
    last_checks = last_candidates = None
    for k, poll in enumerate(polls):
        previous = polls[k - 1].ts if k else None
        checks, candidates = _checks(poll), _candidates(poll)
        # A turn re-records unchanged findings and diagnoses: show them only when something moved
        key = None if checks is None else sorted((c.tool, c.devices, c.verdict) for c in checks)
        if key is not None and key == last_checks:
            checks = None
        last_checks = key if key is not None else last_checks
        key = None if candidates is None else [(c.label, c.score, c.selected) for c in candidates]
        if key is not None and key == last_candidates:
            candidates = None
        last_candidates = key if key is not None else last_candidates
        cards.append(TurnCard(
            index=k + 1, ts=poll.ts, since=previous, final=poll.final, checks=checks,
            symptoms=_symptoms(poll, polls[k - 1] if k else None),
            unexplained=list(poll.diagnosis.unexplained) if poll.diagnosis and candidates is not None else [],
            candidates=candidates, llm_calls=list(poll.llm_calls), proposals=list(poll.proposals),
            transcripts=list(poll.transcripts),
            actions=(sent := [a for a in actions
                              if a.requested_at == poll.ts or (previous and previous < a.requested_at <= poll.ts)]),
            gate=_gate_rows(poll, sent),
            verifications=list(poll.verifications), escalations=list(poll.escalations), notes=list(poll.notes),
            steps=[t for t in poll.timeline], outcomes=_outcomes(poll)))
    return cards


def cause_statuses(log: RunLog, now: datetime) -> dict[str, list[str]]:
    """Each cause the agent has named so far and its status, from the record (diagnosis, actions,
    verifications, escalations, and the agent's own lines for expected and flagged causes)."""
    status: dict[str, str] = {}
    diagnosis = log.diagnosis_until(now)
    for h in diagnosis.root_causes if diagnosis else []:
        status[h.labels[0]] = OPEN
    for item in log.timeline_until(now):
        if item.node != "rank":
            continue
        for prefix, value in (("expected behaviour: ", EXPECTED), ("flagged for security review: ", FLAGGED)):
            if item.summary.startswith(prefix):
                status[item.summary[len(prefix):].split(" (")[0]] = value
    actions = log.actions_until(now)
    recovered = {v.action_id for _, v in log.verifications_until(now) if v.recovered}
    for a in actions:
        if a.cause in status and status[a.cause] == OPEN:
            status[a.cause] = ACTING
    for a in actions:
        if a.cause in status and a.request.id in recovered:
            status[a.cause] = RESOLVED
    for e in log.escalations_until(now):
        if e.subject in status or "@" in e.subject:
            status[e.subject] = ESCALATED
    out: dict[str, list[str]] = {s: [] for s in CAUSE_STATUSES}
    for label, s in sorted(status.items()):
        out[s].append(label)
    return out


def header(log: RunLog, now: datetime, live: bool = False) -> Header:
    polls = log.polls_until(now)
    calls = log.llm_calls_until(now)
    return Header(ts=now, turn=len(polls), turns_total=None if live and not log.finished else len(log.polls),
                  causes=cause_statuses(log, now), llm_calls=len(calls),
                  llm_s=round(sum(c.latency_s for c in calls), 2))


def gate(a: ActionEntry) -> tuple[str, str]:
    """(gate, decision) words for an action: who had to decide and what was decided."""
    needs = "needs approval" if a.gated else "auto (allowlisted)"
    if a.status is None:
        return needs, "waiting for a decision"
    word = {ActionStatus.APPLIED: "applied", ActionStatus.REJECTED: "rejected", ActionStatus.FAILED: "failed"}[a.status]
    who = {"operator": "by the operator", "auto": "automatically", "console": "by the console"}.get(a.decided_by,
                                                                                                    a.decided_by)
    return needs, f"{word} {who}"


def check_counts(cards: list[TurnCard]) -> Counter:
    return Counter(row.kind for card in cards for row in (card.checks or []))
