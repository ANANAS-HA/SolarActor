"""Console runs: the real agent on a live plant, or a recorded run played back.

    run = LiveRun(scenario, Mode.RULES)     # twin + injector, a per-run SQLite file, RaqeebAgent
    run.advance(60)                          # sim minutes; stops early when an action waits for approval
    run.decide(action_id, approve=True, note="")
    run.log.save(path)                       # replay JSON (ui/runlog.py), answer key included once finished

    replay = ReplayRun(RunLog.load(path))    # same interface; plays the recorded polls back
    replay.advance(60)

LiveRun is the agent's environment, like bench/harness.run_scenario: it polls the agent every
`poll_min` sim minutes with only scenario.agent_view() and the telemetry store, gates the requested
actions with configs/actions.yaml, runs allowed ones with twin.controls.apply_action and passes the
outcomes back on the next poll. Unlike the bench, a gated action waits for an operator decision:
the run pauses until decide() is called. Actions, decisions and verifications go to the audit table.
The planted scenario stays inside LiveRun; its faults are copied into the log only when the run ends.
"""

from __future__ import annotations

import os
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from raqeeb.agent import RaqeebAgent
from raqeeb.config import load_actions, load_plant, load_settings
from raqeeb.contracts import (
    STEP_MINUTES,
    ActionOutcome,
    ActionRequest,
    ActionStatus,
    ActionType,
    AgentScenario,
    PlantConfig,
    Scenario,
    ToolResult,
)
from raqeeb.faults.injector import FaultInjector
from raqeeb.telemetry.store import TelemetryStore
from raqeeb.twin.controls import apply_action
from raqeeb.ui.runlog import (
    MODE_LABEL,
    RECORDED_LABEL,
    ActionEntry,
    AnswerKey,
    Mode,
    PollRecord,
    RunLog,
    TimelineItem,
    dump_telemetry,
    load_telemetry,
    plain,
)

POLL_MIN = 15  # as the bench harness
RUN_DIR_ENV = "RAQEEB_UI_RUN_DIR"  # where per-run SQLite files go (default: the system temp folder)
NO_LLM_ENV = "RAQEEB_UI_NO_LLM"  # set (e.g. in the public deployment) to disable the local-model mode
REACH_TIMEOUT_S = 2.0
OPERATOR = "operator"


# -- modes -----------------------------------------------------------------------


def local_model_status() -> tuple[bool, str]:
    """(available, reason). The model server is contacted only if .env names one and the deployment allows it."""
    if os.environ.get(NO_LLM_ENV):
        return False, "disabled in this deployment (rules only and replays)"
    settings = load_settings()
    if not settings.local_llm_configured:
        return False, "LOCAL_LLM_BASE_URL is not set in .env"
    try:
        with urllib.request.urlopen(settings.local_llm_base_url.rstrip("/") + "/models", timeout=REACH_TIMEOUT_S):
            return True, f"local model {settings.local_llm_model}"
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False, "the local model server is not reachable"


def make_router(mode: Mode):
    """None for rules only; the local model (no API fallback) or the scripted test model otherwise."""
    if mode == Mode.RULES:
        return None
    if mode == Mode.TEST:
        from raqeeb.ui.testmodel import scripted_router
        return scripted_router()
    from raqeeb.llm.router import Router
    return Router(api=False)


def model_name(mode: Mode) -> str:
    if mode == Mode.TEST:
        from raqeeb.ui.testmodel import TEST_MODEL
        return TEST_MODEL
    return load_settings().local_llm_model if mode == Mode.LOCAL else ""


def run_dir() -> Path:
    return Path(os.environ.get(RUN_DIR_ENV) or Path(tempfile.gettempdir()) / "raqeeb_ui_runs")


# -- live ------------------------------------------------------------------------


class LiveRun:
    def __init__(self, scenario: Scenario, mode: Mode = Mode.RULES, *, router=None, poll_min: int = POLL_MIN,
                 plant: PlantConfig | None = None, db_path: Path | None = None):
        if poll_min < STEP_MINUTES or poll_min % STEP_MINUTES:
            raise ValueError(f"poll_min must be a multiple of {STEP_MINUTES}")
        self.plant = plant or load_plant()
        self._scenario = scenario  # injector side only: never handed to the agent, never shown before the end
        self._inj = FaultInjector.for_scenario(scenario, self.plant)
        self._owns_db = db_path is None  # a temp file of ours: deleted on close()
        self.db_path = db_path or run_dir() / f"{scenario.id}_{uuid.uuid4().hex[:8]}.db"
        self.store = TelemetryStore(self.db_path)
        self.poll_min = poll_min
        self.mode = mode
        self.specs = load_actions()
        router = router if router is not None else make_router(mode)
        self.agent = RaqeebAgent(self.plant, router=router, name=f"raqeeb_{mode.value}")
        self.incident: AgentScenario = scenario.agent_view()
        self.log = RunLog(label=MODE_LABEL[mode], mode=mode, agent=self.agent.name, model=model_name(mode),
                          poll_min=poll_min, incident=self.incident)
        self._outcomes: list[ActionOutcome] = []
        self._timeline_seen = 0
        self._last_findings: list[dict] | None = None
        self.delays = {ActionType.CREATE_CLEANING_WORK_ORDER: self.plant.controls.cleaning_delay_min,
                       ActionType.RESET_COMMS_GATEWAY: self.plant.controls.comms_reset_delay_min,
                       ActionType.DISPATCH_TECHNICIAN: self.plant.controls.technician_delay_min}
        self.agent.start(self.incident, self.store)  # its "start" timeline entry goes into the first poll

    # -- state ----------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._inj.twin.clock

    @property
    def polled_out(self) -> bool:
        """The agent has had its final poll."""
        return bool(self.log.polls) and self.log.polls[-1].final

    @property
    def waiting(self) -> list[ActionEntry]:
        return [a for a in self.log.actions if a.status is None]

    @property
    def finished(self) -> bool:
        return self.polled_out and not self.waiting

    # -- driving --------------------------------------------------------------------

    def advance(self, minutes: int) -> int:
        """Poll the agent until `minutes` of sim time have passed, an action waits for approval, or the
        run ends. Returns the number of polls made."""
        polls, target = 0, self.now + timedelta(minutes=minutes)
        while not self.polled_out and not self.waiting and self.now < target:
            self.poll()
            polls += 1
        return polls

    def run_to_end(self, approve: bool | None = None, note: str = "") -> None:
        """Run to the end. `approve` decides every gated action (None: stop at the first one)."""
        while not self.finished:
            if self.waiting:
                if approve is None:
                    return
                for entry in self.waiting:
                    self.decide(entry.request.id, approve, note)
            else:
                self.advance(24 * 60)

    def poll(self) -> PollRecord:
        if self.polled_out:
            raise RuntimeError("the run has ended")
        self.store.write_batch(self._inj.advance(self.poll_min))
        now, final = self.now, self._inj.finished
        outcomes, self._outcomes = self._outcomes, []
        started = time.perf_counter()
        turn = self.agent.step(now, outcomes, final=final)
        record = PollRecord(ts=now, final=final, outcomes=outcomes, diagnosis=turn.diagnosis, actions=turn.actions,
                            verifications=turn.verifications, escalations=turn.escalations,
                            llm_calls=turn.llm_calls, report=turn.report, agent_s=time.perf_counter() - started)
        record.timeline = self._take_timeline()
        record.findings = self._take_findings()
        self.log.polls.append(record)
        for v in turn.verifications:
            self._audit("verify", "agent", v.model_dump(mode="json"))
        for e in turn.escalations:
            self._audit("escalate", "agent", e.model_dump(mode="json"))
        for request in turn.actions:
            self._gate(request)
        if self.finished:
            self._close()
        return record

    def decide(self, action_id: str, approve: bool, note: str = "") -> ActionEntry:
        entry = next((a for a in self.log.actions if a.request.id == action_id and a.status is None), None)
        if entry is None:
            raise KeyError(f"no action {action_id!r} is waiting for a decision")
        self._audit("approval", f"human:{OPERATOR}", {"action_id": action_id, "approved": approve, "note": note})
        if approve:
            self._execute(entry, by=OPERATOR, note=note)
        else:
            self._settle(entry, ActionStatus.REJECTED, by=OPERATOR, note=note or "approval refused")
        if self.finished:
            self._close()
        return entry

    # -- actions --------------------------------------------------------------------

    def _gate(self, request: ActionRequest) -> None:
        spec = self.specs.get(request.action)
        entry = ActionEntry(request=request, requested_at=self.now, gated=bool(spec and spec.needs_approval))
        self.log.actions.append(entry)
        self._audit("request", "agent", request.model_dump(mode="json"))
        if any(a.request.id == request.id for a in self.log.actions[:-1]):
            self._settle(entry, ActionStatus.FAILED, by="console", note=f"duplicate action id {request.id!r}")
        elif spec is None:
            self._settle(entry, ActionStatus.FAILED, by="console", note="not in the action allowlist")
        elif entry.gated:
            self._audit("approval", "console", {"action_id": request.id, "waiting": True,
                                                "rationale": request.rationale,
                                                "expected_effect": request.expected_effect})
        else:
            self._execute(entry, by="auto")

    def _execute(self, entry: ActionEntry, by: str, note: str = "") -> None:
        request = entry.request
        try:
            apply_action(self._inj.twin, request.action, request.target, request.params)
        except (ValueError, TypeError, KeyError) as exc:
            self._settle(entry, ActionStatus.FAILED, by=by, note=str(exc))
            return
        self._settle(entry, ActionStatus.APPLIED, by=by, note=note,
                     effective_at=self.now + timedelta(minutes=self.delays.get(request.action, 0)))

    def _settle(self, entry: ActionEntry, status: ActionStatus, by: str, note: str = "",
                effective_at: datetime | None = None) -> None:
        entry.status, entry.decided_by, entry.decided_at, entry.note = status, by, self.now, note
        entry.effective_at = effective_at
        self._outcomes.append(ActionOutcome(action_id=entry.request.id, status=status, ts=self.now, note=note))
        self._audit("execute", "console", {"action_id": entry.request.id, "action": entry.request.action.value,
                                           "target": entry.request.target, "status": status.value, "note": note})

    def _audit(self, step: str, actor: str, payload: dict) -> None:
        self.store.write_audit(step, actor, plain(payload), self.now)

    # -- what the agent did ---------------------------------------------------------

    def _take_timeline(self) -> list[TimelineItem]:
        memory = getattr(self.agent, "memory", None)
        if memory is None:
            return []
        new = memory.timeline[self._timeline_seen:]
        self._timeline_seen = len(memory.timeline)
        return [TimelineItem(ts=e.ts, node=e.node, summary=e.summary, details=plain(e.details)) for e in new]

    def _take_findings(self) -> list[ToolResult] | None:
        memory = getattr(self.agent, "memory", None)
        findings = [r for r in (memory.results.values() if memory else []) if r.finding]
        dumped = [r.model_dump(mode="json") for r in findings]
        if dumped == self._last_findings:
            return None
        self._last_findings = dumped
        return findings

    def _close(self) -> None:
        """End of run: the answer key goes into the log (shown only now) and the telemetry is attached."""
        if self.log.finished:
            return
        s = self._scenario
        key = s.answer_key or s.expected_answer_key()
        self.log.answer_key = AnswerKey(faults=s.faults, root_causes=list(key.root_causes))
        self.log.finished = True
        self.log.telemetry = dump_telemetry(self.store)

    def recording(self, label: str | None = None) -> RunLog:
        """The replay log of a finished run, labelled for playback (e.g. "recorded with test model")."""
        if not self.finished:
            raise RuntimeError("only a finished run can be saved as a replay")
        return self.log.model_copy(update={"label": label or RECORDED_LABEL[self.mode]})

    def close(self) -> None:
        self.store.close()
        if self._owns_db:
            self.db_path.unlink(missing_ok=True)


# -- replay ----------------------------------------------------------------------


class ReplayRun:
    """A recorded run played back poll by poll. Telemetry is loaded into an in-memory store and cut at `now`."""

    def __init__(self, log: RunLog):
        self.log = log
        self.incident = log.incident
        self.mode = log.mode
        self.store = load_telemetry(log.telemetry)
        self.cursor = 0  # polls shown

    @property
    def now(self) -> datetime:
        return self.log.polls[self.cursor - 1].ts if self.cursor else self.incident.start

    @property
    def waiting(self) -> list[ActionEntry]:
        return []  # a replay shows the recorded decisions

    @property
    def finished(self) -> bool:
        return self.cursor >= len(self.log.polls)

    def advance(self, minutes: int) -> int:
        target, before = self.now + timedelta(minutes=minutes), self.cursor
        while not self.finished and self.log.polls[self.cursor].ts <= target:
            self.cursor += 1
        return self.cursor - before

    def run_to_end(self, approve: bool | None = None, note: str = "") -> None:
        self.cursor = len(self.log.polls)

    def close(self) -> None:
        self.store.close()
