"""SQLite storage: workspaces, sessions, messages."""
from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import threading
import time
import uuid

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                created_at REAL, updated_at REAL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace_id TEXT, title TEXT,
                created_at REAL, updated_at REAL,
                processing INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                files TEXT NOT NULL DEFAULT '[]', rating TEXT, created_at REAL);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                seq INTEGER NOT NULL, event TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}', ts REAL);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
            CREATE TABLE IF NOT EXISTS trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                ts REAL NOT NULL, goal TEXT NOT NULL, steps TEXT NOT NULL,
                claims TEXT NOT NULL, verified TEXT NOT NULL,
                pattern TEXT, outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_state (
                session_id TEXT PRIMARY KEY, turn INTEGER NOT NULL,
                plan TEXT NOT NULL, done TEXT NOT NULL, messages TEXT NOT NULL,
                goal TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                stopped INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                due_iso TEXT, due_utc REAL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_utc);
            CREATE TABLE IF NOT EXISTS calendar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                start_iso TEXT NOT NULL, start_utc REAL NOT NULL,
                end_utc REAL,
                created_at REAL NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar(start_utc);
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                pos INTEGER NOT NULL,
                message TEXT NOT NULL,
                files TEXT NOT NULL DEFAULT '[]',
                mode TEXT NOT NULL DEFAULT 'act',
                roast TEXT NOT NULL DEFAULT 'chill',
                resume INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_queue_session ON queue(session_id, pos);
            """
        )
        # WAL: crash-safe writes and safe to online-backup (tools/backup.py)
        # while the server is running; NORMAL sync keeps it fast on local disk
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        # migration: pre-multi-instance DBs have no `thinking` column
        try:
            _conn.execute("ALTER TABLE messages ADD COLUMN thinking TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-Plan/Act DBs have no per-chat mode on sessions
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'act'")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: per-message mode — the mode each assistant turn ran in,
        # so history can tint the final-answer bubble (plan=yellow, act=green)
        try:
            _conn.execute(
                "ALTER TABLE messages ADD COLUMN mode TEXT NOT NULL DEFAULT 'act'")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-Files-tab-folder DBs have no per-chat working dir
        try:
            _conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-roast-level DBs have no per-chat roast level
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN roast TEXT NOT NULL "
                "DEFAULT 'chill'")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: event timestamps — persisted rows had no wall-clock,
        # so "when was the LLM slowest" couldn't be answered; old rows
        # stay NULL (backfilling would be guesswork)
        try:
            _conn.execute("ALTER TABLE events ADD COLUMN ts REAL")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: per-chat last-done stamp — the sidebar's steady green
        # "task done" LED is seeded from this on every page load (before
        # this it lived only in JS memory, so a refresh wiped it)
        try:
            _conn.execute("ALTER TABLE sessions ADD COLUMN last_done_at REAL")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: per-chat last-viewed stamp — the done-LED is "finished
        # AND not opened since", so we need the moment the user last opened
        # the chat to compare against last_done_at
        try:
            _conn.execute("ALTER TABLE sessions ADD COLUMN last_viewed_at REAL")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: sidebar pinning — pinned chats float to the top of the
        # list and are exempt from auto-archiving
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN pinned INTEGER "
                "NOT NULL DEFAULT 0")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: calendar done flag — completed events stay in the
        # store as history (strike-through row) instead of being deleted;
        # old rows default to open (they either still fire or are stale)
        try:
            _conn.execute(
                "ALTER TABLE calendar ADD COLUMN done INTEGER "
                "NOT NULL DEFAULT 0")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # one-shot backfill: stamp every chat that has a terminal event so
        # the green done-LEDs exist on the first load after this ships
        rows = _conn.execute(
            "SELECT session_id FROM events "
            "WHERE event IN ('done','error','stopped') GROUP BY session_id"
        ).fetchall()
        if rows:
            now = time.time()
            for r in rows:
                _conn.execute(
                    "UPDATE sessions SET last_done_at=? "
                    "WHERE id=? AND last_done_at IS NULL",
                    (now, r["session_id"]))
            _conn.commit()
        # migration: per-chat TL;DR flag count — every FINAL answer that
        # should carry a TL;DR but doesn't ticks this up (deterministic
        # check in agent._flag_tldr); the sidebar shows the ⚑ badge so a
        # chat that keeps skipping the rule is visible at a glance
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN tldr_flags "
                "INTEGER NOT NULL DEFAULT 0")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-timeline DBs have no per-message parts (natural-order
        # text segments + tool runs of one turn, so history renders like live)
        try:
            _conn.execute(
                "ALTER TABLE messages ADD COLUMN parts TEXT NOT NULL DEFAULT '[]'")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-draft-persistence DBs have no draft flag — an
        # in-flight assistant row (mid-task timeline, updated as the turn
        # runs) stays out of history until it is finalized or a restart
        # promotes it
        try:
            _conn.execute(
                "ALTER TABLE messages ADD COLUMN draft INTEGER NOT NULL DEFAULT 0")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-reattach-snapshot DBs have no flushed_seq — the
        # in-flight row stamps the last frame seq its snapshot already
        # covers, so a client that opens the chat mid-turn can render the
        # snapshot and stream from exactly there (no gap, no double text)
        try:
            _conn.execute("ALTER TABLE messages ADD COLUMN flushed_seq INTEGER")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-ui-snapshot DBs have no ui column — the in-flight
        # row carries the live dressing at snapshot time (avatar parking,
        # status chip label, live thinking hint) so a reattach restores it
        try:
            _conn.execute("ALTER TABLE messages ADD COLUMN ui TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-per-bubble-timestamp DBs have no answer_ts — the
        # moment the turn's FINAL answer was produced, so the final bubble
        # chips its own time on reload (not the turn's start time). Old
        # rows stay NULL → the UI falls back to the message's created_at
        try:
            _conn.execute("ALTER TABLE messages ADD COLUMN answer_ts REAL")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration (09-19): attachment preambles used to be saved INSIDE
        # the user text, so bubbles showed model-facing boilerplate
        _strip_legacy_attachment_preambles(_conn)
        # migration: per-chat activity phrase — a 3-7 word status line under
        # the sidebar title (what's being done / was just done), generated
        # after each turn instead of truncating raw message text
        try:
            _conn.execute("ALTER TABLE sessions ADD COLUMN summary TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-durable-queue DBs have no queue table at all
        # (CREATE TABLE IF NOT EXISTS inside executescript covers fresh
        # DBs; this covers the live one)
        try:
            _conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    pos INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    files TEXT NOT NULL DEFAULT '[]',
                    mode TEXT NOT NULL DEFAULT 'act',
                    roast TEXT NOT NULL DEFAULT 'chill',
                    resume INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_queue_session
                    ON queue(session_id, pos);
                """)
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-stop-parked DBs have no `stopped` flag on
        # run_state — a deliberate Stop used to be indistinguishable from
        # a crash at boot, so auto-resume resurrected stopped sessions.
        # stopped=1 = the boss hit Stop (parked: the ↻/△ affordances stay,
        # but boot auto-resume skips it until a manual ↻ or Start fresh)
        try:
            _conn.execute(
                "ALTER TABLE run_state ADD COLUMN stopped "
                "INTEGER NOT NULL DEFAULT 0")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-deleted-tracker DBs have no deleted_sessions table —
        # a 30-day tombstone of what got deleted (single chat, Clear all,
        # Clear archived) so "where did that chat go" has an answer
        try:
            _conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deleted_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT,
                    workspace_id TEXT,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    deleted_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'delete');
                """)
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-restore DBs have no message snapshot — the tombstone
        # now keeps the chat's rows so a delete can be undone (restore)
        try:
            _conn.execute(
                "ALTER TABLE deleted_sessions ADD COLUMN messages TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            _conn.execute(
                "ALTER TABLE deleted_sessions ADD COLUMN restored_at REAL")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        # migration: pre-context-meter DBs have no per-chat context token
        # count — the topbar pill shows fill vs the compaction trigger
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN ctx_tokens INTEGER")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # persisted compaction (2026-10-09): the LLM summary of all
            # messages OLDER than compacted_through_msg_id. The rebuild
            # loads [summary] + rows after the marker instead of re-running
            # the lossy summary every turn (compaction is the SOLE memory
            # manager — no fixed-row tail, no token-budget window).
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN compacted_summary TEXT")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            _conn.execute(
                "ALTER TABLE sessions ADD COLUMN compacted_through_msg_id INTEGER")
            _conn.commit()
        except sqlite3.OperationalError:
            pass
    return _conn


def _strip_legacy_attachment_preambles(conn: sqlite3.Connection) -> int:
    """Cut legacy 'Attached image/file: …' preambles out of stored user
    rows (they belong in the model request, not the bubble). Idempotent:
    only rows still carrying the "\\n\\nAttached " marker are touched, and
    new rows never contain it."""
    rows = conn.execute(
        "SELECT id, content FROM messages WHERE role='user' AND ("
        "content LIKE '%Attached image: %' OR content LIKE '%Attached file: %')"
    ).fetchall()
    n = 0
    for r in rows:
        cut = r["content"].split("\n\nAttached ", 1)[0].rstrip()
        if cut and cut != r["content"]:
            conn.execute("UPDATE messages SET content=? WHERE id=?",
                         (cut, r["id"]))
            n += 1
    if n:
        conn.commit()
    return n


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


# ── workspaces ──────────────────────────────────────────────────────

def list_workspaces() -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT * FROM workspaces ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_workspace(wid: str | None) -> dict | None:
    if not wid:
        return None
    with _lock:
        r = _db().execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
    return _row(r)


def add_workspace(path: str, name: str | None = None) -> dict:
    p = pathlib.Path(path)
    p = p.resolve() if p.is_absolute() else (settings.root_dir / p).resolve()
    if not p.is_dir():
        raise ValueError(f"not a directory: {path}")
    if p != settings.root_dir and settings.root_dir not in p.parents:
        raise ValueError(f"workspace must live under the root ({settings.root_dir})")
    now = time.time()
    wid = uuid.uuid4().hex
    with _lock:
        _db().execute(
            "INSERT INTO workspaces(id,name,path,created_at,updated_at) VALUES(?,?,?,?,?)",
            (wid, (name or p.name).strip() or p.name, str(p), now, now))
        _db().commit()
    return get_workspace(wid)


# ── sessions ────────────────────────────────────────────────────────

def list_sessions(workspace_id: str | None = None, general: bool = False) -> list[dict]:
    # `summary` (the sidebar activity phrase) is a plain column, so it
    # rides along with SELECT * — no extra scan needed.
    if general:
        sql = ("SELECT * FROM sessions WHERE workspace_id IS NULL "
               "ORDER BY pinned DESC, updated_at DESC")
        args: tuple = ()
    elif workspace_id:
        sql = ("SELECT * FROM sessions WHERE workspace_id=? "
               "ORDER BY pinned DESC, updated_at DESC")
        args = (workspace_id,)
    else:
        sql = "SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC"
        args = ()
    with _lock:
        rows = _db().execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_session(sid: str) -> dict | None:
    if not sid:
        return None
    with _lock:
        r = _db().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return _row(r)


def set_ctx_tokens(sid: str, n: int | None) -> None:
    """Persist the chat's last known context size (prompt tokens from the
    model's usage chunk, or the agent's estimate). The topbar context pill
    reads it on session load; None clears it (fresh chat)."""
    if not sid:
        return
    with _lock:
        _db().execute("UPDATE sessions SET ctx_tokens=? WHERE id=?",
                      (n, sid))
        _db().commit()


def set_compaction(sid: str, summary: str, through_msg_id: int) -> None:
    """Persist a compaction: everything in this session's message history
    OLDER than `through_msg_id` is represented by `summary` in the model's
    context. The rebuild loads [summary] + rows after the marker — the
    lossy summary runs once per growth past the trigger, not per turn.
    Rows themselves are never touched (search_transcript recall is intact)."""
    if not sid:
        return
    with _lock:
        _db().execute(
            "UPDATE sessions SET compacted_summary=?, "
            "compacted_through_msg_id=? WHERE id=?",
            (summary, through_msg_id, sid))
        _db().commit()


def create_session(workspace_id: str | None = None) -> dict:
    now = time.time()
    sid = uuid.uuid4().hex
    with _lock:
        _db().execute(
            "INSERT INTO sessions(id,workspace_id,title,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (sid, workspace_id or None, "", now, now))
        _db().commit()
    return get_session(sid)


def rename_session(sid: str, title: str) -> None:
    with _lock:
        _db().execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            ((title.strip()[:120] or None), time.time(), sid))
        _db().commit()


def bump_tldr_flags(sid: str) -> int:
    """Tick the chat's TL;DR flag count (one final answer shipped without a
    TL;DR). Returns the new count. The sidebar badge reads the column via
    list_sessions (SELECT *), so no extra endpoint is needed."""
    with _lock:
        _db().execute(
            "UPDATE sessions SET tldr_flags = tldr_flags + 1 WHERE id=?",
            (sid,))
        _db().commit()
        r = _db().execute(
            "SELECT tldr_flags FROM sessions WHERE id=?", (sid,)).fetchone()
    return int(r[0]) if (r is not None and r[0] is not None) else 0


def clear_tldr_flags(sid: str) -> int:
    """Reset the chat's TL;DR flag count to 0 — the counter is a debt, not a
    lifetime tally: the next rule-following final answer clears the badge.
    Returns the new count (0)."""
    with _lock:
        _db().execute(
            "UPDATE sessions SET tldr_flags = 0 WHERE id=?", (sid,))
        _db().commit()
        r = _db().execute(
            "SELECT tldr_flags FROM sessions WHERE id=?", (sid,)).fetchone()
    return int(r[0]) if (r is not None and r[0] is not None) else 0


def rename_session_if(sid: str, expected: str, title: str) -> bool:
    """Rename only if the title still equals `expected` — background
    auto-titling must not clobber a manual rename that landed meanwhile."""
    with _lock:
        r = _db().execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
        if r is None:
            return False
        if (r[0] or "") != (expected or ""):
            return False
        _db().execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            ((title.strip()[:120] or None), time.time(), sid))
        _db().commit()
    return True


def toggle_pinned(sid: str) -> bool:
    """Flip the sidebar pin; returns the new state (True = pinned)."""
    with _lock:
        r = _db().execute("SELECT pinned FROM sessions WHERE id=?", (sid,)).fetchone()
        if r is None:
            return False
        _db().execute("UPDATE sessions SET pinned=? WHERE id=?",
                      (0 if r[0] else 1, sid))
        _db().commit()
        return not r[0]


def set_session_mode(sid: str, mode: str) -> None:
    """Cline-style per-chat Plan/Act mode ('plan' | 'act')."""
    with _lock:
        _db().execute("UPDATE sessions SET mode=? WHERE id=?",
                      ("plan" if mode == "plan" else "act", sid))
        _db().commit()


ROAST_LEVELS = ("off", "chill", "full")


def set_session_roast(sid: str, level: str) -> None:
    """Per-chat roast level: 😴 off · 😏 chill · 🔥 full."""
    with _lock:
        _db().execute(
            "UPDATE sessions SET roast=? WHERE id=?",
            (level if level in ROAST_LEVELS else "chill", sid))
        _db().commit()


def set_session_cwd(sid: str, cwd: str) -> None:
    """Per-chat working folder (chosen in the Files tab); NULL = inherit."""
    with _lock:
        _db().execute("UPDATE sessions SET cwd=? WHERE id=?", (cwd, sid))
        _db().commit()


def touch_session(sid: str) -> None:
    with _lock:
        _db().execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        _db().commit()


def set_processing(sid: str, flag: bool) -> None:
    with _lock:
        _db().execute(
            "UPDATE sessions SET processing=?, updated_at=? WHERE id=?",
            (1 if flag else 0, time.time(), sid))
        _db().commit()


def mark_session_done(sid: str) -> None:
    """Stamp the chat's last finished-run time — the durable half of the
    steady green done-LED (the JS side only shows it while a run is
    in flight in memory)."""
    if not sid:
        return
    with _lock:
        _db().execute(
            "UPDATE sessions SET last_done_at=? WHERE id=?",
            (time.time(), sid))
        _db().commit()


def set_session_summary(sid: str, phrase: str) -> None:
    """Store the chat's activity phrase (the sidebar status line)."""
    if not sid:
        return
    with _lock:
        _db().execute("UPDATE sessions SET summary=? WHERE id=?", (phrase, sid))
        _db().commit()


def mark_session_viewed(sid: str) -> None:
    """Stamp the moment the user opened the chat — the done-LED only means
    'finished AND not opened since', so this is the 'not opened' half."""
    if not sid:
        return
    with _lock:
        _db().execute(
            "UPDATE sessions SET last_viewed_at=? WHERE id=?",
            (time.time(), sid))
        _db().commit()


def clear_stale_processing() -> None:
    """Clear busy flags left behind by a dead server. No agent turn survives
    a restart, so any session still flagged processing at boot is stale —
    leaving it would freeze the UI in a phantom "working" state (and 409
    the next message)."""
    with _lock:
        _db().execute("UPDATE sessions SET processing=0 WHERE processing=1")
        _db().commit()


def finalize_stale_drafts() -> int:
    """Draft rows left by a dead server are turns that never finished
    (restart/crash mid-task). Promote anything visible to a normal message —
    its `parts` timeline is the mid-task history that would otherwise vanish
    with the process — and drop empty shells. Run at boot, right before
    clear_stale_processing()."""
    with _lock:
        rows = _db().execute(
            "SELECT id, content, parts FROM messages WHERE draft=1").fetchall()
        kept = 0
        for r in rows:
            try:
                has_parts = bool(json.loads(r["parts"] or "[]"))
            except Exception:  # noqa: BLE001
                has_parts = False
            if (r["content"] or "").strip() or has_parts:
                _db().execute("UPDATE messages SET draft=0 WHERE id=?", (r["id"],))
                kept += 1
            else:
                _db().execute("DELETE FROM messages WHERE id=?", (r["id"],))
        if rows:
            _db().commit()
    return kept


def _tombstone(conn: sqlite3.Connection, rows: list, source: str) -> None:
    """Record deleted chats in deleted_sessions (the 30-day tracker).
    `rows` are the sessions being deleted (with at least id/title/summary/
    workspace_id). msg_count and a full message snapshot are captured per
    session so a delete can be restored. Fail-soft: the tracker must
    never block a delete."""
    now = time.time()
    for r in rows:
        sid = r["id"]
        try:
            mrows = conn.execute(
                "SELECT role, content, files, thinking, parts, mode, "
                "created_at FROM messages WHERE session_id=? AND draft=0 "
                "ORDER BY id", (sid,)).fetchall()
        except Exception:   # noqa: BLE001
            mrows = []
        try:
            snap = json.dumps(
                [dict(m) for m in mrows], ensure_ascii=False)
        except Exception:  # noqa: BLE001
            snap = "[]"
        try:
            conn.execute(
                "INSERT OR REPLACE INTO deleted_sessions"
                "(session_id,title,summary,workspace_id,msg_count,"
                "deleted_at,source,messages) VALUES(?,?,?,?,?,?,?,?)",
                (sid, r["title"] or "", r["summary"], r["workspace_id"],
                 len(mrows), now, source, snap))
        except Exception:  # noqa: BLE001
            pass


def delete_session(sid: str) -> None:
    with _lock:
        conn = _db()
        r = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        _tombstone(conn, [r] if r else [], "delete")
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM events WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM trajectories WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM run_state WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()


def clear_sessions() -> None:
    with _lock:
        conn = _db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions").fetchall()]
        _tombstone(conn, rows, "clear_all")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM trajectories")
        conn.execute("DELETE FROM run_state")
        conn.execute("DELETE FROM sessions")
        conn.commit()


def clear_archived_sessions(cutoff_ts: float) -> int:
    """Delete non-pinned sessions last touched before cutoff_ts (the
    sidebar's auto-archive threshold). Returns the number deleted."""
    with _lock:
        conn = _db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sessions WHERE pinned=0 AND updated_at<?",
            (cutoff_ts,)).fetchall()]
        _tombstone(conn, rows, "clear_archived")
        ids = [r["id"] for r in rows]
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute("DELETE FROM messages WHERE session_id IN (" + q + ")", ids)
            conn.execute("DELETE FROM events WHERE session_id IN (" + q + ")", ids)
            conn.execute("DELETE FROM trajectories WHERE session_id IN (" + q + ")", ids)
            conn.execute("DELETE FROM run_state WHERE session_id IN (" + q + ")", ids)
            conn.execute("DELETE FROM sessions WHERE id IN (" + q + ")", ids)
            conn.commit()
        return len(ids)


# ── deleted_sessions: the 30-day tombstone (read-only tracker) ──────

def list_deleted_sessions(limit: int = 100) -> list[dict]:
    """Recently deleted chats, newest first (the ⋯ menu's 'Deleted chats'
    list). The message snapshot is excluded — it's the payload, not the
    list (a 200-msg chat would bloat every open of the drawer)."""
    with _lock:
        rows = _db().execute(
            "SELECT session_id,title,summary,workspace_id,msg_count,"
            "deleted_at,source,restored_at FROM deleted_sessions "
            "ORDER BY deleted_at DESC, session_id LIMIT ?",
            (max(1, min(limit, 500)),)).fetchall()
    return [dict(r) for r in rows]


def _get_deleted_session_locked(sid: str) -> dict | None:
    """Tombstone row + parsed snapshot. Caller holds _lock."""
    r = _db().execute(
        "SELECT * FROM deleted_sessions WHERE session_id=?",
        (sid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        msgs = json.loads(d.get("messages") or "[]")
    except Exception:  # noqa: BLE001
        msgs = []
    # the snapshot is raw rows (files/parts stored as JSON strings, like
    # the messages table) — decode them so the restore path re-serializes
    # lists, not strings
    for m in msgs:
        for k in ("files", "parts"):
            try:
                m[k] = json.loads(m.get(k) or "[]")
            except Exception:  # noqa: BLE001
                m[k] = []
    d["messages"] = msgs
    return d


def get_deleted_session(sid: str) -> dict | None:
    """One tombstone WITH its message snapshot (the restore payload).
    None if the chat was never deleted or its tombstone was pruned."""
    with _lock:
        return _get_deleted_session_locked(sid)


def restore_deleted_session(sid: str) -> dict:
    """Bring a deleted chat back: re-insert the session row (original
    id, so deep links / localStorage sid still resolve) plus every
    snapshotted message, then stamp the tombstone restored_at.
    The snapshot is the only copy of the messages, so restore is
    idempotent-safe: if the session somehow exists again, fail clean
    instead of double-inserting. Returns the restored session."""
    now = time.time()
    with _lock:
        conn = _db()
        if conn.execute(
                "SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise ValueError("session already exists")
        d = _get_deleted_session_locked(sid)
        if d is None:
            raise ValueError("no tombstone for this session")
        conn.execute(
            "INSERT INTO sessions(id,workspace_id,title,created_at,"
            "updated_at) VALUES(?,?,?,?,?)",
            (sid, d["workspace_id"], d["title"] or "", now, now))
        for m in d["messages"]:
            try:
                files = json.dumps(m.get("files") or [])
            except Exception:  # noqa: BLE001
                files = "[]"
            try:
                parts = json.dumps(m.get("parts") or [],
                                   ensure_ascii=False)
            except Exception:  # noqa: BLE001
                parts = "[]"
            conn.execute(
                "INSERT INTO messages(session_id,role,content,files,"
                "thinking,parts,mode,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, m.get("role") or "user", m.get("content") or "",
                 files, m.get("thinking"), parts,
                 m.get("mode") or "act", m.get("created_at") or now))
        conn.execute(
            "UPDATE deleted_sessions SET restored_at=? WHERE session_id=?",
            (now, sid))
        conn.commit()
    return get_session(sid)


def prune_deleted_sessions(days: int = 30) -> int:
    """Drop tombstones older than `days` (the tracker has an expiry).
    Returns the number pruned."""
    cutoff = time.time() - days * 86400
    with _lock:
        r = _db().execute(
            "DELETE FROM deleted_sessions WHERE deleted_at<?", (cutoff,))
        _db().commit()
    return r.rowcount or 0


# ── messages ────────────────────────────────────────────────────────

def add_message(sid: str, role: str, content: str, files: list | None = None,
                thinking: str | None = None, parts: list | None = None) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO messages(session_id,role,content,files,thinking,parts,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (sid, role, content, json.dumps(files or []), thinking,
             json.dumps(parts or [], ensure_ascii=False), time.time()))
        _db().commit()
    touch_session(sid)


# ── in-flight assistant row (mid-task timeline durability) ────────────
# A turn opens a draft row and refreshes it as tool runs land, so a
# restart/crash mid-task still leaves the command/edit/search timeline in
# history (finalize_stale_drafts promotes it at boot). Normal completion
# closes the same row, so the turn ends up as ONE message, not two.

def new_draft_message(sid: str, mode: str = "act") -> int:
    with _lock:
        cur = _db().execute(
            "INSERT INTO messages(session_id,role,content,files,thinking,parts,"
            "draft,mode,created_at) VALUES(?,'assistant','','[]',NULL,'[]',1,?,?)",
            (sid, "plan" if mode == "plan" else "act", time.time()))
        _db().commit()
        return int(cur.lastrowid)


def update_draft_message(mid: int, content: str, files: list | None,
                         thinking: str | None, parts: list,
                         flushed_seq: int = 0, ui: dict | None = None) -> None:
    with _lock:
        _db().execute(
            "UPDATE messages SET content=?, files=?, thinking=?, parts=?, "
            "flushed_seq=?, ui=? WHERE id=? AND draft=1",
            (content, json.dumps(files or []), thinking,
             json.dumps(parts or [], ensure_ascii=False),
             int(flushed_seq or 0), json.dumps(ui) if ui else None, mid))
        _db().commit()


def finish_draft_message(mid: int | None, sid: str, content: str,
                         files: list | None = None, thinking: str | None = None,
                         parts: list | None = None, mode: str = "act") -> None:
    """Finalize the in-flight row for this turn (insert fresh if `mid` is
    missing). Keeps whatever the user saw — partial text and/or the tool
    timeline — and drops the row if the turn produced nothing."""
    text = (content or "").strip()
    has_parts = bool(parts)
    answer_ts = time.time()  # when the final answer was produced
    with _lock:
        if mid:
            if text or has_parts:
                _db().execute(
                    "UPDATE messages SET content=?, files=?, thinking=?, "
                    "parts=?, draft=0, answer_ts=? WHERE id=?",
                    (text, json.dumps(files or []), thinking,
                     json.dumps(parts or [], ensure_ascii=False),
                     answer_ts, mid))
            else:
                _db().execute("DELETE FROM messages WHERE id=?", (mid,))
        elif text or has_parts:
            _db().execute(
                "INSERT INTO messages(session_id,role,content,files,thinking,"
                "parts,draft,mode,created_at,answer_ts) "
                "VALUES(?,'assistant',?,?,?,?,0,?,?,?)",
                (sid, text, json.dumps(files or []), thinking,
                 json.dumps(parts or [], ensure_ascii=False),
                 "plan" if mode == "plan" else "act", time.time(), answer_ts))
        _db().commit()
    touch_session(sid)


def get_draft_message(sid: str, max_tool_parts: int | None = 25) -> dict | None:
    """The in-flight assistant row (draft=1) — the reattach snapshot of the
    running turn: timeline parts, in-flight text (content), thinking, files,
    and flushed_seq (the last frame seq the snapshot already covers, so the
    client can resume the stream exactly there). None when no turn is in
    flight. Same parsed shape as list_messages rows.
    max_tool_parts: how many timeline TOOL parts the snapshot ships — the
    LAST N, in order, every text/question part untouched (chat-switch
    freeze, 2026-09-28: a 3h run's 292KB / 77-row snapshot rendered
    synchronously on every reattach). The dropped count comes back as
    omitted_tool_parts; the full timeline still lives in the events DB.
    max_tool_parts=None disables the cap (the DB row itself is never cut)."""
    with _lock:
        r = _db().execute(
            "SELECT * FROM messages WHERE session_id=? AND draft=1 "
            "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if r is None:
        return None
    m = dict(r)
    try:
        m["files"] = json.loads(m.get("files") or "[]")
    except Exception:
        m["files"] = []
    try:
        m["parts"] = json.loads(m.get("parts") or "[]")
    except Exception:
        m["parts"] = []
    try:
        m["ui"] = json.loads(m.get("ui") or "{}")
    except Exception:
        m["ui"] = {}
    m["flushed_seq"] = int(m.get("flushed_seq") or 0)
    m["omitted_tool_parts"] = 0
    if max_tool_parts is not None and m["parts"]:
        tools = [p for p in m["parts"] if p.get("t") == "tool"]
        if len(tools) > max_tool_parts:
            drop = {id(p) for p in tools[:-max_tool_parts]}
            m["parts"] = [p for p in m["parts"] if id(p) not in drop]
            m["omitted_tool_parts"] = len(drop)
    return m


def list_messages(sid: str, limit: int = 200,
                  thinking_tail: int | None = None,
                  before_id: int = 0) -> list[dict]:
    # draft=0: in-flight assistant rows are a durability net, not history —
    # the live SSE stream renders the running turn, this renders the finished
    # before_id: window cursor (chat-switch freeze, 2026-09-28) — 0 = no
    # cursor (the newest `limit` rows), else only rows OLDER than it (the
    # "Load earlier" chunks). Oldest → newest either way; callers that need
    # everything (agent recall) are untouched — big limit, no cursor.
    with _lock:
        rows = _db().execute(
            "SELECT * FROM messages WHERE session_id=? AND draft=0 "
            "AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?",
            (sid, before_id, before_id, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for m in out:
        m.pop("ui", None)  # snapshot dressing — only the draft row carries it
        try:
            m["files"] = json.loads(m.get("files") or "[]")
        except Exception:
            m["files"] = []
        try:
            m["parts"] = json.loads(m.get("parts") or "[]")
        except Exception:
            m["parts"] = []
    if thinking_tail is not None and len(out) > thinking_tail:
        # Payload trim (chat-switch freeze, 2026-09-26): the UI's Thinking
        # tab only ever rehydrates the recent tail (it caps at 100 bursts
        # and the windowed render draws the last 20 messages), but
        # /api/history was shipping EVERY message's full thinking text —
        # 3.5 MB of the 5.5 MB Muji payload, never rendered. Blank
        # thinking for everything older than the tail. The DB rows are
        # untouched; callers that need the full text read the row directly.
        cut = len(out) - thinking_tail
        for m in out[:cut]:
            if m.get("thinking"):
                m["thinking"] = ""
    return out


def search_messages(query: str, sid: str | None = None,
                    limit: int = 8, scan: int = 400) -> list[dict]:
    """Keyword search over finished message contents (search_transcript
    recall tool, 2026-10-07). `sid` = one session, None = all sessions;
    `scan` caps how many newest rows per session are searched (the DB
    keeps everything, but a full-table scan per recall is wasteful);
    returns the newest `limit` matches, oldest → newest."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    sids = ([sid] if sid else [r["id"] for r in
            _db().execute("SELECT id FROM sessions ORDER BY updated_at DESC")
            .fetchall()])
    found: list[dict] = []
    with _lock:
        for s in sids:
            rows = _db().execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? "
                "AND draft=0 ORDER BY id DESC LIMIT ?) WHERE content LIKE ? "
                "ORDER BY id", (s, scan, like)).fetchall()
            found.extend(dict(r) for r in rows)
    out = sorted(found, key=lambda m: m["id"])[-max(1, limit):]
    for m in out:
        m.pop("ui", None)
    return out


def count_messages_before(sid: str, before_id: int) -> int:
    """Finished messages strictly OLDER than the cursor — the "N hidden"
    number behind the Load earlier button (windowed /api/history,
    2026-09-28). before_id=0 counts nothing (id < 0 is empty)."""
    with _lock:
        r = _db().execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND draft=0 "
            "AND id<?", (sid, before_id)).fetchone()
    return int(r[0]) if r else 0


def recent_thinking(sid: str, limit: int = 100,
                    max_bytes: int = 250_000) -> list[str]:
    """Newest non-empty thinking texts, oldest → newest, capped by count AND
    total bytes. Feeds the Thinking tab in windowed history payloads
    (chat-switch freeze, 2026-09-28): the message window only carries its
    OWN thinking, the tab rehydrates from this bounded tail instead of
    every message's full text (3.5 MB of the old 5.5 MB Muji payload)."""
    with _lock:
        rows = _db().execute(
            "SELECT thinking FROM messages WHERE session_id=? AND draft=0 "
            "AND thinking IS NOT NULL AND thinking != '' "
            "ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
    out = [r[0] for r in reversed(rows)]
    total = sum(len(t.encode("utf-8")) for t in out)
    while out and total > max_bytes:
        total -= len(out[0].encode("utf-8"))
        out.pop(0)
    return out


def update_last_assistant(sid: str, content: str, files: list | None = None) -> None:
    with _lock:
        _db().execute(
            "UPDATE messages SET content=?, files=? WHERE id=("
            "SELECT id FROM messages WHERE session_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1)",
            (content, json.dumps(files or []), sid))
        _db().commit()


# ── queue (durable per-session FIFO of messages sent while busy) ─────
#: The in-memory queue in tasks.py is authoritative WHILE the server is
#: up (it drives the drain); these rows are the durability layer — a
#: restart must not eat messages the user already sent. Every mutation
#: mirrors the in-memory state (see TaskManager._sync_queue_rows), so
#: the rows are a plain snapshot: re-reading them on startup restores
#: the queue exactly as it was.

def queue_add(sid: str, entry: dict) -> None:
    """Append one queued message (pos = current count, FIFO order)."""
    with _lock:
        r = _db().execute(
            "SELECT COUNT(*) FROM queue WHERE session_id=?", (sid,)).fetchone()
        _db().execute(
            "INSERT INTO queue(session_id,pos,message,files,mode,roast,"
            "resume,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, int(r[0]), entry["message"],
             json.dumps(entry.get("files") or [], ensure_ascii=False),
             entry.get("mode") or "act", entry.get("roast") or "chill",
             1 if entry.get("resume") else 0, time.time()))
        _db().commit()


def queue_set(sid: str, entries: list[dict]) -> None:
    """Replace a session's whole queue with `entries` (FIFO order).

    Used after a removal: the in-memory deque is the truth, this just
    re-snapshots it (positions renumbered)."""
    with _lock:
        _db().execute("DELETE FROM queue WHERE session_id=?", (sid,))
        for i, e in enumerate(entries):
            _db().execute(
                "INSERT INTO queue(session_id,pos,message,files,mode,roast,"
                "resume,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sid, i, e["message"],
                 json.dumps(e.get("files") or [], ensure_ascii=False),
                 e.get("mode") or "act", e.get("roast") or "chill",
                 1 if e.get("resume") else 0,
                 e.get("created_at") or time.time()))
        _db().commit()


def queue_pop(sid: str) -> dict | None:
    """Remove and return the session's head entry (the drain took it)."""
    with _lock:
        r = _db().execute(
            "SELECT * FROM queue WHERE session_id=? ORDER BY pos LIMIT 1",
            (sid,)).fetchone()
        if r is None:
            return None
        _db().execute("DELETE FROM queue WHERE id=?", (r["id"],))
        _db().commit()
        return {"message": r["message"],
                "files": json.loads(r["files"] or "[]"),
                "mode": r["mode"] or "act", "roast": r["roast"] or "chill",
                "resume": bool(r["resume"]),
                "created_at": r["created_at"]}


def queue_clear(sid: str) -> None:
    with _lock:
        _db().execute("DELETE FROM queue WHERE session_id=?", (sid,))
        _db().commit()


def queue_load(sid: str) -> list[dict]:
    """All of a session's queued entries, FIFO (pos) order."""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM queue WHERE session_id=? ORDER BY pos, id",
            (sid,)).fetchall()
    out = []
    for r in rows:
        try:
            files = json.loads(r["files"] or "[]")
        except Exception:
            files = []
        out.append({"message": r["message"], "files": files,
                    "mode": r["mode"] or "act",
                    "roast": r["roast"] or "chill",
                    "resume": bool(r["resume"]),
                    "created_at": r["created_at"]})
    return out


def queue_all_sessions() -> list[str]:
    """Session ids that still have queued messages (post-restart restore)."""
    with _lock:
        rows = _db().execute(
            "SELECT DISTINCT session_id FROM queue").fetchall()
    return [r[0] for r in rows]


# ── events (structural per-task events, for replay/forensics) ────────

def add_event(sid: str, seq: int, event: str, data: dict) -> int | None:
    with _lock:
        cur = _db().execute(
            "INSERT INTO events(session_id,seq,event,data,ts) "
            "VALUES(?,?,?,?,?)",
            (sid, seq, event, json.dumps(data, ensure_ascii=False), time.time()))
        _db().commit()
        return cur.lastrowid


def max_event_seq(sid: str) -> int:
    """Highest seq persisted for one session (0 if it has none).

    Used to anchor a fresh run's seq line: the session's line must stay
    strictly increasing across runs AND server restarts (after a restart
    the in-memory counter is gone), otherwise new rows collide with old
    ones and `run_start_seq` can no longer isolate one run's events for
    the re-attach replay. Cheap: covered by idx_events_session."""
    with _lock:
        r = _db().execute(
            "SELECT MAX(seq) FROM events WHERE session_id=?", (sid,)).fetchone()
    return int(r[0]) if (r is not None and r[0] is not None) else 0


def has_event(sid: str, event: str, after_seq: int = 0) -> bool:
    """Did this session ever emit `event` at seq > after_seq? The supervisor
    uses this to tell a run that has CYCLED (done at least one full model
    call) from one still on its first call — the ring buffer evicts old
    frames, so the DB (not the ring) is the authority for 'ever'. Strict
    `>`: after_seq is the run's anchor, and a PREVIOUS run's terminal
    llm_end can sit exactly at that anchor (anchor = max_event_seq)."""
    with _lock:
        r = _db().execute(
            "SELECT 1 FROM events WHERE session_id=? AND event=? AND seq>? LIMIT 1",
            (sid, event, after_seq)).fetchone()
    return r is not None


def last_event_ts(sid: str) -> float | None:
    """Timestamp of the session's most recent persisted event (None if it
    has none). The supervisor's silence clock: the in-memory ring holds no
    timestamps, and ring-only frames (token deltas) never land here — so
    this is the authoritative 'last real activity' stamp."""
    with _lock:
        r = _db().execute(
            "SELECT ts FROM events WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
    return float(r[0]) if (r is not None and r[0] is not None) else None


def list_events(sid: str, after: int = 0, limit: int = 500) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
            (sid, after, limit)).fetchall()
    out = [dict(r) for r in rows]
    for e in out:
        try:
            e["data"] = json.loads(e.get("data") or "{}")
        except Exception:
            e["data"] = {}
    return out


def list_tool_events(sid: str, limit: int = 400) -> list[dict]:
    """tool_end rows for the per-chat Terminal tab, oldest → newest,
    capped to the most recent `limit` (ordered by row id, not run-local seq)."""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM events WHERE session_id=? AND event='tool_end' "
            "ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for e in out:
        try:
            e["data"] = json.loads(e.get("data") or "{}")
        except Exception:
            e["data"] = {}
    return out


# ── auto-approve settings (Cline-style panel) ───────────────────────

#: The single approval setting. Destructive-only model: muji runs everything
#: (read, edit, commands, web, browser, self-edit, work-outside-root) without
#: asking — it only pauses for destructive operations (delete, remove, rename,
#: move, and similar irreversible actions), gated by `destructive` (default ON).
DEFAULT_AUTO_APPROVE = {"destructive": True, "auto_resume": False}


def load_auto_approve() -> dict:
    p = settings.settings_path
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt file → defaults
        return dict(DEFAULT_AUTO_APPROVE)
    return {k: bool(data.get(k, v)) for k, v in DEFAULT_AUTO_APPROVE.items()}


def save_auto_approve(patch: dict) -> dict:
    cur = load_auto_approve()
    for k in DEFAULT_AUTO_APPROVE:
        if k in patch:
            cur[k] = bool(patch[k])
    settings.settings_path.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return cur


# ── run state (crash/restart resume of the live loop) ───────────────

def save_run_state(sid: str, turn: int, plan: list, done: set,
                   messages: list, goal: str = "") -> None:
    """Persist the in-flight loop so a restart can re-enter it at the exact
    turn. One small write per turn, not per token. `messages` is the live
    model conversation (already post-compaction when it gets big).

    A checkpoint on a LIVE turn is by definition not stopped — if the boss
    hit Stop and then queued a message that continues the parked task, the
    first checkpoint of that turn must clear the stopped flag (otherwise
    the next boot would skip a run the boss deliberately started)."""
    with _lock:
        _db().execute(
            "INSERT INTO run_state(session_id,turn,plan,done,messages,goal,"
            "updated_at,stopped) VALUES(?,?,?,?,?,?,?,0) "
            "ON CONFLICT(session_id) DO UPDATE SET turn=excluded.turn, "
            "plan=excluded.plan, done=excluded.done, messages=excluded.messages, "
            "goal=excluded.goal, updated_at=excluded.updated_at, "
            "stopped=0",
            (sid, turn, json.dumps(plan or []), json.dumps(sorted(done or [])),
             json.dumps(messages or [], ensure_ascii=False),
             (goal or "")[:500], time.time()))
        _db().commit()


def load_run_state(sid: str) -> dict | None:
    with _lock:
        r = _db().execute(
            "SELECT * FROM run_state WHERE session_id=?", (sid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    try:
        d["plan"] = json.loads(d.get("plan") or "[]")
    except Exception:  # noqa: BLE001
        d["plan"] = []
    try:
        d["done"] = set(json.loads(d.get("done") or "[]"))
    except Exception:  # noqa: BLE001
        d["done"] = set()
    try:
        d["messages"] = json.loads(d.get("messages") or "[]")
    except Exception:  # noqa: BLE001
        d["messages"] = []
    return d


def clear_run_state(sid: str) -> None:
    with _lock:
        _db().execute("DELETE FROM run_state WHERE session_id=?", (sid,))
        _db().commit()


def set_run_state_stopped(sid: str, stopped: bool) -> None:
    """Flag (or unflag) a saved run_state as deliberately stopped.

    stopped=1 (the boss hit Stop) keeps the row — the ↻ chip and the
    sidebar continue symbol stay — but boot auto-resume skips it: a
    deliberate stop is a pause the boss chose, not a crash to heal from.
    Manual ↻ and queued continuations clear the flag on their first
    checkpoint (save_run_state writes stopped=0). No-op when there is no
    saved run_state (nothing to park)."""
    with _lock:
        _db().execute(
            "UPDATE run_state SET stopped=? WHERE session_id=?",
            (1 if stopped else 0, sid))
        _db().commit()


def all_session_ids() -> list[str]:
    """Every session id (boot-time scans, e.g. auto-resume)."""
    with _lock:
        return [r["id"] for r in
                _db().execute("SELECT id FROM sessions").fetchall()]


def has_run_state(sid: str) -> bool:
    """Lightweight check for the session list: does this session have a
    resumable in-flight run — i.e. it was interrupted (stop/crash/restart)
    WITHOUT a final summary? A run that finishes with a done event clears
    its run_state, so presence = "needs continue"."""
    with _lock:
        return _db().execute(
            "SELECT 1 FROM run_state WHERE session_id=?", (sid,)).fetchone() is not None


def has_unstopped_run_state(sid: str) -> bool:
    """Like has_run_state, but excludes deliberately-stopped runs
    (stopped=1 — the boss hit Stop). Boot auto-resume uses this: a stop is
    a pause the boss chose, so the triangle/↻ affordance stays but the
    server must NOT resurrect it on its own."""
    with _lock:
        return _db().execute(
            "SELECT 1 FROM run_state WHERE session_id=? AND stopped=0",
            (sid,)).fetchone() is not None


# ── trajectories (self-improvement raw material) ────────────────────

def log_trajectory(sid: str, goal: str, steps: list, claims: str,
                   verified: str, pattern: str | None, outcome: str) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO trajectories(session_id,ts,goal,steps,claims,verified,"
            "pattern,outcome) VALUES(?,?,?,?,?,?,?,?)",
            (sid, time.time(), (goal or "")[:500],
             json.dumps(steps or [], ensure_ascii=False)[:4000],
             (claims or "")[:4000], (verified or "")[:1000],
             (pattern or None), (outcome or "done")[:32]))
        _db().commit()


def recall_trajectories(goal: str, limit: int = 3, min_overlap: int = 2) -> list[dict]:
    """Most-recent past tasks whose goal shares >= min_overlap content
    words with `goal` (simple token overlap — no embeddings, matches the
    aesthetic). Returns [] when nothing is close enough."""
    words = set(re.findall(r"[a-z0-9_]{4,}", (goal or "").lower()))
    if len(words) < min_overlap:
        return []
    with _lock:
        rows = _db().execute(
            "SELECT goal, steps, verified, pattern, outcome, ts FROM trajectories "
            "ORDER BY ts DESC LIMIT 40").fetchall()
    scored = []
    for r in rows:
        gw = set(re.findall(r"[a-z0-9_]{4,}", (r["goal"] or "").lower()))
        overlap = len(words & gw)
        if overlap >= min_overlap:
            scored.append((overlap, r["ts"], dict(r)))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [d for _, _, d in scored[:limit]]
