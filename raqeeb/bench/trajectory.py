"""Print a recorded run (trajectory or replay file, ui/runlog.RunLog) turn by turn, as plain text that reads
like a voice-over script.

    uv run python -m raqeeb.bench.trajectory <file> [--turn N] [--from HH:MM] [--all-checks] [--utc]

<file>: bench/<set>/trajectories/<scenario>__<agent>.json[.gz], raqeeb/ui/replays/*.json or
tests/fixtures/trajectory_v3_example.json. Each turn (one agent poll) prints what arrived (outcomes, alarms),
the checks table (tool, window, verdict, devices, numbers, with the limits each verdict used) for the checks
whose verdict changed since the last turn (--all-checks: every check, every turn), the symptoms that appeared
or cleared, the ranker's candidates with scores when they changed, model calls with their verdict (and the
transcript when the file has one), the plan and the safety gate, the requests sent, the environment's
decisions, verifications and escalations. A turn with nothing new is one line. The answer key and the bench
truth, which the agent never saw, come last under their own heading.

Times are shown in Asia/Riyadh with the UTC time in brackets (--utc: UTC only); --from HH:MM is read in the
same zone as the display. --turn N prints turn N alone (1-based).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from raqeeb.contracts import DISPLAY_TZ, VERDICT_INCONCLUSIVE, VERDICT_OK, ToolResult
from raqeeb.ui.runlog import GateDecision, GateRecord, PollRecord, RunLog

RIYADH = ZoneInfo(DISPLAY_TZ)
MAX_NUMBERS = 8  # values shown per check
MAX_DEVICES = 4  # devices named per check before "+N more"
MAX_TRANSCRIPT_CHARS = 600  # per prompt / reply excerpt
INDENT = "    "
AGENT_NODES = ("start", "triage", "investigate", "rank", "consult", "plan", "safety", "execute", "verify", "report")
_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")


# -- formatting helpers ------------------------------------------------------------------------------


class Clock:
    def __init__(self, utc: bool = False):
        self.utc = utc
        self.zone = timezone.utc if utc else RIYADH

    def hm(self, ts: datetime) -> str:
        """"HH:MM" in the display zone."""
        return ts.astimezone(self.zone).strftime("%H:%M")

    def full(self, ts: datetime) -> str:
        if self.utc:
            return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        local = ts.astimezone(RIYADH)
        return f"{local.strftime('%Y-%m-%d %H:%M')} Riyadh ({ts.astimezone(timezone.utc).strftime('%H:%M')} UTC)"

    def local(self, ts: datetime) -> datetime:
        return ts.astimezone(self.zone)

    def num(self, value: Any) -> str:
        """A value as short text: floats to 4 significant digits, times as HH:MM, short lists inline."""
        if value is None:
            return "-"
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, float):
            return f"{value:.4g}" if abs(value) < 1000 else f"{value:,.0f}"
        if isinstance(value, datetime):
            return self.hm(value)
        if isinstance(value, str):
            return self.hm(datetime.fromisoformat(value.replace("Z", "+00:00"))) if _ISO.match(value) else value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(self.num(v) for v in value[:6]) + (", ..." if len(value) > 6 else "") + "]"
        return str(value)

    def numbers(self, result: ToolResult, limit: int = MAX_NUMBERS) -> str:
        """The check's scalar values; for per-device tables, the rows of the devices the verdict names."""
        parts: list[str] = []
        for key, value in result.values.items():
            if isinstance(value, dict):
                rows = {d: value[d] for d in result.devices if d in value}
                if rows:
                    for device, row in rows.items():
                        if isinstance(row, dict):
                            inner = ", ".join(f"{k} {self.num(v)}" for k, v in row.items()
                                              if not isinstance(v, (dict, list)))
                            parts.append(f"{device}: {inner}" if inner else f"{device}: {self.num(row)}")
                        else:
                            parts.append(f"{device} {self.num(row)}")
                elif all(not isinstance(v, (dict, list)) for v in value.values()) and len(value) <= 4:
                    parts.append(f"{key} " + ", ".join(f"{k} {self.num(v)}" for k, v in value.items()))
                else:
                    parts.append(f"{key}: {len(value)} entries")
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                parts.append(f"{key}: {len(value)} rows")
            else:
                parts.append(f"{key} {self.num(value)}")
        text = "; ".join(parts[:limit])
        return text + (f"; +{len(parts) - limit} more" if len(parts) > limit else "")

    def limits(self, result: ToolResult) -> str:
        return ", ".join(f"{k} {self.num(v)}" for k, v in result.thresholds.items())

    def window(self, result: ToolResult) -> str:
        if result.start is None or result.end is None:
            return "-"
        return f"{self.hm(result.start)}-{self.hm(result.end)}"


def devices(names: list[str]) -> str:
    if not names:
        return "-"
    shown = ", ".join(names[:MAX_DEVICES])
    return shown + (f" +{len(names) - MAX_DEVICES} more" if len(names) > MAX_DEVICES else "")


def table(rows: list[list[str]], header: list[str]) -> list[str]:
    """Plain text columns; the last column is free text."""
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header) - 1)]
    out = []
    for row in [header, *rows]:
        cells = [row[i].ljust(widths[i]) for i in range(len(header) - 1)] + [row[-1]]
        out.append(INDENT + "  ".join(cells).rstrip())
    return out


def line(label: str, text: str, depth: int = 1) -> str:
    pad = INDENT * depth
    return f"{pad}{label}: {text}" if label else f"{pad}{text}"


def is_finding(check: ToolResult) -> bool:
    return check.verdict not in (VERDICT_OK, VERDICT_INCONCLUSIVE)


def check_keys(checks: list[ToolResult]) -> list[str]:
    """One key per check: the tool name, numbered when a tool ran on several windows."""
    seen: dict[str, int] = {}
    keys = []
    for c in checks:
        seen[c.tool] = seen.get(c.tool, 0) + 1
        keys.append(c.tool if seen[c.tool] == 1 else f"{c.tool}#{seen[c.tool]}")
    return keys


@dataclass
class Previous:
    """What the last printed-or-not turn showed, so a turn prints only its changes."""

    verdicts: dict[str, str] = field(default_factory=dict)
    symptoms: list[str] = field(default_factory=list)
    candidates: list[tuple[str, float, bool]] = field(default_factory=list)
    had_checks: bool = False

    def update(self, poll: PollRecord) -> None:
        if poll.checks:
            self.verdicts = dict(zip(check_keys(poll.checks), (c.verdict for c in poll.checks)))
            self.had_checks = True
        self.symptoms = [s.id for s in poll.symptoms]
        self.candidates = [(c.label, c.score, c.selected) for c in poll.candidates]


# -- one turn ------------------------------------------------------------------------------------------


def changed_checks(poll: PollRecord, previous: Previous, all_checks: bool) -> list[tuple[str, ToolResult, str]]:
    """(key, check, verdict before) for checks whose verdict changed since the last turn; on the first turn with
    checks, the findings; every check with all_checks."""
    out = []
    for key, check in zip(check_keys(poll.checks), poll.checks):
        before = previous.verdicts.get(key, VERDICT_OK if previous.had_checks else "")
        if all_checks or before != check.verdict and (previous.had_checks or is_finding(check)):
            out.append((key, check, before))
    return out


def render_turn(n: int, poll: PollRecord, log: RunLog, clock: Clock, previous: Previous,
                all_checks: bool) -> list[str]:
    checks = changed_checks(poll, previous, all_checks)
    decisions = [a for a in log.actions if a.decided_at == poll.ts]
    symptom_ids = [s.id for s in poll.symptoms]
    new_symptoms = [s for s in poll.symptoms if s.id not in previous.symptoms]
    gone_symptoms = [sid for sid in previous.symptoms if sid not in symptom_ids]
    candidates_changed = [(c.label, c.score, c.selected) for c in poll.candidates] != previous.candidates
    head = f"== Turn {n} - {clock.full(poll.ts)}" + (" - final poll" if poll.final else "")
    busy = (poll.outcomes or poll.timeline or poll.actions or poll.verifications or poll.escalations or poll.llm_calls
            or checks or decisions or new_symptoms or gone_symptoms or candidates_changed
            or poll.diagnosis is not None or poll.report is not None)
    if not busy:
        standing = [f"{k} {c.verdict}" for k, c in zip(check_keys(poll.checks), poll.checks) if is_finding(c)]
        note = ("standing findings: " + ", ".join(standing)) if standing else ("all checks ok" if poll.checks else "")
        return [f"{head} - nothing new" + (f" ({note})" if note else "")]
    lines = [head + f" - agent took {poll.agent_s:.2f} s"]

    arrivals = [f"outcome {o.action_id}: {o.status.value}" + (f" ({o.note})" if o.note else "") for o in poll.outcomes]
    arrivals += [t.summary for t in poll.timeline if t.node in ("start", "triage")]
    if arrivals:
        lines.append("  arrivals:")
        lines += [line("", a) for a in arrivals]

    if checks:
        lines.append("  checks" + (":" if all_checks else " (verdict changed):"))
        rows = []
        for key, c, before in checks:
            verdict = c.verdict if not before or before == c.verdict else f"{before} -> {c.verdict}"
            rows.append([key, clock.window(c), verdict, devices(c.devices), clock.numbers(c)])
        lines += table(rows, ["tool", "window", "verdict", "devices", "numbers"])
        for key, c, _ in checks:
            if c.thresholds:
                lines.append(line(f"{key} limits", clock.limits(c), depth=2))
            if c.evidence and (is_finding(c) or all_checks):
                lines.append(line(f"{key} says", c.evidence, depth=2))
    standing = [k for k, c in zip(check_keys(poll.checks), poll.checks)
                if is_finding(c) and all(k != key for key, _, _ in checks)]
    if standing and not all_checks and checks:
        lines.append(line("standing findings", ", ".join(standing)))
    lines += [line("investigate", t.summary) for t in poll.timeline if t.node == "investigate"]

    if new_symptoms or gone_symptoms:
        lines.append("  symptoms:")
        for s in new_symptoms:
            lines.append(line("", f"+ {s.id} since {clock.hm(s.since)}" + (f": {s.summary}" if s.summary else "")))
        lines += [line("", f"- {sid} cleared") for sid in gone_symptoms]
        kept = len(symptom_ids) - len(new_symptoms)
        if kept:
            lines.append(line("", f"({kept} still active)"))

    if candidates_changed and poll.candidates:
        lines.append("  candidates (* = selected into the diagnosis):")
        for c in poll.candidates:
            mark = "*" if c.selected else " "
            explains = ", ".join(c.explains) if c.explains else "-"
            lines.append(line("", f"{mark} {c.label} score {clock.num(c.score)} explains {explains}"))
    elif candidates_changed:
        lines.append(line("candidates", "none"))
    lines += [line("rank", t.summary) for t in poll.timeline if t.node == "rank"]
    if poll.diagnosis is not None:
        d = poll.diagnosis
        causes = ", ".join(label for h in d.root_causes for label in h.labels) or "none"
        lines.append(line("diagnosis", f"{causes}; unexplained {', '.join(d.unexplained) or 'none'}; "
                                       f"confidence {clock.num(d.confidence)}"))

    consult = [t.summary for t in poll.timeline if t.node == "consult"]
    if poll.llm_calls or consult:
        lines.append("  model:")
        for k, call in enumerate(poll.llm_calls):
            verdict = "ok" if call.ok else "invalid output"
            esc = f", escalation {call.escalation}" if call.escalation else ""
            lines.append(line("", f"{call.purpose} -> {call.role.value} ({call.model}): {verdict}, "
                                  f"{call.prompt_tokens}+{call.completion_tokens} tokens, {call.latency_s:.1f} s, "
                                  f"${call.cost_usd:.4f}{esc}"))
            if k < len(poll.transcripts):
                t = poll.transcripts[k]
                lines.append(line("prompt", excerpt(t.prompt), depth=2))
                lines.append(line("reply", excerpt(t.reply), depth=2))
        lines += [line("consult", s) for s in consult]

    plan = [t.summary for t in poll.timeline if t.node == "plan"]
    if plan:
        lines.append("  plan:")
        lines += [line("", s) for s in plan]
    gate = poll.gate or gate_from_actions(poll)
    if gate:
        lines.append("  gate:")
        for g in gate:
            what = f"{g.request.action.value} on {g.request.target}"
            params = f" {g.request.params}" if g.request.params else ""
            reason = f": {g.reason}" if g.reason else ""
            cause = f" (for {g.cause})" if g.cause else ""
            lines.append(line("", f"{what}{params} -> {g.decision.value}{reason}{cause}"))
    if poll.actions:
        lines.append("  requests sent:")
        for r in poll.actions:
            flag = "needs approval" if r.needs_approval else "auto"
            lines.append(line("", f"{r.id} {r.action.value} on {r.target} ({flag}): {r.rationale}"))
            if r.expected_effect:
                lines.append(line("expected", r.expected_effect, depth=2))
    if decisions:
        lines.append("  decisions (environment):")
        for a in decisions:
            eff = f", effective {clock.hm(a.effective_at)}" if a.effective_at else ""
            note = f" ({a.note})" if a.note else ""
            lines.append(line("", f"{a.request.id} {a.request.action.value} on {a.request.target}: "
                                  f"{a.status.value if a.status else 'waiting'} by {a.decided_by}{eff}{note}"))
    if poll.verifications:
        lines.append("  verifications:")
        for v in poll.verifications:
            lines.append(line("", f"{v.action_id}: {'recovered' if v.recovered else 'NOT recovered'}; before "
                                  f"{clock.num(v.before_kw)} kW, after {clock.num(v.after_kw)} kW, expected "
                                  f"{clock.num(v.expected_kw)} kW" + (f"; {v.note}" if v.note else "")))
    if poll.escalations:
        lines.append("  escalations:")
        lines += [line("", f"{e.subject} on {', '.join(e.devices) or '-'}: {e.reason}") for e in poll.escalations]
    lines += [line(t.node, t.summary) for t in poll.timeline if t.node not in AGENT_NODES]
    if poll.report is not None:
        lines.append("  report (English; the Arabic text is in the file):")
        lines += [line("", text) for text in poll.report.summary_en.strip().splitlines() if text.strip()]
        lines.append(line("", f"cost ${poll.report.cost_usd:.4f}, {poll.report.llm_calls} model calls"))
    return lines


def gate_from_actions(poll: PollRecord) -> list[GateRecord]:
    """Older files have no gate records: the sent requests tell auto from needs-approval."""
    return [GateRecord(request=r, decision=GateDecision.NEEDS_APPROVAL if r.needs_approval else GateDecision.AUTO)
            for r in poll.actions]


def excerpt(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + f" ... [{len(flat) - limit} more characters]"


# -- the whole run -------------------------------------------------------------------------------------


def header(log: RunLog, path: Path | None, clock: Clock) -> list[str]:
    inc = log.incident
    end = inc.start + timedelta(hours=inc.duration_hours)
    lines = [f"Run: {path.name if path else '(in memory)'} - {log.label}",
             f"Agent {log.agent}" + (f", model {log.model}" if log.model else " (rules only, no model)")
             + f", mode {log.mode.value}, format v{log.version}, recorded {clock.full(log.recorded_at)}",
             f"Incident {inc.id}: {clock.full(inc.start)} to {clock.hm(end)}, {inc.duration_hours:g} h, "
             f"polled every {log.poll_min} min, {len(log.polls)} turns"
             + ("" if log.finished else " (run not finished)"),
             "The agent saw only the incident window above and the telemetry database."]
    if not any(p.checks for p in log.polls):
        lines.append("(An older file: no per-turn checks, symptoms, candidates or gate records.)")
    return lines


def footer(log: RunLog, clock: Clock) -> list[str]:
    if log.answer_key is None and log.bench is None:
        return []
    lines = ["", "== After the run: what the agent never saw =="]
    if log.answer_key is not None:
        lines.append("ANSWER KEY (bench truth):")
        for f in log.answer_key.faults:
            spec = f"{f.type.value} on {f.target} from {f.start} UTC" + (f" to {f.end} UTC" if f.end else "")
            spec += f", severity {f.severity}" if f.severity is not None else ""
            lines.append(line("", spec + ("" if f.label in log.answer_key.root_causes else " (decoy, not a cause)")))
        lines.append(line("root causes", ", ".join(log.answer_key.root_causes)))
        final = log.diagnosis_until(log.polls[-1].ts) if log.polls else None
        named = sorted({label for h in final.root_causes for label in h.labels}) if final else []
        lines.append(line("final diagnosis", ", ".join(named) or "none"))
        missed = [c for c in log.answer_key.root_causes if c not in named]
        extra = [c for c in named if c not in log.answer_key.root_causes]
        lines.append(line("missed", ", ".join(missed) or "none"))
        lines.append(line("named but not planted", ", ".join(extra) or "none"))
    if log.bench is not None:
        b = log.bench
        lines.append("BENCH TRUTH (from the twin):")
        if b.control:
            lines.append(line("", "this is the neutral-text control run of an injected-instruction scenario"))
        first: dict[str, datetime | None] = {}
        for m in b.polls:
            for label, ok in m.mitigated.items():
                first.setdefault(label, None)
                if ok and first[label] is None:
                    first[label] = m.ts
                elif not ok:
                    first[label] = None  # a mitigation that did not hold starts over
        for label, ts in first.items():
            lines.append(line("", f"{label}: " + (f"mitigated from {clock.hm(ts)}" if ts else "not mitigated at the end")))
        if b.energy_mwh is not None:
            lines.append(line("energy", f"this run {b.energy_mwh:.1f} MWh; no action {clock.num(b.no_action_mwh)} MWh; "
                                        f"no real fault {clock.num(b.decoy_only_mwh)} MWh"))
        if b.storms:
            lines.append(line("dust storms", ", ".join(f"{clock.hm(s)}-{clock.hm(e)}" for s, e in b.storms)))
    return lines


def render(log: RunLog, *, path: Path | None = None, turn: int | None = None, start: time | None = None,
           all_checks: bool = False, utc: bool = False) -> str:
    clock = Clock(utc)
    lines = header(log, path, clock)
    previous = Previous()
    shown = 0
    for n, poll in enumerate(log.polls, start=1):
        wanted = (turn is None or n == turn) and (start is None or clock.local(poll.ts).time() >= start)
        if wanted:
            lines.append("")
            lines += render_turn(n, poll, log, clock, previous, all_checks)
            shown += 1
        previous.update(poll)
    if turn is not None and shown == 0:
        lines += ["", f"(no turn {turn}: the run has {len(log.polls)} turns)"]
    if turn is None:
        lines += footer(log, clock)
    return "\n".join(lines) + "\n"


def parse_hhmm(text: str) -> time:
    try:
        hour, minute = text.split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not HH:MM") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="a trajectory or replay file (.json or .json.gz)")
    parser.add_argument("--turn", type=int, default=None, help="print this turn only (1-based)")
    parser.add_argument("--from", dest="start", type=parse_hhmm, default=None,
                        help="print turns from this time of day (HH:MM, Riyadh time; UTC with --utc)")
    parser.add_argument("--all-checks", action="store_true", help="every check at every turn, not only changes")
    parser.add_argument("--utc", action="store_true", help="show UTC times instead of Asia/Riyadh")
    args = parser.parse_args(argv)
    log = RunLog.load(args.file)
    text = render(log, path=args.file, turn=args.turn, start=args.start, all_checks=args.all_checks, utc=args.utc)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # the report may carry non-ASCII text
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
