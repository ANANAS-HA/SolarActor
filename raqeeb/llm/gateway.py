"""LLM gateway: one LiteLLM wrapper with two named roles (HANDOFF section 10).

    gateway = Gateway()                                   # settings from .env
    text, call = gateway.complete(LlmRole.LOCAL, "hypotheses", messages, json_mode=True)

- `local` = the OpenAI-compatible server in WSL2 (LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL), cost 0.
- `api`   = API_LLM_MODEL (a LiteLLM model string), cost from LiteLLM's price table.

Every call, failed ones included, is logged as a contracts.LlmCall in `gateway.calls` and to the
`raqeeb.llm` logger. A server that cannot be reached (or a role that is not configured) raises
LlmUnavailable, so the router can fall back. The gateway never logs prompts, replies or keys.

Transcripts (opt-in, off by default): `Gateway(keep_transcripts=True)` or RAQEEB_KEEP_TRANSCRIPTS=1 keeps
the prompt and reply text of every call in `gateway.transcripts` (one dict {purpose, prompt, reply} per
entry of `calls`, same order), in memory only: never in the logger, never on the LlmCall contract. The
bench harness copies them into its trajectory files.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import litellm

from raqeeb.config import Settings, load_settings
from raqeeb.contracts import LlmCall, LlmRole

log = logging.getLogger("raqeeb.llm")

TRANSCRIPTS_ENV = "RAQEEB_KEEP_TRANSCRIPTS"  # "1" keeps prompt and reply text per call (see keep_transcripts)
TIMEOUT_S = {LlmRole.LOCAL: 120.0, LlmRole.API: 90.0}
MAX_TOKENS = 2048
# The local model is a reasoning model: thinking is turned off per call (llama.cpp chat template flag)
LOCAL_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}

# Errors that mean "this endpoint is not there right now", as opposed to a bad request
_UNAVAILABLE = (
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.Timeout,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.InternalServerError,
    litellm.exceptions.AuthenticationError,
    litellm.exceptions.RateLimitError,
    litellm.exceptions.NotFoundError,
)


class LlmError(RuntimeError):
    """The call failed (bad request, provider error). The router counts it as a failed attempt."""

    def __init__(self, role: LlmRole, reason: str):
        super().__init__(f"{role.value} LLM call failed: {reason}")
        self.role = role
        self.reason = reason


class LlmUnavailable(LlmError):
    """The role's endpoint is not configured, cannot be reached, or refused us: try the other role."""


class Gateway:
    def __init__(self, settings: Settings | None = None, completion: Callable[..., Any] | None = None, *,
                 keep_transcripts: bool | None = None):
        """`completion` replaces litellm.completion (tests pass a fake model). `keep_transcripts` keeps the
        prompt and reply text of every call in `transcripts` (default: the RAQEEB_KEEP_TRANSCRIPTS=1
        environment switch, else off)."""
        self.settings = settings or load_settings()
        self._completion = completion or litellm.completion
        self.calls: list[LlmCall] = []
        if keep_transcripts is None:
            keep_transcripts = os.environ.get(TRANSCRIPTS_ENV, "").strip() == "1"
        self.keep_transcripts = bool(keep_transcripts)
        self.transcripts: list[dict[str, str]] = []  # {purpose, prompt, reply} per call, only when kept

    def configured(self, role: LlmRole) -> bool:
        if role == LlmRole.LOCAL:
            return self.settings.local_llm_configured
        return bool(self.settings.api_llm_model and self.settings.anthropic_api_key)

    def model_name(self, role: LlmRole) -> str:
        if role == LlmRole.LOCAL:
            return f"openai/{self.settings.local_llm_model}"
        return self.settings.api_llm_model

    def model_label(self, role: LlmRole) -> str:
        """The model as reports name it: "test model" when a scripted completion stands in for LiteLLM,
        else the local model's file name (", local") or the API model string."""
        if self._completion is not litellm.completion:
            return "test model"
        if LlmRole(role) == LlmRole.LOCAL:
            name = self.settings.local_llm_model.replace("\\", "/").rsplit("/", 1)[-1]
            return f"{name.removesuffix('.gguf') or 'local model'}, local"
        return self.settings.api_llm_model

    def _endpoint(self, role: LlmRole) -> dict[str, Any]:
        if role == LlmRole.LOCAL:
            return {"api_base": self.settings.local_llm_base_url, "api_key": self.settings.local_llm_api_key,
                    "extra_body": LOCAL_EXTRA_BODY}
        return {"api_key": self.settings.anthropic_api_key}

    def complete(self, role: LlmRole, purpose: str, messages: list[dict[str, str]], *, json_mode: bool = False,
                 max_tokens: int = MAX_TOKENS, escalation: str = "") -> tuple[str, LlmCall]:
        """Send `messages` to `role`. Returns the reply text and its logged LlmCall (ok=True until the
        caller marks the output invalid). Raises LlmUnavailable if the endpoint is down or not set up
        (nothing is logged when it is not set up: no call was made), LlmError for any other failure."""
        role = LlmRole(role)
        model = self.model_name(role)
        if not self.configured(role):
            raise LlmUnavailable(role, "not configured in .env")
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens,
                                  "temperature": 0, "timeout": TIMEOUT_S[role], **self._endpoint(role)}
        if json_mode and role == LlmRole.LOCAL:  # llama.cpp constrains the reply to JSON
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            response = self._completion(**kwargs)
        except Exception as exc:
            self._record(LlmCall(role=role, model=model, purpose=purpose, latency_s=time.perf_counter() - started,
                                 ok=False, escalation=escalation))
            self._keep(purpose, messages, "")
            error = LlmUnavailable if isinstance(exc, _UNAVAILABLE) else LlmError
            raise error(role, type(exc).__name__) from None  # no request details (keys) in the chain
        latency = time.perf_counter() - started

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        text = (response.choices[0].message.content or "").strip()
        call = LlmCall(role=role, model=model, purpose=purpose, prompt_tokens=prompt_tokens,
                       completion_tokens=completion_tokens, latency_s=latency,
                       cost_usd=cost_usd(role, model, prompt_tokens, completion_tokens),
                       ok=bool(text), escalation=escalation)
        self._record(call)
        self._keep(purpose, messages, text)
        return text, call

    def _keep(self, purpose: str, messages: list[dict[str, str]], reply: str) -> None:
        """One transcript per recorded call (so `transcripts` lines up with `calls`), when kept."""
        if self.keep_transcripts:
            self.transcripts.append({"purpose": purpose, "prompt": prompt_text(messages), "reply": reply})

    def _record(self, call: LlmCall) -> None:
        self.calls.append(call)
        log.info("llm call role=%s purpose=%s model=%s tokens=%d+%d latency=%.2fs cost=$%.5f ok=%s%s",
                 call.role.value, call.purpose, call.model, call.prompt_tokens, call.completion_tokens,
                 call.latency_s, call.cost_usd, call.ok, f" escalation={call.escalation}" if call.escalation else "")

    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.calls)


def prompt_text(messages: list[dict[str, str]]) -> str:
    """The messages as one readable text, a "[role]" line before each."""
    return "\n\n".join(f"[{m.get('role', '?')}]\n{m.get('content', '')}" for m in messages)


def cost_usd(role: LlmRole, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Local calls are free (our own GPU); API calls are priced from LiteLLM's table (0 if unknown)."""
    if role == LlmRole.LOCAL:
        return 0.0
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    except Exception:  # model missing from the price table
        log.warning("no price for %s; cost logged as 0", model)
        return 0.0
    return float(prompt_cost + completion_cost)
