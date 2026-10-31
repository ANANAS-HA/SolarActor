"""The Summary tab's view of a run: the incident in one screen, built from the RunLog (through the Trace
tab's turn cards) and the power lines. Every item carries the turn it belongs to, so the tab can jump to
that card in the Trace tab.

    moments = key_moments(log, now)          # first symptom, causes, actions and gate, approvals, ...
    rows = cause_rows(log, now)              # the causes table with status, first named, actions, verified
    e = energy(power, actions, now)          # energy below the expected-from-reference line, before/after action

Works the same for a live run and a replay: both only have the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import MutableMapping

import pandas as pd

from raqeeb.contracts import STEP_MINUTES, ActionStatus
from raqeeb.ui import trace
from raqeeb.ui.runlog import ActionEntry, RunLog

SYMPTOM, CAUSE, PROPOSAL, ACTION, APPROVAL, VERIFICATION, ESCALATION = (
    "first symptom", "cause", "model proposal", "action", "approval", "verification", "escalation")
KINDS = (SYMPTOM, CAUSE, PROPOSAL, ACTION, APPROVAL, VERIFICATION, ESCALATION)  # lanes, top to bottom
HUMANS = ("operator", "human:simulated")

# Session-state keys shared with the Trace tab (app.py): the clock index to show and the card to scroll to
CLOCK_KEY, FOCUS_KEY, FOLLOW_KEY, SWITCH_KEY = "trace_clock", "trace_focus", "trace_follow", "trace_switch"


@dataclass
class Moment:
    ts: datetime
    turn: int  # 1-based turn whose card shows it
    kind: str  # one of KINDS
    text: str  # what happened, one line (free text escaped by the page)
    outcome: str = ""  # accepted / rejected / applied / recovered ... (for the colour)


@dataclass
class CauseRow:
    cause: str
    status: str  # trace.CAUSE_STATUSES
    turn: int | None  # turn first named (None: only known from an action or escalation)
    actions: str
    verified: str


@dataclass
class Energy:
    expected_mwh: float  # under the expected-from-reference line, up to now
    actual_mwh: float
    lost_mwh: float  # sum of (expected - actual)+ : the shortfall, not a claim about its cause
    lost_before_mwh: float  # before the first applied action took effect
    lost_after_mwh: float
    gap_worst_pct: float | None  # the worst hourly shortfall (% of expected)
    gap_last_pct: float | None  # the last hour's shortfall
    first_effective: datetime | None


def _turn_at(cards: list[trace.TurnCard], ts: datetime) -> int:
    """The first turn whose poll time is at or after `ts` (the card that reports it), else the last."""
    return next((c.index for c in cards if c.ts >= ts), cards[-1].index if cards else 0)


def key_moments(log: RunLog, now: datetime) -> list[Moment]:
    cards = trace.turn_cards(log, now)
    out: list[Moment] = []
    seen_symptom, named = False, set()
    for card in cards:
        if card.symptoms and not seen_symptom:
            seen_symptom = True
            more = f" (+{len(card.symptoms) - 1} more)" if len(card.symptoms) > 1 else ""
            out.append(Moment(card.ts, card.index, SYMPTOM, card.symptoms[0] + more))
        for c in card.candidates or []:
            if c.selected and c.label not in named:
                named.add(c.label)
                out.append(Moment(card.ts, card.index, CAUSE, f"{c.label} (score {c.score:.2f})", "confirmed"))
        for p in card.proposals:
            text = f"{p.label}: {p.decision}" + (f" ({p.reason})" if p.reason else "")
            out.append(Moment(card.ts, card.index, PROPOSAL, text, p.decision))
        for g in card.gate:
            out.append(Moment(card.ts, card.index, ACTION, f"{g.action} on {g.target}: {g.gate}", g.decision))
        for v in card.verifications:
            word = "recovered" if v.recovered else "not recovered"
            out.append(Moment(card.ts, card.index, VERIFICATION,
                              f"{v.action_id}: {word} ({v.before_kw:.0f} → {v.after_kw:.0f} kW, "
                              f"expected {v.expected_kw:.0f})", word))
        for e in card.escalations:
            out.append(Moment(card.ts, card.index, ESCALATION, f"{e.subject}: {e.reason}", "escalated"))
    for a in log.actions_until(now):
        if a.status is not None and a.decided_by in HUMANS and a.decided_at is not None:
            word = {ActionStatus.APPLIED: "approved", ActionStatus.REJECTED: "rejected"}.get(a.status, a.status.value.lower())
            who = trace.DECIDED_BY.get(a.decided_by, a.decided_by)
            out.append(Moment(a.decided_at, _turn_at(cards, a.decided_at), APPROVAL,
                              f"{a.request.action.value} on {a.request.target}: {word} {who}"
                              + (f" — {a.note}" if a.note else ""), word))
    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda m: (m.ts, m.turn, order[m.kind]))
    return out


def cause_rows(log: RunLog, now: datetime) -> list[CauseRow]:
    statuses = trace.cause_statuses(log, now)
    first: dict[str, int] = {}
    for m in key_moments(log, now):
        if m.kind == CAUSE:
            first.setdefault(m.text.split(" (score")[0], m.turn)
    actions = log.actions_until(now)
    recovered = {v.action_id: v.recovered for _, v in log.verifications_until(now)}
    rows = []
    for status in trace.CAUSE_STATUSES:
        for label in statuses[status]:
            mine = [a for a in actions if a.cause == label]
            acts = "; ".join(f"{a.request.action.value} on {a.request.target}"
                             f" ({a.status.value.lower() if a.status else 'waiting'})" for a in mine) or "—"
            checks = [recovered[a.request.id] for a in mine if a.request.id in recovered]
            verified = "recovered" if checks and all(checks) else "not recovered" if checks else "—"
            rows.append(CauseRow(label, status, first.get(label), acts, verified))
    return rows


def energy(power: pd.DataFrame, actions: list[ActionEntry], now: datetime) -> Energy:
    """Energy under the two lines up to `now`, and the shortfall split at the first applied action's effect."""
    step_h = STEP_MINUTES / 60
    shown = power.loc[: now].dropna(subset=["actual_kw", "expected_ref_kw"])
    short = (shown["expected_ref_kw"] - shown["actual_kw"]).clip(lower=0)
    effective = sorted(a.effective_at or a.decided_at for a in actions
                       if a.status == ActionStatus.APPLIED and (a.effective_at or a.decided_at))
    first = effective[0] if effective else None
    before = short.loc[: first] if first is not None else short

    def hourly_gap(frame: pd.DataFrame) -> float | None:
        if frame.empty or frame["expected_ref_kw"].sum() <= 0:
            return None
        return float((1 - frame["actual_kw"].sum() / frame["expected_ref_kw"].sum()) * 100)

    hours = [hourly_gap(g) for _, g in shown.resample("1h")] if len(shown) else []
    hours = [h for h in hours if h is not None]
    last = shown.loc[shown.index > shown.index.max() - timedelta(hours=1)] if len(shown) else shown
    return Energy(expected_mwh=float(shown["expected_ref_kw"].sum() * step_h / 1000),
                  actual_mwh=float(shown["actual_kw"].sum() * step_h / 1000),
                  lost_mwh=float(short.sum() * step_h / 1000),
                  lost_before_mwh=float(before.sum() * step_h / 1000),
                  lost_after_mwh=float((short.sum() - before.sum()) * step_h / 1000),
                  gap_worst_pct=max(hours) if hours else None, gap_last_pct=hourly_gap(last),
                  first_effective=first)


def jump(state: MutableMapping, turn: int) -> None:
    """Point the Trace tab at `turn` (1-based): its clock, the card to scroll to, follow off, switch tab."""
    state[CLOCK_KEY] = max(turn - 1, 0)
    state[FOCUS_KEY] = turn
    state[FOLLOW_KEY] = False
    state[SWITCH_KEY] = True
