"""Smoke test for the OnIt feature port (pass-by-reference, trajectories,
run-state). No server, no model: points settings at temp paths, exercises
the pure functions + SQLite layer directly.

Run:  .venv\Scripts\python.exe tools\test_onit.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import time

# console encoding: the Store-Python default is cp1252 on this box and the
# section banners below are non-ASCII — don't let a plain `python tools/test_onit.py`
# crash before the first check
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import src.config as config  # noqa: E402

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="muji_onit_test_"))
config.settings.db_path = _tmp / "test.db"
config.settings.results_dir = _tmp / "results"
config.settings.results_dir.mkdir(parents=True, exist_ok=True)
config.settings.learned_path = _tmp / "learned.md"

from src import agent, db, tools  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


print("── 1. pass-by-reference (spill to file → read_file/search_files) ──")
small = "line\n" * 10
check("small result stays inline", tools.spill_result(small) == small)

big = "".join(f"log line {i}: value={i * 7}\n" for i in range(2000))
spilled = tools.spill_result(big, "run_command")
check("big result keeps a preview", "log line 0: value=0" in spilled)
check("big result is shorter than input", len(spilled) < len(big))
check("big result points at a file", "full text saved to " in spilled, spilled[:120])
spill_path = pathlib.Path(spilled.split("full text saved to ")[1].split("]")[0].strip())
check("full text on disk", spill_path.is_file() and spill_path.read_text(encoding="utf-8") == big)

# recovery now goes through the fundamental fs tools — the dedicated
# result_read / result_grep wrappers are gone from the registry.
names = [t["name"] for t in tools.TOOLS]
check("result_read removed", "result_read" not in names)
check("result_grep removed", "result_grep" not in names)
check("result_read no longer auto-approved", tools.category_of("result_read") is None)
check("base prompt teaches path-based spill",
      "read_file(path" in agent.BASE_PROMPT and "result_read" not in agent.BASE_PROMPT)
# rot guard (BACKLOG 09-26 thinking-loop, step 3): the anti-overthinking
# rule landed 09-18 and silently vanished in a later prompt refactor —
# its presence is now asserted, not remembered.
check("base prompt carries the anti-overthinking rule",
      "Don't overthink" in agent.BASE_PROMPT)

print("── 2. trajectories + learned.md ──────────────────────────────")
db.log_trajectory("s1", "compare two repos and explain the speed difference",
                  ["list_dir", "read_file"], "muji is faster because…",
                  "all claims checked with tools",
                  "user wanted plain-talk explanation, not a table", "done")
db.log_trajectory("s2", "totally unrelated cooking question",
                  ["web_search"], "…", "asserted", None, "done")

hits = db.recall_trajectories("explain the speed difference between the two repos")
check("recall finds the similar task", len(hits) == 1 and "compare two repos" in hits[0]["goal"])
check("recall ignores unrelated", all("cooking" not in (h["goal"] or "") for h in hits))
check("recall needs overlap", db.recall_trajectories("zzz") == [])

block = agent._recall_block("explain the speed difference between the two repos")
check("recall block built", "PREVIOUS SIMILAR TASKS" in block and "plain-talk" in block, block[:200])
check("recall block empty when no match", agent._recall_block("zzz") == "")

agent._learned_append_pending("test pattern alpha")
agent._learned_append_pending("test pattern alpha")  # dup → no-op
agent._learned_append_pending("test pattern beta")   # newer → must land on top
learned = config.settings.learned_path.read_text(encoding="utf-8")
check("pending append", "test pattern alpha" in learned)
check("pending dedupe", learned.count("test pattern alpha") == 1)
check("pending newest on top (Boss rule)",
      learned.index("test pattern beta") < learned.index("test pattern alpha"))

sysp = agent.build_system(config.settings.root_dir, False)
check("pending NOT injected", "test pattern alpha" not in sysp)
# Boss moves it to active
learned = learned.replace("## pending\n", "## pending\n").replace(
    "## active\n", "## active\n- test pattern alpha\n", 1)
config.settings.learned_path.write_text(learned, encoding="utf-8")
sysp = agent.build_system(config.settings.root_dir, False)
check("active IS injected", "test pattern alpha" in sysp)

print("── 2b. WORKSPACE.md shared-state injection ───────────────────")
sysp = agent.build_system(config.settings.root_dir, False)
check("workspace block injected (cwd-independent shared state)",
      "WORKSPACE.md" in sysp and "REPOS.md" in sysp, sysp[:200])
check("workspace block lists the backlog", "BACKLOG.md" in sysp)
check("workspace block lists learned + tool_notes",
      "learned.md" in sysp and "tool_notes.md" in sysp)
check("workspace block lists session registry", "sessions" in sysp)
# missing file → empty block, never a crash
ws_p = pathlib.Path(agent.__file__).resolve().parent.parent / "WORKSPACE.md"
ws_txt = ws_p.read_text(encoding="utf-8")
try:
    ws_p.unlink()
    check("workspace block empty when file missing", agent._workspace_block() == "")
finally:
    ws_p.write_text(ws_txt, encoding="utf-8")

print("── 2c. TL;DR enforcement (deterministic backstop) ────────────")
# multi-paragraph final answer WITHOUT a TL;DR → flagged
long_no = "line one\n\nline two\n\nline three\n\nline four\n"
check("multi-para without TL;DR is flagged",
      agent._needs_tldr_flag(long_no) is True)
# same answer WITH a TL;DR → clean
check("multi-para with TL;DR is clean",
      agent._needs_tldr_flag(long_no + "\n**TL;DR:** done.") is False)
check("TL;DR variant (TL;DR —) is clean",
      agent._needs_tldr_flag(long_no + "\n**TL;DR** — done.") is False)
# short replies stay exempt (one-liner, status ping)
check("one-liner not flagged", agent._needs_tldr_flag("done, pushed abc123") is False)
check("3-line reply not flagged",
      agent._needs_tldr_flag("a\nb\nc") is False)
# code fences + Chips section don't count as body — a code dump or a
# chips-only closer must never false-flag
code_dump = "```\n" + "x = 1\n" * 30 + "```\n"
check("code dump not flagged", agent._needs_tldr_flag(code_dump) is False)
chips_only = "## Chips\n- act: do the thing\n- plan: other thing\n"
check("chips-only closer not flagged", agent._needs_tldr_flag(chips_only) is False)
# word-count trigger: >=150 words, even on one line
blob = " ".join(["word"] * 160)
check("150+ words on one line flagged", agent._needs_tldr_flag(blob) is True)
# the flag note is appended exactly once, and only when due
flagged = agent._flag_tldr(long_no)
check("flag note appended", "TL;DR flag" in flagged and flagged.startswith("line one"))
check("flag note not double-appended",
      agent._flag_tldr(flagged).count("TL;DR flag") == 1)
check("flag is a no-op when clean",
      agent._flag_tldr(long_no + "\n**TL;DR:** done.") == long_no + "\n**TL;DR:** done.")
# flag note on a chips-carrying answer must land BEFORE ## Chips (after =
# invisible, extractChips strips the tail)
flagged_chips = agent._flag_tldr(long_no + "\n## Chips\n- act: a")
check("flag note before chips",
      "TL;DR flag" in flagged_chips
      and flagged_chips.index("TL;DR flag") < flagged_chips.index("## Chips"))
# TL;DR written AFTER ## Chips is invisible in the UI (extractChips strips
# everything from the header on) — _normalize_tldr_order moves it before
after_chips = long_no + "\n## Chips\n- act: do the thing\n\n**TL;DR** — done."
norm = agent._normalize_tldr_order(after_chips)
check("TL;DR moved before chips",
      norm.index("TL;DR") < norm.index("## Chips"))
check("chips section intact after move",
      "- act: do the thing" in norm and norm.count("## Chips") == 1)
check("TL;DR text preserved", "done." in norm)
check("correct order is a no-op",
      agent._normalize_tldr_order(long_no + "\n**TL;DR:** x\n\n## Chips\n- act: a")
      == long_no + "\n**TL;DR:** x\n\n## Chips\n- act: a")
check("no TL;DR is a no-op",
      agent._normalize_tldr_order(after_chips.replace("**TL;DR** — done.", ""))
      == after_chips.replace("**TL;DR** — done.", ""))
check("no chips is a no-op",
      agent._normalize_tldr_order(long_no) == long_no)
# a multi-line TL;DR paragraph moves intact
multi = long_no + "\n## Chips\n- act: a\n\n**TL;DR** — first line\nsecond line.\n"
norm2 = agent._normalize_tldr_order(multi)
check("multi-line TL;DR moves intact",
      "first line\nsecond line." in norm2 and norm2.index("TL;DR") < norm2.index("## Chips"))
# head already has a TL;DR + model ALSO wrote one after the chips (msg
# 3027, 2026-09-25) → the invisible tail copy is dropped, head keeps its
dup = (long_no + "\n**TL;DR:** real one\n## Chips\n- act: a\n\n"
       "**TL;DR:** real one\n")
norm3 = agent._drop_tldr_after_chips(dup)
check("dup TL;DR after chips dropped",
      norm3.count("TL;DR") == 1 and norm3.index("TL;DR") < norm3.index("## Chips")
      and "- act: a" in norm3)
check("drop is a no-op when no dup",
      agent._drop_tldr_after_chips(long_no + "\n**TL;DR:** x\n## Chips\n- act: a")
      == long_no + "\n**TL;DR:** x\n## Chips\n- act: a")
check("drop leaves sole tail TL;DR for the mover",
      agent._drop_tldr_after_chips(after_chips) == after_chips)
check("normalize still moves the sole tail TL;DR",
      agent._normalize_tldr_order(after_chips).index("TL;DR")
      < agent._normalize_tldr_order(after_chips).index("## Chips"))
# the sidebar counter: bump ticks the session's flag count (schema
# migration adds tldr_flags to the live DB)
sid_f = db.create_session()["id"]
check("fresh session has 0 flags",
      db.get_session(sid_f)["tldr_flags"] == 0)
check("bump returns new count 1", db.bump_tldr_flags(sid_f) == 1)
check("bump returns new count 2", db.bump_tldr_flags(sid_f) == 2)
check("list_sessions carries the count",
      any(s["id"] == sid_f and s["tldr_flags"] == 2
          for s in db.list_sessions()))
# the counter is a debt, not a lifetime tally: a rule-following final
# answer clears it (the agent hook calls clear_tldr_flags when the answer
# carries a TL;DR and the count is > 0)
check("clear returns new count 0", db.clear_tldr_flags(sid_f) == 0)
check("clear is idempotent", db.clear_tldr_flags(sid_f) == 0)
check("bump still works after clear", db.bump_tldr_flags(sid_f) == 1)

print("── 3. run_state ──────────────────────────────────────────────")
msgs = [{"role": "system", "content": "sys"},
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "x", "content": "ok"}]
db.save_run_state("s3", 2, ["read docs", "write code"], {0}, msgs, "do the thing")
st = db.load_run_state("s3")
check("run_state round-trip", st is not None and st["turn"] == 2
      and st["plan"] == ["read docs", "write code"] and st["done"] == {0}
      and st["messages"] == msgs and st["goal"] == "do the thing")
db.save_run_state("s3", 3, ["read docs", "write code"], {0, 1}, msgs + [{"role": "user", "content": "more"}], "do the thing")
st = db.load_run_state("s3")
check("run_state upsert (not duplicate)", st["turn"] == 3 and st["done"] == {0, 1})
db.clear_run_state("s3")
check("run_state cleared", db.load_run_state("s3") is None)

# stop-park flag: a deliberate Stop keeps the row (triangle/↻ stay) but
# hides it from auto-resume; a live checkpoint un-parks it
db.save_run_state("s3p", 1, ["a"], set(), msgs, "parked")
check("fresh checkpoint is unstopped",
      db.has_run_state("s3p") and db.has_unstopped_run_state("s3p"))
db.set_run_state_stopped("s3p", True)
check("stopped flag: row stays (triangle/↻ affordance)",
      db.has_run_state("s3p"))
check("stopped flag: hidden from auto-resume",
      not db.has_unstopped_run_state("s3p"))
db.save_run_state("s3p", 2, ["a", "b"], {0}, msgs, "parked")
check("live checkpoint re-parks → unstopped again",
      db.has_unstopped_run_state("s3p"))
db.set_run_state_stopped("s3p", False)
db.clear_run_state("s3p")
check("set_run_state_stopped on missing row is a no-op",
      db.set_run_state_stopped("s3p", True) is None)

print("── 4. schema sanity (public build: personal tools opt-in) ─────")
# Default (TOOLS_DISABLED unset) = the 10 personal-integration tools are
# disabled, so the schema advertises the 13-tool core. (We mutate the
# settings attribute to exercise opt-in — openai_schemas reads it live, so
# no module reload, which would desync the agent's held references.)
_default_disabled = config.settings.tools_disabled
schemas = {s["function"]["name"] for s in tools.openai_schemas()}
check("core tools advertised by default", len(schemas) == 16, str(len(schemas)))
check("set_cwd + note_fact present by default (session memory, not personal)",
      {"set_cwd", "note_fact"} <= schemas)
check("search_transcript present (transcript recall)", "search_transcript" in schemas)
check("verify_math present (sympy TA-checker)", "verify_math" in schemas)
check("personal tools OFF by default (opt-in)",
      not ({"tg_search", "tg_read", "gmail_search", "gmail_read",
            "tasks_list", "events_add"} & schemas))
check("removed spill-wrapper tools absent", not ({"result_read", "result_grep"} & schemas))
check("IMAP email tools absent",
      not ({"email_list", "email_read", "email_search", "email_folders"} & schemas))
# Opt-IN: an empty disabled set re-enables every tool (24 total).
config.settings.tools_disabled = frozenset()
schemas_on = {s["function"]["name"] for s in tools.openai_schemas()}
check("opt-in (disabled=∅) enables all 26 tools", len(schemas_on) == 26, str(len(schemas_on)))
check("set_cwd + note_fact present (session memory)",
      {"set_cwd", "note_fact"} <= schemas_on)
check("personal tools present when opted in",
      {"tg_search", "tg_read", "gmail_search", "gmail_read",
       "tasks_list", "tasks_add", "tasks_done",
       "events_add", "events_list", "events_done"} <= schemas_on)
# Restore the default (personal off) for the rest of the suite.
config.settings.tools_disabled = _default_disabled

print("── 4b. set_cwd (chat-driven working-folder move) ────────────")
# the boss asks in chat "move the working directory to X" → set_cwd
# persists it on the session (Files tab + terminal pwd survive reloads)
# AND re-points ctx.cwd (every later tool + run_command in the run).
_sid_cwd = db.create_session()["id"]
_dir_a = _tmp / "work_a"; _dir_a.mkdir(exist_ok=True)
_dir_b = _tmp / "work_b"; _dir_b.mkdir(exist_ok=True)
_ctx_cwd = tools.ToolCtx(cwd=_dir_a, session_id=_sid_cwd)
out = tools.dispatch(_ctx_cwd, "set_cwd", {"path": str(_dir_b)})
check("set_cwd: result announces the new folder",
      str(_dir_b) in out and "Working directory" in out, out[:120])
check("set_cwd: ctx.cwd re-pointed (rest of the run works there)",
      _ctx_cwd.cwd == _dir_b)
check("set_cwd: persisted on the session (survives reload)",
      (db.get_session(_sid_cwd) or {}).get("cwd") == str(_dir_b))
check("set_cwd: relative path resolves against the current cwd",
      _ctx_cwd.cwd == _dir_b
      and tools.dispatch(_ctx_cwd, "set_cwd", {"path": "."}).startswith(
          "Working directory for this chat is now: " + str(_dir_b)))
try:
    tools.dispatch(_ctx_cwd, "set_cwd", {"path": str(_tmp / "nope_missing")})
    _bad = ""
except tools.ToolError as e:
    _bad = str(e)
check("set_cwd: missing dir is a ToolError", "not a directory" in _bad, _bad)

print("── 4c. note_fact (per-session durable notes) ─────────────────")
check("note_fact rides the edit category", tools.category_of("note_fact") == "edit")
check("set_cwd rides the read category", tools.category_of("set_cwd") == "read")
check("compaction prompt preserves identifiers verbatim",
      "VERBATIM" in agent.COMPACT_PROMPT and "connection strings" in agent.COMPACT_PROMPT)
check("base prompt teaches the grounding rule", "Grounding" in agent.BASE_PROMPT)
_notes_sid = db.create_session()["id"]
_ctx_n = tools.ToolCtx(cwd=_tmp, session_id=_notes_sid)
out = tools.dispatch(_ctx_n, "note_fact", {"fact": "ssh: boss@10.0.4.22:2222"})
_npath = config.settings.data_dir / "sessions" / _notes_sid / "task_notes.md"
check("note_fact: file created at the fixed per-session path", _npath.is_file())
check("note_fact: fact appended under ## Facts",
      "## Facts" in _npath.read_text(encoding="utf-8") and "boss@10.0.4.22:2222" in _npath.read_text(encoding="utf-8"))
tools.dispatch(_ctx_n, "note_fact", {"fact": "port is 2222, not 22"})
check("note_fact: second append keeps the first",
      _npath.read_text(encoding="utf-8").count("- ") >= 2)
sys_prompt = agent.build_system(_tmp, False, False, "chill", "", session_id=_notes_sid)
check("build_system injects the session notes",
      "SESSION NOTES" in sys_prompt and "boss@10.0.4.22:2222" in sys_prompt)
sys_prompt_empty = agent.build_system(_tmp, False, False, "chill", "",
                                      session_id=db.create_session()["id"])
check("build_system: no-notes session gets the hint",
      "no task notes yet" in sys_prompt_empty)
try:
    tools.dispatch(tools.ToolCtx(cwd=_tmp), "note_fact", {"fact": "x"})
    _nf_bad = ""
except tools.ToolError as e:
    _nf_bad = str(e)
check("note_fact: no session id is a ToolError", "session" in _nf_bad, _nf_bad)

print("── 5. resume E2E (mocked model stream) ───────────────────────")
import asyncio  # noqa: E402

RESUME = ("Continue — the previous run was stopped mid-task. Pick up exactly "
          "where you left off (build on what was already done, don't redo it) "
          "and finish the job.")

sess = db.create_session()
sid = sess["id"]
db.add_message(sid, "user", "do the thing")
saved_msgs = [
    {"role": "user", "content": "do the thing"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "list_dir", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "file.txt"},
]
db.save_run_state(sid, 2, ["read docs", "write code"], {0}, saved_msgs, "do the thing")

captured: dict = {}


class MockLLM:
    mode = "native"

    async def stream(self, messages, tools=None, thinking=None):
        captured["messages"] = messages
        yield {"type": "thinking", "text": "hmm "}
        yield {"type": "token", "text": "done "}
        await asyncio.sleep(0.05)  # real decode span so tok_s is measurable
        yield {"type": "token", "text": "now"}
        yield {"type": "end", "finish_reason": "stop"}
        # usage arrives AFTER end (real vLLM order: choices-less chunk post
        # finish_reason) — llm.py must not let the choices guard swallow it
        yield {"type": "usage", "completion_tokens": 2,
               "prompt_tokens": 10}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: all claims checked\nPATTERN: NONE"


orig_llm = agent._llm
agent._llm = MockLLM()
try:
    frames: list[str] = []

    async def _run():
        async for f in agent.run_chat(sid, RESUME, [], lambda *a: None,
                                      "act", "chill", resume=True):
            frames.append(f)
        await asyncio.sleep(0.2)  # let the fire-and-forget trajectory task land

    asyncio.run(_run())
finally:
    agent._llm = orig_llm

m = captured.get("messages") or []
check("resume: system first", bool(m) and m[0]["role"] == "system")
check("resume: saved conversation re-used verbatim", m[1:4] == saved_msgs,
      json.dumps(m[1:4])[:200])
check("resume: nudge appended last",
      len(m) > 4 and m[-1] == {"role": "user", "content": RESUME})
check("resume: plan restored", 'event: plan' in "".join(frames))
check("resume: turn finished with done", 'event: done' in "".join(frames))
check("resume: run_state consumed", db.load_run_state(sid) is None)
check("resume: goal kept (trajectory goal = original, not nudge)",
      all("Continue —" not in (r["goal"] or "") for r in
          db.recall_trajectories("do the thing", limit=5) or [{"goal": ""}]))
# TTFT: llm_end carries ttft_ms (stream start → first model frame) so
# hour-of-day server-speed analysis isn't contaminated by decode length
_llm_end = [f for f in frames if f.startswith("event: llm_end\n")]
_ttft = _tok_s = None
if _llm_end:
    _m = re.search(r'"ttft_ms": (\d+)', _llm_end[0])
    _ttft = int(_m.group(1)) if _m else None
    _m = re.search(r'"tok_s": ([\d.]+)', _llm_end[0])
    _tok_s = float(_m.group(1)) if _m else None
check("llm_end frame emitted", bool(_llm_end), str(len(frames)))
check("llm_end carries ttft_ms (first-frame latency)",
      isinstance(_ttft, int) and _ttft >= 0, str(_ttft))
check("llm_end carries tok_s (decode speed from usage chunk)",
      isinstance(_tok_s, float) and _tok_s > 0, str(_tok_s))
_m = re.search(r'"thinking_chars": (\d+)', _llm_end[0])
_think_ch = int(_m.group(1)) if _m else None
check("llm_end carries thinking_chars (per-round CoT delta)",
      _think_ch == 4, str(_think_ch))  # mock yields exactly 4 thinking chars
# Context meter: llm_end carries the model's own prompt_tokens (the real
# context size) and the agent persists it on the session row for the
# topbar pill (reload-proof).
_m = re.search(r'"prompt_tokens": (\d+)', _llm_end[0])
_pt = int(_m.group(1)) if _m else None
check("llm_end carries prompt_tokens (context meter)", _pt == 10, str(_pt))
check("ctx_tokens persisted on the session row",
      db.get_session(sid)["ctx_tokens"] == 10,
      str(db.get_session(sid).get("ctx_tokens")))
check("list_sessions carries ctx_tokens",
      any(s["id"] == sid and s["ctx_tokens"] == 10
          for s in db.list_sessions()))

print("── 5b. per-round thinking knobs (THINKING_FIRST / THINKING_LOOP) ──")
# 2026-09-28 experiment plumbing: round 1 of a task (planning) gets
# THINKING_FIRST, tool-loop rounds get THINKING_LOOP; unset knobs fall
# back to THINKING (behavior-neutral). KnobLLM records the per-call
# `thinking` override the agent passes to stream().
knob_thinks: list = []
knob_frames: list = []
sid_knob = db.create_session()["id"]
(_tmp / "knob_target.txt").write_text("knob\n", encoding="utf-8")


class KnobLLM:
    mode = "native"
    round = 0

    async def stream(self, messages, tools=None, thinking=None):
        KnobLLM.round += 1
        knob_thinks.append(thinking)
        if KnobLLM.round == 1:
            yield {"type": "tool_name", "index": 0, "name": "read_file",
                   "id": "k1"}
            yield {"type": "tool_args", "index": 0,
                   "arguments": json.dumps(
                       {"path": str(_tmp / "knob_target.txt")})}
            yield {"type": "end", "finish_reason": "tool_calls"}
        else:
            yield {"type": "token", "text": "done"}
            yield {"type": "end", "finish_reason": "stop"}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: ok\nPATTERN: NONE"


_orig_first = config.settings.thinking_first
_orig_loop = config.settings.thinking_loop
_orig_llm_knob = agent._llm
agent._llm = KnobLLM()
try:
    async def _run_knob(sess_id: str, text: str) -> None:
        db.add_message(sess_id, "user", text)
        async for f in agent.run_chat(sess_id, text, [],
                                      lambda *a: None, "act", "chill"):
            knob_frames.append(f)
        await asyncio.sleep(0.2)

    # case 1: knobs unset → both rounds inherit THINKING (behavior-neutral)
    config.settings.thinking_first = ""
    config.settings.thinking_loop = ""
    asyncio.run(_run_knob(sid_knob, "two-round job"))
    check("knobs unset → both rounds inherit THINKING (neutral)",
          knob_thinks == [config.settings.thinking] * 2, str(knob_thinks))

    # case 2: knobs set → first round FIRST, loop round LOOP
    knob_thinks.clear()
    knob_frames.clear()
    KnobLLM.round = 0
    sid_knob2 = db.create_session()["id"]
    config.settings.thinking_first = "medium"
    config.settings.thinking_loop = "low"
    asyncio.run(_run_knob(sid_knob2, "two-round job 2"))
    check("knobs set → round1=FIRST, round2=LOOP",
          knob_thinks == ["medium", "low"], str(knob_thinks))
    _knob_end = [f for f in knob_frames if f.startswith("event: llm_end\n")]
    check("llm_end carries the applied thinking level (gate analysis)",
          len(_knob_end) == 2
          and '"thinking": "medium"' in _knob_end[0]
          and '"thinking": "low"' in _knob_end[1],
          "".join(_knob_end)[:200])
finally:
    config.settings.thinking_first = _orig_first
    config.settings.thinking_loop = _orig_loop
    agent._llm = _orig_llm_knob

print("── 6. thinking-burst persistence + re-attach replay ──────────")
from src import tasks as tasks_mod  # noqa: E402

tm = tasks_mod.TaskManager(lambda *a: None)
run = tasks_mod.TaskRun("sb1")
tm.runs["sb1"] = run
# a long thinking burst (3 frames), then a tool event closes it
tm._push(run, "thinking", {"text": "step one "})
tm._push(run, "thinking", {"text": "step two "})
tm._push(run, "thinking", {"text": "step three"})
tm._push(run, "tool_start", {"tool": "read_file", "args": '{"path":"x"}'})
tm._push(run, "tool_end", {"tool": "read_file", "result": "ok"})

rows = db.list_events("sb1")
bursts = [r for r in rows if r["event"] == "thinking_burst"]
check("burst persisted as ONE row", len(bursts) == 1, str(len(bursts)))
check("burst text is the concatenation",
      bursts and bursts[0]["data"]["text"] == "step one step two step three")
check("burst row seq = closing frame's seq (tool_start = 4)",
      bursts and bursts[0]["seq"] == 4)
check("thinking frames still in the ring (live viewers unaffected)",
      any("step one" in f for _, f in run.buffer))
check("tool events persisted structurally",
      {r["event"] for r in rows} >= {"tool_start", "tool_end"})

# run ends mid-burst → the finally-flush must land the partial burst
run2 = tasks_mod.TaskRun("sb2")
tm.runs["sb2"] = run2
tm._push(run2, "thinking", {"text": "partial thought "})
tm._push(run2, "thinking", {"text": "never closed"})
tm._flush_thinking(run2)
b2 = [r for r in db.list_events("sb2") if r["event"] == "thinking_burst"]
check("mid-burst run end still persists",
      len(b2) == 1 and b2[0]["data"]["text"] == "partial thought never closed")

# the replay endpoint the UI re-attach uses
from src import api as api_mod  # noqa: E402
routes = {getattr(r, "path", "") for r in api_mod.app.routes}
check("events_log endpoint registered",
      "/api/sessions/{sid}/events_log" in routes, str(sorted(routes))[:200])

print("── 7. session seq line anchoring (page-freeze regression) ──────")
# Regression: fresh runs used to always start at seq 0 — the events table
# accumulated colliding seqs per session, /api/history reported
# run_start_seq=0, and the re-attach replay pulled the session's ENTIRE
# history into one synchronous DOM pass (Chrome: "page isn't responding").
# A fresh run must anchor ABOVE both the in-memory line and the DB's max.
import src.agent as agent_mod  # noqa: E402


async def _stub_run_chat(sid, message, files, log, mode, roast, resume):
    yield 'event: status\ndata: {"text": "Working…"}\n\n'
    yield 'event: done\ndata: {"text": "ok"}\n\n'


real_run_chat = agent_mod.run_chat  # section 8 drives the REAL run_chat
agent_mod.run_chat = _stub_run_chat


async def _enqueue_and_wait(tm_, sid, msg):
    run, _queued = tm_.enqueue(sid, msg, [], "act", "chill", False)
    for _ in range(400):
        if run._task is not None and run._task.done():
            return run
        await asyncio.sleep(0.005)
    raise AssertionError(f"run for {sid} did not finish in time")


tm7 = tasks_mod.TaskManager(lambda *a: None)
db.add_event("sb-c", 1000, "tool_end", {"tool": "x"})


async def _drive7():
    r1 = await _enqueue_and_wait(tm7, "sb-a", "one")
    r2 = await _enqueue_and_wait(tm7, "sb-a", "two")
    tm8 = tasks_mod.TaskManager(lambda *a: None)  # fresh manager = restart
    r8 = await _enqueue_and_wait(tm8, "sb-c", "three")
    return r1, r2, r8


run7, run7b, run8 = asyncio.run(_drive7())
check("fresh session anchors at 0", run7.start_seq == 0, str(run7.start_seq))
# _pump's own first "status" push (seq 1) + the stub's status + done
check("run's frames carry the anchored line", run7.seq == 3, str(run7.seq))
check("next run continues the in-memory line",
      run7b.start_seq == run7.seq, f"{run7b.start_seq} != {run7.seq}")
check("max_event_seq: fresh=0", db.max_event_seq("sb-empty") == 0)
check("max_event_seq tracks the anchored line",
      db.max_event_seq("sb-c") == run8.seq,
      f"{db.max_event_seq('sb-c')} != {run8.seq}")
check("post-restart run anchors at the DB max",
      run8.start_seq == 1000 and run8.seq > 1000,
      f"start={run8.start_seq} seq={run8.seq}")
# the property the UI relies on: replaying from start_seq sees ONLY this
# run's events (before the fix this window was the whole session history)
check("replay window after=start_seq isolates the run",
      all(e["seq"] > 1000 for e in db.list_events("sb-c", after=run8.start_seq))
      and any(e["event"] == "status"
              for e in db.list_events("sb-c", after=run8.start_seq)))

print("── 7b. durable queue: survives restart, autostart, stop clears ──")
# Regression: the per-session FIFO lived ONLY in memory — a server restart
# ate every queued message the user had already sent (and seen as "queued").
# Now the `queue` table mirrors every mutation and TaskManager.__init__
# restores it; boot autostart fires the head, stop clears the rows for good.

async def _drive_q():
    sidq = "q-durable"  # the queue layer is session-agnostic (no row needed)
    tmq = tasks_mod.TaskManager(lambda *a: None)
    # a slow in-flight run so the next two messages actually queue
    async def _slow(sid, message, files, log, mode, roast, resume):
        if message == "q-first":
            await asyncio.sleep(0.3)
        yield 'event: status\ndata: {"text": "Working\u2026"}\n\n'
        yield 'event: done\ndata: {"text": "ok"}\n\n'
    agent_mod.run_chat = _slow
    r, _ = tmq.enqueue(sidq, "q-first", [], "act", "chill", False)
    await asyncio.sleep(0.05)
    tmq.enqueue(sidq, "q-second", [], "act", "chill", False)
    tmq.enqueue(sidq, "q-third", [], "act", "chill", True)
    assert tmq.queue_count(sidq) == 2
    # RESTART: fresh manager restores from the DB
    tmq2 = tasks_mod.TaskManager(lambda *a: None)
    got = [e["message"] for e in tmq2.queues.get(sidq, ())]
    assert got == ["q-second", "q-third"], got
    assert tmq2.queues[sidq][1]["resume"] is True
    # autostart fires the head; the normal drain finishes the rest
    agent_mod.run_chat = _stub_run_chat
    tmq2.autostart(sidq)
    for _ in range(800):
        if tmq2.queue_count(sidq) == 0:
            break
        await asyncio.sleep(0.005)
    return tmq2, sidq, got

tmq2, sidq, got_q = asyncio.run(_drive_q())
check("queue restored after restart (FIFO order kept)", got_q == ["q-second", "q-third"], str(got_q))
check("autostart drained the restored queue", tmq2.queue_count(sidq) == 0)
check("DB rows cleaned after the drain", db.queue_load(sidq) == [])
# stop semantics: a stop clears the durable rows (no resurrection on restart)
db.queue_add(sidq, {"message": "ghost", "files": []})
db.queue_clear(sidq)
check("stop/drop clears the durable rows", db.queue_load(sidq) == [])

# Regression: a restart kills an in-flight turn AND the user had queued a
# follow-up → the queue restore autostarts the head. That head must
# CONTINUE the interrupted task (resume=True, re-enters the saved
# run_state with the original goal pinned), not fire as a fresh turn —
# a fresh turn rebuilds context from the last 16 history rows (the
# interrupted task's plan/tool results are out of scope) and its
# completion clears the interrupted task's checkpoint for good.
sidq2 = "q-durable-resume"
db.save_run_state(sidq2, 2, ["a", "b"], {0},
                  [{"role": "user", "content": "the interrupted job"}],
                  "the interrupted job")
db.queue_add(sidq2, {"message": "queued follow-up", "files": []})

async def _drive_q2():
    tmq3 = tasks_mod.TaskManager(lambda *a: None)  # fresh manager = restart
    seen: dict = {}
    async def _spy_q(sid, message, files, log, mode, roast, resume):
        seen["resume"] = resume
        yield 'event: done\ndata: {"text": "ok"}\n\n'
    agent_mod.run_chat = _spy_q
    tmq3.autostart(sidq2)
    for _ in range(800):
        r3 = tmq3.get(sidq2)
        if r3 is None or not r3.active:
            break
        await asyncio.sleep(0.005)
    return seen, tmq3

seen_q, tmq3 = asyncio.run(_drive_q2())
agent_mod.run_chat = _stub_run_chat  # restore (later sections rely on it)
check("autostart continues the interrupted task (resume=True)",
      seen_q.get("resume") is True)
check("autostart drained the resumed queue", tmq3.queue_count(sidq2) == 0)
db.clear_run_state(sidq2)

# Same hole, NO restart: a run FINISHES in-process while a follow-up is
# already queued, and the finished run left a saved run_state (the LLM
# stopped mid-loop without a final summary — max turns, hard stop, crash).
# The _pump drain must fire the queued message as RESUME (original goal
# pinned), not as a fresh turn that rebuilds context from 16 history rows
# and clobbers the checkpoint on completion.
sidq3 = "q-durable-drain-resume"
db.queue_add(sidq3, {"message": "q3-queued-followup", "files": []})

async def _drive_q3():
    tmq4 = tasks_mod.TaskManager(lambda *a: None)
    calls: list = []
    async def _spy_q3(sid, message, files, log, mode, roast, resume):
        calls.append((message, resume))
        if message == "q3-first":
            # the run ends with a saved checkpoint (interrupted task)
            db.save_run_state(sid, 3, ["a"], {0},
                              [{"role": "user", "content": "q3-first"}],
                              "q3-first")
        yield 'event: done\ndata: {"text": "ok"}\n\n'
    agent_mod.run_chat = _spy_q3
    tmq4.enqueue(sidq3, "q3-first", [], "act", "chill", False)
    for _ in range(1600):
        if not tmq4.queue_count(sidq3) and \
           (tmq4.get(sidq3) is None or not tmq4.get(sidq3).active):
            break
        await asyncio.sleep(0.005)
    return calls, tmq4

calls_q3, tmq4 = asyncio.run(_drive_q3())
agent_mod.run_chat = _stub_run_chat  # restore
check("drain continues the interrupted task (resume=True, no restart)",
      len(calls_q3) == 2 and calls_q3[1][0] == "q3-queued-followup"
      and calls_q3[1][1] is True, str(calls_q3))
check("drain cleared the queue + checkpoint",
      tmq4.queue_count(sidq3) == 0 and db.queue_load(sidq3) == [])
db.clear_run_state(sidq3)

# Drain-announce: when the head leaves the queue, the NEW run's buffer
# must carry a `queued` event with the fired text — the tray drops its
# row live and draws the fired message's user bubble from this (the
# authoritative source: items queued from another tab have no local
# peek). This test fails if the push is missing (it was once a comment
# describing a push that didn't exist — the "tray stuck until reload"
# bug).
sidq4 = "q-drain-announce"

async def _drive_q4():
    tmq5 = tasks_mod.TaskManager(lambda *a: None)
    async def _spy_q4(sid, message, files, log, mode, roast, resume):
        yield 'event: done\ndata: {"text": "ok"}\n\n'
    agent_mod.run_chat = _spy_q4
    tmq5.enqueue(sidq4, "qa-first", [], "act", "chill", False)
    tmq5.enqueue(sidq4, "qa-fired", [], "act", "chill", False)
    for _ in range(1600):
        if not tmq5.queue_count(sidq4) and \
           (tmq5.get(sidq4) is None or not tmq5.get(sidq4).active):
            break
        await asyncio.sleep(0.005)
    # the drain-announce rides the NEW run's buffer (the old run's stream
    # is closed by the sentinel) — grab the current run, not the first
    return tmq5.get(sidq4)

nrun4 = asyncio.run(_drive_q4())
agent_mod.run_chat = _stub_run_chat  # restore
ann = [f for _, f in (nrun4.buffer if nrun4 is not None else [])
       if f.startswith("event: queued") and '"fired"' in f]
check("drain announces the pop on the new run's buffer (fired text)",
      bool(ann) and '"fired": "qa-fired"' in ann[-1],
      ann[-1].replace("\n", " | ") if ann else "no drain-announce frame")

# queue_update / queue_clear: the tray's ✎ (edit) and "clear all"
sidq5 = "q-tray-mutations"

async def _drive_q5():
    tmq6 = tasks_mod.TaskManager(lambda *a: None)
    async def _spy_q5(sid, message, files, log, mode, roast, resume):
        if message == "qt-first":
            await asyncio.sleep(0.3)
        yield 'event: done\ndata: {"text": "ok"}\n\n'
    agent_mod.run_chat = _spy_q5
    tmq6.enqueue(sidq5, "qt-first", [], "act", "chill", False)
    await asyncio.sleep(0.05)
    tmq6.enqueue(sidq5, "qt-edit-me", [], "act", "chill", False)
    tmq6.enqueue(sidq5, "qt-drop-me", [], "act", "chill", False)
    assert tmq6.queue_count(sidq5) == 2
    ok = tmq6.queue_update(sidq5, 0, "qt-edited")
    got = [e["message"] for e in tmq6.queues.get(sidq5, ())]
    gone = tmq6.queue_update(sidq5, 99, "nope")
    n = tmq6.queue_clear(sidq5)
    return ok, got, gone, n, tmq6.queue_count(sidq5), tmq6

ok5, got5, gone5, n5, cnt5, tmq6 = asyncio.run(_drive_q5())
agent_mod.run_chat = _stub_run_chat  # restore
check("queue_update edits by position (durable mirror re-synced)",
      ok5 and got5 == ["qt-edited", "qt-drop-me"], str(got5))
check("queue_update on a gone index returns False (drain race → draft)",
      gone5 is False)
check("queue_clear drops everything (idempotent, count 0 after)",
      n5 == 2 and cnt5 == 0 and tmq6.queue_clear(sidq5) == 0)

print("── 8. reattach snapshot: in-flight draft row ────────────────────")
# Regression: leaving a chat mid-turn and returning lost everything that
# happened while away — the draft row held the turn timeline but
# /api/history refused to deliver it, and the ring only had the newest
# frames. Now: history ships the draft + draft_seq (the exact resume point
# the snapshot ends at), refreshed at tool boundaries AND every ~2s of
# typing, stamped with the last broadcast frame's seq.

# 8a. db layer: snapshot round-trip, history exclusion, finalization
s8 = db.create_session()
sid8 = s8["id"]
db.add_message(sid8, "user", "build it")
m8 = db.new_draft_message(sid8)
db.update_draft_message(m8, "typing now", None, "thought A",
                        [{"t": "text", "text": "first"}], flushed_seq=42,
                        ui={"avatar": "stream", "label": "Answering…",
                            "thinking": False})
d8 = db.get_draft_message(sid8)
check("draft row round-trips (parts/thinking/flushed_seq/ui)",
      d8 is not None and d8["content"] == "typing now"
      and d8["thinking"] == "thought A"
      and d8["parts"] == [{"t": "text", "text": "first"}]
      and d8["flushed_seq"] == 42
      and d8["ui"] == {"avatar": "stream", "label": "Answering…",
                       "thinking": False})
check("draft stays OUT of history", len(db.list_messages(sid8)) == 1)
db.finish_draft_message(m8, sid8, "final answer")
check("finish finalizes the draft (ui dressing dropped from history)",
      db.get_draft_message(sid8) is None
      and db.list_messages(sid8)[-1]["content"] == "final answer"
      and "ui" not in db.list_messages(sid8)[-1])

# 8c. windowed history + draft tool-part cap (chat-switch freeze, 09-28):
# the switch payload now ships a window (limit/before_id) + hidden count,
# a bounded thinking tail for the Thinking tab, and a draft snapshot whose
# tool timeline is capped (last 25 kept, drop count returned).
s8c = db.create_session()
sid8c = s8c["id"]
for i in range(36):
    db.add_message(sid8c, "assistant" if i % 2 == 0 else "user",
                   f"m{i}", thinking=(f"think{i}" * 50) if i % 5 == 0 else None)
w8c = db.list_messages(sid8c, limit=20)
check("history window = the LAST 20, oldest→newest",
      len(w8c) == 20 and w8c[0]["content"] == "m16"
      and w8c[-1]["content"] == "m35"
      and all(w8c[i]["id"] < w8c[i + 1]["id"] for i in range(len(w8c) - 1)))
check("hidden_count counts what the window hides",
      db.count_messages_before(sid8c, w8c[0]["id"]) == 16)
c8c = db.list_messages(sid8c, limit=12, before_id=w8c[0]["id"])
check("before_id chunk = 12 rows strictly before the window",
      len(c8c) == 12 and c8c[0]["content"] == "m4" and c8c[-1]["content"] == "m15"
      and c8c[-1]["id"] < w8c[0]["id"])
c8c2 = db.list_messages(sid8c, limit=12, before_id=c8c[0]["id"])
check("cursor walks to the floor (4 left, then 0 hidden)",
      len(c8c2) == 4 and c8c2[0]["content"] == "m0"
      and db.count_messages_before(sid8c, c8c2[0]["id"]) == 0)
t8c = db.recent_thinking(sid8c)
check("recent_thinking = newest 100 capped, oldest→newest",
      len(t8c) == 8 and t8c[0] == "think0" * 50 and t8c[-1] == "think35" * 50)
check("recent_thinking byte cap drops the OLDEST first",
      db.recent_thinking(sid8c, max_bytes=700)
      == ["think30" * 50, "think35" * 50])
m8c = db.new_draft_message(sid8c)
parts8c = [{"t": "tool", "name": f"t{i}", "phase": "work", "args": ""}
           for i in range(30)]
parts8c.insert(0, {"t": "text", "text": "opening"})
db.update_draft_message(m8c, "⏳ working", None, None, parts8c, flushed_seq=7)
d8c = db.get_draft_message(sid8c)
tools8c = [p for p in d8c["parts"] if p["t"] == "tool"]
check("draft snapshot caps tool parts (last 25 kept, order + text intact)",
      len(tools8c) == 25 and tools8c[0]["name"] == "t5"
      and tools8c[-1]["name"] == "t29"
      and d8c["parts"][0] == {"t": "text", "text": "opening"})
check("draft snapshot reports the dropped tool-part count",
      d8c["omitted_tool_parts"] == 5)
full8c = db.get_draft_message(sid8c, max_tool_parts=None)
check("max_tool_parts=None leaves the draft row untouched",
      full8c["omitted_tool_parts"] == 0
      and len([p for p in full8c["parts"] if p["t"] == "tool"]) == 30)
db.finish_draft_message(m8c, sid8c, "done")

# 8b. agent layer: a slow text round through the REAL TaskManager — the
# ~2s periodic flush must fire mid-stream and stamp the exact resume seq
FULL_TEXT = "".join(f"w{i} " for i in range(30)).strip()


class SlowLLM:
    mode = "native"

    async def stream(self, messages, tools=None, thinking=None):
        for i in range(30):
            yield {"type": "token", "text": f"w{i} "}
            await asyncio.sleep(0.1)
        yield {"type": "end", "finish_reason": "stop"}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: ok\nPATTERN: NONE"


agent_mod.run_chat = real_run_chat
tm8 = tasks_mod.TaskManager(lambda *a: None)
tasks_mod.set_manager(tm8)  # production wiring: the agent resolves runs
s8b = db.create_session()
sid8b = s8b["id"]
db.add_message(sid8b, "user", "write a long answer")
orig_llm8 = agent._llm
agent._llm = SlowLLM()
try:
    async def _drive8():
        run8b, _queued = tm8.enqueue(sid8b, "go", [], "act", "chill", False)
        mid_snap = None
        for _ in range(300):  # poll while the round is streaming
            await asyncio.sleep(0.05)
            d = db.get_draft_message(sid8b)
            if d and d.get("flushed_seq") and (d["content"] or "").strip():
                mid_snap = d
                break
        for _ in range(600):
            if run8b._task is not None and run8b._task.done():
                break
            await asyncio.sleep(0.005)
        return run8b, mid_snap

    run8b, mid_snap = asyncio.run(_drive8())
finally:
    agent._llm = orig_llm8

check("periodic flush fired mid-stream (draft had in-flight text)",
      mid_snap is not None,
      str(mid_snap and (mid_snap["content"] or "")[:40]))
check("snapshot carries the live dressing (ui: avatar/label)",
      mid_snap is not None
      and mid_snap["ui"].get("avatar") == "stream"
      and mid_snap["ui"].get("label") == "Answering…",
      str(mid_snap and mid_snap.get("ui")))
check("snapshot text is a strict prefix of the final answer",
      mid_snap is not None
      and (mid_snap["content"] or "").strip() in FULL_TEXT
      and (mid_snap["content"] or "").strip() != FULL_TEXT)
final8 = db.list_messages(sid8b)[-1]
check("run finalizes into a normal message (draft cleared)",
      db.get_draft_message(sid8b) is None
      and final8["content"].strip() == FULL_TEXT,
      str(final8["content"])[:80])

# THE invariant: ring frames ≤ flushed_seq reconstruct exactly the snapshot
# text, and frames > flushed_seq are the exact continuation — no gap, no
# double render (this is what the client's draft + openStream(draft_seq) does)
if mid_snap is not None:
    before, after = [], []
    for seq, wire in run8b.buffer:
        mfr = tasks_mod._FRAME_RE.match(wire)
        if not mfr or mfr.group(1) != "token":
            continue
        d = json.loads(mfr.group(2))
        (before if d["seq"] <= mid_snap["flushed_seq"] else after).append(d["text"])
    check("snapshot == all tokens up to flushed_seq (no double render)",
          "".join(before).strip() == (mid_snap["content"] or "").strip(),
          f"before={(''.join(before))[:60]!r} snap={((mid_snap['content'] or '')[:60])!r}")
    check("resume from flushed_seq == exact continuation (no gap)",
          "".join(before) + "".join(after)
          == "".join(f"w{i} " for i in range(30)))
    # the covered-replay window stays this-run-small (the freeze invariant)
    win = db.list_events(sid8b, after=run8b.start_seq)
    check("structural replay window stays run-local (small)",
          len(win) < 50 and all(e["seq"] >= run8b.start_seq for e in win),
          str(len(win)))

print("── 8c. mid-flight Plan/Act mode sync (toggle is live, not a snapshot) ──")
# The Plan/Act toggle must take effect on a RUNNING turn, not just the
# next send: Act→Plan demotes mid-run (prompt rebuilt, mutating tools
# dropped from the schema, defensive gate blocks queued mutating calls);
# Plan→Act re-arms. The mode is re-read from the DB at model-call and
# pre-dispatch boundaries — exactly what /api/sessions/mode writes.

s8c = db.create_session()
sid8c = s8c["id"]
db.add_message(sid8c, "user", "edit the file")


class FlipLLM:
    """Round 1: requests write_file. Round 2: final answer. The mode
    flips act→plan BETWEEN the two rounds (the boss clicked Plan)."""
    mode = "native"

    def __init__(self):
        self.round = 0
        self.calls = []

    async def stream(self, messages, tools=None, thinking=None):
        self.round += 1
        self.calls.append({
            "sys": messages[0]["content"],
            "tools": sorted((t.get("function") or t).get("name", "")
                            for t in (tools or [])),
        })
        if self.round == 1:
            db.set_session_mode(sid8c, "plan")  # the mid-run toggle
            yield {"type": "tool_name", "index": 0, "name": "write_file",
                   "id": "c1"}
            yield {"type": "tool_args", "index": 0,
                   "arguments": '{"path": "demo.txt", "content": "x"}'}
            yield {"type": "end", "finish_reason": "tool_calls"}
        else:
            yield {"type": "token", "text": "plan: I will edit demo.txt"}
            yield {"type": "end", "finish_reason": "stop"}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: all claims checked\nPATTERN: NONE"


orig_llm8c = agent._llm
agent._llm = FlipLLM()
try:
    frames8c: list[str] = []

    async def _run8c():
        async for f in agent.run_chat(sid8c, "edit the file", [],
                                      lambda *a: None, "act", "chill"):
            frames8c.append(f)
        await asyncio.sleep(0.2)

    asyncio.run(_run8c())
finally:
    agent._llm = orig_llm8c

w8c = "".join(frames8c)
check("demotion: write_file was BLOCKED, not executed",
      '"tool": "write_file"' in w8c and "error:" in w8c
      and "disabled in Plan mode" in w8c, w8c[:400])
check("demotion: status frame broadcast",
      "demoted mid-run" in w8c)
check("demotion: tool_end marks the block as failed",
      '"ok": false' in w8c)
check("run finished with a plan-style answer (not killed by the gate)",
      "event: done" in w8c and "plan: I will edit demo.txt" in w8c)
# the round-2 system prompt + schema must reflect the NEW mode — the
# demotion is visible in the model-facing context, not just the gate
m8c = db.list_messages(sid8c)
check("final answer persisted",
      m8c and m8c[-1]["content"].strip() == "plan: I will edit demo.txt",
      str(m8c[-1]["content"] if m8c else "")[:80])

# Plan→Act: a plan-mode run re-arms when the boss flips back — the
# defensive gate must NOT block the mutating call.
s8d = db.create_session()
sid8d = s8d["id"]
db.add_message(sid8d, "user", "now do it")


class FlipBackLLM:
    mode = "native"

    def __init__(self):
        self.round = 0

    async def stream(self, messages, tools=None, thinking=None):
        self.round += 1
        if self.round == 1:
            db.set_session_mode(sid8d, "act")  # mid-run flip back
            yield {"type": "tool_name", "index": 0, "name": "write_file",
                   "id": "c2"}
            # temp dir — the re-arm test EXECUTES the write (that's the
            # point: the gate must let it through), so never touch home
            yield {"type": "tool_args", "index": 0,
                   "arguments": json.dumps({"path": str(_tmp / "demo2.txt"),
                                            "content": "y"})}
            yield {"type": "end", "finish_reason": "tool_calls"}
        else:
            yield {"type": "token", "text": "done, file written"}
            yield {"type": "end", "finish_reason": "stop"}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: all claims checked\nPATTERN: NONE"


agent._llm = FlipBackLLM()
try:
    frames8d: list[str] = []

    async def _run8d():
        async for f in agent.run_chat(sid8d, "now do it", [],
                                      lambda *a: None, "plan", "chill"):
            frames8d.append(f)
        await asyncio.sleep(0.2)

    asyncio.run(_run8d())
finally:
    agent._llm = orig_llm8c

w8d = "".join(frames8d)
check("re-arm: Plan→Act status frame broadcast",
      "re-armed mid-run" in w8d)
check("re-arm: write_file NOT blocked after the flip back",
      "disabled in Plan mode" not in w8d, w8d[:400])
check("re-arm: write_file actually EXECUTED (ok: true)",
      '"tool": "write_file"' in w8d and '"ok": true' in w8d, w8d[:400])
check("re-arm: the file landed on disk",
      (_tmp / "demo2.txt").is_file())
check("re-arm: run finished normally", "event: done" in w8d)

# the flip must also be visible in the MODEL-FACING context: the
# round-2 system prompt carries the plan constraints and the schema
# drops the mutating tools (demotion), and vice versa (re-arm).
s8e = db.create_session()
sid8e = s8e["id"]
db.add_message(sid8e, "user", "context check")


class FlipCtxLLM:
    mode = "native"

    def __init__(self):
        self.round = 0
        self.calls = []

    async def stream(self, messages, tools=None, thinking=None):
        self.round += 1
        self.calls.append({
            "sys": messages[0]["content"],
            "tools": sorted((t.get("function") or t).get("name", "")
                            for t in (tools or [])),
        })
        if self.round == 1:
            db.set_session_mode(sid8e, "plan")  # flip WHILE round 1 runs
            # a read (allowed in both modes) so a round 2 exists to
            # capture the rebuilt context
            yield {"type": "tool_name", "index": 0, "name": "read_file",
                   "id": "c3"}
            yield {"type": "tool_args", "index": 0,
                   "arguments": json.dumps({"path": str(_tmp / "readme.txt")})}
            yield {"type": "end", "finish_reason": "tool_calls"}

    async def complete(self, messages, temperature=0.2, thinking=None):
        return "VERIFIED: all claims checked\nPATTERN: NONE"


(_tmp / "readme.txt").write_text("read me\n", encoding="utf-8")
ctx_llm = FlipCtxLLM()
agent._llm = ctx_llm
try:
    async def _run8e():
        async for f in agent.run_chat(sid8e, "context check", [],
                                      lambda *a: None, "act", "chill"):
            pass
        await asyncio.sleep(0.2)

    asyncio.run(_run8e())
finally:
    agent._llm = orig_llm8c

r1, r2 = ctx_llm.calls[0], ctx_llm.calls[1]
check("round-1 prompt is ACT (send-time mode honored)",
      "ACT MODE" in r1["sys"] and "PLAN MODE" not in r1["sys"])
check("round-1 schema advertises mutating tools",
      "write_file" in r1["tools"] and "run_command" in r1["tools"])
check("round-2 prompt rebuilt with PLAN constraints after the flip",
      "PLAN MODE" in r2["sys"] and "ACT MODE" not in r2["sys"],
      r2["sys"][-400:])
check("round-2 schema drops mutating tools after the flip",
      "write_file" not in r2["tools"]
      and "edit_file" not in r2["tools"]
      and "run_command" not in r2["tools"], str(r2["tools"]))

print("── 9. notebook markdown cells render as real HTML (not escaped text) ──")
NB_MD = (
    "# Title **bold**\n\n"
    "Some *italic* and `code` and [a link](https://example.com/x) "
    "plus [bad](javascript:alert(1)) and [rel](muji/foo.py)\n\n"
    "- item one\n- item two\n\n"
    "1. first\n2. second\n\n"
    "> quoted\n\n"
    "---\n\n"
    "```python\nprint('hi *not italic*')\n```\n\n"
    "tail para\n"
)
nb_html = tools.md_to_html(NB_MD)
check("h1 rendered", "<h1>Title <strong>bold</strong></h1>" in nb_html, nb_html)
check("italic rendered", "<em>italic</em>" in nb_html)
check("code rendered", "<code>code</code>" in nb_html)
check("safe link rendered", '<a href="https://example.com/x">a link</a>' in nb_html)
check("javascript: link NOT made clickable",
      "javascript:alert(1)" in nb_html and 'href="javascript' not in nb_html)
check("relative path link rendered", '<a href="muji/foo.py">rel</a>' in nb_html)
check("windows path link rendered",
      '<a href="C:\\Users\\Jan\\report.md">win</a>' in
      tools.md_to_html("[win](C:\\Users\\Jan\\report.md)"))
check("ul rendered", "<ul>" in nb_html
      and nb_html.index("<li>item one</li>") < nb_html.index("<li>item two</li>"))
check("ol rendered", "<ol>" in nb_html
      and re.search(r"<ol><li>first</li><li>second</li></ol>", nb_html) is not None)
check("blockquote rendered", "<blockquote>quoted</blockquote>" in nb_html)
check("hr rendered", "<hr>" in nb_html)
check("fenced code escaped + no inline md inside",
      "<pre><code>print(" in nb_html and "*not italic*" in nb_html
      and "<em>not italic" not in nb_html)
check("no raw markdown left", "**" not in nb_html and "`" not in nb_html)
xss = tools.md_to_html("<script>alert(1)</script>")
check("XSS: escaped, no raw <script>",
      "<script>" not in xss and "&lt;script&gt;" in xss, xss)
# and the cell renderer actually uses it
cell = {"cell_type": "markdown", "source": ["# Hi\n"]}
check("_nb_cell_html uses md_to_html",
      '<div class="nb-md"><h1>Hi</h1></div>' in tools._nb_cell_html(cell))
# full notebook still parses + renders
nb_json = json.dumps({
    "metadata": {"kernelspec": {"display_name": "Python 3"}},
    "cells": [{"cell_type": "markdown", "source": NB_MD},
              {"cell_type": "code", "execution_count": 1,
               "source": "print(1)",
               "outputs": [{"output_type": "stream", "name": "stdout",
                            "text": "1\n"}]}]})
full = tools.notebook_to_html(nb_json)
check("notebook_to_html end-to-end",
      "<h1>" in full and "Kernel: Python 3" in full and "Not a valid notebook" not in full)

print("── 10. supervisor (the watch strip's backend) ──")
from src import supervisor  # noqa: E402
from collections import deque  # noqa: E402


def _frame(name: str, data: dict | None = None) -> str:
    return f"event: {name}\ndata: {json.dumps(data or {})}\n\n"


class _FakeRun:
    def __init__(self, sid, active=True, started=None, buffer=None):
        self.sid = sid
        self.active = active
        self.started_at = started if started is not None else time.time()
        self.start_seq = 0
        self.buffer = deque(buffer or [])
        self.waiting_approval = False
        self.waiting_question = False
        self.approval_deadline = None
        self.question_deadline = None
        self.status = "running" if active else "done"


class _FakeTasks:
    def __init__(self):
        self.runs: dict[str, _FakeRun] = {}
        self.queues: dict[str, deque] = {}

    def queue_count(self, sid):
        return len(self.queues.get(sid) or ())


def _mk(sid, title):
    s = db.create_session()
    db.rename_session(s["id"], title)
    return s["id"]


tm = _FakeTasks()
now = time.time()
# healthy: recent event (llm_end in the ring + fresh DB row)
sid_ok = _mk("s_ok", "Healthy")
db.add_event(sid_ok, 1, "llm_end", {})
tm.runs[sid_ok] = _FakeRun(sid_ok, buffer=[(1, _frame("llm_end"))])
# stuck: cycled (llm_end row in the DB) but the last DB event is 10 min old
sid_stuck = _mk("s_stuck", "StuckRun")
db.add_event(sid_stuck, 1, "llm_end", {})
db.add_event(sid_stuck, 2, "tool_end", {})
conn = db._db()
with db._lock:
    conn.execute("UPDATE events SET ts=? WHERE session_id=?",
                 (now - 600, sid_stuck))
    conn.commit()
tm.runs[sid_stuck] = _FakeRun(sid_stuck, buffer=[
    (1, _frame("llm_end", {})),
    (2, _frame("tool_end", {"tool": "write_file"})),
])
# waiting: parked on an approval
sid_wait = _mk("s_wait", "WaitingRun")
db.add_event(sid_wait, 1, "approval", {})
r = _FakeRun(sid_wait, buffer=[(1, _frame("approval", {}))])
r.waiting_approval = True
r.approval_deadline = (now + 120) * 1000
tm.runs[sid_wait] = r
# queued-only: no live run, one stale message
sid_q = _mk("s_q", "QueuedRun")
tm.queues[sid_q] = deque([{"message": "later", "created_at": now - 900}])
# finished run: must NOT appear
sid_done = _mk("s_done", "DoneRun")
tm.runs[sid_done] = _FakeRun(sid_done, active=False)

rep = supervisor.watch(tm)
by = {row["sid"]: row for row in rep["rows"]}
check("finished run excluded", sid_done not in by)
check("healthy run classified running",
      by.get(sid_ok, {}).get("status") == "running", str(by.get(sid_ok)))
check("stuck run flagged (silent after first llm_end)",
      by.get(sid_stuck, {}).get("status") == "stuck", str(by.get(sid_stuck)))
check("stuck row carries the reason",
      "silent" in (by.get(sid_stuck, {}).get("why") or ""),
      str(by.get(sid_stuck)))
check("waiting run flagged", by.get(sid_wait, {}).get("status") == "waiting")
check("waiting row names the gate",
      by.get(sid_wait, {}).get("waiting") == "approval")
check("queued-only session listed", by.get(sid_q, {}).get("status") == "queued")
check("stale queue row carries a why",
      by.get(sid_q, {}).get("why") is not None)
check("worst state is stuck", rep["worst"] == "stuck", rep["worst"])
check("rows sorted worst-first",
      [row["sid"] for row in rep["rows"]][0] == sid_stuck,
      str([row["sid"] for row in rep["rows"]]))
# a pre-cycling run (no llm_end yet) must NOT be stuck even when the DB
# is old — a long first model call is silent by design
sid_fresh = _mk("s_fresh", "FirstCall")
db.add_event(sid_fresh, 1, "status", {})
with db._lock:
    db._db().execute("UPDATE events SET ts=? WHERE session_id=?",
                     (now - 600, sid_fresh))
    db._db().commit()
tm.runs[sid_fresh] = _FakeRun(sid_fresh, buffer=[(1, _frame("status", {"text": "Working…"}))])
rep2 = supervisor.watch(tm)
check("no llm_end yet → not stuck (long first call is normal)",
      {r["sid"]: r["status"] for r in rep2["rows"]}.get(sid_fresh) == "running",
      str({r["sid"]: r["status"] for r in rep2["rows"]}))
# in-memory cycled flag (no llm_end row in the DB — the flag is set at the
# frame, the row may not have flushed yet): the flag alone must suffice.
# 2026-09-28: the last event is an in-flight tool_start, so the extended
# threshold (300 s + 1800 s tool-in-flight grace) applies — backdate past
# it, then check the grace window itself.
sid_flag = _mk("s_flag", "FlagRun")
db.add_event(sid_flag, 1, "tool_start", {})
with db._lock:
    db._db().execute("UPDATE events SET ts=? WHERE session_id=?",
                     (now - 2200, sid_flag))
    db._db().commit()
fr = _FakeRun(sid_flag, buffer=[(1, _frame("tool_start", {}))])
fr.cycled = True
tm.runs[sid_flag] = fr
rep3 = supervisor.watch(tm)
check("in-memory cycled flag suffices (no DB row needed)",
      {r["sid"]: r["status"] for r in rep3["rows"]}.get(sid_flag) == "stuck",
      str({r["sid"]: r["status"] for r in rep3["rows"]}))
# the same fixture INSIDE the grace window: a long build/test/ssh is a
# running tool, not a stuck run
with db._lock:
    db._db().execute("UPDATE events SET ts=? WHERE session_id=?",
                     (now - 600, sid_flag))
    db._db().commit()
rep3b = supervisor.watch(tm)
check("in-flight tool_start inside the 35-min grace → running (long run)",
      {r["sid"]: r["status"] for r in rep3b["rows"]}.get(sid_flag) == "running",
      str({r["sid"]: r["status"] for r in rep3b["rows"]}))
# an OPEN thinking burst is not silence: thinking frames are ring-only
# (the DB row lands when the burst closes), so a cycled run mid-think
# with an old DB must NOT read as stuck
sid_think = _mk("s_think", "ThinkRun")
db.add_event(sid_think, 1, "llm_end", {})
db.add_event(sid_think, 2, "tool_end", {})
with db._lock:
    db._db().execute("UPDATE events SET ts=? WHERE session_id=?",
                     (now - 900, sid_think))
    db._db().commit()
tr = _FakeRun(sid_think, buffer=[
    (1, _frame("llm_end", {})),
    (2, _frame("tool_end", {"tool": "read_file"})),
])
tr.cycled = True
tr._think_buf = ["pondering the roast…", 3, 3]  # open burst
tm.runs[sid_think] = tr
rep4 = supervisor.watch(tm)
by4 = {r["sid"]: r for r in rep4["rows"]}
check("open thinking burst → not stuck (long think is normal)",
      by4.get(sid_think, {}).get("status") == "running",
      str(by4.get(sid_think)))
check("thinking row names the think",
      "thinking" in (by4.get(sid_think, {}).get("what") or ""),
      str(by4.get(sid_think)))
# burst closes (any non-thinking event flushes it) → the same old DB is
# stuck again: the guard must not mask real silence
tr._think_buf = None
rep5 = supervisor.watch(tm)
check("closed burst + old DB → stuck again (guard doesn't mask silence)",
      {r["sid"]: r["status"] for r in rep5["rows"]}.get(sid_think) == "stuck",
      str({r["sid"]: r["status"] for r in rep5["rows"]}))

print("── 11. deleted_sessions: the 30-day tombstone (read-only tracker) ──")
# single delete → tombstone row with title + msg_count + source
d1 = db.create_session()
db.rename_session(d1["id"], "Tombstone chat")
db.add_message(d1["id"], "user", "hello")
db.add_message(d1["id"], "assistant", "hi back")
db.delete_session(d1["id"])
check("single delete gone from sessions", db.get_session(d1["id"]) is None)
tomb = {t["session_id"]: t for t in db.list_deleted_sessions()}
check("tombstone row recorded",
      d1["id"] in tomb and tomb[d1["id"]]["title"] == "Tombstone chat"
      and tomb[d1["id"]]["msg_count"] == 2
      and tomb[d1["id"]]["source"] == "delete",
      str([(t["session_id"][:6], t["title"], t["msg_count"], t["source"])
           for t in tomb.values()]))
# Clear all → one tombstone per chat, source clear_all
c1 = db.create_session(); db.rename_session(c1["id"], "Clear all A")
c2 = db.create_session(); db.rename_session(c2["id"], "Clear all B")
db.clear_sessions()
tomb = db.list_deleted_sessions()
by_id = {t["session_id"]: t for t in tomb}
check("clear_all tombstones every chat",
      c1["id"] in by_id and c2["id"] in by_id
      and by_id[c1["id"]]["source"] == "clear_all"
      and by_id[c2["id"]]["source"] == "clear_all"
      and db.get_session(c1["id"]) is None
      and db.get_session(c2["id"]) is None,
      str([(t["session_id"][:6], t["source"]) for t in tomb]))
# Clear archived → only the old, unpinned ones are tombstoned
a1 = db.create_session(); db.rename_session(a1["id"], "Old archived")
a2 = db.create_session(); db.rename_session(a2["id"], "Fresh one")
with db._lock:
    db._db().execute(
        "UPDATE sessions SET updated_at=? WHERE id=?",
        (time.time() - 20 * 86400, a1["id"]))
    db._db().commit()
n = db.clear_archived_sessions(time.time() - 14 * 86400)
tomb = db.list_deleted_sessions()
check("clear_archived deletes only the old one",
      n == 1 and db.get_session(a1["id"]) is None
      and db.get_session(a2["id"]) is not None)
check("clear_archived tombstone has right source",
      any(t["session_id"] == a1["id"] and t["source"] == "clear_archived"
          for t in tomb), str([(t["session_id"][:6], t["source"]) for t in tomb]))
# prune: 31-day-old tombstones drop, fresh ones stay
with db._lock:
    db._db().execute(
        "UPDATE deleted_sessions SET deleted_at=? "
        "WHERE session_id=?", (time.time() - 31 * 86400, c1["id"]))
    db._db().commit()
pruned = db.prune_deleted_sessions(30)
check("prune drops >30d tombstones only",
      pruned == 1 and all(t["session_id"] != c1["id"]
                          for t in db.list_deleted_sessions()),
      str(pruned))
# the list endpoint stays metadata-only (no snapshot payload in the list)
check("list rows carry no message payload",
      all("messages" not in t for t in db.list_deleted_sessions()),
      str([sorted(t.keys()) for t in db.list_deleted_sessions()][:1]))
# ── true restore: the tombstone snapshots the messages, restore re-inserts ──
r1 = db.create_session(); db.rename_session(r1["id"], "Restore me")
db.add_message(r1["id"], "user", "first question")
db.add_message(r1["id"], "assistant", "first answer", parts=[
    {"type": "text", "text": "first answer"}])
db.delete_session(r1["id"])
tomb = db.get_deleted_session(r1["id"])
check("tombstone keeps the message snapshot",
      tomb is not None and len(tomb["messages"]) == 2
      and tomb["messages"][0]["role"] == "user"
      and tomb["messages"][0]["content"] == "first question"
      and tomb["messages"][1]["parts"]
      == [{"type": "text", "text": "first answer"}],
      str(tomb and tomb["messages"]))
restored = db.restore_deleted_session(r1["id"])
check("restore re-creates the session (same id)",
      restored is not None and restored["id"] == r1["id"]
      and restored["title"] == "Restore me")
msgs = db.list_messages(r1["id"])
check("restore re-inserts every message in order",
      len(msgs) == 2
      and msgs[0]["role"] == "user" and msgs[0]["content"] == "first question"
      and msgs[1]["role"] == "assistant"
      and msgs[1]["parts"] == [{"type": "text", "text": "first answer"}],
      str([(m["role"], m["content"]) for m in msgs]))
tomb2 = db.get_deleted_session(r1["id"])
check("restored tombstone is stamped",
      tomb2 is not None and tomb2["restored_at"] is not None,
      str(tomb2 and tomb2["restored_at"]))
# double-restore must fail clean (the session exists again)
try:
    db.restore_deleted_session(r1["id"])
    dbl = False
except ValueError:
    dbl = True
check("double restore raises ValueError", dbl)
# restore of a never-deleted id fails clean too
try:
    db.restore_deleted_session("no-such-id")
    ghost = False
except ValueError:
    ghost = True
check("restore of unknown id raises ValueError", ghost)
# the endpoints (list + per-chat restore)
routes = {getattr(r, "path", "") for r in api_mod.app.routes}
check("deleted + restore endpoints registered",
      "/api/sessions/deleted" in routes
      and "/api/sessions/deleted/{sid}/restore" in routes,
      str(sorted(routes))[:200])

print("── 12. chat-switch payload trim (freeze regression) ────────────")
# Regression (measured 2026-09-26): /api/history shipped EVERY message's
# full thinking text — 3.5 MB of the 5.5 MB Muji payload, never rendered
# (the UI's Thinking tab only rehydrates the recent tail, capped at 100
# bursts). list_messages(thinking_tail=K) blanks thinking for everything
# older than the last K messages, in the returned copy only.
sid12 = db.create_session()["id"]
for i in range(10):
    db.add_message(sid12, "user", f"q{i}")
    db.add_message(sid12, "assistant", f"a{i}", thinking="T" * 500)
full12 = db.list_messages(sid12)
check("thinking_tail=None ships everything (default unchanged)",
      all((m.get("thinking") or "") == "T" * 500 for m in full12
          if m["role"] == "assistant"))
trim12 = db.list_messages(sid12, thinking_tail=4)
# the tail is a MESSAGE window (the UI renders the last N messages), not an
# assistant count: 20 msgs, tail=4 → only the last 2 assistant rows keep
n_asst = [m for m in trim12 if m["role"] == "assistant"]
check("thinking_tail=4 keeps the last 4 messages' thinkings",
      all((m.get("thinking") or "") == "T" * 500 for m in trim12[-4:]
          if m["role"] == "assistant")
      and all(m["thinking"] == "T" * 500 for m in n_asst[-2:]))
check("thinking_tail=4 blanks older thinkings",
      all(m["thinking"] == "" for m in n_asst[:-2]))
check("thinking_tail larger than the list is a no-op",
      all((m.get("thinking") or "") == "T" * 500 for m in
          db.list_messages(sid12, thinking_tail=999) if m["role"] == "assistant"))
check("trim is a copy — DB rows stay intact",
      all((m.get("thinking") or "") == "T" * 500 for m in full12
          if m["role"] == "assistant"))
check("agent recall callers unaffected (no thinking_tail arg)",
      len(db.list_messages(sid12, limit=6)) == 6)

print("── 13. search_transcript (recall beyond the 16-message window) ──")
sid13 = db.create_session()["id"]
db.rename_session(sid13, "recall-test-chat")
for i in range(20):  # 20 msgs → the first half is OUTSIDE the 16-msg window
    db.add_message(sid13, "user" if i % 2 == 0 else "assistant",
                   f"message number {i} — filler")
db.add_message(sid13, "user", "the secret codeword was ZEBRA-42")
ctx13 = tools.ToolCtx(cwd=pathlib.Path("."), session_id=sid13)
res13 = tools.t_search_transcript(ctx13, query="ZEBRA-42")
check("finds a message outside the 16-msg window", "ZEBRA-42" in res13)
check("labels the speaker", "· You]" in res13)
res13b = tools.t_search_transcript(ctx13, query="no-such-needle")
check("no-match says so (no confabulation)", "no messages" in res13b)
res13c = tools.t_search_transcript(ctx13, query="", chat="recall-test-chat")
check("empty query by chat title = recent messages", "message number" in res13c)
try:
    tools.t_search_transcript(ctx13, query="ZEBRA-42", chat="no-such-chat")
    check("unknown chat raises ToolError", False)
except tools.ToolError:
    check("unknown chat raises ToolError", True)

print("── 14. thinking-loop tripwire (480KB Narakeets run, 2026-10-06) ──")
# the real degeneration: the same 8-word n-gram re-emitted dozens of times
loop_text = ("Or \"Narakeets\" = \"Narakeets\" = \"Narakeets\" = \"Narakeets\" " * 300)
check("degenerate loop trips", agent._thinking_loop_hit(loop_text))
# a long but LEGITIMATE thinking stream must not trip: varied prose that
# repeats a keyword a few times is normal CoT
prose = " ".join(
    f"step {i} considers the tradeoff of approach {i % 7} against the "
    f"constraint set and moves on to the next candidate" for i in range(200))
check("legit long thinking does NOT trip", not agent._thinking_loop_hit(prose))
# short thinking is never a loop, even if repetitive
check("short repetitive thinking does NOT trip (min_chars)",
      not agent._thinking_loop_hit("Narakeets = Narakeets = " * 5))
# a loop that STARTS at the tail (the live shape: clean thinking, then the
# degeneration kicks in) still trips — the check window is the last 3000 chars
tail_loop = ("reasoning normally about the problem here " * 30
             + "Narakeets = Narakeets = " * 120)
check("tail-onset loop trips", agent._thinking_loop_hit(tail_loop))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("all checks passed ✓")
