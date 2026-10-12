"""FastAPI app: SSE chat, sessions, uploads, previews, approvals, logs."""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from . import agent, db, reminders, supervisor, taskstore
from .config import APP_ROOT, settings
from .sse import request_stop
from .tasks import TaskManager, set_manager
from .tools import (_under_root, decode_token, exec_command, file_kind,
                    notebook_to_html, preview_token)

logs: deque[dict] = deque(maxlen=500)

#: process start time — the client's revival detector. A tab that opened
#: its streams against a dead server sees a DIFFERENT boot_id after a
#: crash/watchdog revival and hard-reloads (whole-tab convergence); a
#: same-process blip keeps the same id and takes the gentle retry path.
BOOT_ID = time.time()


def log(level: str, message: str) -> None:
    logs.append({"timestamp": time.strftime("%H:%M:%S"), "level": level,
                 "message": message})


#: background task manager — agent turns run detached from any browser.
#: db._db() FIRST: TaskManager.__init__ restores durable queues from the
#: `queue` table, and at import time the DB hasn't been opened yet
#: (its schema/migrations run on first _db() call) — without this the
#: restore would silently no-op on every boot.
db._db()
tasks = TaskManager(log)
set_manager(tasks)  # the agent resolves runs through this (flushed_seq)

#: the user row auto-resume writes at boot. Same contract as the UI's
#: RESUME_TEXT (static/app.js): the client renders it as the compact ↻
#: marker, never a chat bubble, and the agent re-enters the saved
#: run_state instead of building fresh context (the ORIGINAL goal stays
#: pinned — the nudge is only the trigger).
AUTO_RESUME_TEXT = (
    "Continue — the previous run was stopped mid-task. Pick up exactly where "
    "you left off (build on what was already done, don't redo it) and finish the job.")
# the mid-stream auto-resume (agent._maybe_auto_resume) re-fires THIS
# exact text in-process on a transient LLM error — same marker contract
agent._auto_resume_text = AUTO_RESUME_TEXT


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(60)
        tasks.prune()


async def _reminder_loop() -> None:
    """The alarm clock: every 30 s, fire a Windows toast for anything in the
    local task/calendar store whose due moment has arrived (once per item).
    Runs in a thread — toast delivery shells out to PowerShell/wscript and
    must never block the event loop."""
    while True:
        await asyncio.sleep(30)
        try:
            fired = await asyncio.to_thread(reminders.fire_due)
            for f in fired:
                log("info", f"reminder fired: {f['kind']} #{f['id']} "
                            f"{f['title']!r} via {f['via']}")
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            log("error", f"reminder tick failed: {e}")


#: findings already toasted (sid → status) — a stuck run must not re-toast
#: every 60 s; the finding clears when the run leaves that state
_sup_toasted: dict[str, str] = {}


async def _supervisor_loop() -> None:
    """The supervisor's heartbeat: every 60 s, classify every live run,
    AUTO stop-resume a STUCK run (2+ consecutive ticks, tool-in-flight
    grace, budget 2 per episode — supervisor.auto_stop_decisions), and
    toast ONCE per NEW stuck/waiting finding (deduped by sid+status).
    `waiting` stays the boss's gate — a manual click only; the manual
    stop+resume click works alongside the auto action."""
    while True:
        await asyncio.sleep(60)
        try:
            rep = await asyncio.to_thread(supervisor.watch, tasks)
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            log("error", f"supervisor tick failed: {e}")
            continue
        # AUTO stop-resume (stuck only, 2+ ticks, budget 2) — decided HERE,
        # never in watch(): the UI's 5 s /api/watch polls must not feed the
        # tick counters. The visible chat event is the ↻ continuation
        # marker the enqueued nudge renders; the log line is the audit
        # trail.
        for row in supervisor.auto_stop_decisions(rep["rows"]):
            cur = tasks.get(row["sid"])
            if cur is None or not cur.active:
                continue  # settled between watch() and the action
            log("info", f"supervisor: AUTO stop+resume {row['title']!r} "
                        f"({row['why']}) — 2nd consecutive stuck tick")
            await _stop_resume_session(row["sid"], source="auto",
                                       why=row.get("why"))
            supervisor.note_auto_stop(row["sid"])
        # a finished run ends its stuck episode (fresh budget next time)
        supervisor.note_finished_runs(tasks)
        for row in rep["rows"]:
            status = row["status"]
            if status not in ("stuck", "waiting"):
                _sup_toasted.pop(row["sid"], None)
                continue
            if _sup_toasted.get(row["sid"]) == status:
                continue  # already toasted — the strip shows it live
            _sup_toasted[row["sid"]] = status
            if status == "stuck":
                title = "👁 watch — stuck"
                msg = f"{row['title']}: {row['why'] or 'silent'} (open the strip to stop + resume)"
            else:
                title = "👁 watch — needs you"
                msg = f"{row['title']}: waiting {row['waiting']}"
            via = await asyncio.to_thread(reminders.send_toast, title, msg)
            log("info", f"supervisor: {status} {row['title']!r} ({row['why']}) via {via}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # A restart kills every in-memory agent turn. Two things to reconcile:
    # 1. in-flight assistant rows (the mid-task timeline) — promote whatever
    #    the user saw into history so an interrupted run keeps its
    #    command/edit/search timeline instead of vanishing down to the
    #    user's prompt,
    # 2. stale busy flags — clear them so the UI can't stick on a phantom
    #    "working" state with no work actually ongoing.
    if db.finalize_stale_drafts():
        log("info", "promoted in-flight draft rows from the previous run")
    db.clear_stale_processing()
    # 3. durable queues — messages the user queued before the restart
    #    survive in the DB. The restart killed the in-flight turn, so
    #    nothing is draining them: autostart fires each session's head
    #    now; the rest follow via the normal drain (FIFO preserved).
    #    Stop stays the explicit "kill everything" path — it clears the
    #    durable rows too, so a stop-then-restart won't resurrect them.
    restored = sum(len(q) for q in tasks.queues.values())
    autostarted = set()
    if restored:
        log("info", f"restored {restored} queued message(s) from the DB")
        for sid in list(tasks.queues):
            tasks.autostart(sid)
            # autostart pops the sid out of tasks.queues when it was the only
            # entry, so capture the set HERE for the auto-resume guard below
            autostarted.add(sid)
    # 4. auto-resume — a crash/restart kills in-flight turns too. When the
    #    setting is on, every session left with a saved run_state (interrupted
    #    without a final summary) re-enters its saved loop automatically:
    #    same path as the user's ↻ click (stateful resume, original goal
    #    pinned, no re-reading/re-running), just fired by the server at boot.
    #    Queued messages on the same session drain after the resumed turn
    #    finishes, so nothing double-fires. Off (default) = the ↻ affordance
    #    stays the user's to press.
    if db.load_auto_approve().get("auto_resume"):
        try:
            pids = db.all_session_ids()
        except Exception:  # noqa: BLE001 — never kill boot
            pids = []
        for sid in pids:
            # stopped=1 (the boss hit Stop) is a deliberate pause — the
            # triangle/↻ affordance stays, but boot must NOT resurrect it.
            # Only ✕ Start fresh removes the row; a manual ↻ or a queued
            # continuation clears the flag on its first checkpoint.
            if not db.has_unstopped_run_state(sid):
                continue
            if sid in autostarted:
                # durable-queue restore already autostarted this session's
                # head (the user's own queued words, continuing the
                # interrupted task via resume when a run_state exists) —
                # enqueuing a nudge too would stack a second continuation
                # on top of it. The saved run_state stays for a later ↻
                # only if the autostarted turn didn't consume it.
                continue
            # NOTE: no explicit db.add_message here — agent.run_chat
            # persists the user row itself when the turn starts. Writing it
            # here too stacked a duplicate nudge row (same text, same
            # second) on every auto-resumed session.
            tasks.enqueue(sid, AUTO_RESUME_TEXT, [],
                          (db.get_session(sid) or {}).get("mode") or "act",
                          (db.get_session(sid) or {}).get("roast") or "chill",
                          resume=True)
            log("info", f"auto-resume: session {sid[:8]} re-entering saved run_state")
    prune_task = asyncio.create_task(_prune_loop())
    remind_task = asyncio.create_task(_reminder_loop())
    sup_task = asyncio.create_task(_supervisor_loop())
    # Telegram bridge (Telethon, user mode): silent connect when a session
    # file exists; if none does it logs a hint and stays off until the
    # boss runs tools/tg_login.py once. Never blocks boot.
    tg_task = None
    # The bridge only makes sense when the Telegram tools are opted-in
    # (not in TOOLS_DISABLED). Fresh installs ship them disabled, so the
    # bridge stays off and never touches Telethon / a session file.
    if "tg_search" in settings.tools_disabled:
        log("info", "telegram bridge off (tg tools disabled in .env)")
    else:
        try:
            from . import telegram as tg
            tg_task = asyncio.create_task(tg.start())
        except Exception as e:  # noqa: BLE001 — the bridge must not kill boot
            log("warn", f"telegram bridge failed to start: {type(e).__name__}: {e}")
    yield
    for sid in list(tasks.runs):
        tasks.cancel(sid)
    prune_task.cancel()
    remind_task.cancel()
    sup_task.cancel()
    if tg_task is not None:
        try:
            from . import telegram as tg
            await tg.stop()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title=settings.title, lifespan=_lifespan)


# ── static + security headers ──────────────────────────────────────

app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "index.html")


def _server_python() -> str:
    """Interpreter that can import the app's deps. The repo virtualenv is
    self-contained (its python.exe reads the pyvenv.cfg next to itself), so
    prefer it; fall back to the running interpreter only when there is no
    .venv. The Windows Store base python does NOT have fastapi, so a bare
    `sys.executable` must never be used while the venv exists."""
    if os.name == "nt":
        cand = APP_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        cand = APP_ROOT / ".venv" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


@app.post("/api/restart")
async def api_restart():
    """Respawn the server with the latest on-disk code. User-initiated from the
    UI — the click is explicit consent, so no approval gate. Spawns a fresh
    detached `server.py` in the repo virtualenv, then exits this instance; the
    child's port-wait (server.py) avoids "address already in use"."""
    server_script = str(APP_ROOT / "server.py")
    # The child's console goes to the data/ log files, NOT DEVNULL: a failed
    # restart (port race, import error, anything) must leave its banner and
    # traceback somewhere readable. Append mode — the dying parent writes the
    # same files too, but the overlap is <1s (parent exits 0.5s after spawn).
    # If the files can't be opened, fall back to DEVNULL: a restart must
    # never fail because logging can't.
    out_log = err_log = None
    try:
        out_log = open(APP_ROOT / "data" / "server.log", "a",
                       encoding="utf-8", errors="replace")
        err_log = open(APP_ROOT / "data" / "server.err.log", "a",
                       encoding="utf-8", errors="replace")
    except OSError:
        for f in (out_log, err_log):
            if f is not None:
                f.close()
        out_log = err_log = subprocess.DEVNULL
    # The child must NOT inherit the parent's MUJI_BOOT_TOKEN: a fresh boot
    # has to mint its own so /api/health can distinguish "new server" from
    # "the dying parent still answering" (the client's reload gate checks
    # token != parent's). Popen inherits the full env by default, so strip it.
    child_env = {k: v for k, v in os.environ.items() if k != "MUJI_BOOT_TOKEN"}
    spawn = dict(stdin=subprocess.DEVNULL, stdout=out_log, stderr=err_log,
                 cwd=str(APP_ROOT), env=child_env)
    if os.name == "nt":
        spawn["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        spawn["start_new_session"] = True
    try:
        subprocess.Popen([_server_python(), server_script], **spawn)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"could not spawn new server: {e}")
    # Clicking ⟳ is "I want muji running" — same intent as start.bat, so
    # clear any leftover stop.flag: otherwise a restart after stop-muji.ps1
    # would come back with the watchdog still disarmed.
    try:
        (APP_ROOT / "data" / "stop.flag").unlink(missing_ok=True)
    except OSError:
        pass
    # Durable audit line: log() is an in-memory ring that dies with this
    # process, but the child's stdout IS data/server.log (opened above) —
    # a flushed print survives and is greppable ("restart: parent ...").
    parent_token = os.environ.get("MUJI_BOOT_TOKEN", "")
    print(f"restart: parent boot_token={parent_token} exiting; "
          f"child = {server_script}", flush=True)
    log("info", "restart: spawned fresh server.py, exiting this instance")

    def _exit_after_flush() -> None:
        time.sleep(0.5)  # let this response reach the browser before we die
        os._exit(0)

    threading.Thread(target=_exit_after_flush, daemon=True).start()
    # parent_boot_token lets the client verify a FRESH boot: the old code
    # reloaded on the first 200 after the POST, but that 200 can come from
    # the OLD process (still serving while it waits to exit) or a sibling
    # checkout on the same port — so the page could come back stale. A fresh
    # boot writes a new per-boot token; "token != parent" is the only real
    # signal that a NEW server is answering (see restartServer in app.js).
    return {"ok": True, "restarting": True, "parent_boot_token": parent_token}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/preview/"):
        # Sandbox for agent-generated pages: opaque origin in the browser
        # (iframe sandbox without allow-same-origin), pinned here as well.
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    if path.startswith(("/api/", "/uploads/")):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "frame-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# ── config / workspaces / sessions ─────────────────────────────────

@app.get("/api/health")
async def health():
    # "boot_token" identifies WHICH checkout answers: server.py writes a
    # per-boot random token to server.boot and sets it here; the launcher
    # compares it against probe responses, so "is the port ours?" is
    # answered by a secret no other checkout (even another muji clone)
    # can match. Empty when run outside server.py (tests) — probes then
    # just report "not ours", which is the safe answer.
    return {"ok": True, "model": settings.model, "tool_mode": agent._llm.mode,
            "boot_id": BOOT_ID, "root": str(APP_ROOT),
            "boot_token": os.environ.get("MUJI_BOOT_TOKEN", "")}


@app.get("/api/config")
async def api_config():
    return {
        "title": settings.title, "brand": settings.brand,
        "model": settings.model, "root_dir": str(settings.root_dir),
        "fact_check": settings.fact_check, "auth_enabled": False,
        "tz_offset": settings.tz_offset,
        # context meter: denominator = the model's real limit (boundary,
        # the 100% mark); the sublabel shows where compaction actually
        # fires (trigger, at turn boundaries)
        "compact_trigger": settings.compact_trigger,
        "compact_boundary": settings.compact_boundary,
        "compact_enabled": settings.compact_enabled,
    }


# ── First-run setup ────────────────────────────────────────────────────
# Shown when the three model-credential env vars are all unset/empty.
# The overlay collects them, writes them to .env, then the user reloads.

def _setup_configured() -> bool:
    """True when the user has already connected a model endpoint."""
    return bool(settings.base_url) and bool(settings.api_key) and settings.api_key != "sk-no-key" and bool(settings.model)


@app.get("/api/setup/status")
async def api_setup_status():
    return {"configured": _setup_configured()}


@app.get("/api/models")
async def api_models(base: str = Query(...), key: str = Query("")):
    """Proxy GET {base}/models so the UI can auto-populate the model dropdown.

    Local endpoints (Ollama, vLLM, llama.cpp) accept any key (or none), so
    the UI can call this before the user has typed a real key for those.
    Hosted providers (OpenAI, OpenRouter) need a valid key — a 401 from
    upstream is surfaced as an empty list + a hint, not an error."""
    import httpx as _httpx
    url = base.rstrip("/") + "/models"
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {"models": [], "error": f"upstream {r.status_code}"}
            data = r.json()
            # OpenAI-compatible shape: {"data": [{"id": "..."}, ...]}
            items = data.get("data", data if isinstance(data, list) else [])
            models = []
            for it in items:
                if isinstance(it, str):
                    models.append(it)
                elif isinstance(it, dict) and "id" in it:
                    models.append(it["id"])
            models.sort()
            return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


def _upsert_env_line(path: Path, key: str, value: str) -> None:
    """Set or append a single KEY=VALUE line in a .env file, preserving
    all other lines and comments. Creates the file with a header if absent."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        out, found = [], False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    out.append(f"{key}={value}")
                    found = True
                    continue
            out.append(line)
        if not found:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        path.write_text(
            "# muji — model endpoint\n"
            f"{key}={value}\n",
            encoding="utf-8",
        )


@app.post("/api/setup/save")
async def api_setup_save(payload: dict):
    """Persist the three model-credential values to .env. The user must
    then reload the page (or hit the sidebar ⟳) for the server to pick
    them up — the running process reads env vars at import time."""
    base_url = (payload.get("base_url") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    model = (payload.get("model") or "").strip()
    if not base_url or not model:
        raise HTTPException(400, "base_url and model are required")
    env_path = APP_ROOT / ".env"
    _upsert_env_line(env_path, "OPENAI_BASE_URL", base_url)
    _upsert_env_line(env_path, "OPENAI_API_KEY", api_key or "sk-no-key")
    _upsert_env_line(env_path, "MODEL", model)
    log("info", f"setup: saved base_url={base_url} model={model}")
    return {"ok": True, "needs_reload": True}


@app.get("/api/latency")
async def api_latency():
    """Day × hour latency heatmap data, recomputed on demand from the
    events table (every click re-queries — no stale cache). Same shape the
    standalone qwen_latency_day_hour.html used to bake in as a constant:
    days[], hours[], tok_s[day][hour], ms[day][hour], n_tok/n_ms counts,
    day_tot[day] = {n, med_tok, med_ms}. Hours are LOCAL time (tz_offset
    from config), not UTC — the UTC bucketing was the bug that made the
    "fast lane" look like it drifted around. tok_s is null for days before
    the tracker started logging it (Sep 26); ms covers all days."""
    import json as _json
    import statistics as _stats
    import datetime as _dt
    con = db._db()
    tz = _dt.timezone(_dt.timedelta(hours=settings.tz_offset))
    rows = con.execute(
        "SELECT data, ts FROM events WHERE event='llm_end' AND ts IS NOT NULL"
    ).fetchall()
    days = sorted({_dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
                   for _, ts in rows})
    acc_tok = {d: [[] for _ in range(24)] for d in days}
    acc_ms = {d: [[] for _ in range(24)] for d in days}
    for d, ts in rows:
        o = _json.loads(d)
        day = _dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        h = _dt.datetime.fromtimestamp(ts, tz).hour
        if o.get("ms") is not None:
            acc_ms[day][h].append(o["ms"])
        if o.get("tok_s") is not None:
            acc_tok[day][h].append(o["tok_s"])
    tok_s, ms = {d: [None] * 24 for d in days}, {d: [None] * 24 for d in days}
    n_tok, n_ms = {d: {} for d in days}, {d: {} for d in days}
    day_tot = {}
    for d in days:
        for h in range(24):
            if acc_ms[d][h]:
                ms[d][h] = _stats.median(acc_ms[d][h])
                n_ms[d][str(h)] = len(acc_ms[d][h])
            if acc_tok[d][h]:
                tok_s[d][h] = _stats.median(acc_tok[d][h])
                n_tok[d][str(h)] = len(acc_tok[d][h])
        all_ms = [v for h in range(24) for v in acc_ms[d][h]]
        all_tok = [v for h in range(24) for v in acc_tok[d][h]]
        day_tot[d] = {
            "n": len(all_ms),
            "med_tok": round(_stats.median(all_tok), 1) if all_tok else None,
            "med_ms": _stats.median(all_ms) if all_ms else None,
        }
    return {"days": days, "hours": list(range(24)), "tok_s": tok_s,
            "ms": ms, "n_tok": n_tok, "n_ms": n_ms, "day_tot": day_tot}


# ── google tasks OAuth ───────────────────────────────────────────────

@app.get("/api/tasks/auth")
async def tasks_auth():
    """Return the Google consent URL. Boss opens it in a browser, authorizes,
    and Google bounces back to /api/tasks/callback?code=..."""
    from . import gtasks as gt
    try:
        return {"auth_url": gt.get_auth_url()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks/callback")
async def tasks_callback(code: str = "", error: str = ""):
    """OAuth redirect target. Exchanges the code for tokens, then tells the
    Boss to come back to muji (the redirect is a plain browser GET — the
    PWA tab can't receive it, so this page is the handoff)."""
    from . import gtasks as gt
    if error:
        return Response(f"<h3>Tasks auth failed: {error}</h3><p>Go back to muji.</p>",
                        media_type="text/html")
    try:
        gt.exchange_code(code)
        log("info", "google tasks connected (tokens stored)")
        return Response("<h3>✓ Tasks connected</h3><p>Go back to muji — "
                        "you can now add and list your Google Tasks.</p>",
                        media_type="text/html")
    except Exception as e:
        log("error", f"tasks callback: {e}")
        return Response(f"<h3>Tasks auth failed</h3><p>{e}</p>",
                        media_type="text/html")


@app.get("/api/tasks/status")
async def tasks_status():
    from . import gtasks as gt
    if not gt.TOKEN_FILE.exists():
        return {"connected": False}
    import json as _json, time as _time
    tokens = _json.loads(gt.TOKEN_FILE.read_text(encoding="utf-8"))
    return {"connected": True,
            "valid_until": int(tokens.get("expiry", 0)) if tokens.get("expiry", 0) > _time.time() else 0}


# ── gmail OAuth (readonly — retires the IMAP pipe) ───────────────────

@app.get("/api/gmail/auth")
async def gmail_auth():
    """Return the Google consent URL. Boss opens it in a browser, authorizes,
    and Google bounces back to /api/gmail/callback?code=..."""
    from . import gmail as gm
    try:
        return {"auth_url": gm.get_auth_url()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/gmail/callback")
async def gmail_callback(code: str = "", error: str = ""):
    """OAuth redirect target. Exchanges the code for tokens, then tells the
    Boss to come back to muji (the redirect is a plain browser GET — the
    PWA tab can't receive it, so this page is the handoff)."""
    from . import gmail as gm
    if error:
        return Response(f"<h3>Gmail auth failed: {error}</h3><p>Go back to muji.</p>",
                        media_type="text/html")
    try:
        gm.exchange_code(code)
        log("info", "gmail connected (tokens stored)")
        return Response("<h3>✓ Gmail connected</h3><p>Go back to muji — "
                        "I can now search and read your mail.</p>",
                        media_type="text/html")
    except Exception as e:
        log("error", f"gmail callback: {e}")
        return Response(f"<h3>Gmail auth failed</h3><p>{e}</p>",
                        media_type="text/html")


@app.get("/api/gmail/status")
async def gmail_status():
    from . import gmail as gm
    if not gm.TOKEN_FILE.exists():
        return {"connected": False}
    import json as _json, time as _time
    tokens = _json.loads(gm.TOKEN_FILE.read_text(encoding="utf-8"))
    return {"connected": True,
            "valid_until": int(tokens.get("expiry", 0)) if tokens.get("expiry", 0) > _time.time() else 0}


@app.get("/api/settings")
async def api_settings():
    return {"auto_approve": db.load_auto_approve()}


@app.post("/api/settings")
async def api_settings_set(body: dict):
    saved = db.save_auto_approve(body.get("auto_approve") or {})
    log("info", f"auto-approve settings: {saved}")
    return {"auto_approve": saved}


@app.get("/api/workspaces")
async def api_workspaces():
    return {"workspaces": db.list_workspaces()}


@app.post("/api/workspaces")
async def api_workspace_new(body: dict):
    try:
        ws = db.add_workspace(body.get("path", ""), body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    log("info", f"workspace added: {ws['path']}")
    return ws


@app.get("/api/sessions")
async def api_sessions(workspace_id: str | None = Query(default=None),
                       general: int = 0):
    if workspace_id is None and general:
        sessions = db.list_sessions(general=True)
    elif workspace_id:
        sessions = db.list_sessions(workspace_id)
    else:
        sessions = db.list_sessions()
    for s in sessions:
        run = tasks.get(s["id"])
        if run is not None:
            s["status"] = run.status
            s["waiting_approval"] = run.waiting_approval
            s["approval_deadline"] = run.approval_deadline
            s["approval_total"] = run.approval_total
            s["waiting_question"] = run.waiting_question
            s["question_deadline"] = run.question_deadline
            s["question_total"] = run.question_total
        else:
            s["status"] = "idle"
            s["waiting_approval"] = False
            s["approval_deadline"] = None
            s["approval_total"] = 0
            s["waiting_question"] = False
            s["question_deadline"] = None
            s["question_total"] = 0
        # interrupted-without-final-summary flag: a saved run_state means the
        # last run stopped/crashed mid-task (a finished run clears it), so the
        # sidebar can show the continue symbol
        s["needs_continue"] = db.has_run_state(s["id"])
        s["queued"] = tasks.queue_count(s["id"])
    return {"sessions": sessions}


@app.get("/api/watch")
async def api_watch():
    """The supervisor tick (the UI's watch strip polls this every 5 s):
    classify every live run + queued session. Deterministic, no LLM —
    rows carry server-side phrasing so the client stays dumb."""
    return await asyncio.to_thread(supervisor.watch, tasks)


async def _stop_resume_session(sid: str, source: str = "manual",
                               why: str | None = None) -> dict:
    """Stop a live run and queue a continuation nudge — ONE body shared
    by the manual /api/watch/stop-resume click and the supervisor's AUTO
    action (a stuck run, 2+ consecutive ticks — boss-approved
    2026-09-28). The run is cancelled, its waiting queue dropped the way
    /api/chat/stop drops it (the resume nudge is the ONE continuation; a
    stale queue would stack more on top), and the saved run_state
    re-entered (same path as the composer's ↻). The visible chat event
    is the ↻ continuation marker the enqueued nudge renders;
    `source`/`why` land in server.log so the auto action is never
    silent."""
    queued_dropped = tasks.queue_count(sid)
    request_stop(sid)
    tasks.cancel(sid)
    tasks.queues.pop(sid, None)
    try:
        db.queue_clear(sid)
    except Exception:  # noqa: BLE001
        pass
    # wait for the cancel to land (the run's finally clears processing +
    # flushes the draft) before the nudge enqueues — otherwise the nudge
    # would join the queue BEHIND the dying run instead of starting fresh
    for _ in range(50):
        cur = tasks.get(sid)
        if cur is None or not cur.active:
            break
        await asyncio.sleep(0.1)
    sess = db.get_session(sid) or {}
    # the UI's own resume nudge text (identical to RESUME_TEXT in
    # static/app.js — the client renders it as the compact resume marker
    # instead of a chat row)
    tasks.enqueue(sid, AUTO_RESUME_TEXT, [],
                  sess.get("mode") or "act",
                  sess.get("roast") or "chill", resume=True)
    log("info", f"watch: stop+resume {sid[:8]} (source={source}, "
                f"{why or 'no why'}, queued_dropped={queued_dropped})")
    return {"ok": True, "stopped": True, "queued_dropped": queued_dropped}


@app.post("/api/watch/stop-resume")
async def api_watch_stop_resume(body: dict):
    """The user's gate for a stuck run: stop the run, then queue a
    continuation nudge (same contract as the composer's ↻). Always
    available alongside the supervisor's AUTO action, which shares this
    body — the boss's click also re-arms the auto stop-resume budget."""
    sid = body.get("session_id", "")
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "unknown session")
    run = tasks.get(sid)
    if run is None or not run.active:
        raise HTTPException(409, "nothing running in that session")
    supervisor.note_manual_resume(sid)
    return await _stop_resume_session(sid, source="manual")


@app.post("/api/sessions/new")
async def api_session_new(body: dict):
    return db.create_session(body.get("workspace_id") or None)


@app.post("/api/sessions/rename")
async def api_session_rename(body: dict):
    if not db.get_session(body.get("session_id", "")):
        raise HTTPException(404, "unknown session")
    db.rename_session(body["session_id"], body.get("title", ""))
    return {"ok": True}


@app.post("/api/sessions/pin")
async def api_session_pin(body: dict):
    """Toggle the sidebar pin — pinned chats float to the top of the list
    and are exempt from auto-archiving."""
    if not db.get_session(body.get("session_id", "")):
        raise HTTPException(404, "unknown session")
    pinned = db.toggle_pinned(body["session_id"])
    return {"ok": True, "pinned": pinned}


@app.post("/api/sessions/mode")
async def api_session_mode(body: dict):
    """Cline-style per-chat Plan/Act mode."""
    if not db.get_session(body.get("session_id", "")):
        raise HTTPException(404, "unknown session")
    db.set_session_mode(body["session_id"], body.get("mode", "act"))
    return {"ok": True}


@app.post("/api/sessions/roast")
async def api_session_roast(body: dict):
    """Per-chat roast level (off | chill | full)."""
    if not db.get_session(body.get("session_id", "")):
        raise HTTPException(404, "unknown session")
    db.set_session_roast(body["session_id"], body.get("roast", "chill"))
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
async def api_session_delete(sid: str):
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    tasks.drop_session(sid)
    db.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/viewed")
async def api_session_viewed(sid: str):
    """Client tells the server 'the user just opened this chat' — clears
    the green done-LED (a finished run that was already seen is done
    business, not a notification)."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    db.mark_session_viewed(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/cwd")
async def api_set_session_cwd(sid: str, body: dict):
    """Per-chat working folder (Files tab: choose or paste a path).
    May be ANY existing directory, incl. outside the agent root."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    p = Path(str(body.get("path") or "").strip())
    if not p.is_absolute():
        p = settings.root_dir / p
    rp = p.resolve()
    # the working folder may be ANY existing dir, incl. outside the agent root
    # (the user is explicitly pointing this chat there); the agent tool
    # scope-gate still governs the model's own file operations
    if not rp.is_dir():
        raise HTTPException(404, "not a directory")
    db.set_session_cwd(sid, str(rp))
    return {"cwd": str(rp)}


@app.get("/api/sessions/{sid}/notes")
async def api_session_notes(sid: str):
    """This chat's durable task notes (data/sessions/<sid>/task_notes.md) —
    the file the 📝 button opens in the preview. 404 when the chat has no
    notes yet (the UI then shows a 'no notes yet' state)."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    p = settings.data_dir / "sessions" / sid / "task_notes.md"
    if not p.is_file():
        raise HTTPException(404, "no notes yet for this chat")
    return {"path": str(p), "name": p.name}


@app.post("/api/sessions/clear")
async def api_sessions_clear():
    for sid in list(tasks.runs):
        tasks.cancel(sid)
    db.clear_sessions()
    return {"ok": True}


@app.post("/api/sessions/clear-archived")
async def api_sessions_clear_archived(body: dict):
    """Delete archived chats — non-pinned, idle longer than `days`
    (default 14, matching the sidebar's auto-archive threshold)."""
    days = body.get("days", 14)
    if not isinstance(days, (int, float)) or days <= 0:
        raise HTTPException(400, "days must be a positive number")
    cutoff = time.time() - days * 86400
    n = db.clear_archived_sessions(cutoff)
    db.prune_deleted_sessions()
    return {"ok": True, "deleted": n}


@app.get("/api/sessions/deleted")
async def api_sessions_deleted():
    """List of recently deleted chats (the 30-day tombstone): what got
    deleted, when, from which bulk op. The message snapshot stays
    server-side; the list is metadata only."""
    db.prune_deleted_sessions()
    return {"deleted": db.list_deleted_sessions()}


@app.post("/api/sessions/deleted/{sid}/restore")
async def api_sessions_deleted_restore(sid: str):
    """Restore a deleted chat from its tombstone: re-inserts the session
    row (original id) + every snapshotted message. The chat comes back
    exactly as it was — same id, so a stale localStorage sid resolves."""
    try:
        s = db.restore_deleted_session(sid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "session": s}


# ── learned.md: the human gate (activate / veto pending patterns) ─────
# muji appends candidate patterns to learned.md `## pending` after each task;
# they stay inert until the Boss activates (→ `## active`) or vetoes (deletes)
# them. These endpoints power the in-chat review banner — the model never
# self-activates, the file is the single source of truth.

@app.get("/api/learned")
async def api_learned():
    return agent.learned_status()


@app.post("/api/learned/decide")
async def api_learned_decide(body: dict):
    decisions = body.get("decisions") or []
    if not isinstance(decisions, list) or len(decisions) > 500:
        raise HTTPException(400, "decisions must be a list")
    return agent.apply_learned_decisions(decisions)


# ── tool_notes.md: the human gate (activate / veto pending tool quirks) ───
# Same mechanics as learned.md, different file: verified tool/API quirks with
# a fix + last-verified date + recheck rule. Inert until the Boss activates
# them; only `## active` is injected into the system prompt.

@app.get("/api/tool_notes")
async def api_tool_notes():
    return agent.tool_notes_status()


@app.post("/api/tool_notes/decide")
async def api_tool_notes_decide(body: dict):
    decisions = body.get("decisions") or []
    if not isinstance(decisions, list) or len(decisions) > 500:
        raise HTTPException(400, "decisions must be a list")
    return agent.apply_tool_notes_decisions(decisions)


# ── local task store + calendar (option A — the alarm-clock layer) ───
# The sidebar Today zone (step 4) reads/writes these. due/start are ISO 8601
# with offset; the reminder poller toasts at the exact moment.

@app.get("/api/tasks")
async def api_tasks(include_done: bool = False, max_results: int = 50):
    return {"tasks": taskstore.list_tasks(include_done, max_results)}


@app.post("/api/tasks")
async def api_tasks_add(body: dict):
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    try:
        return taskstore.add_task(title, body.get("due") or "",
                                  body.get("note") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tasks/{tid}/toggle")
async def api_tasks_toggle(tid: int, body: dict | None = None):
    done = bool((body or {}).get("done", True))
    t = taskstore.toggle_task(tid, done)
    if t is None:
        raise HTTPException(404, "no such task")
    return t


@app.post("/api/tasks/{tid}/update")
async def api_tasks_update(tid: int, body: dict | None = None):
    """Edit a task (title / note / due). Only provided fields are touched —
    the UI edit modal sends all three, but partial updates stay legal."""
    body = body or {}
    try:
        t = taskstore.update_task(tid, body.get("title"), body.get("note"),
                                  body.get("due"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if t is None:
        raise HTTPException(404, "no such task")
    return t


@app.post("/api/tasks/{tid}/delete")
async def api_tasks_delete(tid: int):
    if not taskstore.delete_task(tid):
        raise HTTPException(404, "no such task")
    return {"ok": True}


@app.get("/api/events")
async def api_events(limit: int = 50, include_done: bool = False):
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    # window starts at local midnight, not `now` — an event that already
    # happened today still belongs on today's view (struck through when
    # done), otherwise "mark done = history" would vanish from the UI
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=14)).timestamp()
    return {"events": taskstore.list_events(start.timestamp(), end, limit,
                                            include_done)}


@app.post("/api/events")
async def api_events_add(body: dict):
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    try:
        return taskstore.add_event(title, body.get("start") or "",
                                   body.get("end") or "", body.get("note") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/events/{eid}/toggle")
async def api_events_toggle(eid: int, body: dict | None = None):
    done = bool((body or {}).get("done", True))
    e = taskstore.toggle_event(eid, done)
    if e is None:
        raise HTTPException(404, "no such event")
    return e


@app.post("/api/events/{eid}/delete")
async def api_events_delete(eid: int):
    if not taskstore.delete_event(eid):
        raise HTTPException(404, "no such event")
    return {"ok": True}


@app.get("/api/history")
async def api_history(session_id: str = Query(...),
                      limit: int = Query(default=200, ge=0, le=200),
                      before_id: int = Query(default=0, ge=0)):
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(404, "unknown session")
    run = tasks.get(session_id)
    active = run is not None and run.active
    # Windowed history (chat-switch freeze, 2026-09-28): a switch used to
    # pull the last-200 payload — 1.4–2.9 MB on long chats, parsed
    # synchronously in the tab. The UI only renders a window (recent 20)
    # and backfills 12-msg chunks behind "Load earlier", so the server now
    # ships exactly that: the newest `limit` rows (older than `before_id`
    # when given) + hidden_count. Default (limit=200, no cursor) is the old
    # behavior; thinking_tail stays at the last 40 for full pulls, keeps
    # everything for small windows.
    msgs = db.list_messages(session_id, limit=limit,
                            thinking_tail=min(limit, 40), before_id=before_id)
    hidden_count = db.count_messages_before(
        session_id, msgs[0]["id"] if msgs else before_id)
    # Thinking tab rehydration: a windowed payload only carries its own
    # messages' thinking — the tab wants the recent 100 regardless, so it
    # ships as its own bounded tail (oldest → newest, count + byte capped).
    thinking_recent = db.recent_thinking(session_id)
    # stateful resume available? A saved run_state means the last run ended
    # without a final summary (a finished run clears it in agent.py, and a
    # consumed resume clears it too) — so presence IS the "needs continue"
    # flag, identical to the sidebar's needs_continue. The old extra
    # condition (last message must be the user's) diverged from that: a
    # hard stop finalizes the in-flight draft as an EMPTY assistant row, so
    # the last message was assistant and this flag said false while the
    # sidebar showed the continue symbol — the in-chat ↻ chip never appeared
    # for exactly the chats the symbol promised.
    run_state = db.load_run_state(session_id)
    resumable = run_state is not None
    # the run's original goal, authoritative from the run_state — for long
    # runs it's OLDER than the window, so `messages` can't carry it
    # (keeps the job label honest after a reattach)
    run_goal = (run_state or {}).get("goal") or None
    # the in-flight turn's durable snapshot (draft row, refreshed at every
    # tool boundary + every ~2s of typing): the UI renders it on reattach,
    # so intermediate text / the in-flight answer — which never exist as
    # events — survive a chat switch. Excluded from `messages` (finished
    # history) but shipped here while the run is live. Its TOOL parts are
    # capped server-side (last 25, see get_draft_message) — the full
    # timeline lives in the events DB, the dropped count rides back as
    # draft_omitted_tools.
    draft = db.get_draft_message(session_id) if active else None
    # stream resume point: the last frame seq the snapshot already covers —
    # render the snapshot, then stream from here: the in-flight text
    # continues with no gap and no double render
    draft_seq = int(draft.get("flushed_seq") or 0) if draft else 0
    if active and not draft_seq and run is not None:
        draft_seq = run.start_seq
    draft_omitted = int(draft.pop("omitted_tool_parts", 0)) if draft else 0
    return {
        "session_id": session_id,
        "messages": msgs,
        # Live truth from the job registry, not the DB flag — a flag left
        # behind by a dead server must not look like work that is ongoing.
        "processing": active,
        # seq boundary of the in-flight run — the client replays events_log from
        # here, so re-attaching shows THIS run's work (small + correct) instead
        # of the session's oldest 5000 events (which belong to finished runs)
        "run_start_seq": (run.start_seq if active else 0),
        "draft": draft,
        "draft_seq": draft_seq,
        "draft_omitted_tools": draft_omitted,
        "queued": tasks.queue_count(session_id),
        "mode": sess.get("mode") or "act",
        "roast": sess.get("roast") or "chill",
        "resumable": resumable,
        "run_goal": run_goal,
        "hidden_count": hidden_count,
        "thinking_recent": thinking_recent,
    }


TREE_SKIP = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


@app.get("/api/tree")
async def api_tree(path: str = "", hidden: int = 0, dirs: int = 0):
    """One directory level under the allowed root (right-panel Files tab).
    `hidden=1` includes dot-dirs and TREE_SKIP entries (user's explicit ask).
    `dirs=1` lists folders only (working-folder picker)."""
    base = settings.root_dir
    p = base if not (path or "").strip() else Path(path)
    if not p.is_absolute():
        p = base / p
    rp = p.resolve()
    # the Files tab can browse ANY existing dir (the chat may be pointed at a
    # working folder outside the agent root)
    if not rp.is_dir():
        raise HTTPException(404, "not a directory")
    try:
        items = sorted(rp.iterdir(),
                       key=lambda q: (not q.is_dir(), q.name.lower()))
    except OSError:
        raise HTTPException(403, "cannot read directory")
    entries = []
    for q in items[:400]:
        if not hidden and (q.name in TREE_SKIP or q.name.startswith(".")):
            continue
        try:
            is_dir = q.is_dir()
            size = 0 if is_dir else q.stat().st_size
        except OSError:
            continue
        entries.append({"name": q.name, "path": str(q), "is_dir": is_dir,
                        "size": size,
                        "kind": "dir" if is_dir else file_kind(q.name)})
    if dirs:
        entries = [e for e in entries if e["is_dir"]]
    return {"path": str(rp), "entries": entries}


@app.get("/api/sessions/{sid}/tool_log")
async def api_session_tool_log(sid: str):
    """Per-chat Terminal tab: persisted tool_end events (oldest → newest)."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    return {"session_id": sid, "events": db.list_tool_events(sid)}


# ── chat (SSE) / stop / approvals / logs ───────────────────────────

@app.post("/api/chat")
async def api_chat(body: dict):
    """Enqueue a background agent turn. Events stream from
    GET /api/sessions/{sid}/events — closing that stream (or the tab)
    does NOT stop the job."""
    sid = body.get("session_id")
    message = (body.get("message") or "").strip()
    files = body.get("files") or []
    # image-only sends are legal: the composer ships zero words + files,
    # so an empty message is fine as long as SOMETHING rides along
    if not sid or (not message and not files):
        raise HTTPException(400, "session_id and a message (or files) are required")
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "unknown session")
    # A busy session no longer 409s — the message joins the per-session
    # FIFO queue and runs the moment the current turn fully ends.
    run, queued = tasks.enqueue(sid, message, files,
                                body.get("mode") or "act",
                                body.get("roast") or "chill")
    if not (sess.get("title") or "").strip():
        # first message of a new chat → name it: derived phrase now,
        # model-generated short title a moment later (background).
        # Image-only sends have no words to derive from — fall back to
        # the file names so the chat still gets a sensible title.
        title_src = message or ", ".join(
            (f.get("original") or f.get("name") or "") for f in files[:4])
        asyncio.get_running_loop().create_task(agent.auto_title(sid, title_src))
    return {"ok": True, "task_id": run.task_id, "session_id": sid, "seq": 0,
            "queued": queued, "queued_count": tasks.queue_count(sid)}


@app.post("/api/chat/resume")
async def api_chat_resume(body: dict):
    """Resume an interrupted run from its saved run_state (crash/restart,
    max-turns stop, hard stop). Falls back to a plain fresh turn when no
    state was saved — the client can't tell the difference and doesn't care."""
    sid = body.get("session_id")
    message = (body.get("message") or "").strip()
    if not sid or not message:
        raise HTTPException(400, "session_id and message are required")
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(404, "unknown session")
    has_state = db.load_run_state(sid) is not None
    # a MANUAL resume is the user's explicit intent — it wins over BOTH
    # auto caps: the mid-stream LLM auto-resume budget (agent) and the
    # supervisor's stuck stop-resume budget (the budgets exist to stop
    # loops against a broken state, not to block the boss)
    agent._auto_resume_fires.pop(sid, None)
    supervisor.note_manual_resume(sid)
    run, queued = tasks.enqueue(sid, message, [],
                                body.get("mode") or "act",
                                body.get("roast") or "chill", resume=has_state)
    return {"ok": True, "task_id": run.task_id, "session_id": sid, "seq": 0,
            "resumed": has_state, "queued": queued,
            "queued_count": tasks.queue_count(sid)}


@app.post("/api/sessions/{sid}/abandon")
async def api_session_abandon(sid: str, body: dict):
    """Drop the unfinished task: clear the saved run_state so the resume
    affordances (composer chip, sidebar continue symbol) disappear. The
    chat and its history stay untouched — the next message is a plain
    fresh turn. Refuses while a run is live (stop it first)."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    run = tasks.get(sid)
    if run is not None and run.active:
        raise HTTPException(409, "a run is in flight — stop it first")
    db.clear_run_state(sid)
    return {"ok": True}


@app.get("/api/sessions/{sid}/events_log")
async def api_session_events_log(sid: str, after: int = Query(default=0),
                                 limit: int = Query(default=5000, le=20000)):
    """Durable structural events for one session (oldest → newest), from the
    DB — NOT the in-memory ring. The client re-attaches to an in-flight
    run with this: the ring only holds the tail (maxlen 4000 frames), but
    the events table holds every structural event + thinking burst the run
    ever produced, so early progress survives a chat switch no matter how
    long the run is. `thinking_burst_id` dedupe rows are excluded."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    evs = [e for e in db.list_events(sid, after=after, limit=limit)
           if e["event"] != "thinking_burst_id"]
    return {"events": evs}


@app.get("/api/sessions/{sid}/events")
async def api_session_events(sid: str, after: int = Query(default=0)):
    """SSE stream for one session: replays buffered frames after `after`,
    then follows the live task until it ends. Idle session → empty stream."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")

    async def gen():
        async for frame in tasks.events(sid, after=after):
            yield frame

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/sessions/{sid}/queue")
async def api_session_queue(sid: str):
    """The session's queued messages (FIFO order) — the client's queue chip
    and drain both treat this as authoritative."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    q = tasks.queues.get(sid) or deque()
    return {"queued": [{"text": e["message"], "files": e.get("files") or []}
                       for e in q]}


@app.post("/api/sessions/{sid}/queue/remove")
async def api_session_queue_remove(sid: str, body: dict):
    """Cancel one queued message by position (the client's ✕ on a queued
    turn)."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    ok = tasks.queue_remove(sid, int(body.get("index") or 0))
    if not ok:
        raise HTTPException(404, "nothing to remove at that index")
    return {"ok": True, "queued": tasks.queue_count(sid)}


@app.post("/api/sessions/{sid}/queue/update")
async def api_session_queue_update(sid: str, body: dict):
    """Edit one queued message by position (the tray's ✎ inline box).
    The message hasn't run yet, so the edit is safe — the drain pops by
    position. If the index is already gone (the item drained between
    load and save), return `drained` so the client can offer the text
    back as a composer draft instead of silently losing the edit."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    index = int(body.get("index") or 0)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "no text to save")
    if tasks.queue_update(sid, index, text):
        return {"ok": True, "queued": tasks.queue_count(sid)}
    return {"ok": False, "drained": True}


@app.post("/api/sessions/{sid}/queue/clear")
async def api_session_queue_clear(sid: str):
    """Drop EVERY queued message for this session (the tray's
    "clear all"). Idempotent — an empty queue is a no-op."""
    if not db.get_session(sid):
        raise HTTPException(404, "unknown session")
    n = tasks.queue_clear(sid)
    return {"ok": True, "cleared": n, "queued": tasks.queue_count(sid)}


@app.post("/api/chat/stop")
async def api_chat_stop(body: dict):
    sid = body.get("session_id", "")
    run = tasks.get(sid) if sid else None
    stopped = bool(run is not None and run.active)
    if stopped:
        request_stop(sid)          # belt: cooperative checks in the agent
        tasks.cancel(sid)          # braces: cancel the run task itself — the
                                   # CancelledError lands wherever the turn is
                                   # awaiting (model stream, running command,
                                   # approval, compaction) and ends it now;
                                   # the `stopped` event follows on the stream
        # Park the saved run_state as DELIBERATELY stopped: the row stays
        # (the ↻ chip and the sidebar triangle stay — the task is a pause,
        # not a discard), but boot auto-resume skips it. Only ✕ Start fresh
        # (abandon) removes the row; a manual ↻ or a queued continuation
        # clears the flag on its first checkpoint.
        try:
            db.set_run_state_stopped(sid, True)
        except Exception:  # noqa: BLE001 — parking must never kill the stop
            pass
    # Stop = stop everything: queued follow-ups are dropped too (the drain
    # in TaskManager._pump skips stopped runs, so nothing auto-fires after).
    queued_dropped = tasks.queue_count(sid)
    tasks.queues.pop(sid, None)
    try:
        db.queue_clear(sid)  # stop = drop the queue FOR GOOD (durable too)
    except Exception:  # noqa: BLE001
        pass
    if not stopped and sid and db.get_session(sid):
        # No live job (server restarted or the turn already ended) — drop
        # any stale busy flag and tell the client there is nothing to stop.
        db.set_processing(sid, False)
    return {"ok": True, "stopped": stopped, "queued_dropped": queued_dropped}


@app.post("/api/approvals/{aid}")
async def api_approval(aid: str, body: dict):
    fut = agent.approvals.pop(aid, None)
    if fut is None or fut.done():
        raise HTTPException(404, "approval not found (answered or expired)")
    decision = body.get("decision", "denied")
    fut.set_result(decision)
    log("info", f"approval {aid[:8]} → {decision}")
    return {"ok": True}


@app.post("/api/questions/{qid}")
async def api_question(qid: str, body: dict):
    fut = agent.questions.pop(qid, None)
    if fut is None or fut.done():
        raise HTTPException(404, "question not found (answered or expired)")
    try:
        choice = int(body.get("choice", 0))
    except (TypeError, ValueError):
        choice = 0
    fut.set_result(choice)
    log("info", f"question {qid[:8]} → choice {choice}")
    return {"ok": True}


@app.get("/api/logs")
async def api_logs():
    return {"logs": list(logs)}


@app.get("/api/telegram/status")
async def api_tg_status():
    """Bridge liveness for the UI: is the user-mode client and the bot
    channel connected? (The UI polled this before the endpoint existed —
    it 404'd; now it tells the boss whether his Telegram is actually
    listening instead of guessing.)"""
    from . import telegram as tg
    return tg.status()


# ── uploads / previews / files / link verification ─────────────────

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 25_000_000:
        raise HTTPException(413, "file too large (25 MB max)")
    safe = re.sub(r"[^\w.\-]+", "_", file.filename or "file")[:80]
    stored = f"{uuid.uuid4().hex[:12]}_{safe}"
    (settings.uploads_dir / stored).write_bytes(raw)
    log("info", f"upload: {stored} ({len(raw)} bytes)")
    return {"name": stored, "url": f"/uploads/{stored}",
            "original": file.filename or stored}


@app.get("/uploads/{name}")
async def upload_file(name: str, inline: int = 0):
    p = (settings.uploads_dir / name)
    rp = p.resolve()
    if not p.is_file() or settings.uploads_dir not in rp.parents:
        raise HTTPException(404, "not found")
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if inline and (media.startswith("image/") or media in ("text/html", "application/pdf")):
        return FileResponse(rp, media_type=media)
    return FileResponse(rp, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{rp.name}"'})


@app.get("/preview/{token}")
async def preview(token: str):
    p = decode_token(token)
    if not p or not p.is_file():
        raise HTTPException(404, "not found")
    kind = file_kind(p.name)
    if kind == "html":
        return FileResponse(p, media_type="text/html; charset=utf-8")
    if kind == "ipynb":
        # no byte cap here — slicing a notebook mid-JSON breaks the parse
        # (embedded base64 images make big notebooks large by design)
        body = notebook_to_html(p.read_text("utf-8", errors="replace"))
        css = """
<style>
body{margin:0;padding:16px;font-family:system-ui,Segoe UI,sans-serif;
     font-size:13px;color:#1c2330;background:#fff}
.nb-kernel{color:#6b7484;font-size:12px;margin:0 0 12px}
.nb-cell{margin:0 0 12px;border:1px solid #e3e8f0;border-radius:8px;
         padding:10px 12px;background:#fafbfd}
.nb-code{border-left:3px solid #4c7dd0;padding-left:10px}
.nb-ec{display:inline-block;min-width:52px;color:#6b7484;font-size:11px}
pre{margin:0;font-family:Consolas,Menlo,monospace;font-size:12.5px;
    white-space:pre-wrap;word-break:break-word;line-height:1.5}
.cl{display:flex;align-items:flex-start}
.ln{flex:0 0 24px;margin-right:10px;padding-right:6px;
    border-right:1px solid #d5dce8;text-align:right;color:#a3adbf;
    user-select:none;font-size:11px}
.ct{white-space:pre-wrap;word-break:break-word;flex:1}
.nb-md{line-height:1.55;font-size:13px}
.nb-md h1,.nb-md h2,.nb-md h3,.nb-md h4{margin:.4em 0;line-height:1.3}
.nb-md h1{font-size:1.5em;border-bottom:1px solid #e3e8f0;padding-bottom:.2em}
.nb-md h2{font-size:1.3em}
.nb-md h3{font-size:1.15em}
.nb-md p{margin:.45em 0}
.nb-md ul,.nb-md ol{margin:.45em 0;padding-left:1.6em}
.nb-md blockquote{margin:.45em 0;padding:.2em .9em;border-left:3px solid #c9d2e0;
                   color:#5a6577;background:#f4f6fa;border-radius:0 6px 6px 0}
.nb-md code{font-family:Consolas,Menlo,monospace;font-size:12px;background:#eef1f6;
            padding:1px 5px;border-radius:4px}
.nb-md pre{background:#f4f6fa;border-radius:6px;padding:8px 10px;overflow-x:auto;
           white-space:pre}
.nb-md pre code{background:none;padding:0}
.nb-md a{color:#4c7dd0}
.nb-md hr{border:none;border-top:1px solid #e3e8f0;margin:.8em 0}
.nb-md del{color:#8b93a3}
.nb-md img{max-width:100%;height:auto;border-radius:4px;margin:.3em 0}
.nb-md table{border-collapse:collapse;margin:.6em 0;font-size:12.5px}
.nb-md th,.nb-md td{border:1px solid #d5dce8;padding:4px 10px}
.nb-md th{background:#eef1f6}
.nb-md tbody tr:nth-child(even){background:#f7f9fc}
.nb-out{margin-top:8px;border-top:1px dashed #d5dce8;padding-top:8px;
        color:#2c3547;background:#f4f6fa;border-radius:6px;padding:8px}
.nb-out img{max-width:100%;height:auto;border-radius:4px}
.nb-err{color:#b3261e;background:#fdecea}
.nb-raw{opacity:.75}
</style>"""
        return Response("<!doctype html><meta charset='utf-8'>" + css + body,
                        media_type="text/html; charset=utf-8")
    if kind == "image":
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0]
                            or "application/octet-stream")
    return Response(p.read_text("utf-8", errors="replace")[:500_000],
                    media_type="text/plain; charset=utf-8")


def _resolve_any(path: str, must: str = "file") -> Path:
    p = Path((path or "").strip())
    if not p.is_absolute():
        p = settings.root_dir / p
    rp = p.resolve()
    if must == "any":
        if not rp.exists():
            raise HTTPException(404, "not found")
    elif must == "dir":
        if not rp.is_dir():
            raise HTTPException(404, "not a directory")
    elif not rp.is_file():
        raise HTTPException(404, "not found")
    return rp


def _resolve_dir(path: str) -> Path:
    p = Path((path or "").strip())
    if not p.is_absolute():
        p = settings.root_dir / p
    rp = p.resolve()
    if not rp.is_dir():
        raise HTTPException(404, "not a directory")
    return rp


@app.get("/api/files/raw")
async def files_raw(path: str = Query(...)):
    p = _resolve_any(path)
    text = p.read_text("utf-8", errors="replace")
    truncated = len(text) > 400_000
    return {"path": str(p), "name": p.name, "kind": file_kind(p.name),
            "size": p.stat().st_size, "truncated": truncated,
            "text": text[:400_000]}


@app.get("/api/files/blob")
async def files_blob(path: str = Query(...)):
    p = _resolve_any(path)
    return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0]
                        or "application/octet-stream")


@app.get("/api/files/download")
async def files_download(path: str = Query(...)):
    p = _resolve_any(path)
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{p.name}"'})


@app.get("/api/files/preview_url")
async def files_preview_url(path: str = Query(...)):
    p = _resolve_any(path)
    return {"preview_url": "/preview/" + preview_token(p), "kind": file_kind(p.name)}


def _reveal_foreground_windows(rp: Path) -> None:
    """Windows: spawn File Explorer and force the new window to the
    foreground. Three layers of Windows fought this feature:
    1. Bare `explorer.exe` launches UNFOCUSED (lands behind the browser).
    2. SetForegroundWindow from a background process is silently refused
       by the foreground lock (Win10/11) — bypassed with a brief simulated
       Alt key press (SendInput) that lifts the lock for this process.
    3. `explorer.exe` often exits after handing the window to another
       process, so the window is found by TITLE, not process handle —
       and the title match must be exact ("name" or "name - File
       Explorer"), because a substring match once grabbed the
       "DesktopWindowXamlSource" overlay and stole focus away.
    If the focus attempt ever fails, the trace lands in
    data/_reveal_debug.log for diagnosis."""
    target = (rp.parent if rp.is_file() else rp).name
    cmd = (["explorer", "/select", str(rp)] if rp.is_file()
           else ["explorer", str(rp)])
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _bring_to_front() -> None:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        dbg = []

        def _log(msg) -> None:
            dbg.append(f"{time.strftime('%H:%M:%S')} {msg}")

        def _fg_title() -> str:
            h = user32.GetForegroundWindow()
            if not h:
                return "<none>"
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            return buf.value

        def _alt_tap() -> bool:
            # Alt key down+up: unblocks the foreground lock for this process.
            # INPUT is a union — the struct must be as big as the LARGEST
            # member (MOUSEINPUT, 28 bytes) or SendInput returns 0 with
            # ERROR_INVALID_PARAMETER (87). The v2 version declared only
            # the 24-byte keyboard variant, so every tap was rejected and
            # the lock was never lifted (see data/_reveal_debug.log).
            class _KEYBDINPUT(ctypes.Structure):
                _fields_ = [("wVk", ctypes.wintypes.WORD),
                            ("wScan", ctypes.wintypes.WORD),
                            ("dwFlags", ctypes.wintypes.DWORD),
                            ("time", ctypes.wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            class _MOUSEINPUT(ctypes.Structure):
                _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                            ("mouseData", ctypes.wintypes.DWORD),
                            ("dwFlags", ctypes.wintypes.DWORD),
                            ("time", ctypes.wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t)]

            class _INPUT(ctypes.Structure):
                class _Union(ctypes.Union):
                    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]
                _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _Union)]

            def _key(flags):
                inp = _INPUT(type=1)
                inp.u.ki = _KEYBDINPUT(wVk=0x12, dwFlags=flags)
                return inp

            try:
                for flags in (0, 2):  # KEYEVENTF_KEYDOWN=0, KEYUP=2
                    inp = _key(flags)
                    sent = user32.SendInput(
                        1, ctypes.byref(inp),
                        ctypes.sizeof(_INPUT))  # 40 on x64
                    if sent != 1:
                        _log(f"SendInput failed ({sent}, err "
                             f"{ctypes.GetLastError()})")
                        return False
                return True
            except Exception as e:
                _log(f"SendInput raised {e!r}")
                return False

        def _focus(hwnd) -> None:
            _log(f"candidate hwnd={hwnd} visible={user32.IsWindowVisible(hwnd)} "
                 f"fg_before=[{_fg_title()}]")
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            _alt_tap()
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            _log(f"fg_after=[{_fg_title()}] (target hwnd={hwnd})")

        def _match(title: str) -> bool:
            # EXACT match on the window title, or the title minus Explorer's
            # " - File Explorer" suffix. A substring match was the v3 bug:
            # target "aml" also matched "DesktopWindowXamlSource" (a XAML
            # overlay window) and SetForegroundWindow on THAT stole focus
            # away from the already-focused Explorer window.
            t = title.strip()
            return t == target or t == f"{target} - File Explorer"

        def _cb(hwnd, _lparam) -> bool:
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            buf = ctypes.create_unicode_buffer(
                user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, buf, len(buf))
            if _match(buf.value):
                _focus(hwnd)
                return False  # stop the enumeration — don't touch others
            return True

        # Keep the wrapped callback alive for the whole poll loop — a bare
        # lambda gets garbage-collected mid-EnumWindows and crashes.
        proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)(_cb)
        _log(f"start target={target!r}")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            user32.EnumWindows(proc, 0)
            if dbg and "fg_after" in dbg[-1]:
                break
            time.sleep(0.1)
        _log(f"end; no window found" if "fg_after" not in "".join(dbg)
             else "end")
        try:
            p = APP_ROOT / "data" / "_reveal_debug.log"
            p.write_text("\n".join(dbg) + "\n", "utf-8")
        except Exception:
            pass

    threading.Thread(target=_bring_to_front, daemon=True).start()


@app.get("/api/files/reveal")
async def files_reveal(path: str = Query(...)):
    """Reveal this path in the OS file manager (the user's own click — no
    approval gate, same contract as /api/terminal/run). Windows: File
    Explorer — files get selected in their parent, folders open themselves,
    and the window is forced to the foreground (SetForegroundWindow)."""
    rp = _resolve_any(path, must="any")
    if os.name == "nt":
        _reveal_foreground_windows(rp)
    elif sys.platform == "darwin":
        cmd = ["open", str(rp if rp.is_dir() else rp.parent)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        cmd = ["xdg-open", str(rp if rp.is_dir() else rp.parent)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"revealed": str(rp)}


@app.post("/api/terminal/run")
async def api_terminal_run(body: dict):
    """User's own terminal (right panel → lower pane → Terminal): runs a
    PowerShell command in the chat's working folder. No approval gate —
    this is the user's own hands, not the model's."""
    command = (body.get("command") or "").strip()
    if not command:
        raise HTTPException(400, "empty command")
    if len(command) > 4000:
        raise HTTPException(400, "command too long (4000 chars max)")
    sess = db.get_session(body.get("session_id") or "")
    cwd = None
    if sess and sess.get("cwd"):
        try:
            cwd = _resolve_dir(str(sess["cwd"]))
        except HTTPException:
            cwd = None
    try:
        out = await exec_command(command, cwd)
    except Exception as e:  # ToolError (timeout, missing powershell), …
        return {"output": str(e), "ok": False}
    m = re.search(r"\[exit code (\d+)\]\s*$", out)
    return {"output": out, "ok": m is None or m.group(1) == "0"}


@app.post("/api/files/create")
async def files_create(body: dict):
    """Create a folder or empty file in a directory under the root
    (Files tab: 📁＋ / 📄＋)."""
    kind = body.get("kind")
    if kind not in ("dir", "file"):
        raise HTTPException(400, "kind must be 'dir' or 'file'")
    parent = _resolve_dir(str(body.get("path") or ""))
    name = str(body.get("name") or "").strip()
    if (not name or name in (".", "..") or len(name) > 255
            or any(sep in name for sep in filter(None, (os.sep, os.altsep, "/")))
            or re.search(r"[:?*<>|]", name)):
        raise HTTPException(400, "bad name")
    target = (parent / name).resolve()
    if not _under_root(target):
        raise HTTPException(400, "outside the allowed root")
    if target.exists():
        raise HTTPException(409, "already exists")
    try:
        target.mkdir() if kind == "dir" else target.touch()
    except OSError as e:
        raise HTTPException(500, f"could not create: {e}")
    return {"path": str(target)}


@app.post("/api/files/rename")
async def files_rename(body: dict):
    """Rename a file or folder under the root (Files tab: drag a row onto
    itself or ✏)."""
    src = Path(str(body.get("path") or "").strip())
    if not src.is_absolute():
        src = settings.root_dir / src
    src = src.resolve()
    if not _under_root(src) or not src.exists():
        raise HTTPException(404, "not found")
    name = str(body.get("name") or "").strip()
    if (not name or name in (".", "..") or len(name) > 255
            or any(sep in name for sep in filter(None, (os.sep, os.altsep, "/")))
            or re.search(r"[:?*<>|]", name)):
        raise HTTPException(400, "bad name")
    target = (src.parent / name).resolve()
    if not _under_root(target):
        raise HTTPException(400, "outside the allowed root")
    if target.exists():
        raise HTTPException(409, "a file or folder with that name already exists")
    try:
        src.rename(target)
    except OSError as e:
        raise HTTPException(500, f"could not rename: {e}")
    log("info", f"rename: {src.name} -> {name} in {src.parent}")
    return {"path": str(target)}


@app.post("/api/files/move")
async def files_move(body: dict):
    """Move a file or folder into another directory under the root
    (Files tab: drag a row onto a folder row)."""
    src = Path(str(body.get("path") or "").strip())
    if not src.is_absolute():
        src = settings.root_dir / src
    src = src.resolve()
    if not _under_root(src) or not src.exists():
        raise HTTPException(404, "not found")
    dest_dir = _resolve_dir(str(body.get("dest") or ""))
    # Only a *folder* can be dropped into itself/a descendant. A file may
    # move to any dir (even a sibling of its current parent).
    if src.is_dir() and (src == dest_dir or dest_dir.is_relative_to(src)):
        raise HTTPException(400, "cannot move a folder into itself")
    name = str(body.get("name") or "").strip() or src.name
    if (not name or name in (".", "..") or len(name) > 255
            or any(sep in name for sep in filter(None, (os.sep, os.altsep, "/")))
            or re.search(r"[:?*<>|]", name)):
        raise HTTPException(400, "bad name")
    target = (dest_dir / name).resolve()
    if not _under_root(target):
        raise HTTPException(400, "outside the allowed root")
    if target.exists():
        raise HTTPException(409, "a file or folder with that name already exists in the destination")
    try:
        shutil.move(str(src), str(target))
    except OSError as e:
        raise HTTPException(500, f"could not move: {e}")
    log("info", f"move: {src.name} -> {dest_dir}")
    return {"path": str(target)}


@app.post("/api/files/delete")
async def files_delete(body: dict):
    """Delete a file or folder under the root (Files tab: 🗑)."""
    p = Path(str(body.get("path") or "").strip())
    if not p.is_absolute():
        p = settings.root_dir / p
    p = p.resolve()
    if not _under_root(p) or not p.exists():
        raise HTTPException(404, "not found")
    if p == settings.root_dir.resolve():
        raise HTTPException(400, "cannot delete the workspace root")
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as e:
        raise HTTPException(500, f"could not delete: {e}")
    log("info", f"delete: {p}")
    return {"deleted": str(p)}


@app.post("/api/fs/upload")
async def fs_upload(path: str = Query(...), file: UploadFile = File(...)):
    """Save an uploaded file into a directory under the root (Files tab
    drag & drop). Existing names are skipped, never overwritten."""
    d = _resolve_dir(path)
    name = Path(file.filename or "").name or "upload"
    target = (d / name).resolve()
    if not _under_root(target):
        raise HTTPException(400, "outside the allowed root")
    if target.exists():
        return {"path": str(target), "skipped": True}
    raw = await file.read()
    if len(raw) > 25_000_000:
        raise HTTPException(413, "file too large (25 MB max)")
    try:
        target.write_bytes(raw)
    except OSError as e:
        raise HTTPException(500, f"could not save: {e}")
    log("info", f"drop-upload: {target.name} ({len(raw)} bytes) -> {d}")
    return {"path": str(target), "skipped": False}


@app.post("/api/verify_links")
async def api_verify_links(body: dict):
    links = [str(u) for u in (body.get("links") or [])][:40]
    sid = body.get("session_id") or ""
    sources = " ".join(agent.session_sources.get(sid, [])).lower()

    async def check(u: str) -> bool:
        low = u.lower()
        if low.startswith("mailto:"):
            return low[7:].split("?")[0] in sources
        if not low.startswith(("http://", "https://")):
            return True
        try:
            async with httpx.AsyncClient(timeout=6, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = await c.get(u)
            return r.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    results = await asyncio.gather(*(check(u) for u in links))
    return {"ok": dict(zip(links, (bool(r) for r in results)))}
