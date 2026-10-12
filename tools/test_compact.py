"""Quick sanity checks for context compaction (no model call needed)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import est_tokens, _compact_context, _clear_old_tool_results  # noqa: E402
from src.config import settings  # noqa: E402


def test_est_tokens():
    msgs = [{"role": "user", "content": "a" * 4000}]
    assert est_tokens(msgs) == 1000, est_tokens(msgs)
    msgs2 = [{"role": "assistant", "content": "",
              "tool_calls": [{"function": {"name": "read_file",
                                           "arguments": "{" + "x" * 4000 + "}"}}]}]
    assert est_tokens(msgs2) == (4000 + len("read_file")) // 4, est_tokens(msgs2)
    assert est_tokens([{"role": "user", "content": "x" * 3}]) == 0
    print("est_tokens ok")


def test_compact_shape(monkeypatch_llm=None):
    """_compact_context must keep [system, summary, *recent] and drop the old part."""
    calls = []

    async def fake_complete(messages, temperature=0.2, thinking=None):
        calls.append((messages, thinking))
        return "CONTEXT SUMMARY:\n- task: build a thing\n- files: a.py (main)"

    import src.agent as agent
    orig = agent._llm.complete
    agent._llm.complete = fake_complete
    try:
        msgs = ([{"role": "system", "content": "SYS"}]
                + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                   for i in range(12)])
        out = asyncio.run(_compact_context(msgs, lambda *a: None, "sess"))
        assert out is not None
        assert out[0] == {"role": "system", "content": "SYS"}
        assert out[1]["role"] == "user"
        assert "CONTEXT SUMMARY:" in out[1]["content"]
        assert out[1]["content"].startswith("(Earlier conversation compacted")
        assert [m["content"] for m in out[2:]] == ["m8", "m9", "m10", "m11"]
        assert len(calls) == 1
        # compaction runs at thinking=low (real summarization, no tools) —
        # never the server default xhigh (09-28 metering work)
        assert calls[0][1] == "low", calls[0][1]
        # the summarizer saw the old part, not the system/recent
        blob = calls[0][0][1]["content"]
        assert "m0" in blob and "m7" in blob and "SYS" not in blob and "m8" not in blob
        print("compact shape ok")
    finally:
        agent._llm.complete = orig


def test_compact_failure_returns_none():
    async def boom(messages, temperature=0.2, thinking=None):
        raise RuntimeError("model down")

    import src.agent as agent
    orig = agent._llm.complete
    agent._llm.complete = boom
    try:
        msgs = [{"role": "system", "content": "SYS"}] + [
            {"role": "user", "content": f"m{i}"} for i in range(12)]
        out = asyncio.run(_compact_context(msgs, lambda lvl, msg: None, "sess"))
        assert out is None
        print("compact failure ok")
    finally:
        agent._llm.complete = orig


def test_compact_too_short():
    import src.agent as agent
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    out = asyncio.run(_compact_context(msgs, lambda *a: None, "sess"))
    assert out is None
    print("compact too-short ok")


def _tool_msgs(n_old=6, recent=4):
    """[system, (assistant-call, tool-result) x n_old, tail x recent]"""
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(n_old):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}",
                     "content": "x" * 5000})
    for i in range(recent):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"tail{i}"})
    return msgs


def test_ctx_tokens_floor():
    """Turn-boundary compaction must use max(est_tokens, sessions.ctx_tokens)
    as its trigger input (2026-10-12, TA-session bug): est_tokens is a char/4
    estimate that undercounts the real provider count by ~10k (tool schemas +
    API overhead), so a session running at 107k REAL tokens can estimate
    under the 100k trigger and never compact. The previous turn's llm_end
    already persists the real count to sessions.ctx_tokens — use it as a
    floor so the LLM summary fires when the provider says so."""
    import src.agent as agent
    src = Path(agent.__file__).read_text(encoding="utf-8")
    # the floor exists and feeds BOTH the outer gate and the post-clear gate
    assert "real_ctx = int(sess.get(\"ctx_tokens\") or 0)" in src
    assert "ctx_est = max(est_tokens(messages), real_ctx)" in src
    assert "and ctx_est >= settings.compact_trigger" in src
    assert src.count("max(est_tokens(messages), real_ctx)") >= 2

    trigger = settings.compact_trigger

    def fires(est: int, real: int) -> bool:
        return max(est, real) >= trigger

    # the TA-session shape: real 107k, estimate ~95k (under trigger) → fires
    assert fires(95_000, 107_337) is True
    # both under → no fire (round 1 / fresh session)
    assert fires(50_000, 40_000) is False
    # estimate over, no real read yet (ctx_tokens NULL → 0) → fires (fallback)
    assert fires(105_000, 0) is True
    # real under, estimate over → fires (max, not min)
    assert fires(110_000, 80_000) is True
    print("ctx_tokens floor ok")


def test_clear_stubs_old_only():
    """Old tool results become stubs; the keep-tail is untouched; tool
    CALLS and conversation text survive; the stub keeps the receipt."""
    msgs = _tool_msgs()
    freed = _clear_old_tool_results(msgs, 4)
    assert freed > 0
    # old tool messages are stubbed with the tool name (pair: assistant
    # at 1+2i, tool at 2+2i)
    for i in range(6):
        m = msgs[2 + 2 * i]
        assert m["role"] == "tool"
        assert m["content"].startswith("[cleared: read_file"), m["content"]
        assert "5,000 chars" in m["content"]
        assert m["tool_call_id"] == f"call_{i}"  # id intact (API-safe)
    # assistant tool-call messages untouched
    for i in range(6):
        assert msgs[1 + 2 * i]["tool_calls"][0]["function"]["name"] == "read_file"
    # keep-tail verbatim
    assert [m["content"] for m in msgs[-4:]] == ["tail0", "tail1", "tail2", "tail3"]
    # idempotent: second pass frees nothing
    assert _clear_old_tool_results(msgs, 4) == 0
    print("clear stubs-old-only ok")


def test_clear_keeps_recent_tool_results():
    """A tool result INSIDE the keep-tail must stay verbatim (the model
    is actively working with it)."""
    msgs = _tool_msgs(n_old=2, recent=6)  # 2 old pairs + 6 tail
    # tail contains one live tool pair
    msgs[-2] = {"role": "assistant", "content": "",
                "tool_calls": [{"id": "call_live", "type": "function",
                                "function": {"name": "run_command",
                                             "arguments": "{}"}}]}
    live = "x" * 5000
    msgs[-1] = {"role": "tool", "tool_call_id": "call_live", "content": live}
    _clear_old_tool_results(msgs, 6)
    assert msgs[-1]["content"] == live  # untouched
    assert msgs[2]["content"].startswith("[cleared: read_file")  # old one stubbed
    print("clear keeps-recent ok")


def test_clear_text_mode_tool_result():
    """Text mode: user-role TOOL_RESULT messages get the same treatment."""
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "assistant", "content": "let me check"},
            {"role": "user", "content": "TOOL_RESULT:\n" + "y" * 4000},
            {"role": "assistant", "content": "done checking"},
            {"role": "user", "content": "tail0"},
            {"role": "assistant", "content": "tail1"},
            {"role": "user", "content": "tail2"},
            {"role": "assistant", "content": "tail3"}]
    _clear_old_tool_results(msgs, 4)
    assert msgs[2]["content"].startswith("TOOL_RESULT:\n[cleared")
    assert "4,001 chars" in msgs[2]["content"]  # body = "\n" + 4000 y's
    # conversation text in the old region is untouched
    assert msgs[1]["content"] == "let me check"
    assert msgs[3]["content"] == "done checking"
    # keep-tail verbatim
    assert [m["content"] for m in msgs[-4:]] == ["tail0", "tail1", "tail2", "tail3"]
    print("clear text-mode ok")


def test_clear_too_short():
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"}]
    assert _clear_old_tool_results(msgs, 4) == 0
    print("clear too-short ok")


def test_compact_prompt_preserves_workarounds():
    """The compaction prompt must explicitly preserve workarounds/gotchas
    (the class of detail that dies by default — anthropics/claude-code
    #10232)."""
    from src.agent import COMPACT_PROMPT
    assert "workarounds" in COMPACT_PROMPT
    assert "gotchas" in COMPACT_PROMPT
    print("compact prompt workarounds ok")


def test_settings():
    import os
    assert settings.compact_enabled is True
    # The ambient environment can override the config defaults (running from
    # a shell that inherited ANOTHER muji's env), so compare against what
    # config actually reads — not a hardcoded number.
    assert settings.compact_trigger == int(
        os.environ.get("COMPACT_TRIGGER", "100000"))
    assert settings.compact_boundary == int(
        os.environ.get("COMPACT_BOUNDARY", "256000"))
    assert settings.compact_trigger < settings.compact_boundary
    assert settings.compact_context_limit == int(
        os.environ.get("COMPACT_CONTEXT_LIMIT", "265000"))
    assert settings.compact_recent == int(os.environ.get("COMPACT_RECENT", "4"))
    # mid-turn lossless clear: enabled by default (boss re-enabled
    # 2026-10-13) unless the env explicitly turns it off
    assert settings.compact_mid_turn_clear == (
        os.environ.get("COMPACT_MID_TURN_CLEAR", "1") == "1")
    print("settings ok")


def test_mid_turn_clear_wiring():
    """The per-round tool-result stubbing must be gated on
    COMPACT_MID_TURN_CLEAR (on by default since 2026-10-13): the mid-turn
    clear call sits behind the flag, while the overflow recovery's
    aggressive clear (keep-tail 1) stays ungated — it's the backstop."""
    import src.agent as agent
    src = Path(agent.__file__).read_text(encoding="utf-8")
    # the gate: flag in the mid-turn condition
    assert "settings.compact_mid_turn_clear" in src
    gate = src.index("settings.compact_mid_turn_clear")
    mid = src.index("cleared old tool results mid-turn")
    assert gate < mid, "flag must gate the mid-turn clear, not follow it"
    # the mid-turn clear is the only call site gated by the flag
    assert src.count("settings.compact_mid_turn_clear") == 1
    # overflow recovery's aggressive clear is untouched (no flag between
    # the 'Context overflow — recovering' marker and the keep-tail-1 call)
    rec = src.index("Context overflow — recovering")
    rec_clear = src.index("_clear_old_tool_results(messages, 1)")
    assert rec < rec_clear
    assert "settings.compact_mid_turn_clear" not in src[rec:rec_clear]
    # default is ON (re-enabled 2026-10-13)
    assert settings.compact_mid_turn_clear is True
    print("mid-turn clear wiring ok")


def test_unlimited_loop_condition():
    """The agent loop must run forever when MAX_TURNS=0 (compaction paces
    the run) and still stop at the cap when it is set."""
    import src.agent as agent
    src = Path(agent.__file__).read_text(encoding="utf-8")
    cond = "settings.max_turns <= 0 or turn < settings.max_turns"
    assert cond in src, "unlimited loop condition missing from agent.py"

    def loop_runs(max_turns: int, cap: int = 10_000) -> int:
        """Simulate the while-condition; return iterations actually run."""
        turn, ran = 0, 0
        while max_turns <= 0 or turn < max_turns:
            turn += 1
            ran += 1
            if ran >= cap:
                break
        return ran

    assert loop_runs(0) == 10_000      # unlimited: never self-terminates
    assert loop_runs(5) == 5           # capped: stops exactly at the cap
    print("unlimited loop condition ok")


def test_real_token_trigger():
    """The trigger must prefer the provider's real prompt_tokens over the
    char/4 estimate (Cline's approach): real 150k with a hot estimate must
    NOT fire (that was the early-compaction bug), real 221k with a tiny
    estimate MUST fire, and the estimate is the fallback when no real read
    exists yet (round 1 / post-compact)."""
    import src.agent as agent
    src = Path(agent.__file__).read_text(encoding="utf-8")
    assert "last_prompt_tokens: int | None = None" in src
    assert "ctx_now = (last_prompt_tokens" in src
    assert "else est_tokens(messages))" in src
    assert "and ctx_now >= settings.compact_trigger" in src
    # feed: the round's real read lands on the trigger variable
    assert "last_prompt_tokens = prompt_tokens" in src
    # reset: post-compact the stale pre-compact real number must not re-fire
    assert src.index("last_prompt_tokens = None") > src.index("messages = compacted")

    def ctx_now(last, msgs):
        return last if last is not None else est_tokens(msgs)

    hot_est = [{"role": "user", "content": "a" * 880000}]  # est = 220k
    assert ctx_now(None, hot_est) >= 220000              # estimate fires (fallback)
    assert ctx_now(150000, hot_est) < 220000             # real 150k: NO early fire
    assert ctx_now(221000, [{"role": "user", "content": "hi"}]) >= 220000
    print("real-token trigger ok")


def test_overflow_detection():
    """_is_context_overflow must catch the real provider error strings
    (OpenAI/vLLM/SGLang) and NOT catch unrelated LLM errors."""
    from src.agent import _is_context_overflow
    hits = [
        "Error code: 400 - {'error': {'message': 'This model's maximum "
        "context length is 262144 tokens. However, your messages "
        "contained 270000 tokens', 'type': 'invalid_request_error'}}",  # OpenAI
        "Maximum context length is 262144 tokens, but you requested "
        "270000 (260000 output + 10000 input)",                        # vLLM
        "This model's maximum context length is 32768 tokens. However, "
        "you requested 2048 output tokens and your prompt contains "
        "32000 input tokens",                                            # SGLang
        "context window exceeded",
        "reduce the length of the messages",
    ]
    for h in hits:
        assert _is_context_overflow(h), h
    # rate-limit / unrelated strings must NOT trip recovery: 'rate limit
    # exceeded' contains both a weak verb and 'limit' — the exclusion
    # tier is what keeps it out
    for m in ("Model error: 429 rate limit exceeded for API key",
              "connection timed out after 30s",
              "no such model: qwen3-235b",
              "tool 'run_command' not found"):
        assert not _is_context_overflow(m), m
    print("overflow detection ok")


def test_hard_truncate():
    """_hard_truncate: drops oldest MIDDLE messages (system + tail 8
    survive), drops tool responses WITH their assistant call (no orphans),
    strips an orphaned tool message left at the front, and is a no-op
    when already under budget."""
    from src.agent import _hard_truncate
    msgs = [{"role": "system", "content": "SYS"}]
    for i in range(20):  # 20 old (call, result) pairs
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}",
                     "content": "x" * 20000})
    for i in range(8):  # keep-tail
        msgs.append({"role": "user", "content": f"tail{i}"})
    n0 = len(msgs)
    dropped = _hard_truncate(msgs, 100000)  # 400k chars in, 100k budget
    assert dropped > 0
    assert len(msgs) == n0 - dropped
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert [m["content"] for m in msgs[-8:]] == [f"tail{i}" for i in range(8)]
    # no orphaned tool messages: every tool response's call id must exist
    ids = set()
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            ids.add(tc.get("id"))
    for m in msgs[1:]:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in ids, "orphaned tool response"
    # already-under-budget: no-op
    small = [{"role": "system", "content": "SYS"},
             {"role": "user", "content": "hi"}]
    assert _hard_truncate(small, 100000) == 0
    assert small[1]["content"] == "hi"
    print("hard truncate ok")


def test_recovery_wiring():
    """The agent loop must wire recovery: overflow flag init, the
    LLMError catch after the ToolUnsupported catch, the no-partial-output
    guard, the aggressive clear + hard-truncate, the once-only retry,
    and the re-raise for everything else."""
    import src.agent as agent
    src = Path(agent.__file__).read_text(encoding="utf-8")
    assert "overflow_retried = False" in src
    assert "except LLMError as e:" in src
    assert src.index("except LLMError as e:") > src.index("except ToolUnsupported as e:")
    assert "_is_context_overflow(str(e))" in src
    assert "not overflow_retried" in src
    # no-partial-output guard: text, tool timeline, tool calls, AND thinking
    assert "not buf and not draft and not tool_calls" in src
    assert "len(thinking_text) == think_t0" in src
    # shrink steps: aggressive clear (keep-tail 1) then hard truncate
    assert "_clear_old_tool_results(messages, 1)" in src
    assert "_hard_truncate(" in src
    # the retry is the SAME round (continue, not a fresh user turn) and
    # the stale real read is reset
    assert "last_prompt_tokens = None  # pre-overflow read is stale" in src
    # a second overflow (or non-overflow LLMError) re-raises to the
    # outer handler — the turn dies instead of looping forever
    tail = src[src.index("except LLMError as e:"):]
    assert "raise" in tail
    print("recovery wiring ok")


if __name__ == "__main__":
    test_est_tokens()
    test_real_token_trigger()
    test_ctx_tokens_floor()
    test_clear_stubs_old_only()
    test_clear_keeps_recent_tool_results()
    test_clear_text_mode_tool_result()
    test_clear_too_short()
    test_compact_prompt_preserves_workarounds()
    test_compact_shape()
    test_compact_failure_returns_none()
    test_compact_too_short()
    test_settings()
    test_unlimited_loop_condition()
    test_overflow_detection()
    test_hard_truncate()
    test_recovery_wiring()
    test_mid_turn_clear_wiring()
    print("ALL OK")
