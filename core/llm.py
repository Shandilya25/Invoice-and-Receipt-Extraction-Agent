"""LLM access: GPT-OSS-120B on Groq (primary), Gemini 3.1 Flash-Lite (fallback).

Both expose an OpenAI-compatible API, so one client library covers both.
Guardrails here:
  - timeout per request (LLM_TIMEOUT_S, default 30 s)
  - retries with exponential backoff on 429 / 5xx / timeouts (SDK built-in, 2 retries)
  - fallback to the secondary provider if the primary still fails
  - every call is traced with latency, tokens and list-price cost
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from functools import lru_cache

import openai
from openai import OpenAI

from core.tracing import log_step


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    base_url: str
    key_env: str
    usd_per_m_input: float  # list price; the free tier costs $0 but we trace what it *would* cost
    usd_per_m_output: float
    extra: dict = field(default_factory=dict)


PROVIDERS = [
    Provider(
        name="groq",
        model="openai/gpt-oss-120b",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        usd_per_m_input=0.15,
        usd_per_m_output=0.60,
        extra={"reasoning_effort": "low"},
    ),
    Provider(
        name="gemini",
        model="gemini-3.1-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env="GEMINI_API_KEY",
        usd_per_m_input=0.25,
        usd_per_m_output=1.50,
    ),
]

# Errors worth trying the next provider for. Bad requests (400) are our bug: raise them.
_RETRYABLE = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
    openai.AuthenticationError,  # e.g. a revoked key: the other provider may still work
)


class LLMUnavailable(RuntimeError):
    """No provider could answer."""


@lru_cache(maxsize=4)
def _client_for(name: str) -> OpenAI:
    provider = next(p for p in PROVIDERS if p.name == name)
    return OpenAI(
        api_key=os.environ[provider.key_env],
        base_url=provider.base_url,
        timeout=float(os.getenv("LLM_TIMEOUT_S", "30")),
        max_retries=2,
    )


def chat(
    messages: list[dict],
    *,
    step: str,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    temperature: float | None = None,
    session_id: str = "",
):
    """Send a chat request; returns the assistant message (OpenAI format)."""
    available = [p for p in PROVIDERS if os.getenv(p.key_env)]
    if not available:
        raise LLMUnavailable("No API key set. Copy .env.example to .env and add GROQ_API_KEY.")

    kwargs: dict = {}
    if tools:
        kwargs["tools"] = tools
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None:
        kwargs["temperature"] = temperature

    errors = []
    for provider in available:
        start = time.perf_counter()
        try:
            response = _client_for(provider.name).chat.completions.create(
                model=provider.model, messages=messages, **kwargs, **provider.extra
            )
        except _RETRYABLE as e:
            errors.append(f"{provider.name}: {type(e).__name__}")
            log_step(
                step,
                provider=provider.name,
                session_id=session_id,
                latency_ms=_ms_since(start),
                error=type(e).__name__,
            )
            continue
        _trace(step, provider, response, session_id, _ms_since(start))
        return response.choices[0].message
    raise LLMUnavailable("All LLM providers failed: " + "; ".join(errors))


def _ms_since(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _trace(step: str, provider: Provider, response, session_id: str, latency_ms: int) -> None:
    usage = response.usage
    tokens_in = usage.prompt_tokens if usage else 0
    tokens_out = usage.completion_tokens if usage else 0
    cost = (tokens_in * provider.usd_per_m_input + tokens_out * provider.usd_per_m_output) / 1e6
    log_step(
        step,
        provider=provider.name,
        model=provider.model,
        session_id=session_id,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=round(cost, 6),
    )
