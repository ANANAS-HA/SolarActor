"""Check that an LLM endpoint from .env answers.

    uv run python -m raqeeb.llm.smoke          # local model (free)
    uv run python -m raqeeb.llm.smoke --api    # Anthropic API model (costs a little)
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import litellm

from raqeeb.config import Settings, load_settings

PROMPT = "Reply with the single word: ready"


@dataclass
class SmokeResult:
    role: str
    model: str
    reply: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int


def ping(settings: Settings, role: str = "local", timeout_s: float = 60) -> SmokeResult:
    if role == "local":
        if not settings.local_llm_configured:
            raise RuntimeError("LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL must be set in .env")
        model = f"openai/{settings.local_llm_model}"
        extra = {"api_base": settings.local_llm_base_url, "api_key": settings.local_llm_api_key}
    elif role == "api":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY must be set in .env")
        model = settings.api_llm_model
        extra = {"api_key": settings.anthropic_api_key}
    else:
        raise ValueError(f"unknown role {role!r}")

    started = time.perf_counter()
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": PROMPT}],
        max_tokens=16,
        temperature=0,
        timeout=timeout_s,
        **extra,
    )
    usage = response.usage
    return SmokeResult(
        role=role,
        model=model,
        reply=(response.choices[0].message.content or "").strip(),
        latency_s=time.perf_counter() - started,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM endpoint smoke test")
    parser.add_argument("--api", action="store_true", help="test the API model instead of the local one")
    args = parser.parse_args()
    try:
        result = ping(load_settings(), role="api" if args.api else "local")
    except Exception as exc:  # report and fail without a traceback full of request details
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"OK [{result.role}] {result.model}: {result.reply!r} "
          f"in {result.latency_s:.2f}s ({result.prompt_tokens}+{result.completion_tokens} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
