"""Thin wrapper around the optional AI model.

Every AI call in TruthTrace goes through `ask_json`. If no model is configured, or a
call fails, it returns None and the caller falls back to the deterministic heuristic
path. Callers must never treat model output as evidence on its own: anything the
model says about a source is re-checked against the retrieved text (see evidence.py).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time

import httpx

from . import config

log = logging.getLogger("truthtrace.llm")

_anthropic_client = None
_cooldown_until = 0.0
_cooldown_reason = ""

SYSTEM_BASE = (
    "You are the analysis engine of TruthTrace, a news verification system. "
    "Rules: use ONLY the text provided to you; never invent sources, quotes, numbers, "
    "dates or URLs; when the provided text is not enough, say so with 'unknown' or "
    "'insufficient'. Keep facts and opinions separate. Influence and popularity are "
    "never evidence of truth. Reply with a single JSON object and nothing else."
)


def provider_info() -> dict:
    configured = config.LLM_PROVIDER in ("anthropic", "openai_compat")
    remaining = max(0, math.ceil(_cooldown_until - time.monotonic()))
    if config.LLM_PROVIDER == "anthropic":
        return {"provider": "anthropic", "model": config.ANTHROPIC_MODEL, "enabled": configured,
                "available": remaining == 0, "retry_after_seconds": remaining, "issue": _cooldown_reason if remaining else None}
    if config.LLM_PROVIDER == "openai_compat":
        return {"provider": "openai_compat", "model": config.LLM_MODEL, "enabled": configured,
                "available": remaining == 0, "retry_after_seconds": remaining, "issue": _cooldown_reason if remaining else None}
    return {"provider": "none", "model": None, "enabled": False, "available": False,
            "retry_after_seconds": 0, "issue": None}


def enabled() -> bool:
    return config.LLM_PROVIDER in ("anthropic", "openai_compat") and time.monotonic() >= _cooldown_until


def _cool_down(seconds: int, reason: str) -> None:
    """Stop repeated calls after an upstream outage or rate limit."""
    global _cooldown_until, _cooldown_reason
    now = time.monotonic()
    if now >= _cooldown_until:
        log.warning("Pausing AI requests for %s seconds: %s", seconds, reason)
    _cooldown_until = max(_cooldown_until, now + seconds)
    _cooldown_reason = reason


def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    for end in range(len(text), start, -1):
        if text[end - 1] in "}]":
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
    return None


async def _ask_anthropic(system: str, prompt: str, max_tokens: int) -> str | None:
    global _anthropic_client
    import anthropic

    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=30.0, max_retries=0)
    try:
        response = await _anthropic_client.beta.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": config.ANTHROPIC_EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as exc:
        _cool_down(90, "The AI provider is rate limiting requests (HTTP 429).")
        return None
    except anthropic.APIStatusError as exc:
        seconds = 90 if exc.status_code == 429 or exc.status_code >= 500 else 300
        _cool_down(seconds, f"The AI provider returned HTTP {exc.status_code}.")
        return None
    except anthropic.APIConnectionError as exc:
        _cool_down(30, "The AI provider could not be reached.")
        return None
    if response.stop_reason == "refusal":
        log.warning("Claude declined the request")
        return None
    return "".join(b.text for b in response.content if b.type == "text")


async def _ask_openai_compat(system: str, prompt: str, max_tokens: int) -> str | None:
    url = config.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    body = {
        "model": config.LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        seconds = 90 if code == 429 or code >= 500 else 300
        _cool_down(seconds, f"The AI provider returned HTTP {code}.")
        return None
    except httpx.TimeoutException:
        _cool_down(30, "The AI provider timed out.")
        return None
    except httpx.HTTPError:
        _cool_down(30, "The AI provider could not be reached.")
        return None
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("LLM response could not be read: %s", exc)
        return None


async def ask_json(task: str, prompt: str, max_tokens: int = 4000):
    """Ask the configured model for JSON. Returns parsed JSON or None."""
    if not enabled():
        return None
    system = SYSTEM_BASE + "\nTask: " + task
    if config.LLM_PROVIDER == "anthropic":
        text = await _ask_anthropic(system, prompt, max_tokens)
    else:
        text = await _ask_openai_compat(system, prompt, max_tokens)
    if not text:
        return None
    data = _extract_json(text)
    if data is None:
        log.warning("Model reply was not valid JSON for task %r", task[:60])
    return data
