"""OpenAI-compatible streaming client (works with any base_url).

Streams are iterated as parsed chunks; the model's chain-of-thought
(Qwen3 `reasoning_content` / vLLM `reasoning`) is read via getattr /
model_extra so it survives the SDK's parsing.
"""
from __future__ import annotations

import asyncio
import re
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
    """Map a THINKING value to the extra_body the active endpoint HONORS.

    Qwen3/vLLM (probed 2026-09-28 — tools/probe_thinking.py + tool_notes.md):
    low|medium|xhigh → reasoning_effort tier; "off" →
    chat_template_kwargs.enable_thinking=False (the only working off —
    top-level enable_thinking and thinking_budget are silently ignored).
    Empty → {} (server default = xhigh on this endpoint).

    Other endpoints (2026-10-23, model profiles): profiles that don't
    honour `reasoning_effort` (settings.reasoning_effort_ok False, e.g.
    Groq llama-3.3 — it 400s on the unknown param) get NO thinking kwargs
    at all; the model's native behaviour applies."""
    t = (thinking or "").strip().lower()
    if not t:
        return {}
    if not getattr(settings, "reasoning_effort_ok", True):
        return {}
    style = getattr(settings, "thinking_style", "qwen")
    if style == "none":
        return {}
    if style == "qwen_hosted":
        # Qwen3 on Groq (live-probed 2026-11-08): low|medium|high|none|
        # default accepted, xhigh 400s, chat_template_kwargs unsupported
        return {"reasoning_effort": {"off": "none", "xhigh": "high",
                                     "minimal": "low"}.get(t, t)}
    if style == "openai":
        # OpenAI-style endpoints (gpt-oss on Groq, OpenAI o-series/gpt-5):
        # only low|medium|high exist ("xhigh" 400s), and
        # chat_template_kwargs is a vLLM-only param -> "off" maps to the
        # lowest tier instead.
        return {"reasoning_effort": {"off": "low", "xhigh": "high",
                                     "minimal": "low"}.get(t, t)}
    if t == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    # the UI's top level is "high"; this vLLM/Qwen endpoint's top tier is
    # xhigh (probed: low|medium|xhigh honored)
    return {"reasoning_effort": "xhigh" if t == "high" else t}


#: Message keys every OpenAI-compatible endpoint accepts. Groq & co. 400
#: on unknown properties ("property 'x' is unsupported"); vLLM ignores them.
_STD_MSG_KEYS = {"role", "content", "name", "tool_calls", "tool_call_id"}

_NO_VISION_NOTE = ("[image omitted — the active model has no vision; the file "
                   "path is in the text, inspect it with tools (OCR/PIL) if needed]")


def _text_only(parts: list) -> str:
    out = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            out.append(part.get("text") or "")
        elif isinstance(part, dict) and part.get("type") == "image_url":
            out.append(_NO_VISION_NOTE)
    return "\n".join(x for x in out if x)


def adapt_messages(messages: list[dict]) -> list[dict]:
    """Make muji's message list portable to the ACTIVE endpoint (returns
    a new list; the agent's own list is untouched).

    muji was built against vLLM/Qwen, which tolerates things standard
    OpenAI-compatible APIs reject:
      * image parts for a model with no vision (Groq llama/gpt-oss 400:
        "content must be a string") -> replaced by a text note;
      * image parts INSIDE a role=tool message (browser screenshots) —
        OpenAI/Groq only allow text there -> the tool message keeps the
        text and the image moves to a user message right after it;
      * non-standard message keys -> dropped.
    The Qwen/vLLM profile (thinking_style "qwen") passes through as-is."""
    if getattr(settings, "thinking_style", "qwen") == "qwen":
        return messages
    vision = bool(getattr(settings, "vision", False))
    out: list[dict] = []
    for m in messages:
        m2 = {k: v for k, v in m.items() if k in _STD_MSG_KEYS}
        content = m2.get("content")
        if isinstance(content, list):
            has_img = any(isinstance(x, dict) and x.get("type") == "image_url"
                          for x in content)
            if m2.get("role") == "tool":
                m2["content"] = _text_only(
                    [x for x in content if not (isinstance(x, dict)
                                                and x.get("type") == "image_url")]) \
                    or "(see the image in the next message)"
                out.append(m2)
                if has_img and vision:
                    imgs = [x for x in content if isinstance(x, dict)
                            and x.get("type") == "image_url"]
                    out.append({"role": "user", "content": [
                        {"type": "text", "text": "Image returned by the tool call above:"},
                        *imgs]})
                elif has_img:
                    out[-1]["content"] += "\n" + _NO_VISION_NOTE
                continue
            if not vision:
                m2["content"] = _text_only(content)
        out.append(m2)
    return out


_PARAM_REJECT_WORDS = ("unsupported", "not supported", "unrecognized",
                       "unknown", "invalid", "not allowed", "does not support",
                       "is not available", "must be")


def _adapt_kwargs_after_error(kwargs: dict, msg: str) -> str | None:
    """One-shot repair of a request the endpoint rejected for a PARAMETER
    (400). Mutates kwargs and returns what was changed, or None when the
    error isn't a recognisable parameter rejection. Handles:
      max_tokens -> max_completion_tokens (OpenAI reasoning models),
      max_tokens too large -> clamp to the limit quoted in the error,
      reasoning_effort / chat_template_kwargs / stream_options /
      temperature / top_p rejected -> drop it."""
    # The vLLM/Qwen profile's request shape is known-good — its 400s are
    # real (context overflow etc.) and belong to the agent's recovery.
    if getattr(settings, "thinking_style", "qwen") == "qwen":
        return None
    low = (msg or "").lower()
    # context-window rejections quote max_tokens too ("'max_tokens' is too
    # large … maximum context length") — never "repair" those by dropping
    # the cap; the agent's overflow recovery owns them.
    if any(k in low for k in ("context length", "context_length", "context window",
                              "maximum context", "reduce the length")):
        return None
    if not any(w in low for w in _PARAM_REJECT_WORDS):
        return None
    if ("max_tokens" in kwargs and "max_completion_tokens" in low and "max_tokens" in low
            and any(w in low for w in ("instead", "unsupported", "not supported"))):
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        return "max_tokens -> max_completion_tokens"
    for key in ("max_tokens", "max_completion_tokens"):
        if key in kwargs and key in low:
            m = re.search(r"(?:less than or equal to|at most|maximum(?: value)?(?: is| of)?|<=)\s*`?(\d{3,7})", low)
            if m and int(m.group(1)) < int(kwargs[key]):
                kwargs[key] = int(m.group(1))
                return f"{key} clamped to {m.group(1)}"
            kwargs.pop(key)
            return f"dropped {key}"
    extra = kwargs.get("extra_body") or {}
    for key in ("reasoning_effort", "chat_template_kwargs"):
        if key in extra and key in low:
            extra.pop(key)
            if not extra:
                kwargs.pop("extra_body", None)
            return f"dropped {key}"
    for key in ("stream_options", "temperature", "top_p"):
        if key in kwargs and key in low:
            kwargs.pop(key)
            return f"dropped {key}"
    return None


class LLMError(Exception):
    pass


class ToolUnsupported(Exception):
    """Server rejected the `tools` parameter — fall back to the text protocol."""


class ToolCallFailed(LLMError):
    """The model emitted a tool call the API couldn't parse/accept — retry
    the round with a note; NOT a reason to drop native tools."""


def is_tool_call_failure(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in (
        "tool_use_failed", "failed to call a function", "tool choice is none",
        "failed_generation", "invalid tool call", "tool call validation",
        "could not parse tool", "error parsing tool call"))


def _tools_unsupported(low: str) -> bool:
    """Only a rejection of the `tools` PARAMETER itself means "no native tool
    support" (vLLM without --enable-auto-tool-choice, old servers). The
    original blanket "tool"/"function" substring match also fired on the
    model's own malformed calls."""
    if "tool" not in low and "function" not in low:
        return False
    return bool(re.search(
        r"(tools?|tool_choice|functions?)\W[^.]{0,80}?(not supported|unsupported|"
        r"not allowed|unknown|unrecognized|unexpected|extra (?:fields|inputs)|"
        r"requires --enable-auto-tool-choice|auto-tool-choice)", low)
        or "enable-auto-tool-choice" in low)


class LLM:
    def __init__(self) -> None:
        self._base_mode = settings.tool_mode  # auto | native | text
        #: endpoints ((base_url, model)) that proved they can't take native
        #: tools — the text-protocol fallback is remembered PER ENDPOINT
        #: (2026-11-08: a process-global `mode = "text"` set by one Groq
        #: chat leaked into every later chat until a restart; an image chat
        #: then emitted its TOOL_CALL inside its reasoning and died as an
        #: "empty answer")
        self._text_endpoints: set[tuple[str, str]] = set()
        self._client: AsyncOpenAI | None = None
        self._client_url: str | None = None
        self._client_key: str | None = None

    def _endpoint(self) -> tuple[str, str]:
        return (settings.base_url or "", settings.model or "")

    @property
    def mode(self) -> str:
        if self._endpoint() in self._text_endpoints:
            return "text"
        return self._base_mode

    @mode.setter
    def mode(self, value: str) -> None:
        if value == "text" and self._base_mode != "text":
            self._text_endpoints.add(self._endpoint())  # fallback for THIS endpoint
        else:
            self._text_endpoints.discard(self._endpoint())
            self._base_mode = value

    @property
    def client(self) -> AsyncOpenAI:
        # Rebuild when the active profile changes base_url (in-UI model
        # switch, 2026-10-23) — a cached client is bound to one endpoint.
        # Also rebuild when the key changes (a key saved from the Model
        # profiles panel applies live, 2026-11-08).
        if (self._client is None or self._client_url != settings.base_url
                or self._client_key != settings.api_key):
            if not settings.base_url:
                raise LLMError("OPENAI_BASE_URL is not set — edit .env")
            # (the text-protocol fallback is per endpoint — see `mode` — so
            # a switch needs no reset here)
            self._client = AsyncOpenAI(base_url=settings.base_url,
                                       api_key=settings.api_key)
            self._client_url = settings.base_url
            self._client_key = settings.api_key
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
            messages=adapt_messages(messages),
            stream=True,
            stream_options={"include_usage": True},  # final-chunk usage → tok/s
            temperature=temperature,
        )
        cap = settings.effective_max_tokens() if hasattr(settings, "effective_max_tokens") \
            else settings.max_tokens
        if cap > 0:
            kwargs["max_tokens"] = cap
        return kwargs

    async def _create(self, kwargs: dict, what: str):
        """chat.completions.create with (a) the bounded transient retry and
        (b) up to 3 parameter repairs when the endpoint 400s on a param it
        doesn't support (see _adapt_kwargs_after_error) — muji's request
        shape was tuned for vLLM/Qwen, other providers are stricter."""
        repairs = 0
        attempt = 0
        while True:
            try:
                return await self.client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                fix = _adapt_kwargs_after_error(kwargs, str(e)) if repairs < 3 else None
                if fix:
                    repairs += 1
                    log.warning("%s: endpoint rejected a parameter (%s) — retrying with %s",
                                what, str(e)[:160], fix)
                    continue
                if not _is_retryable(e) or attempt >= _MAX_RETRIES:
                    if is_tool_call_failure(str(e)):
                        raise ToolCallFailed(str(e)) from e
                    raise LLMError(str(e)) from e
                delay = _BACKOFFS[attempt]
                attempt += 1
                log.warning("%s attempt %d/%d failed: %s — retry in %ds",
                            what, attempt, _MAX_RETRIES + 1, e, delay)
                await asyncio.sleep(delay)

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
        stream = await self._create(kwargs, "stream create")
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
            # 2026-11-08: a MALFORMED tool call (Groq `tool_use_failed`,
            # "Failed to call a function", unparseable args) is the model's
            # output, not proof the server lacks tool support — it used to
            # flip the turn to the text protocol, the next request went out
            # without tools, and Groq died on "Tool choice is none, but model
            # called a tool". Surface it as retryable instead.
            if is_tool_call_failure(msg):
                raise ToolCallFailed(msg) from e
            if self.mode == "auto" and tools and _tools_unsupported(low):
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
        kwargs = dict(model=settings.model, messages=adapt_messages(messages),
                      temperature=temperature, stream=False)
        if settings.max_tokens > 0:  # same per-call cap as stream (2026-10-08)
            kwargs["max_tokens"] = settings.max_tokens
            if hasattr(settings, "effective_max_tokens"):  # per-profile cap
                kwargs["max_tokens"] = settings.effective_max_tokens() or settings.max_tokens
        extra = _thinking_kwargs(thinking if thinking is not None
                                 else settings.thinking)
        if extra:
            kwargs["extra_body"] = extra
        resp = await self._create(kwargs, "complete")
        return (resp.choices[0].message.content or "") if resp.choices else ""
