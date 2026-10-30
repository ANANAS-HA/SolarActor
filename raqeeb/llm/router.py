"""Router: rules first, the local model next, the API model only when needed (HANDOFF section 10).

    router = Router()                                       # Gateway() from .env, load_plant()
    hypotheses, calls = router.propose_hypotheses(results, symptoms, ranked, unexplained)
    text, calls = router.explain_diagnosis(diagnosis, symptoms, results)
    report, calls = router.write_shift_report(draft_report, notes)

1. Triage, grouping, numbers, ranking, planning, safety, verification: no LLM (see RULES_ONLY).
2. Hypothesis proposals, explanations and the shift report (English + Arabic): local.
3. Hypotheses go straight to the API model when the rules' top two competing candidates score within
   TOP2_MARGIN, or more than UNEXPLAINED_MAX symptoms are unexplained. When the rules explain every
   symptom with no near tie, no model is called at all.
4. Every reply is parsed into a Pydantic model and checked (known fault types, targets and symptom
   ids; no numbers that are not in the data). An invalid reply is retried once on the same model;
   a second failure on the local model escalates to the API model.
5. If the local server is down, the call goes to the API model (escalation "local_down") and the
   local model is skipped for LOCAL_RETRY_AFTER_S. If no model gives a valid reply, the methods
   return an empty / None result and the agent keeps its rule-based output.

Every method returns the LlmCalls it made (also kept in `router.gateway.calls`), for AgentTurn.llm_calls.
The model never computes numbers: hypotheses come back with score 0 for the agent's ranker to score.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from raqeeb.config import load_plant
from raqeeb.contracts import (
    DECOY_FAULTS,
    PLANT_TARGET,
    Diagnosis,
    FaultType,
    Hypothesis,
    IncidentReport,
    LlmCall,
    LlmRole,
    PlantConfig,
    Symptom,
    ToolResult,
)
from raqeeb.llm.gateway import Gateway, LlmError, LlmUnavailable
from raqeeb.llm.prompting import data_block, extract_json, plain_json, render, unsupported_numbers

log = logging.getLogger("raqeeb.llm")

T = TypeVar("T")

TOP2_MARGIN = 0.25  # rank.py score units: two competing candidates closer than this are a near tie
UNEXPLAINED_MAX = 2  # more unexplained symptoms than this: the hard case goes to the API model
MAX_HYPOTHESES = 6
REPORT_MAX_WORDS = 200  # per language
ATTEMPTS = 2  # per model: one try plus one retry on invalid output
LOCAL_RETRY_AFTER_S = 120.0  # after the local server failed to answer, skip it for this long (wall clock)
ARABIC_SHARE = 0.5  # the Arabic report: at least this share of its letters must be Arabic

RULES_ONLY = frozenset({"triage", "grouping", "investigate", "rank", "plan", "safety", "execute", "verify"})


class Purpose(StrEnum):
    HYPOTHESES = "hypotheses"
    EXPLAIN = "explain"
    REPORT = "report"


# The model may propose any fault type except decoys; curtailment stays proposable because it is how
# a plant-wide cap under a grid limit gets explained (the agent then marks it expected, never acts)
PROPOSABLE = tuple(f for f in FaultType if f not in DECOY_FAULTS or f == FaultType.CURTAILMENT)
_PLANT_WIDE = {FaultType.SOILING, FaultType.CURTAILMENT}
_STATION = {FaultType.SENSOR_SOILING}
_INVERTER = {FaultType.INVERTER_DERATE, FaultType.TRACKER_STUCK, FaultType.STRING_OPEN, FaultType.UNAUTHORIZED_CMD}

FOOTER_EN = "Written by a language model ({role}) from the agent's records; numbers come from code. Plant data is synthetic."
FOOTER_AR = "كتبه نموذج لغوي ({role}) من سجلات الوكيل، والأرقام محسوبة برمجياً. بيانات المحطة اصطناعية."
_ARABIC_LETTER = re.compile(r"[؀-ۿ]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")


def route(purpose: str) -> LlmRole | None:
    """Default role for a step: None for rules-only steps, local for the language steps."""
    return None if purpose in RULES_ONLY else LlmRole.LOCAL


def near_tie(ranked: Sequence[Hypothesis], margin: float = TOP2_MARGIN) -> bool:
    """The two best candidates compete for the same symptoms and score within `margin`.
    Candidates that explain disjoint symptoms are complementary causes, not a tie."""
    if len(ranked) < 2:
        return False
    a, b = sorted(ranked, key=lambda h: h.score, reverse=True)[:2]
    competing = not a.explains or not b.explains or bool(set(a.explains) & set(b.explains))
    return competing and a.score - b.score <= margin


def route_hypotheses(results: Sequence[ToolResult], ranked: Sequence[Hypothesis] = (),
                     unexplained: Sequence[str] = (), *, margin: float = TOP2_MARGIN,
                     unexplained_max: int = UNEXPLAINED_MAX) -> tuple[LlmRole | None, str]:
    """(role, reason) for a hypothesis proposal; role None = the rules suffice, call no model."""
    if near_tie(ranked, margin):
        return LlmRole.API, "top2_margin"
    if len(unexplained) > unexplained_max:
        return LlmRole.API, "unexplained"
    if ranked and not unexplained:
        return None, "rules_explain_all"
    if not ranked and not any(r.finding for r in results):
        return None, "no_findings"
    return LlmRole.LOCAL, ""


# --- reply schemas (private to the LLM layer; the agent gets contract types) --------------------


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ProposedHypothesis(_Reply):
    fault_type: FaultType
    targets: list[str] = Field(min_length=1)
    explains: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(min_length=1)


class _Proposals(_Reply):
    hypotheses: list[_ProposedHypothesis] = Field(max_length=MAX_HYPOTHESES)


class _Explanation(_Reply):
    explanation: str = Field(min_length=1)


class _ShiftReport(_Reply):
    summary_en: str = Field(min_length=1)
    summary_ar: str = Field(min_length=1)


def _check_numbers(texts: Sequence[str], sources: str) -> None:
    bad = [n for text in texts for n in unsupported_numbers(text, sources)]
    if bad:
        raise ValueError(f"numbers not in the data: {sorted(set(bad))[:8]}; use only numbers from the data")


def _compact(results: Sequence[ToolResult]) -> list[dict]:
    """Findings in full; tools that found nothing as one line each (keeps the local prompt small)."""
    return [r.model_dump(mode="json") if r.finding else {"tool": r.tool, "verdict": r.verdict, "evidence": r.evidence}
            for r in results]


class Router:
    def __init__(self, gateway: Gateway | None = None, plant: PlantConfig | None = None, *,
                 margin: float = TOP2_MARGIN, unexplained_max: int = UNEXPLAINED_MAX):
        self.gateway = gateway or Gateway()
        self.plant = plant or load_plant()
        self.margin = margin
        self.unexplained_max = unexplained_max
        self._local_down_until = 0.0

    @property
    def calls(self) -> list[LlmCall]:
        return self.gateway.calls

    # --- (a) hypotheses ---------------------------------------------------------------------

    def propose_hypotheses(self, results: Sequence[ToolResult], symptoms: Sequence[Symptom] = (),
                           ranked: Sequence[Hypothesis] = (), unexplained: Sequence[str] = (), *,
                           force: bool = False) -> tuple[list[Hypothesis], list[LlmCall]]:
        """Root-cause hypotheses proposed by a model from the tool results (ids "llm-1", ..., score 0,
        most likely first). `ranked` = the rules' candidates with rank.py scores, `unexplained` = the
        symptom ids they leave unexplained. Returns ([], []) without a call when the rules suffice
        (unless `force`), and ([], calls) when no model gave a valid reply."""
        role, reason = route_hypotheses(results, ranked, unexplained, margin=self.margin,
                                        unexplained_max=self.unexplained_max)
        if role is None and not force:
            log.debug("hypotheses: no LLM (%s)", reason)
            return [], []
        role = role or LlmRole.LOCAL
        payload = {"tool_results": _compact(results), "symptoms": [s.model_dump(mode="json") for s in symptoms],
                   "ranked": [h.model_dump(mode="json") for h in ranked], "unexplained": list(unexplained)}
        messages = render(
            Purpose.HYPOTHESES, fault_types=", ".join(PROPOSABLE), targets=", ".join(self._targets()),
            max_hypotheses=str(MAX_HYPOTHESES),
            **{key: data_block(key, value) for key, value in payload.items()})
        symptom_ids = {s.id for s in symptoms} | set(unexplained) | {i for h in ranked for i in h.explains}
        sources = plain_json(payload)

        def parse(text: str) -> list[Hypothesis]:
            return self._hypotheses(_Proposals.model_validate_json(extract_json(text)), symptom_ids, sources)

        value, calls = self._run(Purpose.HYPOTHESES, messages, parse, role, reason)
        return value or [], calls

    def _targets(self) -> list[str]:
        return [PLANT_TARGET, self.plant.weather_station_id, *self.plant.inverters.ids]

    def _valid_target(self, fault: FaultType, target: str) -> bool:
        inverters, station = set(self.plant.inverters.ids), self.plant.weather_station_id
        if fault in _PLANT_WIDE:
            return target == PLANT_TARGET
        if fault in _STATION:
            return target == station
        if fault in _INVERTER:
            return target in inverters
        return target in inverters or target == station  # comms dropout: any device that reports

    def _hypotheses(self, proposals: _Proposals, symptom_ids: set[str], sources: str) -> list[Hypothesis]:
        out: list[Hypothesis] = []
        seen: set[tuple[FaultType, tuple[str, ...]]] = set()
        for p in proposals.hypotheses:
            if p.fault_type not in PROPOSABLE:
                raise ValueError(f"{p.fault_type.value} cannot be proposed as a cause")
            bad = [t for t in p.targets if not self._valid_target(p.fault_type, t)]
            if bad:
                raise ValueError(f"invalid targets for {p.fault_type.value}: {bad}")
            unknown = [i for i in p.explains if i not in symptom_ids]
            if unknown:
                raise ValueError(f"unknown symptom ids in explains: {unknown[:5]}")
            _check_numbers(p.evidence, sources)
            key = (p.fault_type, tuple(sorted(p.targets)))
            if key in seen:
                continue
            seen.add(key)
            out.append(Hypothesis(id=f"llm-{len(out) + 1}", fault_type=p.fault_type, targets=p.targets,
                                  explains=p.explains, evidence=p.evidence, score=0.0))
        return out

    # --- (b) explanation --------------------------------------------------------------------

    def explain_diagnosis(self, diagnosis: Diagnosis, symptoms: Sequence[Symptom] = (),
                          results: Sequence[ToolResult] = ()) -> tuple[str | None, list[LlmCall]]:
        """A plain-English explanation of `diagnosis` for operators; None if no model gave a valid reply."""
        payload = {"diagnosis": diagnosis.model_dump(mode="json"),
                   "symptoms": [s.model_dump(mode="json") for s in symptoms], "tool_results": _compact(results)}
        messages = render(Purpose.EXPLAIN, fault_types=", ".join(PROPOSABLE),
                          **{key: data_block(key, value) for key, value in payload.items()})
        sources = plain_json(payload)

        def parse(text: str) -> str:
            explanation = _Explanation.model_validate_json(extract_json(text)).explanation.strip()
            _check_numbers([explanation], sources)
            return explanation

        return self._run(Purpose.EXPLAIN, messages, parse, LlmRole.LOCAL, "")

    # --- (c) shift handover report ----------------------------------------------------------

    def write_shift_report(self, draft: IncidentReport, notes: Sequence[str] = ()) -> tuple[IncidentReport, list[LlmCall]]:
        """The shift report in English and Arabic, written by a model from the agent's own draft (its
        causes, actions, verifications and template summary, times already in Riyadh time). Returns
        the draft with new summaries (a code-written footer says a model wrote them and that the data
        is synthetic) and its cost_usd / llm_calls increased by these calls. If no model gives a valid
        reply, the draft's summaries are kept."""
        records = draft.model_dump(mode="json", exclude={"summary_ar", "cost_usd", "llm_calls"})
        messages = render(Purpose.REPORT, max_words=str(REPORT_MAX_WORDS), records=data_block("records", records),
                          notes=data_block("notes", list(notes)))
        sources = plain_json({"records": records, "notes": list(notes)})

        def parse(text: str) -> _ShiftReport:
            report = _ShiftReport.model_validate_json(extract_json(text))
            arabic = len(_ARABIC_LETTER.findall(report.summary_ar))
            latin = len(_LATIN_LETTER.findall(report.summary_ar))
            if arabic < ARABIC_SHARE * (arabic + latin):
                raise ValueError("summary_ar must be written in Arabic")
            _check_numbers([report.summary_en, report.summary_ar], sources)
            return report

        value, calls = self._run(Purpose.REPORT, messages, parse, LlmRole.LOCAL, "")
        update: dict = {"cost_usd": draft.cost_usd + sum(c.cost_usd for c in calls),
                        "llm_calls": draft.llm_calls + len(calls)}
        if value is not None:
            role = next(c.role for c in reversed(calls) if c.ok).value
            update |= {"summary_en": f"{value.summary_en.strip()}\n\n{FOOTER_EN.format(role=role)}",
                       "summary_ar": f"{value.summary_ar.strip()}\n\n{FOOTER_AR.format(role=role)}"}
        return draft.model_copy(update=update), calls

    # --- routing core -----------------------------------------------------------------------

    def _run(self, purpose: str, messages: list[dict[str, str]], parse: Callable[[str], T], role: LlmRole,
             escalation: str) -> tuple[T | None, list[LlmCall]]:
        """Try `role` (local escalates to api when down or invalid twice); returns (value, calls made)."""
        first = len(self.gateway.calls)
        value: T | None = None
        if role == LlmRole.LOCAL:
            if time.monotonic() < self._local_down_until:
                role, escalation = LlmRole.API, "local_down"
            else:
                try:
                    value = self._attempts(LlmRole.LOCAL, purpose, messages, parse, escalation)
                except LlmUnavailable as exc:
                    log.warning("local LLM unavailable (%s); falling back to api", exc.reason)
                    self._local_down_until = time.monotonic() + LOCAL_RETRY_AFTER_S
                    role, escalation = LlmRole.API, "local_down"
                else:
                    if value is None:
                        role, escalation = LlmRole.API, "invalid_output"
        if role == LlmRole.API and value is None:
            try:
                value = self._attempts(LlmRole.API, purpose, messages, parse, escalation)
            except LlmUnavailable as exc:
                log.warning("api LLM unavailable (%s); keeping the rule-based result", exc.reason)
        return value, self.gateway.calls[first:]

    def _attempts(self, role: LlmRole, purpose: str, messages: list[dict[str, str]], parse: Callable[[str], T],
                  escalation: str) -> T | None:
        """Up to ATTEMPTS calls on one model, feeding the validation error back after a bad reply.
        Raises LlmUnavailable if the model cannot be reached."""
        msgs = list(messages)
        for _ in range(ATTEMPTS):
            try:
                text, call = self.gateway.complete(role, purpose, msgs, json_mode=True, escalation=escalation)
            except LlmUnavailable:
                raise
            except LlmError as exc:
                log.warning("%s call failed (%s)", role.value, exc.reason)
                continue
            try:
                return parse(text)
            except ValueError as exc:  # pydantic ValidationError is a ValueError
                call.ok = False
                log.info("llm output invalid role=%s purpose=%s: %s", role.value, purpose, str(exc)[:200])
                msgs = [*msgs, {"role": "assistant", "content": text},
                        {"role": "user", "content": f"That reply was not valid: {str(exc)[:500]}\n"
                                                    "Reply again with only the JSON object the instructions describe."}]
        return None
