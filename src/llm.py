"""OpenAI-compatible streaming client (works with any base_url).

Streams are iterated as parsed chunks; the model's chain-of-thought
(Qwen3 `reasoning_content` / vLLM `reasoning`) is read via getattr /
model_extra so it survives the SDK's parsing.
"""
from __future__ import annotations

import asyncio
import logging

from openai import (AsyncOpenAI, APIConnectionError, APIStatusError,
                    APITimeoutError, RateLimitError)

from .config import settings

log = logging.getLogger("muji.llm")

# --- Bounded retry for transient model errors (timeout / 5xx / 429) ---
_MAX_RETRIES = 3
_BACKOFFS = (2, 4, 8)  # seconds between attempts


def _is_retryable(exc: Exception) -> bool:
    """True for transient errors worth retrying; False for permanent ones."""
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (500, 502, 503, 504):
        return True
    return False


def is_transient_message(msg: str) -> bool:
    """Message-level twin of `_is_retryable` for errors that already lost
    their exception type: `LLMError` carries only `str(e)`, and the
    mid-stream auto-resume (agent.py) must decide "worth a re-fire" from
    that string alone. Same vocabulary the bounded retry uses —
    timeout / connection / 429 / 5xx — so both paths agree on what
    "transient" means."""
    low = (msg or "").lower()
    if any(k in low for k in ("timeout", "timed out", "connection",
                              "temporarily", "unavailable", "rate limit",
                              "too many requests")):
        return True
    return any(code in low for code in (" 500", " 502", " 503", " 504"))


def _thinking_kwargs(thinking: str) -> dict:
    """Map a THINKING value to the extra_body the Qwen3-style endpoint
    HONORS (probed 2026-09-28 — tools/probe_thinking.py + tool_notes.md):
    low|medium|xhigh → reasoning_effort tier; "off" →
    chat_template_kwargs.enable_thinking=False (the only working off —
    top-level enable_thinking and thinking_budget are silently ignored).
    Empty → {} (server default = xhigh on this endpoint)."""
    t = (thinking or "").strip().lower()
    if not t:
        return {}
    if t == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"reasoning_effort": t}


class LLMError(Exception):
    pass


class ToolUnsupported(Exception):
    """Server rejected the `tools` parameter — fall back to the text protocol."""


class LLM:
    def __init__(self) -> None:
        self.mode = settings.tool_mode  # auto | native | text
        self._client: AsyncOpenAI | None = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not settings.base_url:
                raise LLMError("OPENAI_BASE_URL is not set — edit .env")
            self._client = AsyncOpenAI(base_url=settings.base_url,
                                       api_key=settings.api_key)
        return self._client

    def native_tools_enabled(self) -> bool:
        return self.mode in ("native", "auto")

    def stream_kwargs(self, messages: list[dict], temperature: float) -> dict:
        """Base kwargs for a STREAMING completion (extracted so tests can
        assert on the request shape). Carries the per-round completion
        cap (2026-10-08): the hard backstop for a degeneration the
        thinking tripwire misses — without it the server ran a looping
        round to its own limit (72 min, 497 KB, AI Classes)."""
        kwargs = dict(
            model=settings.model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},  # final-chunk usage → tok/s
            temperature=temperature,
        )
        if settings.max_tokens > 0:
            kwargs["max_tokens"] = settings.max_tokens
        return kwargs

    async def stream(self, messages: list[dict], tools: list[dict] | None = None,
                     thinking: str | None = None):
        """Yield event dicts:
        {"type":"token","text":str}
        {"type":"thinking","text":str}        # model chain-of-thought (Qwen3 etc.)
        {"type":"tool_name","index":int,"id":str,"name":str}
        {"type":"tool_args","index":int,"arguments":str}
        {"type":"end","finish_reason":str|None}

        `thinking` is a per-call override (2026-09-28): the agent loop
        passes THINKING_FIRST for the first round of a task and
        THINKING_LOOP for tool-loop rounds; None → settings.thinking."""
        if not settings.model:
            raise LLMError("MODEL is not set — edit .env")
        kwargs = self.stream_kwargs(messages, settings.temperature)
        # thinking control: this endpoint (vLLM Qwen3) honours
        # reasoning_effort {low|medium|xhigh} and chat_template_kwargs —
        # probed live with tools/probe_thinking.py. Per-call override
        # (agent loop: THINKING_FIRST vs THINKING_LOOP) wins over .env.
        extra = _thinking_kwargs(settings.thinking if thinking is None
                                 else thinking)
        if extra:
            kwargs["extra_body"] = extra
        if tools and self.native_tools_enabled():
            kwargs["tools"] = tools
        # Bounded retry: only the initial connection (before any tokens
        # are yielded). A mid-stream timeout can't be retried — the
        # consumer already has partial output.
        stream = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                stream = await self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:  # noqa: BLE001
                if not _is_retryable(e) or attempt == _MAX_RETRIES:
                    raise LLMError(str(e)) from e
                delay = _BACKOFFS[attempt]
                log.warning("stream create attempt %d/%d failed: %s — retry in %ds",
                            attempt + 1, _MAX_RETRIES + 1, e, delay)
                await asyncio.sleep(delay)
        try:
            async for chunk in stream:
                # usage chunk: vLLM sends it as a choices-LESS chunk after
                # finish_reason — check it BEFORE the choices guard, or the
                # `continue` above swallows it (live-verified: tok_s was null)
                u = getattr(chunk, "usage", None)
                if u is not None:
                    yield {"type": "usage",
                           "completion_tokens": getattr(u, "completion_tokens", None),
                           "prompt_tokens": getattr(u, "prompt_tokens", None)}
                    continue
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None:
                    if getattr(delta, "content", None):
                        yield {"type": "token", "text": delta.content}
                    # chain-of-thought: Qwen3 servers send `reasoning_content`,
                    # vLLM with a reasoning parser sends `reasoning`
                    reasoning = (getattr(delta, "reasoning_content", None)
                                 or getattr(delta, "reasoning", None))
                    if not reasoning:  # older SDKs: fall back to extra fields
                        extra = getattr(delta, "model_extra", None) or {}
                        reasoning = (extra.get("reasoning_content")
                                     or extra.get("reasoning"))
                    if reasoning:
                        yield {"type": "thinking", "text": reasoning}
                    for tc in (getattr(delta, "tool_calls", None) or []):
                        idx = tc.index if tc.index is not None else 0
                        fn = tc.function
                        if fn is not None:
                            if fn.name:
                                yield {"type": "tool_name", "index": idx,
                                       "id": (getattr(tc, "id", "") or f"call_{idx}"),
                                       "name": fn.name}
                            if fn.arguments:
                                yield {"type": "tool_args", "index": idx,
                                       "arguments": fn.arguments}
                if choice.finish_reason:
                    yield {"type": "end", "finish_reason": choice.finish_reason}
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            low = msg.lower()
            if self.mode == "auto" and tools and ("tool" in low or "function" in low):
                raise ToolUnsupported(msg) from e
            raise LLMError(msg) from e

    async def complete(self, messages: list[dict], temperature: float = 0.2,
                       thinking: str | None = None) -> str:
        """Non-streaming one-shot (fact-checker, trajectory audit,
        compaction, chat title/summary).

        `thinking` defaults to settings.thinking — before 2026-09-28 this
        method sent NO thinking knob at all, so every one-shot ran at the
        server default (xhigh on this endpoint) regardless of .env.
        Callers that need no reasoning pass thinking="off"; compaction
        passes "low" (a real summarization job, no tools)."""
        if not settings.model:
            raise LLMError("MODEL is not set — edit .env")
        kwargs = dict(model=settings.model, messages=messages,
                      temperature=temperature, stream=False)
        extra = _thinking_kwargs(thinking if thinking is not None
                                 else settings.thinking)
        if extra:
            kwargs["extra_body"] = extra
        resp = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:  # noqa: BLE001
                if not _is_retryable(e) or attempt == _MAX_RETRIES:
                    raise LLMError(str(e)) from e
                delay = _BACKOFFS[attempt]
                log.warning("complete attempt %d/%d failed: %s — retry in %ds",
                            attempt + 1, _MAX_RETRIES + 1, e, delay)
                await asyncio.sleep(delay)
        return (resp.choices[0].message.content or "") if resp.choices else ""
