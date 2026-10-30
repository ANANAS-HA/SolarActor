"""The Raqeeb agent: a LangGraph flow run once per environment poll.

    triage -> investigate -> rank -> consult -> plan -> safety -> execute -> verify -> report
                                                 ^                              |
                                                 +---- a cause was reopened ----+  (once per poll)

RaqeebAgent implements contracts.Agent: the environment calls start(view, telemetry) once, then
step(now, outcomes, final) every poll. The agent sees only the AgentScenario view and the
telemetry store. Without a router it is rules only (consult does nothing); with one
(raqeeb.llm.router.Router) consult asks the model when the rules are stuck, and the report is
written by the model at the end (see agent/consult.py).
"""

from __future__ import annotations

from datetime import datetime

from langgraph.graph import END, START, StateGraph

from raqeeb.agent.consult import consult
from raqeeb.agent.execute import execute
from raqeeb.agent.investigate import investigate
from raqeeb.agent.plan import plan
from raqeeb.agent.rank import rank
from raqeeb.agent.report import is_done, report
from raqeeb.agent.safety import safety
from raqeeb.agent.state import Memory, PollState
from raqeeb.agent.triage import triage
from raqeeb.agent.verify import verify
from raqeeb.config import load_actions, load_alarm_codes, load_plant
from raqeeb.contracts import ActionOutcome, AgentScenario, AgentTurn, FaultType, PlantConfig, TelemetryReader


def _after_verify(state: PollState) -> str:
    return "plan" if state.get("reopened") and not state.get("replanned") else "report"


def _mark_replanned(state: PollState) -> PollState:
    return {"replanned": True} if state.get("reopened") else {}


def build_graph():
    graph = StateGraph(PollState)
    graph.add_node("triage", triage)
    graph.add_node("investigate", investigate)
    graph.add_node("rank", rank)
    graph.add_node("consult", consult)
    graph.add_node("plan", lambda s: {**plan(s), **_mark_replanned(s)})
    graph.add_node("safety", safety)
    graph.add_node("execute", execute)
    graph.add_node("verify", verify)
    graph.add_node("report", report)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "investigate")
    graph.add_edge("investigate", "rank")
    graph.add_edge("rank", "consult")
    graph.add_edge("consult", "plan")
    graph.add_edge("plan", "safety")
    graph.add_edge("safety", "execute")
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges("verify", _after_verify, {"plan": "plan", "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()


GRAPH = build_graph()
NODES = ["triage", "investigate", "rank", "consult", "plan", "safety", "execute", "verify", "report"]


class RaqeebAgent:
    def __init__(self, plant: PlantConfig | None = None, router=None, name: str = "raqeeb",
                 disabled: frozenset[FaultType] = frozenset()):
        """`router`: a raqeeb.llm.router.Router for the model-assisted variants; None = rules only.
        `disabled`: fault types whose rules are switched off (the ablation set's stand-in for unknown faults)."""
        self.plant = plant or load_plant()
        self.router = router
        self.disabled = frozenset(disabled)
        self.name = name
        self.memory: Memory | None = None

    def start(self, view: AgentScenario, telemetry: TelemetryReader) -> None:
        # The tools need the full TelemetryStore read helpers, not only TelemetryReader.read
        for helper in ("meter", "inverter_wide", "inverter_readings", "weather_station", "span"):
            if not hasattr(telemetry, helper):
                raise TypeError(f"Raqeeb needs a TelemetryStore (missing {helper}()); got {type(telemetry).__name__}")
        self.memory = Memory(view=view, telemetry=telemetry, plant=self.plant, action_specs=load_actions(),
                             alarm_codes=load_alarm_codes(), router=self.router, disabled=self.disabled)
        self.memory.log(view.start, "start", f"watching incident window {view.id} ({view.duration_hours:g} h)")

    def step(self, now: datetime, outcomes: list[ActionOutcome], final: bool = False) -> AgentTurn:
        if self.memory is None:
            raise RuntimeError("call start() first")
        memory = self.memory
        if hasattr(memory.telemetry, "set_clock"):
            memory.telemetry.set_clock(now)  # never read ahead of the simulated time, whatever the store holds
        report_before, escalated_before, calls_before = memory.report, len(memory.escalations), len(memory.llm_calls)
        state = GRAPH.invoke({"memory": memory, "now": now, "final": final, "outcomes": list(outcomes),
                              "actions": [], "verifications": [], "reopened": [], "replanned": False})
        return AgentTurn(
            diagnosis=memory.diagnosis if state.get("diagnosis_changed") else None,
            llm_calls=memory.llm_calls[calls_before:],
            actions=state.get("actions", []),
            verifications=state.get("verifications", []),
            escalations=memory.escalations[escalated_before:],
            report=memory.report if memory.report is not report_before else None,
            done=is_done(memory),
        )
