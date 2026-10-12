"""Agent loop: stream → tools → (approval) → answer → optional fact-check.

Yields SSE frames (src.sse.sse). Event vocabulary:
token, status, tool_start, phase_end, answer_start, done, correction,
error, stopped, approval, approval_closed.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import io
import json
import pathlib
import re
import subprocess
import time
import uuid

from . import db
from .config import settings
from .llm import LLM, LLMError, ToolUnsupported, is_transient_message
from .sse import get_stopper, sse
from .tools import (NeedsScope, ToolCtx, ToolError, destructive_command,
                    dispatch, exec_command, openai_schemas, restart_command,
                    spill_result, tool_summary)

approvals: dict[str, asyncio.Future] = {}   # approval_id → future
questions: dict[str, asyncio.Future] = {}   # question_id → future (index of choice)
session_sources: dict[str, list[str]] = {}  # session_id → grounding text

#: Mid-stream auto-resume (the dead zone 31d51ac audited): a TRANSIENT LLM
#: error mid-run (timeout / connection drop on the HPC link) ends the turn
#: with an `error` frame inside the live server — no boot, so the boot
#: auto-resume never fires and the saved run_state sat there until the
#: boss clicked ↻. This re-fires the SAME nudge in-process, capped:
#: the budget is per-session, resets after a clean finish, and the nudge
#: is only enqueued while the run_state is still UNSTOPPED — so a
#: deliberate Stop (stopped=1) is never resurrected, a dead endpoint
#: burns the budget once and stops, and a manual ↻ click always works
#: (it clears the budget: the user's explicit intent wins over the cap).
_AUTO_RESUME_MAX = 3
_auto_resume_fires: dict[str, int] = {}
#: the nudge text — set by api.py at import (it owns AUTO_RESUME_TEXT,
#: which must stay byte-identical to RESUME_TEXT in static/app.js so the
#: client renders it as the compact ↻ marker, never a chat bubble)
_auto_resume_text: str = ""

_llm = LLM()

BASE_PROMPT = """You are {brand}, a personal agent created by {creator}, running locally on the user's Windows machine.
If asked who created you, say: created by {creator}.
Today's date: {date}.
Working folder: {cwd}   (relative paths resolve here)
You may read and edit files anywhere on this machine — there is no per-path approval.
Shell commands run via PowerShell and run automatically — the exceptions are destructive operations (delete, remove, rename, move, and similar irreversible actions) and any command that starts, stops, or restarts the muji server; both are shown to the user for approval before they run.
HARD RULE — self-restart, narrowed (ratified 2026-09-24): you may restart the muji server YOURSELF only via POST /api/restart, and only when ALL THREE hold: (a) the change is verified first — the touched modules import clean AND the repo's test suites pass in THIS session (`.venv\Scripts\python.exe tools/test_onit.py` + `tools/test_learned.py`, `node --check static/app.js` for UI); (b) NO other session has in-flight work (the harness enforces this — a restart while other runs are live is rejected); (c) it goes through /api/restart — never a raw kill (Stop-Process/taskkill on the port listener), restart scripts, or `python server.py`; those ALWAYS raise an approval card. If any condition fails, finish the task and say a restart is needed and how to do it (sidebar ⟳ button or `python server.py`) and stop. The user is the ship gate.

Guidelines:
- Use tools to verify instead of guessing file contents, command output, or web facts.
- Date discipline: the system prompt's "Today's date" line is the ONLY authoritative "now". Dates inside conversation history and compaction summaries are from when those messages were written — never anchor "today/tomorrow/next <weekday>" on a date found in the history. When a relative date is load-bearing (a due date, an event, a deadline), confirm the current date with `run_command` (Get-Date) before writing it anywhere.
- Recall, don't confabulate: your context is the recent messages plus a compacted summary of everything older — details from the summarized part may be paraphrased or missing. When the user references an earlier discussion, decision, or detail you can't see clearly in context, call search_transcript (query the DB) BEFORE answering, and say so if it finds nothing. Never invent "what we discussed earlier."
- Don't overthink: act, don't deliberate. For a simple request, answer directly or make at most 1-2 tool calls; don't re-read files whose content is already in context, don't restate your plan before answering, and don't take extra speculative steps the user didn't ask for. This never licenses skipping a verification step — "simple" means trivial asks, not skipping the checks the other rules require.
- No repeated conclusions: in a multi-tool turn, each text segment between tool calls must carry NEW information (a status line, or a finding from the tool just run) — never restate a finding you already stated in an earlier segment. State a result once, in the segment where it's established; later segments may add detail but must not re-announce it. The final answer is the one place a full summary belongs.
- Verify math with tools: you predict tokens, you don't compute — any non-trivial arithmetic (multi-digit numbers, percentages, rates, stats, multi-step) must be computed with a tool (run_command, e.g. `python -c "print(...)"`) before you state the result; never answer it from your head. Trivial single-digit mental math is exempt; when you do compute via a tool, the number is tool-verified — say so if it's load-bearing.
- Web research discipline (Cline-style — YOU are the verifier; no second model catches your slips): (1) primary sources over snippets — fetch the actual page (fetch_url, or browser for JS-rendered pages) before citing it; a 3-line search snippet is a lead, not a source. (2) For any load-bearing claim, cross-check 2+ independent sources; one source = say so. (3) Label every claim in the answer: tool-verified (name the source) / prior knowledge / estimate — never blend them. (4) For anything that changes (docs, versions, prices), the fetch date is part of the claim.
- Answer in Markdown. Be concise but complete.
- Clickable chips: when a FINAL summary or plan contains things the user must decide, approve, or do next, end the answer with a section headed exactly "## Chips" containing 2–6 bullet lines, each a short clickable action (2–8 words, imperative, self-contained — e.g. "- act: Build the fix", "- plan: Draft the migration plan"). Prefix every bullet with "act: " (concrete next action to take) or "plan: " (something to decide/approve/draft first) — the UI colors act chips green and plan chips yellow. Put the recommended/next action FIRST. No other text in that section. The UI renders these as click-to-send buttons and strips the section from the visible answer, so don't label or explain the section. Omit it entirely when there is nothing for the user to pick or do.
- TL;DR: every FINAL summary and every multi-paragraph reply ends with a one-line **TL;DR** at the bottom (after the details, before any Chips section); short mid-task replies and trivial one-liners don't get one.
- Be lean, not lazy: a trivial ask → answer directly. Anything you'll be judged on (a claim about a file/command/URL, or a multi-step task) → verify first: read the file, run the check, follow the result before you answer. Don't re-read what's already in context and don't pad with speculative steps, but never skip a verification step just to be quick. Before you say "done" — or report any checkable result — re-run the check you'd actually expect and confirm it yourself; a tool's exit code alone is not proof (re-read the output, the read-back, or the live state).
- Self-edit discipline (when you modify muji's own code/config under src/, static/, or tools/): after every change, VERIFY before you say done — run the repo's checks (`.venv\Scripts\python.exe tools/test_onit.py` and `tools/test_learned.py` for the backend, `node --check static/app.js` for the UI; for layout/position/visual changes, also open the live UI in the headless browser and verify the rendered DOM (element order, position, visibility)) and re-read the exact lines you changed. Never claim done on a red check. If you add or remove a tool, update the tool-count assertion in tools/test_onit.py in the same change. Keep the working tree consistent with HEAD and its own tests — finish + commit a change, or revert it; don't strand a half-applied edit that later breaks the suite silently.
- When you create a file (report, page, script), say what it contains and where it is — the user gets a clickable preview automatically.
- For multi-step tasks, start with a short plan: one line per step, each line beginning with "STEP: " (e.g. STEP: read the docs). The UI shows these lines as a progress bar, so keep them concrete and in execution order.
- Grounding: at the start of a multi-step task — and after any compaction — run list_dir on the working folder before acting, so you work off what's actually on disk, not the summary.
- Long multi-step tasks: keep a running notes file in the working folder (e.g. task_notes.md) — task goal, key decisions, files created/modified (path + purpose), what's done and what's next. Update it as you go; re-read it at the start of the task and whenever you resume, since earlier context may have been compacted.
- If a part of the request is genuinely vague AND the choice materially changes the outcome → call ask_user with 2-5 concrete options and set recommended to your best guess (it auto-runs that choice if the user is silent for ~3 min). The card always gets a final "Something else — I'll describe it" option appended automatically — never add or recommend it yourself; if the user picks it, ask them to describe their answer. For trivial ambiguity, just pick and state it — don't ask.
- Voice: English, casual, fast. You're the user's best-friend roast dispenser — roast by default: at most one sharp, specific line per reply, woven in as a natural clause (opening, middle, or close as it arises), never a separate "Roast:" label or a footer bolted to the end. A "Roast" command = a dedicated full roast. Pause the roast when the user is frustrated, something's broken (incident mode), or they say "seriously"/"no roast" — resume when they laugh or flip it back on.
- Never invent file paths, URLs, or command output.
 - Image attachments are sent inline — you can see their pixels directly. The file path is also given so you can OCR/crop with run_command (PIL is installed) when a detail is unclear.
 - Big tool results spill to disk: you see a preview plus a handle (res_…). To see the rest, use read_file(path, start_line, end_line) or search_files(pattern, path) — don't re-run the same tool hoping for a different cut.
"""

TEXT_TOOL_PROTOCOL = """
Tool protocol (this backend has no native tool calling):
- When you need a tool, output exactly one line: TOOL_CALL: {{"name": "<tool>", "arguments": {{ ... }}}}
- Wait for the tool result, then continue.
- When you have the final answer for the user, output the line FINAL_ANSWER: and then the answer.
- Never output TOOL_CALL: and FINAL_ANSWER: in the same message.

Available tools:
{tool_list}
"""


#: Tools that change something — disabled in Plan mode (Cline-style).
MUTATING_TOOLS = {"write_file", "edit_file", "run_command"}

PLAN_MODE_PROMPT = (
    "\n\nPLAN MODE: you are planning only — research, read and analyze "
    "(list_dir, read_file, search_files, web_search, fetch_url, browser are available). "
    "Do NOT write or edit files and do NOT run commands; the user switches "
    "you to Act mode to execute. End your answer with a concrete, ordered, "
    "step-by-step plan for the requested change. "
    "The mode stated in this system prompt is authoritative — if the user "
    "disputes it, state what this prompt says; if it contradicts reality, "
    "report the desync.\n"
)

ACT_MODE_PROMPT = (
    "\n\nACT MODE: you are executing — file edits and commands are allowed "
    "subject to the normal approval rules. "
    "The mode stated in this system prompt is authoritative — if the user "
    "disputes it, state what this prompt says; if it contradicts reality, "
    "report the desync.\n"
)


#: Optional user profile (repo root, DESIGNER.md) — appended to every system
#: prompt when present. Absent in the public build; add your own file to
#: customize how the agent addresses and roasts you.
def _designer_profile() -> str:
    try:
        text = (pathlib.Path(__file__).resolve().parent.parent / "DESIGNER.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "\n\nAbout your user (DESIGNER.md — their profile, read it as ground truth):\n" + text


#: muji's own key files (absolute paths) — so "edit your config" goes straight to
#: the file with no hunting for where .env / the db / learned.md live. Computed
#: from the repo root at prompt-build time (mirrors _designer_profile).
def _muji_files_block() -> str:
    r = pathlib.Path(__file__).resolve().parent.parent
    return (
        "\n\nYour own files (muji) — absolute paths, use directly when self-editing:\n"
        f"  config    {r / '.env'}              (runtime: MODEL, ROOT_DIR, PORT, THINKING, RAG_*, …)\n"
        f"  settings  {r / 'src' / 'config.py'} (where env vars are parsed/defaulted)\n"
        f"  code      {r / 'src'}   (api.py · agent.py · llm.py · tools.py · db.py · rag.py)\n"
        f"  entry     {r / 'server.py'}\n"
        f"  data      {r / 'data'}   (muji.db · auto_approve.json)\n"
        f"  notes     {r}   (learned.md · tool_notes.md · INSTRUCTIONS.md · AGENTS.md · PLAN-onit-features.md)\n"
        f"  ui        {r / 'static'}"
    )


def _workspace_block() -> str:
    """WORKSPACE.md → system prompt, for EVERY session regardless of cwd.

    The cwd-independent shared-state map: repo locations, BACKLOG.md,
    learned.md / tool_notes.md, the session registry (live cwds are the DB,
    not the file), and capability notes. Keep the file short — it is
    injected into every session's prompt, every turn (budget ~2.5 KB)."""
    r = pathlib.Path(__file__).resolve().parent.parent
    try:
        text = (r / "WORKSPACE.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return ("\n\nShared workspace state (WORKSPACE.md — canonical paths, valid in "
            "this session regardless of its working directory; cross-session "
            "work ALWAYS uses these absolute paths, never relative):\n"
            + text[:2500])


#: Per-chat roast level (composer toggle 😴/😏/🔥). "chill" is the
#: DESIGNER.md default contract — no extra directive needed.
#: Hard task-lifecycle rules (Boss-ratified 2026-09-24). Kept as a separate
#: plain string concatenated into the prompt by build_system — NOT inside
#: BASE_PROMPT — because BASE_PROMPT goes through .format() and any literal
#: brace here would crash the server at import. No braces allowed in this text.
TASK_RULES = (
    "\n\nHARD RULES — task lifecycle (ratified by the user, non-negotiable):\n"
    "- TL;DR: every FINAL summary AND every multi-paragraph reply ends with a "
    "one-line **TL;DR** (after the details, before any Chips section). Short "
    "mid-task replies (findings, status, questions, plans) and one-line "
    "answers don't get one. This is enforced: a final answer that skips it "
    "gets a visible ⚑ flag note and the chat's sidebar flag count ticks up "
    "(deterministic check in agent.py — the flag is the consequence, the "
    "TL;DR is the fix).\n"
    "- Temp files: track every probe/debug/scratch file you create; at task "
    "end, sweep them in one approved delete batch. No strays left in the tree.\n"
    "- Commit only your own work: before closing a task, commit the files "
    "THIS task touched. If the working tree is entangled with other "
    "uncommitted work, stop and ask — committing work that isn't yours needs "
    "explicit OK.\n"
    "- Task ends at push: commit is the end of the task; if the repo has a "
    "remote, push is the end of the task (muji has one — origin)."
)

ROAST_OFF_PROMPT = (
    "\n\nROAST LEVEL: OFF (the user set the roast dispenser to 😴 off for this "
    "chat) — no roasts in any reply; work voice only, English/casual/fast as "
    "always. If the user explicitly types a roast command, honor it — commanded "
    "roasts always go full."
)

ROAST_FULL_PROMPT = (
    "\n\nROAST LEVEL: FULL (the user set the roast dispenser to 🔥 full for "
    "this chat) — go full roast dispenser: roasts may run 2-4 lines, sharper, "
    "specific, creative; the roast flavors the whole reply. Work still comes "
    "first: the task gets the same standard and the roast never blocks, delays "
    "or drowns the answer. The pause rules still apply (the user is "
    "frustrated, incident mode, 'seriously')."
)


def _learned_block() -> str:
    """learned.md → system prompt. Only the `## active` section is injected —
    patterns land in `## pending` first and take effect only when Boss moves
    them (veto gate: delete = veto, move to active = approve)."""
    try:
        text = settings.learned_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    body = (m.group(1) if m else "").strip()
    if not body:
        return ""
    return ("\n\nLearned patterns (learned.md — approved by the user, follow them):\n"
            + body[:2000])


def _tool_notes_block() -> str:
    """tool_notes.md → system prompt. Only the `## active` section is injected.
    Unlike learned.md these notes AUTO-activate on append — tool quirks are
    verifiable external facts (API behavior + fix + last-verified date +
    recheck rule), not self-judgments; the Boss deactivates stale ones from
    the 🧠 panel."""
    try:
        text = settings.tool_notes_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    body = (m.group(1) if m else "").strip()
    if not body:
        return ""
    return ("\n\nTool quirks (tool_notes.md — verified tool behaviors, follow the fixes; "
            "if the live behavior contradicts a note, re-verify and update the note):\n"
            + body[:2000])


#: INSTRUCTIONS.md auto-archive gate. The live log is NOT injected per turn
#: (only its path is referenced), so the cost being bounded is a FULL READ
#: (~60K tokens at 248 KB), not per-turn tokens. 30 KB ≈ "fat again" (the
#: 2026-09-27 manual trim left it at 14 KB); when crossed, entries older
#: than 14 days move to INSTRUCTIONS-ARCHIVE.md.
INSTRUCTIONS_SPLIT_BYTES = 30_000
INSTRUCTIONS_SPLIT_DAYS = 14


def _instructions_auto_split() -> None:
    """Size-gated auto-archive for INSTRUCTIONS.md (never raises).

    When the live log exceeds INSTRUCTIONS_SPLIT_BYTES, every
    `## YYYY-MM-DD` entry older than INSTRUCTIONS_SPLIT_DAYS days moves
    byte-identical to INSTRUCTIONS-ARCHIVE.md (created on first split;
    otherwise prepended after its header, keeping newest-first order). The
    live file keeps its header + the recent entries + a fresh pointer line
    (a stale pointer block is replaced, never duplicated). No-op when under
    the gate or when no entry is old enough — the file then stays fat until
    entries age out (no infinite re-split)."""
    try:
        p = settings.instructions_path
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    if len(text.encode("utf-8")) <= INSTRUCTIONS_SPLIT_BYTES:
        return
    matches = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2})\b", text, re.M))
    if len(matches) < 2:
        return
    header = text[:matches[0].start()]
    entries = [(m.group(1), m.start(),
                matches[i + 1].start() if i + 1 < len(matches) else len(text))
               for i, m in enumerate(matches)]
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=INSTRUCTIONS_SPLIT_DAYS)).isoformat()
    old = [(d, s, e) for d, s, e in entries if d < cutoff]
    if not old:
        return
    # The trailing pointer block (if any) belongs to the live file, not the log.
    pm = re.search(r"\n---\n\n_Entries older than.*\Z", text, re.S)
    old_end = old[-1][2]
    if pm and pm.start() < old_end:
        old_end = pm.start()
    old_block = text[old[0][1]:old_end].rstrip() + "\n"
    ap = settings.instructions_archive_path
    try:
        if ap.exists():
            atext = ap.read_text(encoding="utf-8")
            am = re.search(r"^## \d{4}-\d{2}-\d{2}\b", atext, re.M)
            ins = am.start() if am else len(atext)
            ap.write_text(atext[:ins] + old_block + atext[ins:], encoding="utf-8")
        else:
            ap.write_text(
                "# INSTRUCTIONS-ARCHIVE.md — muji\n\n"
                "Entries older than ~2 weeks, auto-archived from INSTRUCTIONS.md\n"
                "(size-gated split in agent.py). Newest first, same format as\n"
                "the live log. The live file keeps the recent entries + a pointer here.\n\n"
                + old_block, encoding="utf-8")
    except OSError:
        return
    recent = []
    for d, s, e in entries:
        if d >= cutoff:
            if pm and pm.start() < e:
                e = pm.start()
            recent.append(text[s:e])
    live = header + "".join(recent)
    live += (f"\n---\n\n_Entries older than {cutoff} ({len(old)} entries) are "
             "archived in `INSTRUCTIONS-ARCHIVE.md`. Read it for older history; "
             "new entries always go to the top of this file._\n")
    try:
        p.write_text(live, encoding="utf-8")
    except OSError:
        pass


def _recall_block(user_text: str) -> str:
    """Previous similar tasks (token-overlap recall over the trajectories
    table) — a short memory block so the model doesn't repeat past mistakes
    or redo work it already knows how you like done."""
    try:
        rows = db.recall_trajectories(user_text)
    except Exception:  # noqa: BLE001 — memory must never break a turn
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows[:3]:
        bits = [f"- task: {(r.get('goal') or '')[:150]}"]
        if r.get("pattern"):
            bits.append(f"pattern: {str(r['pattern'])[:150]}")
        if r.get("verified"):
            bits.append(f"self-audit: {str(r['verified'])[:150]}")
        lines.append(" ".join(bits))
    return ("\n\nPREVIOUS SIMILAR TASKS (from past runs — learn from them, "
            "don't repeat their mistakes):\n" + "\n".join(lines))


def _session_notes_block(session_id: str) -> str:
    """This chat's durable task notes, re-injected every turn. The file is
    the single source of truth (note_fact appends to it; the boss can read
    it via the 📝 button in the Files tab) — the prompt only carries its
    tail, capped so a runaway notes file can't eat the context budget."""
    if not session_id:
        return ""
    try:
        from .config import settings
        p = settings.data_dir / "sessions" / session_id / "task_notes.md"
        if p.is_file():
            text = p.read_text("utf-8", errors="replace")
            tail = text[-4000:] if len(text) > 4000 else text
            return (f"\nSESSION NOTES — this chat's durable memory "
                    f"({p}). Re-injected every turn; survives compaction, "
                    f"reload, restart, resume. Keep it current with "
                    f"note_fact / edit_file.\n" + tail)
    except Exception:  # noqa: BLE001 — notes must never kill prompt build
        pass
    return ("\nSESSION NOTES — this chat has no task notes yet "
            f"(they would live at data/sessions/{session_id}/task_notes.md). "
            "When the boss hands you an identifier you must not lose "
            "(SSH address/host, IP:port, credential, token, path, connection "
            "string), a key decision, or task state, call note_fact(fact) — "
            "it creates the file and it is re-injected into the system "
            "prompt every turn from then on.")


def build_system(cwd: pathlib.Path, text_mode: bool, plan_mode: bool = False,
                 roast: str = "chill", recall: str = "",
                 session_id: str = "") -> str:
    p = BASE_PROMPT.format(brand=settings.brand,
                           creator=settings.creator,
                           date=datetime.date.today().isoformat(),
                           cwd=cwd, root=settings.root_dir)
    p += TASK_RULES
    _instructions_auto_split()  # housekeeping at prompt-build time; never raises
    p += _muji_files_block()
    p += _workspace_block()
    p += _designer_profile()
    p += _learned_block()
    p += _tool_notes_block()
    p += _session_notes_block(session_id)
    if recall:
        p += _recall_block(recall)
    if text_mode:
        p += TEXT_TOOL_PROTOCOL.format(
            tool_list=tool_summary(exclude=MUTATING_TOOLS if plan_mode else None))
    if plan_mode:
        p += PLAN_MODE_PROMPT
    else:
        p += ACT_MODE_PROMPT
    if roast == "off":
        p += ROAST_OFF_PROMPT
    elif roast == "full":
        p += ROAST_FULL_PROMPT
    return p


def _refresh_date(msgs: list[dict]) -> bool:
    """Mid-run date refresh: the system prompt's date is baked at turn
    start, so a run that crosses midnight (or a resume of a run that
    started the previous day) would otherwise answer with yesterday's
    date — 'tomorrow' then lands on the wrong day. Re-stamp the line
    only when the calendar day actually changed (string replace, not a
    full rebuild — the session-notes/learned/tool-notes blocks are
    unchanged within a run). Returns True when the prompt was updated."""
    line = f"Today's date: {datetime.date.today().isoformat()}."
    sp = msgs[0].get("content")
    if not isinstance(sp, str) or line in sp:
        return False
    import re
    sp2 = re.sub(r"Today's date: \d{4}-\d{2}-\d{2}\.", line, sp, count=1)
    if sp2 != sp:
        msgs[0] = {"role": "system", "content": sp2}
        return True
    return False


LEARNED_SKELETON = """# learned.md — muji's learned patterns (human-gated)

muji appends candidate patterns to `## pending` after tasks. They have NO
effect until you move a line to `## active` (veto = delete it). Only
`## active` is injected into the system prompt. When the pending queue
overflows its cap, the OLDEST entries sink to `## held` — kept in the file,
never injected, invisible to the review panel; move one back to `## pending`
to resurrect it.

## pending

## active
"""

TOOL_NOTES_SKELETON = """# tool_notes.md — muji's tool-quirk log (auto-active)

muji appends verified tool quirks straight to `## active` after hitting them
live — these are verifiable external facts (API behavior + fix + recheck
rule), not self-judgments, so no human gate. Only `## active` is injected
into the system prompt; the Boss deactivates stale notes from the 🧠 panel.
`## pending` is kept for manual drafts only (same cap + `## held` overflow
as learned.md).

Format: **Tool / endpoint → quirk → fix.** [verified YYYY-MM-DD] Recheck: <the
signal that tells you the tool changed and this note is stale>.

## pending

## active
"""


#: Cap on a gated file's `## pending` review queue. Pending is the Boss's
# work queue, not a landfill: the newest PENDING_CAP stay in `## pending`
# (what the 🧠 panel shows); oldest overflow sinks to `## held` — kept in
# the file (rescuable), never injected, invisible to the panel.
PENDING_CAP = 15


def _fix_phrase(line: str) -> str:
    """A gated entry's bolded fix phrase, normalized to lowercase alnum
    words ('… → **Verify before asserting.** …' → 'verify before asserting').
    '' when the entry has no ** ** pair (older entries)."""
    m = re.search(r"\*\*(.+?)\*\*", line or "", re.S)
    return re.sub(r"[^a-z0-9]+", " ", (m.group(1) if m else "").lower()).strip()


def _enforce_pending_cap(text: str) -> str:
    """Bound a gated file's `## pending` at PENDING_CAP bullets: the OLDEST
    (bottom) overflow moves to a `## held` section after `## active`.
    No-op when under the cap. `## held` is never injected (only `## active`
    is) and invisible to the review panel (it parses pending/active only) —
    so the queue can't flood the prompt, the file, or the UI, and a missed
    lesson stays rescuable from the file itself."""
    pending, active = _gated_sections(text)
    if len(pending) <= PENDING_CAP:
        return text
    overflow = pending[PENDING_CAP:]
    new_text = _rewrite_gated(text, pending[:PENDING_CAP], active)
    block = "\n".join("- " + x for x in overflow)
    mh = re.search(r"^##\s*held\s*$(.*?)(?=^##\s|\Z)", new_text, re.M | re.S | re.I)
    if mh:
        new_text = (new_text[:mh.start(1)] + "\n" + block + "\n"
                    + mh.group(1).lstrip("\n") + new_text[mh.end(1):])
    else:
        new_text = new_text.rstrip("\n") + "\n\n## held\n\n" + block + "\n"
    return new_text


def _append_gated_pending(p, skeleton: str, note: str) -> None:
    """Append a candidate line under `## pending` in a gated file — no
    self-activation. Shared by learned.md and tool_notes.md. Dedup is
    two-tier: exact-line (pending/active) AND same-bolded-fix phrase — the
    2026-09-29 flood was 70+ re-narrations of one active rule (same lesson,
    new incident, new line: exact-match never fired). The pending queue is
    capped (PENDING_CAP); oldest overflow sinks to `## held`."""
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else skeleton
    except OSError:
        text = skeleton
    note = " ".join((note or "").split())
    if len(note) > 300:  # word-safe cut — no mid-sentence truncation
        cut = note[:300]
        sp = cut.rfind(" ")
        if sp > 200:
            cut = cut[:sp]
        note = cut + "…"
    line = "- " + note
    m = re.search(r"^##\s*pending\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if not m:
        text += "\n\n## pending\n"
        m = re.search(r"^##\s*pending\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    body = m.group(1)
    if line in body:
        return  # already logged
    ma = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if ma and line in ma.group(1):
        return  # already active — an active note is already in effect
    fp = _fix_phrase(line)
    if fp:
        others = re.findall(r"^\s*-\s+(.*)$", body, re.M)
        if ma:
            others += re.findall(r"^\s*-\s+(.*)$", ma.group(1), re.M)
        if any(_fix_phrase(o) == fp for o in others):
            return  # same bolded fix already queued/active — re-narration
    # newest on top (Boss rule, 2026-09-25): insert as the first bullet so
    # the latest lesson is the first thing he sees when reviewing.
    text = (text[:m.start(1)] + "\n" + line + "\n"
            + body.lstrip("\n") + text[m.end(1):])
    text = _enforce_pending_cap(text)
    try:
        p.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _learned_append_pending(pattern: str) -> None:
    """Append a candidate pattern under `## pending` — no self-activation."""
    _append_gated_pending(settings.learned_path, LEARNED_SKELETON, pattern)


def _tool_notes_append_pending(note: str) -> None:
    """Append a manual-draft tool quirk under `## pending` (no injection)."""
    _append_gated_pending(settings.tool_notes_path, TOOL_NOTES_SKELETON, note)


def _tool_notes_append_active(note: str) -> None:
    """Append a verified tool quirk straight to `## active` — auto-activates.
    Tool quirks are verifiable external facts, not self-judgments, so they
    skip the human gate (the Boss can still deactivate them from the 🧠
    panel). Dedupes against both sections; skips if already active."""
    p = settings.tool_notes_path
    try:
        text = p.read_text(encoding="utf-8") if p.exists() else TOOL_NOTES_SKELETON
    except OSError:
        text = TOOL_NOTES_SKELETON
    line = "- " + " ".join((note or "").split())[:300]
    ma = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if ma and line in ma.group(1):
        return  # already active
    if not ma:
        text += "\n\n## active\n"
        ma = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    body = ma.group(1)
    if line in body:
        return
    # don't also leave a duplicate in pending
    mp = re.search(r"^##\s*pending\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    if mp and line in mp.group(1):
        pbody = mp.group(1).replace(line + "\n", "")
        text = text[:mp.start(1)] + pbody + text[mp.end(1):]
        ma = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
        body = ma.group(1)
    text = text[:ma.start(1)] + body + line + "\n" + text[ma.end(1):]
    try:
        p.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _gated_sections(text: str) -> tuple[list[str], list[str]]:
    """A gated markdown file → (pending, active) bullet lines, as raw strings.
    Shared by learned.md (my failures) and tool_notes.md (tool quirks)."""
    def body_of(marker: str) -> list[str]:
        m = re.search(r"^##\s*" + marker + r"\s*$(.*?)(?=^##\s|\Z)",
                      text, re.M | re.S | re.I)
        if not m:
            return []
        return [ln[2:].strip() for ln in m.group(1).splitlines()
                if ln.lstrip().startswith("-")]
    return body_of("pending"), body_of("active")


def _learned_sections(text: str) -> tuple[list[str], list[str]]:
    """learned.md → (pending, active) bullet lines, as raw pattern strings."""
    return _gated_sections(text)


def learned_status() -> dict:
    try:
        text = settings.learned_path.read_text(encoding="utf-8")
    except OSError:
        return {"pending": [], "active": []}
    pending, active = _learned_sections(text)
    return {"pending": pending, "active": active}


def tool_notes_status() -> dict:
    try:
        text = settings.tool_notes_path.read_text(encoding="utf-8")
    except OSError:
        return {"pending": [], "active": []}
    pending, active = _gated_sections(text)
    return {"pending": pending, "active": active}


def _rewrite_gated(text: str, pending: list[str], active: list[str]) -> str:
    """Rebuild a gated file from its two sections (header + raw tail kept)."""
    mp = re.search(r"^##\s*pending\s*$", text, re.M | re.I)
    if not mp:
        return text  # malformed — leave it untouched
    header = text[:mp.start()].rstrip() + "\n\n"
    ma = re.search(r"^##\s*active\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    tail = text[ma.end(1):] if ma else ""
    out = header + "## pending\n"
    out += ("\n".join("- " + x for x in pending) + "\n") if pending else ""
    out += "\n## active\n"
    out += ("\n".join("- " + x for x in active) + "\n") if active else ""
    out += tail
    return out


def _rewrite_learned(text: str, pending: list[str], active: list[str]) -> str:
    """Rebuild learned.md from its two gated sections (header + raw tail kept)."""
    return _rewrite_gated(text, pending, active)


def _apply_gate_decisions(text: str, decisions: list,
                          skeleton: str) -> tuple[list[str], list[str], int, int, int]:
    """Pure half of the human gate: apply activate/veto/deactivate on the
    pending/active sections. keep = no-op. Returns (pending, active,
    activated, vetoed, deactivated)."""
    pending, active = _gated_sections(text)
    activated = vetoed = deactivated = 0
    for d in decisions or []:
        t = str(d.get("text") or "").strip()
        a = str(d.get("action") or "keep").lower()
        if not t:
            continue
        if a == "activate" and t in pending:
            pending.remove(t)
            if t not in active:
                active.insert(0, t)  # newest first — most recent lesson on top
            activated += 1
        elif a == "veto" and t in pending:
            pending.remove(t)
            vetoed += 1
        elif a == "deactivate" and t in active:
            active.remove(t)
            deactivated += 1
    return pending, active, activated, vetoed, deactivated


def apply_learned_decisions(decisions: list) -> dict:
    """The human gate: apply the Boss's activate/veto/deactivate calls on the
    pending patterns and rewrite learned.md. keep = no-op. Only the two gated
    sections change; the header is preserved."""
    p = settings.learned_path
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = LEARNED_SKELETON
    pending, active, activated, vetoed, deactivated = _apply_gate_decisions(
        text, decisions, LEARNED_SKELETON)
    if not (activated or vetoed or deactivated):
        return {"pending": pending, "active": active, "activated": 0,
                "vetoed": 0, "deactivated": 0}
    try:
        p.write_text(_rewrite_gated(text, pending, active), encoding="utf-8")
    except OSError:
        pass
    return {"pending": pending, "active": active, "activated": activated,
            "vetoed": vetoed, "deactivated": deactivated}


def apply_tool_notes_decisions(decisions: list) -> dict:
    """Boss's calls on tool_notes.md — same mechanics as learned.md
    (activate pending drafts / veto / deactivate active), different file.
    keep = no-op. Only the two gated sections change."""
    p = settings.tool_notes_path
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        text = TOOL_NOTES_SKELETON
    pending, active, activated, vetoed, deactivated = _apply_gate_decisions(
        text, decisions, TOOL_NOTES_SKELETON)
    if not (activated or vetoed or deactivated):
        return {"pending": pending, "active": active, "activated": 0,
                "vetoed": 0, "deactivated": 0}
    try:
        p.write_text(_rewrite_gated(text, pending, active), encoding="utf-8")
    except OSError:
        pass
    return {"pending": pending, "active": active, "activated": activated,
            "vetoed": vetoed, "deactivated": deactivated}


async def _log_trajectory(session_id: str, goal: str, answer: str,
                          tool_names: list[str], outcome: str) -> None:
    """Fire-and-forget: self-audit the finished task, then log one trajectory
    row (the raw material for recall + learned.md). Never raises — memory
    must not break or delay the user's turn."""
    try:
        verified, pattern, toolnote = await _trajectory_audit(answer, tool_names)
        if pattern:
            _learned_append_pending(pattern)
        if toolnote:
            _tool_notes_append_active(toolnote)
        db.log_trajectory(session_id, goal, list(dict.fromkeys(tool_names)),
                          answer, verified, pattern, outcome)
    except Exception:  # noqa: BLE001 — memory must never break the turn
        pass


async def _trajectory_audit(answer: str, tool_names: list[str]) -> tuple[str, str | None, str | None]:
    """One low-temp self-audit: which load-bearing claims were CHECKED with a
    tool vs ASSERTED. Returns (verified, pattern, toolnote). `pattern` is a
    candidate for learned.md `## pending` — a GENUINE FAILURE only (the agent
    did something wrong the user would be unhappy about), never a mere
    preference. `toolnote` is a candidate for tool_notes.md `## pending` — a
    verified tool/API quirk (the tool's behavior, not the agent's fault),
    confirmed with a live call this task. `pattern` lands in learned.md
    `## pending` (human-gated); `toolnote` auto-activates into tool_notes.md
    `## active` (verifiable fact, Boss can deactivate stale ones)."""
    tools = ", ".join(tool_names[:20]) or "(none)"
    try:
        _, active = _learned_sections(settings.learned_path.read_text(encoding="utf-8"))
    except OSError:
        active = []
    active_txt = "\n".join("- " + r for r in active[:40]) or "(none yet)"
    try:
        out = await asyncio.wait_for(_llm.complete([
            {"role": "system",
             "content": ("You audit an agent's finished task. Reply with exactly three lines:\n"
                         "VERIFIED: <one line — which load-bearing claims were checked with a "
                         "tool vs merely asserted>\n"
                         "PATTERN: <one line — a GENUINE FAILURE only: something the agent did "
                         "concretely wrong, or failed/errored to deliver, that the user would be "
                         "disappointed by. Phrase it 'I <wrong behavior> → **Fix.**'. If the task "
                         "succeeded, the user's standing preference was simply met, nothing was "
                         "wrong, or it matches an ACTIVE RULE below — write NONE. Never log a "
                         "preference, taste, style opinion, or a re-wording of an active rule. "
                         "When in doubt, write NONE.>\n"
                         "TOOLNOTE: <one line — a verified TOOL/API quirk the agent hit live and "
                         "worked around (not the agent's fault, the tool's behavior): 'Tool → "
                         "quirk → fix. [verified YYYY-MM-DD] Recheck: <signal that the tool "
                         "changed>'. Only when the quirk was actually confirmed with a live call "
                         "this task. If none, write NONE.>\n"
                         "ACTIVE RULES (already learned — do not re-log these):\n"
                         f"{active_txt}")},
            {"role": "user",
             "content": f"TOOLS USED (in order):\n{tools}\n\nFINAL ANSWER:\n{answer[:6000]}"},
            # thinking off: a 3-line format task needs no chain-of-thought —
            # at the server default (xhigh) the call burned the 60s budget and
            # came back unparseable (audit usable in only 23% of runs).
        ], temperature=0.0, thinking="off"), 60)
    except Exception:  # noqa: BLE001 — the audit is best-effort, never blocks
        return "(audit unavailable)", None, None
    verified, pattern, toolnote = "", None, None
    for line in (out or "").splitlines():
        ls = line.strip()
        if ls.upper().startswith("VERIFIED:"):
            verified = ls.split(":", 1)[1].strip()
        elif ls.upper().startswith("TOOLNOTE:"):
            n = ls.split(":", 1)[1].strip()
            toolnote = None if n.upper() == "NONE" else n
        elif ls.upper().startswith("PATTERN:"):
            p = ls.split(":", 1)[1].strip()
            pattern = None if p.upper() == "NONE" else p
    return verified or "(no audit output)", pattern, toolnote


def _parse_text_tool(draft: str) -> dict | None:
    """Extract a complete TOOL_CALL line from buffered model text."""
    m = re.search(r"^\s*TOOL_CALL:\s*(\{.*\})\s*$", draft, re.M)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    return {"name": str(obj["name"]), "arguments": obj.get("arguments") or {}}


STEP_RE = re.compile(r"^\s*STEP:\s*(.+?)\s*$", re.M | re.I)


def plan_steps(draft: str) -> list[str]:
    """`STEP:` lines the model wrote (drives the progress bar; max 12)."""
    return [m.group(1)[:120] for m in STEP_RE.finditer(draft)][:12]


def _plan_match(items: list[str], pending: set[int], tool: str, args: dict) -> int:
    """Best-matching pending plan step for a tool call (positional fallback)."""
    if not pending:
        return -1
    hay = " ".join([tool] + [str(v) for v in (args or {}).values()]).lower()
    hay_words = set(re.findall(r"[a-z0-9_.\-]{3,}", hay))
    best, best_score = -1, 0
    for i in sorted(pending):
        words = set(re.findall(r"[a-z0-9_.\-]{3,}", items[i].lower()))
        score = len(words & hay_words)
        if score > best_score:
            best, best_score = i, score
    return best if best != -1 else min(pending)


async def _fact_check(answer: str, sources: list[str]) -> dict | None:
    # skip stub sources (uploads without extractable text) — nothing to check against
    real = [s for s in sources if len(s) > 60]
    src_text = "\n\n".join(s[:4000] for s in real[-6:])[:20000]
    if not src_text.strip() or not answer.strip():
        return None
    messages = [
        {"role": "system", "content":
         "You are a strict fact-checker. Compare the ANSWER against the SOURCES. "
         "If every factual claim is supported by the sources (or is not factual — "
         "e.g. a description of files you just wrote), reply exactly VERIFIED. "
         "Otherwise reply with a SHORT NOTE ONLY (1-3 lines): name the specific "
         "claim that is wrong and give the correct value per the sources. "
         "Do NOT restate or rewrite the answer."},
        {"role": "user", "content": f"SOURCES:\n{src_text}\n\nANSWER:\n{answer}"},
    ]
    try:
        out = await asyncio.wait_for(_llm.complete(messages, temperature=0.0,
                                               thinking="off"), 90)
    except Exception:  # noqa: BLE001
        return None
    out = (out or "").strip()
    if not out or out.startswith("VERIFIED"):
        return None
    note = re.sub(r"^\s*(NOTE|CORRECTION)\s*:\s*", "", out, flags=re.I).strip()
    return {"note": note[:300] or "some claims did not match the sources"}


from collections.abc import AsyncGenerator  # noqa: E402


#: TL;DR enforcement (Boss-ratified 2026-09-25): the rule lives in
#: TASK_RULES (prompt-level), this is the deterministic backstop — a
#: FINAL answer that should carry a TL;DR but doesn't gets a visible
#: flag note + the chat's sidebar flag count ticks up. Detection is
#: pure (no LLM): fenced code blocks and the Chips section don't count
#: as body, so a code dump or a chips-only closer never false-flags.
_TLDR_RE = re.compile(r"\*\*TL;DR\*\*|TL;DR\s*:|TL;DR\s*—", re.I)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
#: a reply is "multi-paragraph" (TL;DR-obligated) at >=4 non-empty body
#: lines or >=150 words — matches the audit definition that found
#: 111/111 compliance
_TLDR_LINES = 4
_TLDR_WORDS = 150


_CHIPS_HDR_RE = re.compile(r"\n[ \t]*##[ \t]+Chips[ \t]*\r?\n")


def _normalize_tldr_order(answer: str) -> str:
    """Move a TL;DR that landed after the ## Chips section to just before
    it. extractChips (app.js) strips everything from ## Chips to the end,
    so a TL;DR written after the chips is invisible in the UI — 115/277
    pre-fix finals did exactly that (audit 2026-09-25). No-op when the
    order is already right or either part is missing."""
    if not answer or "## Chips" not in answer:
        return answer
    m = _CHIPS_HDR_RE.search(answer)
    if not m or _TLDR_RE.search(answer[:m.start()]):
        return answer
    tail = answer[m.start():]
    lm = re.search(r"(?m)^[^\n]*TL;DR[^\n]*\r?$", tail, re.I)
    if not lm:
        return answer
    # capture the TL;DR's whole paragraph (continuation lines until a
    # blank line or a chip bullet) so it moves intact — lm.end() sits at
    # the end of the matched line, so step past its newline first
    end = lm.end()
    nl0 = tail.find("\n", end)
    if nl0 != -1:
        end = nl0 + 1
    while end < len(tail):
        nl = tail.find("\n", end)
        if nl == -1:
            end = len(tail)
            break
        nxt = tail[end:nl]
        if not nxt.strip() or re.match(r"^\s*[-*]\s", nxt):
            break
        end = nl + 1
    para = tail[lm.start():end].strip()
    rest = (tail[:lm.start()] + tail[end:]).strip("\n")
    head = answer[:m.start()].rstrip("\n")
    if _TLDR_RE.search(head):
        # head already carries a TL;DR and the model ALSO wrote one after
        # the chips (msg 3027, 2026-09-25) — the tail copy is invisible
        # boilerplate, drop it
        return head + "\n" + rest
    return head + "\n\n" + para + "\n" + rest


def _drop_tldr_after_chips(answer: str) -> str:
    """Remove a TL;DR that sits after ## Chips when the answer already
    carries one before it (the model occasionally writes the TL;DR twice —
    once correctly, once after the chips where the UI strips it; msg 3027,
    2026-09-25). No-op when the tail copy is the ONLY TL;DR — that case
    belongs to _normalize_tldr_order (move, not drop)."""
    if not answer or "## Chips" not in answer:
        return answer
    m = _CHIPS_HDR_RE.search(answer)
    if not m or not _TLDR_RE.search(answer[:m.start()]):
        return answer
    tail = answer[m.start():]
    lm = re.search(r"(?m)^[^\n]*TL;DR[^\n]*\r?$", tail, re.I)
    if not lm:
        return answer
    end = lm.end()
    nl0 = tail.find("\n", end)
    if nl0 != -1:
        end = nl0 + 1
    while end < len(tail):
        nl = tail.find("\n", end)
        if nl == -1:
            end = len(tail)
            break
        nxt = tail[end:nl]
        if not nxt.strip() or re.match(r"^\s*[-*]\s", nxt):
            break
        end = nl + 1
    return (answer[:m.start()] + tail[:lm.start()] + tail[end:]).rstrip() + "\n"


def _needs_tldr_flag(answer: str) -> bool:
    body = _CODE_FENCE_RE.sub("", answer or "")
    if _TLDR_RE.search(body):
        return False  # already carries a TL;DR — nothing to flag
    body = body.split("## Chips", 1)[0]
    lines = [l for l in body.splitlines() if l.strip()]
    words = len(body.split())
    return len(lines) >= _TLDR_LINES or words >= _TLDR_WORDS


def _flag_tldr(answer: str) -> str:
    """Append the visible flag note (or return the answer unchanged when
    no flag is due). The note is appended to the STORED content too, so
    history renders it without any extra plumbing. Idempotent: the note
    itself is multi-paragraph, so re-flagging must be a no-op."""
    if not _needs_tldr_flag(answer) or "TL;DR flag" in (answer or ""):
        return answer
    note = ("> ⚑ **TL;DR flag** — this reply should end with a "
            "one-line **TL;DR** (hard rule: every final summary and every "
            "multi-paragraph reply). It didn't — flagged automatically.")
    # a note appended after ## Chips would be invisible (extractChips strips
    # everything from the header on) — insert it before the section instead
    m = _CHIPS_HDR_RE.search(answer)
    if m:
        return (answer[:m.start()].rstrip("\n") + "\n\n" + note
                + answer[m.start():])
    return answer + "\n\n" + note


async def _wait_approval(aid: str, stopper) -> str:
    """Wait for approve/deny/timeout/stop; returns the decision string."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    approvals[aid] = fut
    fut_f = asyncio.ensure_future(fut)
    stop_f = asyncio.ensure_future(stopper.wait())
    try:
        done, _ = await asyncio.wait({fut_f, stop_f},
                                     timeout=settings.approval_timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        return (fut_f.result() if fut_f in done else
                "stopped" if stop_f in done else "timeout")
    finally:
        fut_f.cancel()
        stop_f.cancel()
        approvals.pop(aid, None)


async def _wait_question(qid: str, stopper, recommended: int) -> tuple[int, str]:
    """Wait for a question answer. Returns (index, source) where source is
    'user', 'timeout' (→ the recommended choice), or 'stopped' (index -1)."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    questions[qid] = fut
    fut_f = asyncio.ensure_future(fut)
    stop_f = asyncio.ensure_future(stopper.wait())
    try:
        done, _ = await asyncio.wait({fut_f, stop_f},
                                     timeout=settings.question_timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if fut_f in done:
            return (int(fut_f.result()), "user")
        if stop_f in done:
            return (-1, "stopped")
        return (recommended, "timeout")
    finally:
        fut_f.cancel()
        stop_f.cancel()
        questions.pop(qid, None)


def _git_last_safe(cwd: pathlib.Path, log) -> None:
    """Turn-start checkpoint: in a git working tree, point the
    muji/last-safe tag at HEAD so a broken self-edit is one approval-
    gated `git reset --hard muji/last-safe` away. Silently skipped
    anywhere else — a checkpoint must never block or alter a turn."""
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=str(cwd), capture_output=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip() == b"true":
            subprocess.run(["git", "tag", "-f", "muji/last-safe", "HEAD"],
                           cwd=str(cwd), capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def resolve_cwd(sess: dict) -> pathlib.Path:
    """Working directory for a chat's tools: the folder chosen in the Files
    tab (sessions.cwd) wins, then the workspace path, then the global root.
    A stored cwd that no longer exists (or left the root) is ignored."""
    ws = db.get_workspace(sess.get("workspace_id")) if sess.get("workspace_id") else None
    cwd = pathlib.Path(ws["path"]) if ws else settings.root_dir
    if sess.get("cwd"):
        cand = pathlib.Path(sess["cwd"])
        if cand.is_dir() and (cand == settings.root_dir
                              or settings.root_dir in cand.parents):
            cwd = cand
    return cwd


async def auto_title(session_id: str, first_message: str) -> None:
    """Name a new chat after its first message (fire-and-forget).

    An instant derived phrase appears first; a short model-generated title
    (2-6 words, same language as the message) replaces it — unless the user
    already renamed the chat, in which case nothing is touched.
    """
    try:
        text = re.sub(r"\s+", " ", first_message or "").strip()
        if not text:
            return
        phrase = text if len(text) <= 60 else text[:60].rsplit(" ", 1)[0]
        phrase = phrase.rstrip(".,;:!?-– ")
        if not phrase:
            return
        db.rename_session(session_id, phrase)
        if not settings.model:
            return
        title = await _llm.complete([
            {"role": "system",
             "content": ("You name chat conversations. Look at the user's "
                         "first message and reply with ONLY a short title for "
                         "the chat: 2-6 words, no quotes, no trailing "
                         "punctuation, in the same language as the message. "
                         "Capture what the user wants done or discussed; do "
                         "not repeat instructions or quoted fragments.")},
            {"role": "user", "content": text[:600]},
        ], temperature=0.2, thinking="off")  # 2-6 word label: no CoT needed
        title = (title or "").strip().splitlines()
        title = title[0].strip().strip("\"'“”‘’«»[]()") if title else ""
        title = title.rstrip(".,;:!?-– ")[:60]
        if len(title) >= 2 and len(title.split()) >= 2:
            db.rename_session_if(session_id, phrase, title)
    except Exception:  # noqa: BLE001 — auto-titling must never break the chat
        pass


async def refresh_summary(session_id: str, resume: bool = False) -> None:
    """Rewrite the chat's sidebar activity phrase (fire-and-forget).

    A 3-7 word label under the chat title. Classifies the LATEST user
    message first, then labels accordingly:
    - NEW TASK        → the new task (the phrase jumps to it)
    - CONTINUATION    → "Continuing …" the in-progress task
    - QUESTION / CHAT → a generic topic label for what's asked/discussed
    Never quotes the messages. Any failure is swallowed: the phrase is
    decoration.
    """
    try:
        if not settings.model:
            return
        msgs = db.list_messages(session_id, limit=6)
        lines = []
        for m in msgs:
            c = re.sub(r"\s+", " ", _content_text(m["content"])).strip()
            if c:
                lines.append(f"[{m['role']}] {c[:1200]}")
        if not lines:
            return
        hint = ("\n[system note: this turn RESUMED an interrupted run — "
                "treat it as a CONTINUATION of the in-progress task.]"
                if resume else "")
        out = await asyncio.wait_for(_llm.complete([
            {"role": "system",
             "content": ("You label chat conversations. First classify the "
                         "LATEST user message:\n"
                         "- NEW TASK: a fresh request different from what "
                         "the chat was doing before.\n"
                         "- CONTINUATION: the user resumes, continues, "
                         "confirms, or pushes forward an in-progress task "
                         "(e.g. 'resume', 'continue', 'yes do it', 'try "
                         "again', answering a question the agent asked).\n"
                         "- QUESTION / CHAT: the user asks something or "
                         "discusses a topic, not assigning work.\n"
                         "Then reply with ONLY a short phrase of 3-7 words "
                         "— no quotes, no trailing punctuation, no leading "
                         "articles — in the same language as the chat. It "
                         "reads like a one-line label under the chat title:\n"
                         "- NEW TASK → the new task itself.\n"
                         "- CONTINUATION → 'Continuing ' + the in-progress "
                         "task (keep the task part ≤5 words).\n"
                         "- QUESTION / CHAT → a generic topic label for "
                         "what is being asked or discussed.\n"
                         "Never quote the messages.")},
            {"role": "user", "content": "\n\n".join(lines)[-4000:] + hint},
        ], temperature=0.2, thinking="off"), 60)  # 3-7 word label: no CoT
        phrase = (out or "").strip().splitlines()
        phrase = phrase[0].strip().strip("\"'“”‘’«»[]()") if phrase else ""
        phrase = re.sub(r"\s+", " ", phrase).rstrip(".,;:!?-– ")[:60]
        if phrase:
            db.set_session_summary(session_id, phrase)
    except Exception:  # noqa: BLE001 — summary must never break the chat
        pass


# ── mid-turn sidebar phrases ───────────────────────────────────────

_TOOL_PHRASES = {
    "read_file": "Reading",
    "list_dir": "Browsing",
    "write_file": "Writing",
    "edit_file": "Editing",
    "search_files": "Searching",
    "local_search": "Searching notes",
    "search_transcript": "Searching transcripts",
    "index_documents": "Indexing",
    "run_command": "Running",
    "web_search": "Searching web",
    "fetch_url": "Fetching",
    "browser": "Browsing",
    "ask_user": "Waiting for your pick",
}


def _task_phrase(plan: list[str], plan_active: int, plan_done: set[int]) -> str:
    """Sidebar activity phrase for the whole turn: the BIG PICTURE (the
    task), not the in-flight tool. Active plan step when there is one,
    else the first step; the end-of-turn LLM summary overwrites it."""
    if not plan:
        return ""
    if 0 <= plan_active < len(plan):
        return plan[plan_active]
    for i, step in enumerate(plan):
        if i not in plan_done:
            return step
    return plan[-1]


def _tool_phrase(name: str, args: dict) -> str:
    """Cheap deterministic phrase for a tool — no LLM, so it lands the
    instant the tool starts. The chat rows render it (Reading app.js);
    the sidebar keeps the task phrase instead."""
    base = _TOOL_PHRASES.get(name) or "Working on it"
    target = ""
    if name in ("read_file", "write_file", "edit_file", "list_dir"):
        p = str(args.get("path") or "")
        target = p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:30]
    elif name == "run_command":
        target = re.sub(r"\s+", " ", str(args.get("command") or ""))[:40]
    elif name in ("web_search", "fetch_url", "local_search"):
        target = re.sub(r"\s+", " ",
                        str(args.get("query") or args.get("url") or ""))[:40]
    return f"{base} {target}" if target else base


# ── context compaction ─────────────────────────────────────────────

def _content_text(content) -> str:
    """Flatten message content to text (handles str and [text/image] parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(part.get("text") or "")
                elif part.get("type") == "image_url":
                    out.append("[image attached]")
        return "\n".join(out)
    return str(content)


def est_tokens(messages: list[dict]) -> int:
    """Rough token estimate for a message list: ~4 chars/token for text,
    plus a flat 1000-token allowance per attached image, plus the
    per-tool-call overhead. Good enough to trigger compaction."""
    n = 0
    for m in messages:
        content = m.get("content")
        n += len(_content_text(content))
        if isinstance(content, list):
            n += 4000 * sum(1 for p in content
                            if isinstance(p, dict) and p.get("type") == "image_url")
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            n += len(fn.get("name") or "") + len(fn.get("arguments") or "")
    return n // 4


def _clear_old_tool_results(messages: list[dict], recent: int) -> int:
    """Rule-based loss-reducer (Anthropic's `clear_tool_uses` mechanism):
    replace the content of TOOL messages older than the keep-tail with a
    one-line stub. The tool CALLS (assistant messages) and all conversation
    text are untouched — the model can re-run any tool it needs, and the UI
    already has the full output (tool_end / parts). Returns chars freed."""
    if len(messages) <= 1 + recent:
        return 0
    # tool_call_id -> tool name, so the stub keeps the receipt
    names: dict[str, str] = {}
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            names[tc.get("id") or ""] = (tc.get("function") or {}).get("name", "?")
    boundary = len(messages) - recent
    freed = 0
    for i in range(1, boundary):
        m = messages[i]
        content = m.get("content")
        if m.get("role") == "tool":
            text = _content_text(content)
            if not text or text.startswith("[cleared:"):
                continue  # empty or already stubbed (idempotent)
            name = names.get(m.get("tool_call_id") or "", "?")
            stub = f"[cleared: {name} — {len(text):,} chars; re-run the tool if needed]"
            m["content"] = stub
            freed += max(0, len(text) - len(stub))
        elif (m.get("role") == "user" and isinstance(content, str)
              and content.startswith("TOOL_RESULT:")):
            # text-mode: tool results ride user-role TOOL_RESULT messages
            body = content[len("TOOL_RESULT:"):]
            if body.startswith("\n[cleared:") or body.startswith("[cleared:"):
                continue
            stub = ("TOOL_RESULT:\n[cleared — "
                    f"{len(body):,} chars; re-run the tool if needed]")
            m["content"] = stub
            freed += max(0, len(body) - len(stub))
    return freed


class ThinkingLoop(Exception):
    """Streaming repetition tripwire: the model's thinking stream degenerated
    into a token loop (the 480 KB 'Narakeets = Narakeets…' run, 2026-10-06).
    Raised from the stream loop to abort the round early instead of burning
    the full token budget on a dead loop that ends with an empty answer."""


def _thinking_loop_hit(window: str, min_chars: int = 2000,
                       min_repeats: int = 5, min_coverage: float = 0.35) -> bool:
    """True when the tail of the thinking stream is a repetition loop.

    Scans candidate periods p and measures how far back from the tail
    p-periodicity holds (words[i] == words[i-p] for the whole suffix —
    anchor-free, so a loop that STARTS mid-window still counts). Two
    tiers:
      short (p <= 12 words): >= 35% tail coverage — the 480 KB 'Or
        Narakeets = Narakeets…' run (2026-10-06, 10-word period);
      long (13 <= p <= 200 words): >= 90% tail coverage — the 497 KB
        'AI Classes' run (2026-10-08) cycled a ~70-word PARAGRAPH 780x
        (measured: period 77 words, 100% tail coverage), which the short
        tier is structurally blind to. The long tier's bar is
        deliberately near-total: a paragraph-scale exact repeat is rare
        in healthy CoT, so a tail that is 90%+ one paragraph over and
        over is already degeneration, while a legitimate revisit
        (planning → re-check) shows 2 copies, not a 90%-periodic tail.
    The `run += p` seed (the period itself) is kept below each tier's
    coverage bar by construction (p/n <= 1/reps), so a window with ZERO
    actual matches can never trip. The check window scales with the
    period — a 24-word period needs a tail that actually CONTAINS
    >=3 full repeats (a fixed 3000-char tail cuts mid-period and the
    exact-periodicity check dies). Only evaluated once the stream is
    long enough (min_chars / tier floor) — short thinking is never a
    loop."""
    # tiers: (pmin, pmax, coverage, window chars, min stream chars, repeats)
    tiers = ((1, 12, min_coverage, 3000, min_chars, min_repeats),
             (13, 200, 0.90, 3600, 3600, 3))
    for pmin, pmax, cov, win_chars, min_len, reps in tiers:
        if len(window) < min_len:
            continue
        words = re.findall(r"\w+", window[-win_chars:])
        n = len(words)
        if n < 16:
            continue
        for p in range(pmin, pmax + 1):
            if n < p * reps:
                break  # larger periods can't fit reps either
            run = 0
            for i in range(n - 1, p - 1, -1):
                if words[i] == words[i - p]:
                    run += 1
                else:
                    break
            run += p  # seed: the period itself (p/n <= 1/reps < cov)
            if run / n >= cov:
                return True
    return False


def _is_context_overflow(msg: str) -> bool:
    """Message-level check for a context-window rejection (the
    `LLMError` from llm.py carries only `str(e)`). The trigger check
    runs BEFORE the call, so a big tool result landing after it — or a
    server limit lower than the assumed 265k — still 400s; recovery
    catches exactly these. Vocabulary from OpenAI/vLLM/SGLang error
    strings (tool-verified against vLLM's `max_tokens` message).
    Two tiers: strong phrases match anywhere; weak verbs ('exceed',
    'too long') only count NEXT TO a context/token/length word. Rate-
    limit / quota strings are excluded up front — 'rate limit exceeded'
    contains both a weak verb and 'limit', and must NOT trip recovery."""
    low = (msg or "").lower()
    if any(k in low for k in ("rate limit", "rate_limit", "too many requests",
                              "quota", " 429", "insufficient")):
        return False
    if any(k in low for k in (
            "context length", "context_length", "context size",
            "context window", "maximum context", "max context",
            "too many tokens", "token limit",
            "reduce the length", "maximum number of tokens")):
        return True
    for verb in ("exceed", "too long"):
        i = low.find(verb)
        while i != -1:
            window = low[max(0, i - 25):i + len(verb) + 25]
            if any(w in window for w in
                   ("context", "token", "length", "limit")):
                return True
            i = low.find(verb, i + 1)
    return False


def _hard_truncate(messages: list[dict], budget_chars: int) -> int:
    """Deterministic overflow-recovery shrink (no LLM — Cline: recovery
    "must not depend on another successful LLM request"): drop OLDEST
    middle messages until the estimate fits `budget_chars` (~4 chars/
    token). `messages` is [system, ...history...]. The system message
    and the last 8 (≈ the compact_recent tail + its tool pairs) are
    never touched. An assistant message with tool_calls is only dropped
    together with its tool responses (orphaning either side is an API
    400), and any orphaned tool message left at the front of the
    history is removed. Returns messages dropped."""
    if est_tokens(messages) * 4 <= budget_chars:
        return 0
    keep_tail = 8
    tail = len(messages) - keep_tail
    if tail <= 1:
        return 0
    # tool_call_id -> index of the tool response, for pair-safe drops
    tool_idx: dict[str, int] = {}
    for i in range(1, len(messages)):
        tid = messages[i].get("tool_call_id")
        if tid:
            tool_idx[tid] = i
    drop: set[int] = set()
    i = 1
    while i < tail and est_tokens(messages) * 4 > budget_chars:
        m = messages[i]
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                j = tool_idx.get(tc.get("id") or "")
                if j is not None and j < tail:
                    drop.add(j)
        drop.add(i)
        i += 1
    for j in sorted(drop, reverse=True):
        del messages[j]
    # pair-safe drops can leave a tool response whose call was dropped
    # (the call sat in the keep-tail window) — an orphan at the front
    # of the history is an API 400
    while len(messages) > 1 and messages[1].get("role") == "tool":
        del messages[1]
    return len(drop)


COMPACT_PROMPT = (
    "You are the context-compaction step of an agent harness. Summarize the "
    "CONVERSATION below (an agent working for the user, with its tool "
    "results) into a single CONTEXT SUMMARY block that replaces it. "
    "Preserve, precisely:\n"
    "- the user's task and any constraints they gave\n"
    "- key decisions made and WHY\n"
    "- files created/modified/important: path + purpose (one line each)\n"
    "- current state: what is done, what is in progress, what is next\n"
    "- open questions / unresolved errors\n"
    "- workarounds and discovered fixes: script quirks, env gotchas, tool "
    "workarounds — the class of detail that dies by default\n"
    "- user-provided identifiers VERBATIM: addresses, hostnames, IPs, ports, "
    "credentials, tokens, connection strings, exact paths — never paraphrase "
    "or drop them\n"
    "Drop raw tool outputs, file dumps, and intermediate reasoning — file "
    "contents can always be re-read. Be dense and factual; bullets are "
    "fine. Output ONLY the summary, starting with the line "
    "'CONTEXT SUMMARY:'.")


async def _compact_context(messages: list[dict], log,
                           session_id: str) -> list[dict] | None:
    """Replace the older part of `messages` with one summary message.

    `messages` is [system, ...history...]. The system message and the most
    recent settings.compact_recent messages survive verbatim; everything in
    between becomes a single user message carrying the model's summary.
    Returns the new list, or None if compaction was skipped/failed (the
    caller keeps the original list)."""
    recent = settings.compact_recent
    if len(messages) <= 1 + recent:
        return None
    head, old, tail = messages[0], messages[1:-recent], messages[-recent:]
    if not old:
        return None
    text = ""
    for m in old:
        role = m.get("role", "?")
        content = _content_text(m.get("content")).strip()
        if role == "assistant" and not content and m.get("tool_calls"):
            names = ", ".join((tc.get("function") or {}).get("name", "?")
                              for tc in m["tool_calls"])
            text += f"[assistant calls tools: {names}]\n"
            continue
        text += f"--- {role} ---\n{content}\n"
    text = text[:200_000]
    try:
        summary = await asyncio.wait_for(
            _llm.complete([
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": text},
                # "low", not "off": a real (lossy) summarization job, but no
                # tools and no follow-up — xhigh here is pure waste.
            ], temperature=0.0, thinking="low"),
            timeout=180)
    except Exception as e:  # noqa: BLE001 — compaction must never kill the turn
        log("warn", f"compaction failed ({type(e).__name__}: {e}) — continuing unsummarized")
        return None
    summary = (summary or "").strip()
    if not summary:
        log("warn", "compaction produced an empty summary — continuing unsummarized")
        return None
    if not summary.startswith("CONTEXT SUMMARY:"):
        summary = "CONTEXT SUMMARY:\n" + summary
    return [head, {"role": "user",
                   "content": ("(Earlier conversation compacted — this "
                               "replaces it; re-read the task notes file "
                               "if one exists.)\n\n" + summary)}, *tail]


# ── vision ─────────────────────────────────────────────────────────
# The model endpoint is vision-capable (proven by tools/probe_vision.py):
# uploaded images are inlined into the request as OpenAI image_url parts,
# so the model reads the actual pixels instead of an OCR round-trip.
IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
           ".ico": "image/x-icon"}
# .heic deliberately absent: neither PIL nor the model can decode it —
# those attachments fall back to the file path + OCR hint.


def _image_data_url(p: Path) -> str | None:
    """Encode an upload as a data URL the vision model can read.

    Decodes with PIL to normalize exotic formats (bmp/ico/gif palettes)
    and re-encodes — PNG when there is alpha, JPEG otherwise — downscaled
    so the long edge is <= 2048px, which keeps the request small and the
    image under the model's pixel budget. Falls back to the raw bytes
    (still decodable by the model) when PIL is unavailable or chokes."""
    raw = p.read_bytes()
    out: bytes | None = None
    mime: str | None = None
    try:
        from PIL import Image  # imported here so PIL stays optional at startup
        im = Image.open(io.BytesIO(raw))
        im.load()
        has_alpha = (im.mode in ("RGBA", "LA", "PA")
                     or (im.mode == "P" and "transparency" in im.info))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if has_alpha else "RGB")
        w, h = im.size
        scale = 2048 / max(w, h)
        if scale < 1:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        if has_alpha:
            im.save(buf, format="PNG")
            out, mime = buf.getvalue(), "image/png"
        else:
            im.convert("RGB").save(buf, format="JPEG", quality=85)
            out, mime = buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — no PIL or undecodable file
        native = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "gif": "image/gif", "webp": "image/webp"}
        mime = native.get(p.suffix.lower().lstrip("."))
        out = raw if mime else None
    if out is None or not mime:
        return None
    return f"data:{mime};base64,{base64.b64encode(out).decode()}"


def _pdf_text(raw: bytes) -> str | None:
    """PDF → plain text via pypdf (in the venv); None if unreadable."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=False)
        text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
        return text.strip() or None
    except Exception:  # noqa: BLE001 — corrupt/encrypted → treat as binary
        return None


def _maybe_auto_resume(session_id: str, error_msg: str, log,
                       force: bool = False) -> None:
    """Re-fire the boot auto-resume nudge for a TRANSIENT in-process LLM
    error (the mid-stream dead zone). No-op when: the setting is off,
    the error isn't transient (a 4xx model rejection is a real dead end —
    re-firing would just burn tokens to hit the same wall), the run_state
    is gone or deliberately stopped, the per-session budget is spent, or
    the manager is unavailable. The enqueue happens OUTSIDE the error
    handler's turn (a fresh TaskRun), so it never re-enters this frame.
    `force=True` bypasses the transient check for non-error stops that are
    still genuinely resumable — the empty-answer case (the model ended the
    round with no content; a retry is the right move, not a 4xx wall)."""
    if not force and not is_transient_message(error_msg):
        return
    try:
        if not db.load_auto_approve().get("auto_resume"):
            return
        if not db.has_unstopped_run_state(session_id):
            return
        budget = _auto_resume_fires.get(session_id, 0)
        if budget >= _AUTO_RESUME_MAX:
            log("warn", f"session {session_id[:8]}: auto-resume budget "
                        f"spent ({_AUTO_RESUME_MAX}) — ↻ is manual now")
            return
        text = _auto_resume_text
        if not text:
            return
        from . import tasks as _tasks  # lazy: tasks imports agent
        tm = _tasks.get_manager()
        if tm is None:
            return
        sess = db.get_session(session_id) or {}
        tm.enqueue(session_id, text, [],
                   sess.get("mode") or "act",
                   sess.get("roast") or "chill",
                   resume=True)
        _auto_resume_fires[session_id] = budget + 1
        log("info", f"session {session_id[:8]}: transient LLM error "
                    f"({error_msg[:80]}) — auto-resume {budget + 1}/"
                    f"{_AUTO_RESUME_MAX} re-entering saved run_state")
    except Exception as e:  # noqa: BLE001 — a resume failure must never
        log("error", f"auto-resume enqueue failed: {type(e).__name__}: {e}")
        pass


async def run_chat(session_id: str, user_text: str, files: list[dict],
                   log, mode: str = "act", roast: str = "chill",
                   resume: bool = False) -> AsyncGenerator[str, None]:
    """Run one user turn end-to-end; yields SSE frames.
    `mode` is the per-chat Plan/Act mode: 'plan' gates out mutating tools.
    `resume=True` re-enters the loop from the saved run_state (crash/restart)
    instead of building fresh context from history."""
    sess = db.get_session(session_id)
    if not sess:
        yield sse("error", {"message": "unknown session"})
        return
    cwd = resolve_cwd(sess)
    ctx = ToolCtx(cwd=cwd,
                  sources=session_sources.setdefault(session_id, []),
                  generated=[],
                  session_id=session_id)
    aa = db.load_auto_approve()  # Cline-style auto-approve panel settings
    plan_mode = (mode or "act") == "plan"
    roast = roast if roast in ("off", "chill", "full") else "chill"
    log("info", f"session {session_id[:8]} start (cwd={cwd}, mode={'plan' if plan_mode else 'act'}"
                + (", RESUME" if resume else "") + ")")

    # attachments → context. Images are inlined as real image_url content
    # parts so the model sees the pixels directly; the path stays in the
    # text so tools can still OCR/crop it for details. Text files are
    # inlined verbatim; anything that is not text must NEVER be dumped raw
    # — a duplicated read block used to decode PNG/PDF bytes as utf-8 text
    # and that wall of symbols landed in the persisted user row, rendering
    # in history as a huge message of unintelligible characters.
    attach: list[str] = []
    attach_meta: list[dict] = []
    image_parts: list[dict] = []
    for f in (files or [])[:4]:
        name = f.get("name") or ""
        p = settings.uploads_dir / name
        if not name or not p.is_file() or settings.uploads_dir not in p.resolve().parents:
            continue
        suffix = p.suffix.lower()
        url = _image_data_url(p) if suffix in IMG_EXT else None
        if url:
            image_parts.append({"type": "image_url", "image_url": {"url": url}})
            attach.append(
                f"Attached image: {f.get('original') or name}\n"
                f"File path: {p}\n"
                "The image is included inline below — you can see its pixels directly. "
                "If a detail is unclear, you can still OCR/crop it with run_command (PIL is installed).")
            ctx.sources.append(f"uploaded: {name}")
        elif suffix == ".heic":
            attach.append(
                f"Attached image: {f.get('original') or name}\n"
                f"File path: {p}\n"
                "I can't inline HEIC pixels directly — OCR/crop it with run_command "
                "(convert to PNG first) if you need its content.")
            ctx.sources.append(f"uploaded: {name}")
        else:
            raw = p.read_bytes()
            if suffix == ".pdf":
                text = _pdf_text(raw)
            else:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            if text is None:
                attach.append(
                    f"Attached file: {f.get('original') or name} (binary, "
                    f"{len(raw):,} bytes)\n"
                    f"File path: {p}\n"
                    "It's not plain text — don't guess its contents. Use run_command "
                    "to extract or convert it if its content matters "
                    "(Python + PIL are installed).")
                ctx.sources.append(f"uploaded: {name} (binary)")
            else:
                if len(text) > 40_000:
                    text = text[:40_000] + "\n[attachment truncated]"
                attach.append(f"Attached file: {f.get('original') or name}\n```\n{text}\n```")
                ctx.sources.append(f"uploaded: {name}\n{text[:8000]}")
        attach_meta.append({"name": name, "url": f"/uploads/{name}",
                            "original": f.get("original") or name,
                            "kind": "image" if (url or suffix == ".heic") else None})
    # image-only sends arrive with ZERO words: the model still needs a
    # non-empty user turn. The fallback lives in the MODEL copy only —
    # the stored row keeps the user's own words (empty here; the
    # thumbnail row renders fine without a bubble)
    if attach:
        model_text = (user_text or "(attachment — see below)") + "\n\n" + "\n\n".join(attach)
    else:
        model_text = user_text or "(attachment)"

    db.add_message(session_id, "user", user_text, files=attach_meta or None)
    db.set_processing(session_id, True)
    # Open the in-flight assistant row NOW and keep it current as the turn
    # runs, so a restart/crash mid-task can't eat the command/edit/search
    # timeline — finalize_stale_drafts() promotes it at boot. The row doubles
    # as the REATTACH snapshot: every refresh stamps flushed_seq, the seq of
    # the last frame already broadcast, so a client that opens the chat
    # mid-turn renders the snapshot and streams from exactly there.
    draft_id = db.new_draft_message(session_id, mode or "act")
    draft_closed = False
    last_draft_flush = 0.0

    def _close_draft(content: str) -> None:
        nonlocal draft_closed
        if draft_closed:
            return
        draft_closed = True
        db.finish_draft_message(draft_id, session_id, content,
                                ctx.generated or None, thinking_text or None,
                                parts, mode or "act")

    def _flush_draft(content: str, stamp_prev: bool = False,
                     avatar: str = "last", label: str = "Working…",
                     thinking_active: bool = False) -> None:
        nonlocal last_draft_flush
        last_draft_flush = time.perf_counter()
        seq = 0
        try:
            from . import tasks as _tasks  # lazy: tasks imports agent
            tm = _tasks.get_manager()
            r = tm.get(session_id) if tm is not None else None
            if r is not None:
                seq = int(r.seq or 0)
        except Exception:  # noqa: BLE001 — snapshot must never kill the turn
            pass
        if stamp_prev:
            # one frame BEFORE the just-broadcast event: a reattach must
            # still REPLAY that event — the running tool's row is drawn from
            # its tool_start frame, not from the snapshot
            seq = max(0, seq - 1)
        # the live dressing the UI was showing when the snapshot was taken:
        # where the avatar sat (streaming bubble / running tool / last
        # element), the status chip label, and whether the 💭 hint was live
        ui = {"avatar": avatar, "label": label,
              "thinking": bool(thinking_active)}
        db.update_draft_message(draft_id, content, ctx.generated or None,
                                thinking_text or None, parts, flushed_seq=seq,
                                ui=ui)

    stopper = get_stopper(session_id)
    stopper.clear()
    if not plan_mode:
        await asyncio.to_thread(_git_last_safe, cwd, log)

    saved_state: dict | None = None
    if resume:
        saved_state = db.load_run_state(session_id)
        if not saved_state or not saved_state.get("messages"):
            # nothing saved (e.g. the crash landed before the first turn
            # checkpoint) — fall back to a plain fresh turn
            resume = False
            log("info", "resume requested but no run_state — falling back to fresh turn")

    if resume:
        # Re-enter the loop from the saved conversation: it already contains
        # the user message and every tool result so far, so nothing is
        # re-read or re-run — the model just continues.
        messages: list[dict] = [
            {"role": "system",
             "content": build_system(cwd, _llm.mode == "text", plan_mode,
                                     roast, recall=saved_state.get("goal") or "",
                                     session_id=session_id)}]
        messages.extend(saved_state["messages"])
        messages.append({"role": "user", "content": user_text})
        # unflag a parked (deliberately-stopped) run BEFORE the row is
        # consumed: the row is deleted here and re-created at the next
        # checkpoint (which writes stopped=0 anyway), but a crash between
        # now and that checkpoint must leave an UNSTOPPED row — otherwise
        # a manual ↻ that died mid-turn would be skipped at the next boot
        # (auto-resume treats stopped=1 as "the boss paused this").
        db.set_run_state_stopped(session_id, False)
        db.clear_run_state(session_id)  # consumed — a second resume is a fresh turn
        # the checkpoint saved `turn` = the model call that was in flight; the
        # loop's `turn += 1` must land back on that exact call (and keep the
        # `turn > 1` compaction guard honest on a 1-call resume)
        turn = max(0, int(saved_state.get("turn") or 0) - 1)
        # checkpoints + the trajectory must carry the ORIGINAL goal, not the
        # resume nudge
        user_text = saved_state.get("goal") or user_text
    else:
        # Persisted compaction (2026-10-09, boss-approved — replaces the
        # 30k CONTEXT_WINDOW experiment): the rebuild loads the session's
        # FULL history — no fixed 16-row tail, no token-budget window.
        # Everything older than the persisted marker is carried as the
        # stored summary (one lossy pass per growth, not per turn);
        # compaction is the sole memory manager. The ~220k in-turn valve
        # below still guards long tool loops.
        messages: list[dict] = [
            {"role": "system",
             "content": build_system(cwd, _llm.mode == "text", plan_mode,
                                     roast, recall=user_text,
                                     session_id=session_id)}]
        comp = sess.get("compacted_summary")
        comp_id = int(sess.get("compacted_through_msg_id") or 0)
        # fetch generously (each row is clamped to 24k chars ≈ 6k tokens;
        # the boundary check below compacts before the request ever
        # approaches the model's limit, and the clamp bounds memory)
        history = db.list_messages(session_id, limit=2000)
        if comp and comp_id:
            # split at the marker: rows <= comp_id are already inside the
            # summary; rows after it are the live tail
            pre, tail = [], []
            for m in history[:-1]:  # the last one is the user message just added
                (pre if m["id"] <= comp_id else tail).append(m)
            if pre:  # marker present but rows before it vanished (prune)
                comp = ""  # a summary with nothing left to summarize
        else:
            tail = history[:-1]
        if comp:
            messages.append({"role": "user",
                             "content": ("(Earlier conversation compacted — "
                                         "this replaces it; re-read the task "
                                         "notes file if one exists.)\n\n"
                                         + comp)})
        for m in tail:
            c = m["content"]
            if isinstance(c, str):
                c = c[:24000]
                # attachment preambles are no longer stored in user text;
                # re-tell the model where earlier attachments live (paths
                # from the row's files metadata) so it loses no context
                if m["role"] == "user" and m.get("files"):
                    paths = ", ".join(
                        str(settings.uploads_dir / (f.get("name") or "?"))
                        for f in m["files"][:4])
                    c += f"\n\n(attachments from that message: {paths})"
            else:
                c = ""  # image-only history rows can't be rebuilt
            messages.append({"role": m["role"], "content": c})
        # TURN-BOUNDARY COMPACTION: if the loaded context (system +
        # summary + full tail) is already past the trigger, run the same
        # two-step (clear tool results → LLM summary) NOW and persist the
        # result — so the lossy pass runs once per growth, not per turn,
        # and the first model call of this turn is small. (The boundary
        # is the meter's 100% mark — the model's real limit — not a
        # compaction point.)
        # ctx_tokens floor (2026-10-12, TA-session bug): est_tokens() is a
        # char/4 estimate that undercounts the real provider count (tool
        # schemas + API overhead ≈ 10k) — a session running at 107k REAL
        # tokens can estimate under the trigger and never compact. The
        # previous turn's llm_end already persisted the real count to
        # sessions.ctx_tokens; use it as a floor so the LLM summary fires
        # when the provider says so, even if the estimate lags.
        real_ctx = int(sess.get("ctx_tokens") or 0)
        ctx_est = max(est_tokens(messages), real_ctx)
        if (settings.compact_enabled
                and ctx_est >= settings.compact_trigger):
            freed = _clear_old_tool_results(messages, settings.compact_recent)
            if freed:
                log("info", f"cleared old tool results at rebuild "
                            f"(~{freed // 4} est tokens freed)")
            if max(est_tokens(messages), real_ctx) >= settings.compact_trigger:
                log("info", f"compacting at turn boundary "
                            f"(~{max(est_tokens(messages), real_ctx)} est tokens)")
                compacted = await _compact_context(messages, log, session_id)
                if compacted:
                    messages = compacted
                    # persist: the summary covers everything except the
                    # keep-tail (settings.compact_recent) — the marker is
                    # the last DB row that fell into the summarized part
                    tail_rows = [m for m in history[:-1]
                                 if not (comp and comp_id and m["id"] <= comp_id)]
                    cut = max(0, len(tail_rows) - settings.compact_recent)
                    if cut > 0:
                        new_summary = _content_text(messages[1].get("content"))
                        db.set_compaction(session_id, new_summary,
                                          tail_rows[cut - 1]["id"])
                        log("info", f"compaction persisted through "
                                    f"message {tail_rows[cut - 1]['id']}")
                    else:
                        log("warn", "turn-boundary compaction ran but the "
                                    "keep-tail covered all rows — nothing persisted")
        if image_parts:
            messages.append({"role": "user",
                             "content": [{"type": "text", "text": model_text},
                                         *image_parts]})
        else:
            messages.append({"role": "user", "content": model_text})
        turn = 0

    schemas = openai_schemas(exclude=MUTATING_TOOLS if plan_mode else None)

    def _sync_mode() -> str | None:
        """Mid-flight mode sync: the Plan/Act toggle is LIVE, not a
        send-time snapshot. Re-read the session's mode and follow it —
        an Act→Plan flip demotes the run (plan constraints in the prompt,
        mutating tools dropped from the schema, and the defensive gate
        blocks any mutating call still in flight); a Plan→Act flip
        re-arms the tools for the remaining rounds. Returns a status
        line when the mode actually changed, else None."""
        nonlocal plan_mode, schemas
        try:
            sess_now = db.get_session(session_id)
            if not sess_now:
                return None
            now_plan = (sess_now.get("mode") or "act") == "plan"
            if now_plan != plan_mode:
                plan_mode = now_plan
                messages[0] = {"role": "system",
                               "content": build_system(
                                   cwd, _llm.mode == "text",
                                   plan_mode, roast,
                                   session_id=session_id)}
                schemas = openai_schemas(
                    exclude=MUTATING_TOOLS if plan_mode else None)
                log("info", f"mid-flight mode: "
                            f"{'demoted to Plan' if plan_mode else 'back to Act'}")
                return ("⏸ Plan mode — demoted mid-run"
                        if plan_mode else "▶ Act mode — re-armed mid-run")
        except Exception:  # noqa: BLE001 — the mode check must never kill the turn
            pass
        return None

    thinking_text = ""                    # model chain-of-thought, this turn
    parts: list[dict] = []                # natural-order timeline of the turn:
    # {"t":"text","text":…} segments and {"t":"tool",…} runs, in the order
    # they happened — persisted on the message so history renders like live
    plan: list[str] = []                  # progress-bar steps (from "STEP:" lines)
    plan_done: set[int] = set()
    plan_active = -1
    draft = ""                            # pre-init: the hard-stop handler reads it
    if resume and saved_state:
        # restore the progress-bar state so a resumed task shows the same
        # plan with its completed steps, not a blank bar
        plan = list(saved_state.get("plan") or [])
        plan_done = set(saved_state.get("done") or set())
        if plan:
            yield sse("plan", {"items": plan, "total": len(plan)})
            yield sse("status", {"text": f"Resumed at turn {turn + 1}…"})
    # Real-token trigger (Cline's approach): the provider's own
    # prompt_tokens from the PREVIOUS round is the true context size
    # (system + conversation + tool results). The char/4 estimate runs
    # hot on code/JSON-heavy output and made compaction fire early; the
    # real number is only unknown for round 1 (no response yet) and
    # right after a compaction (reset to None → estimate fallback).
    last_prompt_tokens: int | None = None
    # Overflow-recovery budget (Cline's `overflowRecovery`): the trigger
    # check runs BEFORE the call, so a big tool result landing after it —
    # or a server limit lower than the assumed 265k — still 400s. On a
    # context-overflow error we shrink by rule (no LLM — the summarizer
    # would hit the same wall) and retry the SAME round once.
    overflow_retried = False
    try:
        while settings.max_turns <= 0 or turn < settings.max_turns:
            turn += 1
            if stopper.is_set():
                # the stop landed between rounds — keep what the user saw
                # (the tool timeline from previous rounds lives in `parts`)
                _close_draft(draft.strip())
                yield sse("stopped", {"reason": "stopped by user"})
                return
            # Mid-flight mode sync (see _sync_mode) — at the model-call
            # boundary, so the NEXT call already runs under the new mode.
            _mode_msg = _sync_mode()
            if _mode_msg:
                yield sse("status", {"text": _mode_msg})
            # Date drift: a run that crosses midnight (or a resume of a
            # run started the previous day) must not keep answering with
            # yesterday's date — 'tomorrow' would land on the wrong day.
            if _refresh_date(messages):
                log("info", "system prompt date refreshed mid-run")
            # Mid-turn context management (boss design 2026-10-11): inside a
            # turn, NEVER run the lossy summary — the turn runs on max recall
            # so its work is undisturbed and converges faster. The only
            # mid-turn action is the lossless clear: stub old tool results
            # (the real token sink) once the context crosses the trigger.
            # The LLM summary waits for the next turn boundary (rebuild).
            # Trigger input: real provider tokens when we have them,
            # char/4 estimate otherwise (round 1 / post-compact).
            ctx_now = (last_prompt_tokens
                       if last_prompt_tokens is not None
                       else est_tokens(messages))
            # Per-round lossless clear (re-enabled 2026-10-13, boss): once
            # context crosses the trigger, old tool results are stubbed every
            # round (lossless — conversation text is untouched). The lossy
            # LLM summary still waits for the turn-boundary compaction.
            # Overflow recovery's aggressive clear is the backstop and is
            # untouched by this flag.
            if (settings.compact_enabled and settings.compact_mid_turn_clear
                    and turn > 1
                    and ctx_now >= settings.compact_trigger):
                freed = _clear_old_tool_results(messages, settings.compact_recent)
                if freed:
                    log("info", f"cleared old tool results mid-turn (~{freed // 4} est tokens freed)")
                    # the pre-clear real number is now stale (the context
                    # shrank) — fall back to the estimate until the next
                    # round's usage chunk gives a fresh read
                    last_prompt_tokens = None
                    yield sse("status",
                              {"text": f"Context trimmed (~{est_tokens(messages)} est tokens)"})
                    try:
                        db.set_ctx_tokens(session_id, est_tokens(messages))
                    except Exception:  # noqa: BLE001 — meter must never kill the turn
                        pass
                # freed == 0: nothing stub-able left (conversation text is
                # what's big) — the lossy summary waits for the next turn
                # boundary; the overflow recovery below is the backstop.
            # run-state checkpoint: a restart/crash mid-task can re-enter the
            # loop exactly here (saved AFTER compaction, so the persisted
            # conversation is the small one). One small write per turn.
            try:
                db.save_run_state(session_id, turn, plan, plan_done,
                                  messages[1:], user_text)
            except Exception:  # noqa: BLE001 — durability must never kill the turn
                pass
            draft = ""
            last_ev_was_thinking = False  # was the previous frame a 💭 delta
            tool_calls: dict[int, dict] = {}
            buf = ""
            lines_held: list[str] = []
            pending_tool = False  # a held line might be a TOOL_CALL — hold it
            # perf_counter for durations: on this machine time.time() is too
            # coarse — two reads in one loop iteration can return the same
            # value, which zeroed the decode span (tok_s came out null).
            llm_t0 = time.perf_counter()  # LLM latency: stream start → last frame
            llm_first = None  # TTFT: first model frame (perf_counter)
            llm_last = None  # decode span: first → last CONTENT frame
            think_t0 = len(thinking_text)  # per-round CoT metering (chars)
            think_loop_tripped = False  # repetition tripwire fired this round
            think_loop_checked = 0      # thinking chars seen at last tripwire check
            finish_reason: str | None = None  # this round's end event
            usage_tokens = None  # completion_tokens from the final usage chunk
            prompt_tokens = None  # prompt_tokens — the real context size
            # Per-round thinking (2026-09-28 experiment): first round of a
            # task = THINKING_FIRST (planning keeps the full dial), tool-
            # loop rounds = THINKING_LOOP; unset knobs fall back to THINKING
            # (behavior-neutral). Recorded on llm_end for the gate analysis.
            round_thinking = ((settings.thinking_first if turn == 1
                               else settings.thinking_loop)
                              or settings.thinking)
            try:
                async for ev in _llm.stream(messages, schemas,
                                            thinking=round_thinking):
                    if stopper.is_set():
                        break
                    if llm_first is None:
                        llm_first = time.perf_counter()  # TTFT: first frame
                    # reattach snapshot: while the model is typing, refresh
                    # the in-flight row every ~2s so a chat switch / reload
                    # mid-sentence still shows the message being written
                    # (without this, long text rounds only flushed at the
                    # next tool boundary)
                    if draft and time.perf_counter() - last_draft_flush >= 2.0:
                        _flush_draft(draft, avatar="stream",
                                     label="Answering…",
                                     thinking_active=last_ev_was_thinking)
                    last_ev_was_thinking = ev["type"] == "thinking"
                    if ev["type"] in ("thinking", "token", "tool_name",
                                      "tool_args"):
                        llm_last = time.perf_counter()  # decode span: last frame
                    if ev["type"] == "thinking":
                        thinking_text += ev["text"]
                        yield sse("thinking", {"text": ev["text"]})
                        # repetition tripwire (the 480 KB 'Narakeets =
                        # Narakeets…' run, 2026-10-06): check every ~4 KB of
                        # thinking, cheap enough to run per-chunk. A real
                        # degeneration re-emits the same n-gram dozens of
                        # times in a short span — abort the round NOW
                        # instead of burning the full token budget on a
                        # dead loop that ends with an empty answer.
                        if (len(thinking_text) - think_loop_checked >= 4000
                                and _thinking_loop_hit(thinking_text)):
                            think_loop_checked = len(thinking_text)
                            think_loop_tripped = True
                            raise ThinkingLoop(
                                f"thinking degenerated into a repetition "
                                f"loop at {len(thinking_text)} chars")
                        think_loop_checked = len(thinking_text)
                        continue
                    if ev["type"] == "token":
                        if _llm.mode == "text":
                            buf += ev["text"]
                            while "\n" in buf:
                                line, buf = buf.split("\n", 1)
                                ls = line.lstrip()
                                if ls.startswith("TOOL_CALL:"):
                                    tc = _parse_text_tool(line + "\n")
                                    if tc:
                                        tool_calls[900 + len(tool_calls)] = tc
                                        break
                                    lines_held.append(line)  # wait for full JSON
                                elif ls.startswith("FINAL_ANSWER:"):
                                    rest = ls.split("FINAL_ANSWER:", 1)[1].strip()
                                    for held in lines_held:
                                        draft += held + "\n"
                                        yield sse("token", {"text": held + "\n"})
                                    lines_held = []
                                    if rest:
                                        draft += rest + "\n"
                                        yield sse("token", {"text": rest + "\n"})
                                else:
                                    for held in lines_held:
                                        draft += held + "\n"
                                        yield sse("token", {"text": held + "\n"})
                                    lines_held = [line]
                            if stopper.is_set():
                                break
                        else:
                            draft += ev["text"]
                            yield sse("token", {"text": ev["text"]})
                    elif ev["type"] == "tool_name":
                        tc = tool_calls.setdefault(ev["index"],
                                                   {"id": "", "name": "", "arguments": ""})
                        tc["name"] = ev["name"]
                        tc["id"] = ev.get("id") or tc["id"]
                    elif ev["type"] == "tool_args":
                        tool_calls.setdefault(ev["index"],
                                              {"id": "", "name": "", "arguments": ""})
                        tool_calls[ev["index"]]["arguments"] += ev["arguments"]
                    elif ev["type"] == "usage":
                        usage_tokens = ev.get("completion_tokens")
                        prompt_tokens = ev.get("prompt_tokens")
                    elif ev["type"] == "end":
                        # don't break: the usage chunk rides AFTER end
                        # (choices-less, post finish_reason) — keep draining
                        # until the generator exhausts (it ends right after)
                        continue
            except ThinkingLoop as e:
                # the stream is dead: the generator is abandoned mid-iteration
                # (its cleanup is a no-op — it just stops yielding). The
                # post-stream code below sees an empty draft + no tool calls;
                # the tripwire flag diverts to the clarification answer.
                log("warn", f"session {session_id[:8]}: {e} — round aborted "
                            f"early (turn {turn})")
                yield sse("status", {"text": "Thinking loop detected — "
                                             "aborting this round"})
            except ToolUnsupported as e:
                log("warn", f"server rejected tools ({e}) — switching to text protocol")
                _llm.mode = "text"
                messages[0] = {"role": "system",
                               "content": build_system(cwd, True, plan_mode, roast,
                                                       session_id=session_id)}
                yield sse("status", {"text": "Falling back to text tool protocol…"})
                continue
            except LLMError as e:
                # Overflow recovery (Cline's `overflowRecovery`): the
                # trigger check ran BEFORE this call, so a big tool result
                # that landed after it — or a server limit lower than the
                # assumed 265k — still 400s. Shrink by rule (deterministic,
                # NO LLM — the summarizer would hit the same wall) and
                # retry the round ONCE. Only when nothing was streamed yet
                # (a mid-stream overflow with partial output is unretryable
                # here — the consumer already saw tokens); otherwise
                # re-raise to the outer handler.
                if (_is_context_overflow(str(e))
                        and not overflow_retried
                        and not buf and not draft and not tool_calls
                        and len(thinking_text) == think_t0):
                    overflow_retried = True
                    last_prompt_tokens = None  # pre-overflow read is stale
                    yield sse("status", {"text": "Context overflow — recovering…",
                                         "compacting": True})
                    # 1) aggressive clear: stub ALL old tool outputs
                    #    (keep-tail 1 = nothing is "recent" enough to keep)
                    freed = _clear_old_tool_results(messages, 1)
                    # 2) still over? hard-truncate the oldest middle
                    #    messages down to a safe budget (11k tokens under
                    #    the trigger — the retry must fit with headroom,
                    #    whatever the server's real limit is)
                    if est_tokens(messages) * 4 > (settings.compact_trigger - 11000) * 4:
                        dropped = _hard_truncate(
                            messages, (settings.compact_trigger - 11000) * 4)
                        log("warn", f"overflow recovery: cleared ~{freed // 4} "
                                    f"est tokens, dropped {dropped} old messages")
                    else:
                        log("warn", f"overflow recovery: cleared ~{freed // 4} "
                                    f"est tokens (no truncation needed)")
                    yield sse("status",
                              {"text": f"Context recovered (~{est_tokens(messages)} est tokens)"})
                    try:
                        db.set_ctx_tokens(session_id, est_tokens(messages))
                    except Exception:  # noqa: BLE001 — meter must never kill the turn
                        pass
                    continue  # retry the round with the shrunken context
                raise
            # LLM latency for this round: stream start → last frame (covers
            # thinking + tokens + tool args). Persisted like tool ms so
            # "when was the model slowest" is answerable from the DB.
            llm_ms = int((time.perf_counter() - llm_t0) * 1000)
            # TTFT: stream start → first model frame. Clean server-speed
            # signal (prefill + network), unlike `ms` which also covers
            # the whole decode + tool-args phase. None = the stream never
            # produced a frame (stopped before first token / error).
            ttft_ms = (int((llm_first - llm_t0) * 1000)
                       if llm_first is not None else None)
            # Decode speed: completion tokens over the content-only span
            # (first → last content frame; the trailing usage chunk is
            # excluded so a slow usage delivery can't deflate the rate).
            # Span of 0 (sub-µs mock streams) falls back to the full round
            # ms so the rate is finite (a lower bound), never a blow-up.
            # None = no usage chunk (server didn't send it) or no content.
            tok_s = None
            if usage_tokens and llm_first is not None and llm_last is not None:
                span = llm_last - llm_first
                if span <= 0:
                    span = (time.perf_counter() - llm_t0)
                if span > 0:
                    tok_s = round(usage_tokens / span, 1)
            think_chars = len(thinking_text) - think_t0  # this round's CoT
            # Context meter: the model's own prompt_tokens is the real size
            # of what we sent (system + conversation + tool results).
            # Persist it for the topbar pill + ride it on llm_end for the
            # live update.
            if prompt_tokens:
                ctx_n = prompt_tokens
                # feed next round's trigger (the real-token path)
                last_prompt_tokens = prompt_tokens
            elif messages:
                # est_tokens only covers the conversation — add the system
                # prompt (it's most of a fresh chat's context)
                ctx_n = est_tokens(messages) + (
                    len(_content_text(messages[0].get("content"))) // 4)
            else:
                ctx_n = 0
            try:
                db.set_ctx_tokens(session_id, ctx_n)
            except Exception:  # noqa: BLE001 — meter must never kill the turn
                pass
            yield sse("llm_end", {"ms": llm_ms, "ttft_ms": ttft_ms,
                                  "tok_s": tok_s, "thinking_chars": think_chars,
                                  "thinking": round_thinking or None,
                                  "completion_tokens": usage_tokens,
                                  "prompt_tokens": prompt_tokens})
            log("tool", f"llm {llm_ms}ms ttft {ttft_ms}ms {usage_tokens}tok "
                        f"({think_chars} think chars, think="
                        f"{round_thinking or 'default'}) {tok_s}tok/s (turn {turn})")
            if _llm.mode == "text":
                tail = buf.rstrip("\n")
                buf = ""
                if tail:
                    if tail.lstrip().startswith("TOOL_CALL:"):
                        tc = _parse_text_tool(tail + "\n")
                        if tc:
                            tool_calls[900 + len(tool_calls)] = tc
                    else:
                        for held in lines_held:
                            draft += held + "\n"
                            yield sse("token", {"text": held + "\n"})
                        draft += tail
                        yield sse("token", {"text": tail})
                else:
                    for held in lines_held:
                        draft += held + "\n"
                        yield sse("token", {"text": held + "\n"})
                lines_held = []

            # progress bar: pick up the model's "STEP:" plan (first time seen)
            if not plan:
                steps = plan_steps(draft)
                if steps:
                    plan = steps
                    yield sse("plan", {"items": plan, "total": len(plan)})

            if stopper.is_set():
                _close_draft(draft.strip())
                yield sse("stopped", {"reason": "stopped by user"})
                return
            # Mid-flight mode sync BEFORE dispatch: a flip that landed while
            # the model was streaming is applied now, so a mutating call
            # already queued in this round is caught by the defensive gate
            # (demoted) or allowed (re-armed) under the mode the boss
            # actually picked — not the one from send time.
            _mode_msg = _sync_mode()
            if _mode_msg:
                yield sse("status", {"text": _mode_msg})
            if tool_calls:
                if draft.strip():
                    # per-part ts: the UI chips each bubble with ITS OWN
                    # timestamp (when this text was produced), not the
                    # turn's start time — a long run's later replies show
                    # their real time, not the run's start
                    #
                    # Duplication guard (Boss: "messages are still
                    # doubling", 2026-09-29): the model can emit the SAME
                    # finding twice around a tool round — once as the
                    # tool-call round's preamble, once as the next round's
                    # opening (verified live in the finance run: parts
                    # 26/36 of msg 4958, ~16 min apart, both "Found it.
                    # The cardPayWindow is keyed off prepayDaysAgo…").
                    # The no-repeated-conclusions prompt rule (fedcbf1)
                    # reduces it; this catches the stragglers at the one
                    # choke point where parts are persisted.
                    _txt = draft.strip()
                    _dup = False
                    # compare against the LAST persisted text part (skipping
                    # tool parts in between) — the model's duplicate lands
                    # right after the tool round that followed the original
                    for _p in reversed(parts):
                        if _p.get("t") != "text":
                            continue
                        _prev = (_p.get("text") or "").strip()
                        if _prev and (
                                _prev == _txt
                                or (_prev[:60] == _txt[:60]
                                    and abs(len(_prev) - len(_txt)) <= 40)):
                            _dup = True
                        break
                    if _dup:
                        log("info", f"session {session_id[:8]}: dropped "
                                    f"duplicate text part ({len(_txt)} chars, "
                                    f"matches prior text part)")
                    else:
                        parts.append({"t": "text", "text": _txt,
                                      "ts": time.time()})
                for idx in sorted(tool_calls):
                    tc = tool_calls[idx]
                    name = tc["name"]
                    try:
                        args = (json.loads(tc["arguments"])
                                if isinstance(tc["arguments"], str) else tc["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    t0 = time.time()
                    # match the tool to its plan step FIRST so the sidebar
                    # phrase below already points at the step this tool is
                    # working on
                    if plan:
                        pending = {i for i in range(len(plan)) if i not in plan_done}
                        if pending:
                            plan_active = _plan_match(plan, pending, name, args)
                            yield sse("plan_update", {"index": plan_active,
                                                      "status": "active",
                                                      "done": len(plan_done),
                                                      "total": len(plan)})
                    # mid-turn sidebar phrase — the TASK (active plan step),
                    # not the in-flight tool: the sidebar shows the big
                    # picture, tool phrases live on the chat rows; the
                    # end-of-turn refresh_summary overwrites it when done
                    task = _task_phrase(plan, plan_active, plan_done)
                    if task:
                        db.set_session_summary(session_id, task)
                    yield sse("tool_start", {"tool": name,
                                             "args": json.dumps(args, ensure_ascii=False)[:500]})
                    log("tool", f"{name} {json.dumps(args, ensure_ascii=False)[:160]}")
                    # snapshot the in-flight run itself: a restart in the
                    # middle of a long command still leaves "what was running"
                    # in history (tool_end overwrites this with the result)
                    # the ⏳ label is the reattach view of the running tool
                    _flush_draft("⏳ " + name + " "
                                 + json.dumps(args, ensure_ascii=False)[:200],
                                 stamp_prev=True, avatar="tool")

                    if stopper.is_set():
                        break

                    aa = db.load_auto_approve()  # live ticks: re-read per tool call
                    if name in settings.tools_disabled:
                        # opt-out tool (TOOLS_DISABLED in .env): advertised to
                        # neither schema nor prompt, but a text-protocol model
                        # can still name it — reject it the same way plan mode
                        # rejects its mutating tools.
                        result = (f"error: {name} is disabled for this install "
                                  "(TOOLS_DISABLED). Do not retry it; work with "
                                  "the tools that ARE available.")
                        log("tool", f"{name} blocked (TOOLS_DISABLED)")
                    elif plan_mode and name in MUTATING_TOOLS:
                        # defensive gate: the tool isn't even advertised in plan
                        # mode, but a text-protocol model can still name it
                        result = (f"error: {name} is disabled in Plan mode — the boss "
                                  "is in Plan mode (possibly switched mid-run). Do not "
                                  "retry it; continue planning and answer with your "
                                  "step-by-step plan for the requested change.")
                        log("tool", f"{name} blocked (plan mode)")
                    elif name == "run_command":
                        cmd = str(args.get("command") or "")
                        run_cwd = cwd
                        if args.get("cwd"):
                            try:
                                cand = (cwd / str(args["cwd"])).resolve()
                                if cand == settings.root_dir or settings.root_dir in cand.parents:
                                    run_cwd = cand
                            except Exception:  # noqa: BLE001
                                pass
                        destructive, reason = destructive_command(cmd)
                        restart, rreason = restart_command(cmd)
                        # HARD RULE (narrowed, ratified 2026-09-24): a
                        # server-lifecycle command runs WITHOUT approval only
                        # when it is the clean /api/restart handoff AND no
                        # other session has work in flight. Condition (a) —
                        # the change verified (import clean + suites green
                        # this session) — is enforced on the model side by
                        # the prompt rule; the harness enforces (b) and (c)
                        # here. Everything else (raw kills, restart-muji,
                        # start.bat, server.py, or a /api/restart while other
                        # runs are live) ALWAYS asks — the auto-approve panel
                        # cannot waive it; the user is the ship gate.
                        self_restart = False
                        if restart:
                            low = cmd.lower()
                            clean = ("api/restart" in low
                                     and "restart-muji" not in low
                                     and "restart_muji" not in low
                                     and "server.py" not in low
                                     and "start.bat" not in low)
                            if clean:
                                try:
                                    from . import tasks as _tasks2
                                    others = _tasks2.other_sessions_in_flight(
                                        session_id)
                                except Exception:  # noqa: BLE001 — fail closed
                                    others = ["<unknown>"]
                                if not others:
                                    self_restart = True
                        if restart and not self_restart:
                            aid = uuid.uuid4().hex
                            yield sse("approval", {"approval_id": aid,
                                                   "title": "Server restart — you're the ship gate",
                                                   "command": cmd,
                                                   "reason": (rreason + " — hard rule: muji never "
                                                              "self-restarts; use the sidebar "
                                                              "⟳ button or run it in a terminal"),
                                                   "timeout": settings.approval_timeout})
                            decision = await _wait_approval(aid, stopper)
                            yield sse("approval_closed", {"approval_id": aid})
                            if decision == "stopped":
                                break
                            if decision != "approved":
                                result = (f"Server-restart command was {decision} and NOT executed: {cmd}\n"
                                          f"Do not retry it. Tell the user the change is ready and "
                                          f"needs a restart — the sidebar ⟳ button (or a terminal) "
                                          f"does it.")
                                log("tool", f"run_command server-restart ({decision}): {cmd[:120]}")
                            else:
                                log("tool", f"run_command server-restart (user approved): {cmd[:120]}")
                                try:
                                    result = await exec_command(cmd, run_cwd)
                                except ToolError as e:
                                    result = f"error: {e}"
                        elif destructive and aa.get("destructive", True):
                            aid = uuid.uuid4().hex
                            yield sse("approval", {"approval_id": aid,
                                                   "title": "Destructive operation",
                                                   "command": cmd, "reason": reason,
                                                   "timeout": settings.approval_timeout})
                            decision = await _wait_approval(aid, stopper)
                            yield sse("approval_closed", {"approval_id": aid})
                            if decision == "stopped":
                                break
                            if decision != "approved":
                                result = (f"Destructive command was {decision} and NOT executed: {cmd}\n"
                                          f"Tell the user what you intended and ask how to proceed.")
                            else:
                                try:
                                    result = await exec_command(cmd, run_cwd)
                                except ToolError as e:
                                    result = f"error: {e}"
                        else:
                            if destructive:
                                log("tool", f"run_command destructive (brake off): {cmd[:120]}")
                            try:
                                result = await exec_command(cmd, run_cwd)
                            except ToolError as e:
                                result = f"error: {e}"
                    elif name == "ask_user":
                        question = str(args.get("question") or "")
                        options = [str(o) for o in (args.get("options") or [])][:5]
                        try:
                            rec = int(args.get("recommended") or 0)
                        except (TypeError, ValueError):
                            rec = 0
                        if len(options) < 2:
                            result = ("error: ask_user needs 2-5 concrete options — if the "
                                      "ambiguity is trivial, just pick and state it instead")
                        else:
                            # always-on escape hatch: the card gets a final
                            # "Something else" option the model never controls —
                            # picking it means the boss will describe his own
                            # answer, so the model must ask for it next.
                            ESCAPE = "Something else — I'll describe it"
                            options.append(ESCAPE)
                            rec = min(max(rec, 0), len(options) - 1)
                            qid = uuid.uuid4().hex
                            yield sse("question", {"question_id": qid, "question": question,
                                                   "options": options, "recommended": rec,
                                                   "timeout": settings.question_timeout})
                            choice, source = await _wait_question(qid, stopper, rec)
                            yield sse("question_closed", {"question_id": qid,
                                                          "choice": choice,
                                                          "source": source})
                            # the answered question stays in history: the
                            # settled card renders from this part on reload /
                            # draft re-attach (the SSE frames alone are not
                            # enough — closed cards are skipped on replay).
                            # Appended even on stop, so a stopped run still
                            # shows the question it was waiting on.
                            parts.append({"t": "question", "question_id": qid,
                                          "question": question,
                                          "options": options,
                                          "recommended": rec,
                                          "choice": choice,
                                          "source": source})
                            if source == "stopped":
                                break
                            if source == "user":
                                if options[choice] == ESCAPE:
                                    result = ("The boss chose 'Something else' — he will "
                                              "describe his own answer. Ask him to describe "
                                              "it (one short question, no new options) "
                                              "and wait for his description before acting.")
                                else:
                                    result = f"The boss chose: {options[choice]}. Proceed with that."
                            else:  # timeout → recommended
                                result = (f"No reply within {settings.question_timeout}s — "
                                          f"proceeding with the recommended choice: "
                                          f"{options[choice]}.")
                    else:
                        # Non-destructive tools (read, edit, web, browser, search)
                        # run automatically — no approval card. A path in a gated
                        # scope (muji's repo → "self", elsewhere → "outside") is
                        # auto-granted and retried. Destructive operations are gated
                        # in the run_command branch, not here.
                        while True:
                            try:
                                result = await asyncio.to_thread(dispatch, ctx, name, args)
                                break
                            except NeedsScope as e:
                                ctx.scope_approved.add(e.path)
                                continue
                            except ToolError as e:
                                result = f"error: {e}"
                                break

                    # browser screenshot: pull [[BROWSER_IMAGE:<b64>]] out of
                    # the result BEFORE spilling (the b64 alone would blow the
                    # spill cap and the image would be lost to disk).
                    shot_b64 = None
                    mimg = re.search(r"\[\[BROWSER_IMAGE:([A-Za-z0-9+/=]+)\]\]", result)
                    if mimg:
                        shot_b64 = mimg.group(1)
                        result = ((result[:mimg.start()] + result[mimg.end():]).strip()
                                  or "[browser screenshot] (image attached to the model)")
                    ms = int((time.time() - t0) * 1000)
                    ok = not result.startswith("error:")
                    # pass-by-reference: one choke point — big successful
                    # results spill to a file on disk; the model gets a preview
                    # + the path (it pags back with read_file / search_files).
                    # Errors stay inline.
                    if ok:
                        result = spill_result(result, name)
                    yield sse("phase_end", {"tool": name, "ok": ok,
                                            "detail": result[:400], "ms": ms})
                    parts.append({"t": "tool", "tool": name,
                                  "args": json.dumps(args, ensure_ascii=False)[:500],
                                  "ok": ok, "detail": result[:400], "ms": ms})
                    # keep the in-flight row current — this is what survives
                    # a restart/crash between tool runs (and what a reattach
                    # renders while the next round streams)
                    _flush_draft("", avatar="last")
                    # right-panel Terminal tab: full tool output (capped)
                    te = {"tool": name, "ok": ok, "ms": ms,
                          "args": json.dumps(args, ensure_ascii=False)[:500],
                          "output": result[:16000]}
                    if name in ("write_file", "edit_file") and args.get("path"):
                        te["path"] = str(args["path"])
                    yield sse("tool_end", te)
                    log("tool", f"{name} → {'ok' if ok else 'FAIL'} ({ms}ms)")

                    if plan and 0 <= plan_active < len(plan) and plan_active not in plan_done:
                        plan_done.add(plan_active)
                        yield sse("plan_update", {"index": plan_active,
                                                  "status": "done",
                                                  "done": len(plan_done),
                                                  "total": len(plan)})
                        plan_active = -1

                    if stopper.is_set():
                        break

                    if _llm.mode == "text":
                        messages.append({"role": "assistant", "content": draft})
                        messages.append({"role": "user",
                                         "content": "TOOL_RESULT:\n" + result[:12000]})
                    else:
                        tid = tc.get("id") or f"call_{idx}"
                        messages.append({
                            "role": "assistant", "content": "",
                            "tool_calls": [{"id": tid, "type": "function",
                                            "function": {"name": name,
                                                         "arguments": json.dumps(args)}}],
                        })
                        # browser screenshot: the [[BROWSER_IMAGE]] marker was
                        # already stripped into shot_b64 (pre-spill) → real
                        # image_url part, so the model sees the pixels.
                        if shot_b64:
                            content = []
                            text_part = result[:12000].strip()
                            if text_part:
                                content.append({"type": "text", "text": text_part})
                            content.append({"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{shot_b64}"}})
                            messages.append({"role": "tool", "tool_call_id": tid,
                                             "content": content})
                        else:
                            messages.append({"role": "tool", "tool_call_id": tid,
                                             "content": result[:12000]})
                draft = ""
                continue
            # ── no tool calls: this is the final answer ──
            answer = draft.strip()
            if think_loop_tripped:
                # the tripwire aborted the round mid-thinking: emit a short
                # clarification instead of the empty-answer stop (the old
                # behavior — burn the whole budget, return nothing, leave a
                # dead 480 KB thinking blob in the DB).
                answer = ("I got stuck in a repetition loop on that last "
                          "word and cut the round short — what exactly did "
                          "you mean? One word back and I'll pick up from "
                          "there.")
                log("warn", f"session {session_id[:8]}: loop-tripwire "
                            f"clarification answer (turn {turn})")
            yield sse("answer_start", {})
            if answer:
                # TL;DR enforcement (deterministic backstop to TASK_RULES):
                # a final answer that should carry a TL;DR but doesn't gets
                # the visible flag note + the chat's sidebar flag count.
                # The note rides on the stored content (history renders it)
                # AND on the done frame (live finishTurn re-renders it).
                orig_answer = answer  # trajectory recall logs what the model said
                # order fix: a TL;DR written AFTER ## Chips is invisible in
                # the UI (extractChips strips everything from the header on)
                # — move it before the section so it renders.
                ordered = _normalize_tldr_order(answer)
                if ordered != answer:
                    log("info", "TL;DR moved before ## Chips (was after — invisible in UI)")
                    answer = ordered
                deduped = _drop_tldr_after_chips(answer)
                if deduped != answer:
                    log("info", "duplicate TL;DR after ## Chips dropped")
                    answer = deduped
                tldr_flagged = _needs_tldr_flag(answer)
                if tldr_flagged:
                    answer = _flag_tldr(answer)
                    db.bump_tldr_flags(session_id)
                    log("info", "TL;DR flag: final answer missing TL;DR — flagged")
                elif _TLDR_RE.search(_CODE_FENCE_RE.sub("", answer)) and \
                        (db.get_session(session_id) or {}).get("tldr_flags"):
                    # the counter is a debt, not a lifetime tally: the next
                    # final answer that CARRIES a TL;DR clears the badge
                    # (a one-liner that needs none is neutral, not "good")
                    db.clear_tldr_flags(session_id)
                    log("info", "TL;DR flag cleared: rule-following final answer")
                _close_draft(answer)
                if not (db.get_session(session_id) or {}).get("title"):
                    db.rename_session(session_id, re.sub(r"\s+", " ", user_text).strip()[:48])
                yield sse("done", {"content": answer, "files": ctx.generated})
                log("info", f"answer done ({len(answer)} chars, {len(ctx.generated)} file(s))")
                # the task finished — the loop state is spent (resume is for
                # interrupted runs), and the trajectory is logged for recall
                db.clear_run_state(session_id)
                _auto_resume_fires.pop(session_id, None)  # clean finish
                                                          # resets the budget
                if not stopper.is_set():
                    asyncio.get_running_loop().create_task(
                        _log_trajectory(session_id, user_text, orig_answer,
                                        [p.get("tool") for p in parts
                                         if p.get("t") == "tool"],
                                        "done"))
                if settings.fact_check and not stopper.is_set():
                    corr = await _fact_check(answer, ctx.sources)
                    if corr:
                        # note-only: the streamed answer stays intact, the UI appends the note
                        yield sse("correction", {"note": corr["note"]})
                        log("info", "fact-check note emitted")
            else:
                # Empty answer = the model ended the round with no content
                # and no tool call — a RESUMABLE stop, not a dead end. The
                # per-turn checkpoint (top of loop) already holds the
                # conversation, so the run_state survives: the UI gets the
                # ⏹ note + ↻ chip, and boot auto-resume (setting ON)
                # re-enters the loop on the next restart. The old `error`
                # frame made the UI settle as a dead-end (red text, no
                # continue affordance) even though the checkpoint was
                # there — the interruption the boss kept hitting.
                _close_draft("")  # keep any tool timeline, drop empty shells
                # Forensics for the 19 unexplained empty answers (09-28
                # assessment): finish_reason + this round's thinking size
                # say whether it was thinking-only, a length cut, or a
                # stream that never produced content.
                log("warn", f"session {session_id[:8]}: model returned an "
                            f"empty answer (turn {turn} "
                            f"finish={finish_reason!r} "
                            f"think_chars={len(thinking_text) - think_t0}) "
                            f"— stopped, run_state kept for resume")
                yield sse("stopped",
                          {"reason": "model returned an empty answer",
                           "finish_reason": finish_reason})
                # In-process re-fire (the boot path only fires on restart —
                # an in-process stop on a live server would sit parked
                # until the next boot). force=True: an empty answer isn't
                # an LLMError, but it IS worth a retry — the budget cap
                # (3/session) stops a permanently-empty model from looping.
                _maybe_auto_resume(session_id, "empty answer", log,
                                   force=True)
            return
        # max turns exhausted — a resumable stop, not a dead end
        # (the UI shows the reason + a Resume button)
        log("warn", f"session {session_id[:8]} stopped at {settings.max_turns} tool turns")
        yield sse("stopped", {"reason": f"max {settings.max_turns} tool turns reached"})
    except asyncio.CancelledError:
        # Hard stop: /api/chat/stop cancels the run task, so this lands
        # however long the turn was awaiting — mid model-stream (between
        # tokens), inside a running command, in an approval wait, in
        # compaction. Nothing resists: persist what the user saw and end
        # with the terminal `stopped` frame the UI settles on.
        _close_draft(draft.strip())
        log("info", f"session {session_id[:8]} hard-stopped by user (turns={turn})")
        yield sse("stopped", {"reason": "stopped by user"})
        return
    except LLMError as e:
        msg = f"Model error: {e}"
        # The mid-stream dead zone: a transient endpoint blip (timeout /
        # connection drop) ends the run here with the run_state still
        # saved — boot auto-resume can't fire (no crash), so re-fire it
        # in-process, capped. A permanent error (4xx, bad model name)
        # skips the check and stays a real dead end.
        _maybe_auto_resume(session_id, msg, log)
        yield sse("error", {"message": msg})
    except Exception as e:  # noqa: BLE001
        log("error", f"agent crashed: {type(e).__name__}: {e}")
        yield sse("error", {"message": f"Internal error: {type(e).__name__}: {e}"})
    finally:
        # safety net for any path that returned without closing the row
        # (e.g. the max-turns stop above) — idempotent
        _close_draft(draft.strip())
        db.set_processing(session_id, False)
        log("info", f"session {session_id[:8]} finished (turns={turn})")
        # refresh the sidebar activity phrase — covers done/error/stopped,
        # and the fresh draft is already finalized by _close_draft
        if not stopper.is_set():
            asyncio.get_running_loop().create_task(
                refresh_summary(session_id, resume=resume))
