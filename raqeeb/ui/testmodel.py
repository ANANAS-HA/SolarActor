"""A scripted stand-in for the local language model, for recordings made without a model server.

    router = scripted_router()     # a real llm.router.Router on a Gateway whose completion is ScriptedModel

The router, prompts, parsing and number checks all run for real; only the reply is scripted: no
new hypotheses, a one-line explanation, and a shift report that names the causes and actions in
the agent's own records. Every reply says it comes from a test model. No network call is made.
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
            reply = {"hypotheses": []}
        text = json.dumps(reply, ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=len(prompt) // CHARS_PER_TOKEN,
                                completion_tokens=len(text) // CHARS_PER_TOKEN)
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


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
