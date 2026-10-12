"""Background agent tasks (multi-instance mode).

Agent turns run as detached asyncio tasks: the job keeps running no matter
which chat is open in the browser — or whether any browser is connected.
Every SSE frame gets a sequence number and is fanned out to:
  - live subscribers (a private asyncio.Queue per connection — two tabs or
    a reconnect all receive every frame),
  - a bounded in-memory ring buffer (replay for late joiners / reconnects),
  - the DB `events` table for structural events (forensics / future replay).
Thinking bursts are the one exception to "deltas stay in memory": each
burst (a run of consecutive `thinking` frames, closed by any other event)
is persisted as ONE `thinking_burst` row, so a viewer who re-attaches
after the ring buffer evicted the early frames still sees the reasoning.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque

from . import agent, db
from .config import settings
from .sse import clear_stopper, sse

#: Structural events persisted to the DB. High-frequency token/thinking
#: deltas stay in memory only (they aggregate into the message record).
PERSIST_EVENTS = {"status", "tool_start", "tool_end", "phase_end", "llm_end",
                  "answer_start", "done", "correction", "error", "stopped",
                  "approval", "approval_closed", "question", "question_closed",
                  "plan", "plan_update"}

_FRAME_RE = re.compile(r"^event: (\S+)\ndata: (.*\S)\n\n\Z", re.S)

_SENTINEL = object()


class TaskRun:
    """One agent turn running (or finished) in the background."""

    def __init__(self, sid: str, seq: int = 0) -> None:
        self.sid = sid
        self.task_id = uuid.uuid4().hex
        self.status = "queued"      # queued | running | done | error | stopped
        #: WHY a stopped run stopped — the `stopped` frame's reason. The
        #: queue drain only suppresses on "stopped by user" (a hard stop
        #: means the user's queued follow-ups don't auto-fire); system
        #: stops (empty answer, max turns) drain normally so their
        #: auto-resume nudge can launch.
        self.stop_reason: str | None = None
        self.seq = seq
        #: seq value at the moment this run begins — the events-log replay for an
        #: in-flight turn starts here, isolating THIS run's structural events from
        #: the session's earlier runs. `seq`/`start_seq` must be the session
        #: line's next value (TaskManager._anchor_seq): the line is strictly
        #: increasing across runs (and restarts), so a fresh run starting at 0
        #: would collide with earlier runs' seqs in the events table and break
        #: the replay window (start_seq=0 replays the WHOLE session history —
        #: a multi-thousand-row sync DOM pass that freezes the page).
        self.start_seq = seq
        self.buffer: deque[tuple[int, str]] = deque(maxlen=4000)
        #: one private asyncio.Queue per live subscriber — frames are fanned
        #: out to ALL of them; a single shared queue would split the stream
        #: between two viewers (second tab / reconnect race)
        self.subscribers: set[asyncio.Queue] = set()
        self.waiting_approval = False
        self.waiting_question = False
        #: open question's deadline (epoch ms) + full span — the toast's
        #: countdown reads these, so it survives a page refresh mid-question
        self.question_deadline: float | None = None
        self.question_total: int = 0
        #: same pair for the open approval wait (auto-deny after
        #: APPROVAL_TIMEOUT) — the toast timer for ⚠ waits
        self.approval_deadline: float | None = None
        self.approval_total: int = 0
        #: open thinking burst: [text, start_seq, end_seq] or None —
        #: flushed to the DB as one `thinking_burst` row when it closes
        self._think_buf: list | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        #: the run has finished at least one full model call (an llm_end
        #: frame passed through _push). The supervisor's stuck clock only
        #: applies to CYCLED runs — a run still on its first model call is
        #: silent by design (long first calls are normal). In-memory is the
        #: authority (the ring buffer evicts early frames; the DB fallback
        #: in supervisor.watch covers runs restored across a restart).
        self.cycled = False

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def info(self) -> dict:
        return {"task_id": self.task_id, "status": self.status, "seq": self.seq,
                "waiting_approval": self.waiting_approval,
                "approval_deadline": self.approval_deadline,
                "approval_total": self.approval_total,
                "waiting_question": self.waiting_question,
                "question_deadline": self.question_deadline,
                "question_total": self.question_total,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "error": self.error}


class TaskManager:
    def __init__(self, log) -> None:
        self.log = log
        self.runs: dict[str, TaskRun] = {}
        #: per-session FIFO of messages sent while a turn was running —
        #: drained automatically the moment the current run fully ends.
        #: In-memory is authoritative while the server is up; the DB
        #: `queue` table (db.queue_*) is the durability mirror — every
        #: mutation below re-syncs it so a restart restores the queue.
        self.queues: dict[str, deque[dict]] = {}
        self._sem = asyncio.Semaphore(settings.max_parallel_agents)
        self._restore_queues()

    def _restore_queues(self) -> None:
        """Rebuild the in-memory queues from the DB on boot.

        The in-memory deques die with the process; the rows don't. A
        message the user sent (and saw as "queued") while the old process
        was alive must still run — it's durable, so restore it. Entries
        keep their created_at so the UI can show how long they waited."""
        try:
            for sid in db.queue_all_sessions():
                entries = db.queue_load(sid)
                if entries:
                    self.queues[sid] = deque(entries)
        except Exception:  # noqa: BLE001 — a corrupt queue must not kill boot
            pass

    def get(self, sid: str) -> TaskRun | None:
        return self.runs.get(sid)

    def autostart(self, sid: str) -> None:
        """Boot-time drain: a queue was restored from the DB but nothing is
        running (the restart killed the in-flight turn) — fire the head
        message now. The rest follows via the normal drain in _pump's
        finally, so FIFO order is preserved across the restart."""
        q = self.queues.get(sid)
        if not q:
            return
        cur = self.runs.get(sid)
        if cur is not None and cur.active:
            return  # something is running — the normal drain will handle it
        nxt = q.popleft()
        if not q:
            self.queues.pop(sid, None)
        try:
            db.queue_pop(sid)
        except Exception:  # noqa: BLE001
            pass
        # If the session was ALSO interrupted mid-task (a saved run_state
        # exists — the restart killed a live turn), re-enter THAT loop
        # instead of firing the queued message as a fresh turn. A fresh
        # turn rebuilds context from the last 16 history rows — the
        # interrupted task's in-flight state (plan, tool results, goal)
        # is out of scope, so the queued message lands in a different
        # conversation and, when it completes, its run_state write clears
        # the interrupted task's checkpoint for good. resume=True keeps
        # the original goal pinned and appends the queued message to the
        # saved conversation (agent.run_chat), so the task continues with
        # the user's follow-up as its next input.
        resume = bool(nxt.get("resume"))
        try:
            if not resume and db.has_run_state(sid):
                resume = True
                self.log("info",
                         f"task {sid[:8]} autostart: session has a saved "
                         f"run_state — queued message continues the "
                         f"interrupted task (resume)")
        except Exception:  # noqa: BLE001 — a state read must not kill boot
            pass
        self.log("info", f"task {sid[:8]} autostarting restored queued message")
        nrun = TaskRun(sid, seq=self._anchor_seq(sid))
        self.runs[sid] = nrun
        nrun._task = asyncio.create_task(
            self._pump(nrun, nxt["message"], nxt.get("files") or [],
                       nxt.get("mode") or "act",
                       nxt.get("roast") or "chill",
                       resume))

    def _anchor_seq(self, sid: str) -> int:
        """The session's seq line for the run being created NOW: strictly
        greater than anything the session has ever emitted, in memory or in
        the DB. The in-memory counter covers runs this process still knows
        about; after a server restart (or once prune() dropped the finished
        run) the DB's max seq is the authority. Without this, a fresh run
        restarts at seq 0 — its rows collide with earlier runs' seqs in the
        events table, /api/history reports run_start_seq=0, and the client's
        re-attach replay pulls the session's ENTIRE history into one sync
        DOM pass (page freeze) before the stale max seq also blinds the
        live stream to this run's frames."""
        prev = self.runs.get(sid)
        return max(prev.seq if prev is not None else 0,
                   db.max_event_seq(sid))

    def queue_count(self, sid: str) -> int:
        return len(self.queues.get(sid) or ())

    def _sync_queue_rows(self, sid: str) -> None:
        """Mirror the in-memory queue into the DB (the durability layer).

        Never raises — a queue write hiccup must not break the live turn;
        worst case the message is lost on a crash, same as before this
        feature existed."""
        try:
            q = self.queues.get(sid)
            if q:
                db.queue_set(sid, list(q))
            else:
                db.queue_clear(sid)
        except Exception:  # noqa: BLE001
            pass

    def queue_remove(self, sid: str, index: int) -> bool:
        """Drop one queued message by position (client-side cancel)."""
        q = self.queues.get(sid)
        if not q or not (0 <= index < len(q)):
            return False
        del q[index]
        if not q:
            self.queues.pop(sid, None)
        self._sync_queue_rows(sid)
        run = self.runs.get(sid)
        if run is not None and run.active:
            self._push(run, "queued", {"queued": 0})
        return True

    def queue_update(self, sid: str, index: int, text: str) -> bool:
        """Edit the queued message at `index` (it hasn't run yet — the
        drain pops by position, so the edit applies under the same
        single-threaded loop the drain runs in). Returns False when the
        index is gone — the item already drained and started running, in
        which case the caller offers the text back as a composer draft.
        """
        text = (text or "").strip()
        if not text:
            return False
        q = self.queues.get(sid)
        if not q or not (0 <= index < len(q)):
            return False
        q[index]["message"] = text
        self._sync_queue_rows(sid)
        run = self.runs.get(sid)
        if run is not None and run.active:
            self._push(run, "queued", {"queued": len(q)})
        return True

    def queue_clear(self, sid: str) -> int:
        """Drop EVERY queued message for this session (the tray's
        "clear all"). Returns how many were removed."""
        q = self.queues.get(sid)
        if not q:
            return 0
        n = len(q)
        q.clear()
        self.queues.pop(sid, None)
        self._sync_queue_rows(sid)
        run = self.runs.get(sid)
        if run is not None and run.active:
            self._push(run, "queued", {"queued": 0})
        return n

    def enqueue(self, sid: str, message: str, files: list[dict],
                mode: str = "act", roast: str = "chill",
                resume: bool = False) -> tuple[TaskRun, bool]:
        """Queue a background agent turn. Returns (run, queued):
        `queued=True` when a turn was already in flight and this one waits.
        `resume=True` re-enters the saved run_state (crash/restart) instead
        of building fresh context."""
        run = self.runs.get(sid)
        if run is not None and run.active:
            q = self.queues.setdefault(sid, deque())
            q.append({"message": message, "files": files, "mode": mode,
                      "roast": roast, "resume": resume,
                      "created_at": time.time()})
            self._sync_queue_rows(sid)  # durable: a restart keeps it queued
            # the waiting run's subscribers (already attached to `run`) get
            # the count update live — the UI's queue chip stays in sync
            self._push(run, "queued", {"queued": len(q)})
            return run, True
        run = TaskRun(sid, seq=self._anchor_seq(sid))
        self.runs[sid] = run
        run._task = asyncio.create_task(
            self._pump(run, message, files, mode, roast, resume))
        return run, False

    def cancel(self, sid: str) -> None:
        run = self.runs.get(sid)
        if run is not None and run._task is not None and not run._task.done():
            run._task.cancel()

    def drop_session(self, sid: str) -> None:
        """Cancel any live run and discard the queue (session deleted)."""
        self.cancel(sid)
        self.queues.pop(sid, None)
        try:
            db.queue_clear(sid)  # the rows die with the session
        except Exception:  # noqa: BLE001
            pass

    def prune(self, max_age: float = 900.0) -> None:
        """Drop long-finished runs (their state is durable in the DB)."""
        now = time.time()
        for sid in [s for s, r in self.runs.items()
                    if not r.active and r.finished_at
                    and now - r.finished_at > max_age]:
            self.runs.pop(sid, None)


    _TERMINAL = frozenset({"done", "error", "stopped"})

    def _flush_thinking(self, run: TaskRun, flush_seq: int | None = None) -> None:
        """Persist the open thinking burst as one DB row (if any text).
        Called when any non-thinking event closes the burst, and in the
        run's finally (a run can end mid-burst — a hard stop or crash
        between thinking frames).

        The row is stamped with the seq of the frame that CLOSES the
        burst (or the next frame's seq at run end) — deliberately NOT the
        last thinking frame's seq: a client replaying `after=<last
        thinking seq>` must still see the burst row, while a client that
        already consumed the closing frame (after=<closing seq>) must not
        get it twice."""
        if not run._think_buf:
            return
        text, start_seq, _end_seq = run._think_buf
        run._think_buf = None
        # the caller flushes BEFORE the closing frame is numbered, so the
        # closing frame's seq is run.seq + 1
        seq = flush_seq if flush_seq is not None else run.seq + 1
        try:
            eid = db.add_event(run.sid, seq, "thinking_burst",
                               {"text": text, "start_seq": start_seq,
                                "task_id": run.task_id})
            if eid:
                db.add_event(run.sid, seq, "thinking_burst_id",
                             {"event_id": eid, "start_seq": start_seq,
                              "task_id": run.task_id})
        except Exception:  # noqa: BLE001 — never kill the task
            pass

    def _push(self, run: TaskRun, name: str, data: dict) -> str:
        """Number one event, persist structural ones, buffer + fan out.
        Terminal frames are tagged with the run's task_id so a client that
        is already watching the NEXT queued run can ignore this run's
        terminal frame (and vice versa)."""
        if name == "thinking":
            # accumulate the burst in memory; the DB row lands when the
            # burst closes (any other event) or the run ends
            t = data.get("text") or ""
            if run._think_buf is None:
                run._think_buf = [t, run.seq, run.seq]
            else:
                run._think_buf[0] += t
                run._think_buf[2] = run.seq
        else:
            self._flush_thinking(run)
        run.seq += 1
        data["seq"] = run.seq
        if name == "llm_end":
            run.cycled = True  # the run has done a full model call — the
                                # supervisor's stuck clock may now apply
        if name in self._TERMINAL:
            data["task_id"] = run.task_id
        # wait deadlines: stamp BEFORE persistence, so the persisted event
        # AND the live SSE both carry deadline_ms — a client that re-renders
        # the card after a chat switch / reload derives remaining time from
        # it instead of resetting its local countdown to full. (Stamping
        # after add_event meant the DB copy never had it — the replay path
        # fell back to now+timeout and the countdown reset to 3:00.)
        if name == "approval":
            run.waiting_approval = True
            run.approval_total = int(data.get("timeout") or 0)
            run.approval_deadline = time.time() * 1000 + run.approval_total * 1000
            data["deadline_ms"] = run.approval_deadline
        elif name in ("approval_closed", "done", "error", "stopped"):
            run.waiting_approval = False
            run.approval_deadline = None
            run.approval_total = 0
        if name == "question":
            run.waiting_question = True
            run.question_total = int(data.get("timeout") or 0)
            run.question_deadline = time.time() * 1000 + run.question_total * 1000
            data["deadline_ms"] = run.question_deadline
        elif name in ("question_closed", "done", "error", "stopped"):
            run.waiting_question = False
            run.question_deadline = None
            run.question_total = 0
        if name in PERSIST_EVENTS:
            try:
                eid = db.add_event(run.sid, run.seq, name, data)
                if eid:
                    data["event_id"] = eid  # clients dedupe DB + live
            except Exception:  # noqa: BLE001 — never kill the task
                pass
        wire = sse(name, data)
        run.buffer.append((run.seq, wire))
        for q in list(run.subscribers):
            q.put_nowait((run.seq, wire))
        return name

    async def _pump(self, run: TaskRun, message: str, files: list[dict],
                    mode: str = "act", roast: str = "chill",
                    resume: bool = False) -> None:
        sid = run.sid
        last_name = ""
        try:
            # Instant "working" feedback: the first event is emitted the
            # moment the job is accepted — before an agent slot is taken
            # and before the first model call — so the UI never sits idle.
            last_name = self._push(run, "status",
                                   {"text": "Resuming…" if resume else "Working…"})
            async with self._sem:
                run.status = "running"
                run.started_at = time.time()
                async for frame in agent.run_chat(sid, message, files, self.log,
                                                  mode, roast, resume):
                    m = _FRAME_RE.match(frame)
                    name = m.group(1) if m else "message"
                    data: dict = {}
                    if m:
                        try:
                            data = json.loads(m.group(2))
                        except json.JSONDecodeError:
                            data = {}
                    last_name = self._push(run, name, data)
                    if name == "stopped":
                        # capture the WHY — the drain below only suppresses
                        # on a USER stop; an empty-answer / max-turns stop
                        # must drain so its auto-resume nudge can launch
                        run.stop_reason = str(data.get("reason") or "")
                run.status = {"stopped": "stopped",
                              "error": "error"}.get(last_name, "done")
        except asyncio.CancelledError:
            run.status = "stopped"
            # a hard stop is a USER stop, whatever the stream last said —
            # the drain must suppress queued follow-ups for it
            run.stop_reason = "stopped by user"
            if last_name not in ("done", "error", "stopped"):
                # the cancel landed OUTSIDE the generator (e.g. the turn was
                # still queued for an agent slot) — the stream still needs a
                # terminal frame for the UI to settle on
                self._push(run, "stopped", {"reason": "stopped by user"})
            raise
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            run.error = f"{type(e).__name__}: {e}"
            self.log("error", f"task {sid[:8]} crashed: {run.error}")
            run.seq += 1
            wire = sse("error", {"message": "Internal error — the task crashed.",
                                 "seq": run.seq})
            run.buffer.append((run.seq, wire))
            for q in list(run.subscribers):
                q.put_nowait((run.seq, wire))
        finally:
            self._flush_thinking(run)  # a run can end mid-burst (stop/crash)
            run.finished_at = time.time()
            try:
                db.mark_session_done(sid)  # durable done-LED stamp
            except Exception:  # noqa: BLE001 — never kill the task
                pass
            for q in list(run.subscribers):
                q.put_nowait((0, _SENTINEL))
            clear_stopper(sid)
            # Queue drain: the run above is FULLY done (its finally already
            # cleared the DB processing flag), so the next queued message can
            # start without a busy-flag race. Skipped ONLY when the user
            # hard-stopped the turn — the user's queued follow-ups don't
            # auto-fire after a deliberate stop (the client re-sends them if
            # the user wants). System stops (empty answer, max turns) drain
            # normally: their auto-resume nudge is enqueued right after the
            # `stopped` frame and must launch in-process, not rot in the
            # queue until the next restart (the Muji 10-04 incident: the
            # nudge was enqueued 6 ms after the empty-answer stop, then the
            # old blanket `status != "stopped"` skip ate it).
            if not (run.status == "stopped"
                    and run.stop_reason == "stopped by user"):
                q = self.queues.get(sid)
                if q:
                    cur = self.runs.get(sid)
                    if cur is not None and cur.active:
                        # a drain already replaced the run (or a concurrent
                        # enqueue beat us) — it owns the queue now
                        return
                    nxt = q.popleft()
                    if not q:
                        self.queues.pop(sid, None)
                    try:
                        db.queue_pop(sid)  # the head row goes with the entry
                    except Exception:  # noqa: BLE001
                        pass
                    # Same rule as autostart: the run that just finished can
                    # have left a saved run_state (the LLM stopped mid-loop
                    # without a final summary — max turns, a hard stop, a
                    # crash). Firing the queued message as a FRESH turn would
                    # rebuild context from the last 16 history rows (the
                    # unfinished task's plan/tool results are out of scope)
                    # and its completion would clear that checkpoint for
                    # good — the queue "takes over" the session and the
                    # interrupted task is lost. resume=True re-enters the
                    # saved loop with the original goal pinned and the
                    # queued message appended as its next input.
                    try:
                        if not nxt.get("resume") and db.has_run_state(sid):
                            nxt["resume"] = True
                            self.log("info",
                                     f"task {sid[:8]} drain: session has a "
                                     f"saved run_state — queued message "
                                     f"continues the interrupted task "
                                     f"(resume)")
                    except Exception:  # noqa: BLE001 — state read must not kill the drain
                        pass
                    self.log("info", f"task {sid[:8]} draining next queued message")
                    # continue the session's seq line (already anchored by
                    # _anchor_seq at creation) — a client reconnecting with
                    # after=<old seq> must replay the NEW run's frames
                    # (seq > old), not miss them all
                    nrun = TaskRun(sid, seq=run.seq)
                    self.runs[sid] = nrun
                    # The task is created BEFORE the drain-announcement push
                    # so the pump's first step (status=running) lands in the
                    # same scheduling slot as before — the drain-resume test
                    # polls early and a push between create_task and the
                    # pump's first await would defer the start by one
                    # microtask (the drain would look "not started").
                    nrun._task = asyncio.create_task(
                        self._pump(nrun, nxt["message"], nxt.get("files") or [],
                                   nxt.get("mode") or "act",
                                   nxt.get("roast") or "chill",
                                   bool(nxt.get("resume"))))
                    # Drain-announce: the head just left the queue — tell
                    # every open tab so the tray drops the row live (the
                    # client's local pop can race this server-side pop:
                    # items queued from another tab, a reload mid-run, the
                    # empty-local sync hitting the pre-drain list). Without
                    # this event the tray keeps the dead row until a reload.
                    # Pushed on nrun: the old run's stream is closed by the
                    # sentinel, so the NEW run's buffer is what replays to
                    # the reconnecting client.
                    # Carries the fired text too: the client draws the
                    # fired message's user bubble at the handoff, and this
                    # event is the AUTHORITATIVE source for that text —
                    # the client's local peek (v._nextJob / state.queues)
                    # is empty when the item was queued from another tab.
                    self._push(nrun, "queued",
                               {"queued": len(q), "fired": nxt["message"]})

    async def events(self, sid: str, after: int = 0):
        """Yield SSE frames for one session: buffered replay, then live.

        Each caller gets a private queue: several viewers on the same run
        (two tabs, a reconnect race) all receive every frame — the ring
        buffer covers the replay, the per-subscriber queue the live tail.
        """
        run = self.runs.get(sid)
        if run is None:
            return  # nothing in memory; the UI renders /api/history instead
        last = after
        # `after` before the ring's oldest frame = this subscriber missed
        # frames the ring already evicted (a phone tab iOS froze for hours,
        # reconnecting with a stale seq). Replay only what the ring holds,
        # then tell the client (replay_gap) so it re-attaches from the DB
        # events log instead of trusting a truncated in-memory replay.
        buf = list(run.buffer)
        gap = bool(buf) and after < buf[0][0]
        for seq, frame in buf:
            if seq > last:
                yield frame
                last = seq
        if gap:
            yield sse("replay_gap", {"after": after, "oldest": buf[0][0]})
        sub: asyncio.Queue = asyncio.Queue()
        run.subscribers.add(sub)
        try:
            if not run.active:
                # finished while replaying — at most one sentinel queued
                try:
                    seq, frame = sub.get_nowait()
                    if frame is not _SENTINEL and seq > last:
                        yield frame
                except asyncio.QueueEmpty:
                    pass
                return
            while True:
                # 15s keepalive: an idle SSE (the model thinking, a long
                # tool run) sends nothing — iOS kills that connection the
                # moment the PWA is backgrounded, and the reconnect on
                # resume replays the whole ring (up to 4000 frames) in one
                # burst that chokes the main thread. A comment frame every
                # 15s keeps the socket alive; EventSource ignores `:` lines.
                try:
                    seq, frame = await asyncio.wait_for(sub.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if frame is _SENTINEL:
                    return
                if seq > last:
                    yield frame
                    last = seq
        finally:
            run.subscribers.discard(sub)


# ── live-manager registry ───────────────────────────────────────────
#: api.py registers its TaskManager here at startup. The agent
#: (_flush_draft) resolves the session's run through it to stamp the
#: last broadcast seq onto the in-flight draft row — the exact resume
#: point for a client that re-attaches mid-turn.
_manager: TaskManager | None = None


def set_manager(tm: TaskManager) -> None:
    global _manager
    _manager = tm


def get_manager() -> TaskManager | None:
    return _manager


def other_sessions_in_flight(exclude_sid: str | None = None) -> list[str]:
    """Session ids (other than `exclude_sid`) with work in flight — a run
    queued or running right now. The gate for a self-restart: it kills
    every live run, so it may only run without approval when this list is
    empty (the current run is the only one alive). In-memory is the
    authority — a DB `processing` flag left by a dead server would false-
    positive, and a queued-but-not-yet-started run has no flag at all."""
    tm = _manager
    if tm is None:
        return []
    return [sid for sid, run in tm.runs.items()
            if sid != exclude_sid and run.active]
