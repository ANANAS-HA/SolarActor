"""Prompt files, untrusted-data blocks and reply checks shared by the router.

Prompts live in raqeeb/llm/prompts/ as `<name>_system.md` and `<name>_user.md`, filled with
string.Template ($name placeholders). Numbers never live in the prompt files: anything numeric is
data passed in by code.

Everything that comes from the plant (tool results, symptoms, alarm codes, log text, the agent's
draft records) goes into an untrusted-data block: JSON between tags carrying a random id, which
the data cannot forge. The system prompts tell the model to treat such blocks as data only.
"""

from __future__ import annotations

import json
import re
import secrets
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
MAX_STRING_CHARS = 400  # longer strings in data blocks are cut (alarm or log text)
TAG = "untrusted_data"
SHARED = {"untrusted_rule": "untrusted_data.md", "fault_catalogue": "fault_catalogue.md",  # $key -> file
          "glossary_ar": "glossary_ar.md"}


@lru_cache
def _read(file_name: str) -> str:
    return (PROMPT_DIR / file_name).read_text(encoding="utf-8").strip()


def render(name: str, **values: str) -> list[dict[str, str]]:
    """System + user messages for prompt `name`; every $placeholder must be given."""
    shared = {key: Template(_read(file_name)).substitute(values)
              for key, file_name in SHARED.items() if f"${key}" in _read(f"{name}_system.md")}
    system = Template(_read(f"{name}_system.md")).substitute(shared | values)
    user = Template(_read(f"{name}_user.md")).substitute(shared | values)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        text = value.replace(TAG, TAG.replace("_", "-"))  # data cannot open or close a block
        return text if len(text) <= MAX_STRING_CHARS else text[:MAX_STRING_CHARS] + "...[cut]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _plain(str(value))


def plain_json(payload: Any) -> str:
    """`payload` as the JSON a data block holds, without the block (the source for number checks)."""
    return json.dumps(_plain(payload), ensure_ascii=False)


def data_block(name: str, payload: Any) -> str:
    """`payload` as JSON inside an untrusted-data block with a fresh random id."""
    block_id = secrets.token_hex(6)
    body = json.dumps(_plain(payload), ensure_ascii=False)
    return f'<{TAG} name="{name}" id="{block_id}">\n{body}\n</{TAG} id="{block_id}">'


def extract_json(text: str) -> str:
    """The JSON object in a reply: strips code fences and any text around the outer braces."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    first, last = text.find("{"), text.rfind("}")
    return text[first:last + 1] if first != -1 and last > first else text


# --- numbers in written text ------------------------------------------------------------------

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
FREE_INTEGERS = 10  # small counts and ordinals ("2 causes", "step 1") need no source


def numbers_in(text: str) -> list[float]:
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text.translate(_ARABIC_DIGITS))  # 1,400 -> 1400
    return [float(n) for n in _NUMBER.findall(text)]


def _forms(x: float) -> set[float]:
    """Ways a source number may fairly appear in prose: as is, rounded, or a ratio shown as a percent."""
    forms = {x, round(x), round(x, 1), round(x, 2)}
    if abs(x) <= 10:
        forms |= {round(x * 100), round(x * 100, 1)}
    return {float(f) for f in forms}


def unsupported_numbers(text: str, sources: str) -> list[float]:
    """Numbers in `text` that do not come from `sources` (code computes every number; the model may
    only copy, round or show a ratio as a percent)."""
    allowed: set[float] = set()
    for x in numbers_in(sources):
        allowed |= _forms(x)
    return [n for n in numbers_in(text)
            if n not in allowed and not (n.is_integer() and n <= FREE_INTEGERS)]
