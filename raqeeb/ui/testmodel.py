"""A scripted stand-in for the local language model, for recordings made without a model server.

    router = scripted_router()     # a real llm.router.Router on a Gateway whose completion is ScriptedModel

The router, prompts, parsing and number checks all run for real; only the reply is scripted:
hypotheses from a fixed rule (the site area holding the unexplained weak inverters, plus one
deliberately wrong area), a one-line explanation, and a shift report that names the causes and
actions in the agent's own records. The agent tests every proposal on tool data as it would a
model's. Every reply says it comes from a test model. No network call is made.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from raqeeb.config import load_settings
from raqeeb.contracts import PlantConfig
from raqeeb.llm.gateway import Gateway
from raqeeb.llm.router import Router

TEST_MODEL = "test-model"
TEST_BASE_URL = "http://test-model.invalid/v1"  # never contacted: the completion function is replaced
CHARS_PER_TOKEN = 4  # rough token count for the logged LlmCall

_BLOCK = re.compile(r'<untrusted_data name="(?P<name>\w+)" id="(?P<id>\w+)">\n(?P<body>.*?)\n</untrusted_data id="(?P=id)">',
                    re.S)

EXPLANATION = "Test model reply (scripted, not a language model): see the agent's diagnosis and evidence."
REPORT_EN = "Test model reply (scripted, not a language model)."
REPORT_AR = "رد من نموذج اختبار مبرمج، وليس من نموذج لغوي. الأسباب والإجراءات مذكورة في سجل الوكيل."


class ScriptedModel:
    """Stands in for litellm.completion: picks the reply from the data blocks the prompt carries."""

    def __call__(self, *, messages: list[dict[str, str]], **_: Any) -> SimpleNamespace:
        prompt = "\n".join(m["content"] for m in messages)
        blocks = {m["name"]: m["body"] for m in _BLOCK.finditer(prompt)}
        if "records" in blocks:
            reply = _report(blocks["records"])
        elif "diagnosis" in blocks:
            reply = {"explanation": EXPLANATION}
        else:
            reply = _proposals(blocks)
        text = json.dumps(reply, ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=len(prompt) // CHARS_PER_TOKEN,
                                completion_tokens=len(text) // CHARS_PER_TOKEN)
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _proposals(blocks: dict[str, str]) -> dict[str, list]:
    """Two fixed-rule proposals for the inverters the rules leave unexplained: the smallest site area
    holding all of them (TRANSFORMER_TRIP on a transformer group, NEW_SHADING on a field edge,
    FIRMWARE_ALARM on a single inverter), and a deliberately wrong one on a neighbouring area of the
    same kind, so the agent's tool test has something to reject. No proposal without such inverters."""
    try:
        unexplained = json.loads(blocks.get("unexplained", "[]"))
        symptoms = {s["id"]: s for s in json.loads(blocks.get("symptoms", "[]"))}
        layout = json.loads(blocks.get("site_layout", "{}"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return {"hypotheses": []}
    transformers = {t: set(v.get("inverters", [])) for t, v in layout.get("transformers", {}).items()}
    edges = {e: set(v) for e, v in layout.get("field_edges", {}).items()}
    inverters = set().union(*transformers.values()) if transformers else set()
    explains = [i for i in unexplained if i in symptoms and symptoms[i].get("device_id") in inverters]
    weak = {symptoms[i]["device_id"] for i in explains}
    if not weak:
        return {"hypotheses": []}
    if len(weak) == 1:
        fault, areas = "FIRMWARE_ALARM", {d: {d} for d in sorted(inverters)}
    else:
        areas = {**transformers, **edges}
    holding = sorted((a for a, devs in areas.items() if weak <= devs), key=lambda a: (len(areas[a]), a))
    if not holding:
        return {"hypotheses": []}
    target = holding[0]
    if len(weak) > 1:
        fault = "TRANSFORMER_TRIP" if target in transformers else "NEW_SHADING"
        areas = transformers if target in transformers else edges
    wrong = next((a for a in sorted(areas) if a != target and not areas[a] & weak), None)
    out = [{"fault_type": fault, "targets": [target], "explains": explains,
            "evidence": [f"Test model (scripted rule, not a language model): the unexplained weak units all lie in {target}."]}]
    if wrong:
        out.append({"fault_type": fault, "targets": [wrong], "explains": explains,
                    "evidence": ["Test model (scripted rule, not a language model): a deliberately wrong area, "
                                 "for the agent's tool test to reject."]})
    return {"hypotheses": out}


def _report(records_json: str) -> dict[str, str]:
    try:
        records = json.loads(records_json)
    except json.JSONDecodeError:
        records = {}
    causes = [f"{c['fault_type']}@{t}" for c in records.get("causes", []) for t in c.get("targets", [])]
    actions = [f"{a['action']} on {a['target']}" for a in records.get("actions", [])]
    en = REPORT_EN
    if causes:
        en += " Root causes in the agent's records: " + ", ".join(causes) + "."
    if actions:
        en += " Actions: " + "; ".join(actions) + "."
    return {"summary_en": en, "summary_ar": REPORT_AR}


def scripted_router(plant: PlantConfig | None = None) -> Router:
    settings = load_settings().model_copy(update={"local_llm_base_url": TEST_BASE_URL, "local_llm_model": TEST_MODEL,
                                                  "anthropic_api_key": ""})
    return Router(Gateway(settings, completion=ScriptedModel()), plant, api=False)
