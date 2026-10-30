"""Answer-leak checks for anything the agent can see (agent inputs, tool outputs, prompts).

    from tests.leakcheck import assert_no_answer_leak
    assert_no_answer_leak(tool_output, scenario)   # in any test of an agent-facing tool

Fails if the object contains a planted-fault object, the twin's truth log or twin state, an
answer-key / fault field, or a root-cause label of the scenario. Apply it to what tools and the
environment give the agent, not to the agent's own conclusions (those name causes by design).
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import pandas as pd
from pydantic import BaseModel

from raqeeb.contracts import AnswerKey, FaultSpec, InverterState, PlantState, Scenario
from raqeeb.faults.catalogue import Injection
from raqeeb.twin.simulator import StepTruth

FORBIDDEN_TYPES = (Scenario, FaultSpec, AnswerKey, PlantState, InverterState, StepTruth, Injection)
FORBIDDEN_KEYS = {"faults", "answer_key", "severity", "seed", "truth"}


class AnswerLeak(AssertionError):
    pass


def _encode(value: Any) -> Any:
    if isinstance(value, FORBIDDEN_TYPES):
        raise AnswerLeak(f"agent-facing data contains a {type(value).__name__}")
    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in type(value).model_fields}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, (pd.DataFrame, pd.Series)):
        return value.reset_index().to_dict(orient="list") if isinstance(value, pd.DataFrame) else value.to_dict()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def assert_no_answer_leak(obj: Any, scenario: Scenario, *, strict: bool = False) -> None:
    """`strict` also forbids the seed, fault type names, fault targets and severities anywhere in the
    text (use it for objects that describe the scenario itself, not for telemetry that names devices)."""
    text = json.dumps(obj, default=_encode)
    data = json.loads(text)
    leaked_keys = _keys(data) & FORBIDDEN_KEYS
    if leaked_keys:
        raise AnswerLeak(f"agent-facing data has answer fields: {sorted(leaked_keys)}")
    for fault in scenario.faults:
        if fault.label in text:
            raise AnswerLeak(f"agent-facing data names the planted cause {fault.label}")
    if strict:
        if re.search(rf"(?<![\w.]){scenario.seed}(?![\w.])", text):
            raise AnswerLeak("agent-facing data contains the scenario seed")
        for fault in scenario.faults:
            words = [fault.type.value, fault.target] + ([str(fault.severity)] if fault.severity is not None else [])
            for word in words:
                if re.search(rf"(?<![\w.]){re.escape(word)}(?![\w.])", text):
                    raise AnswerLeak(f"agent-facing data contains {word!r} from the planted faults")
