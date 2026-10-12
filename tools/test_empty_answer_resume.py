"""Empty-answer = resumable stop, not a dead-end error.

Regression for the boss-incident (2026-09-28): the model ended a round
with no content and no tool call; agent.py emitted `error` + "The model
returned an empty answer." — the UI settled on a red dead-end even though
the per-turn checkpoint (run_state) was still there. Now the same
condition emits `stopped` (the UI's ⏹ note + ↻ chip + boot auto-resume
all key off it) and the run_state is KEPT.

Drives the REAL agent.run_chat against a temp DB with a stubbed model
stream (no server, no endpoint, no user data).

Run:  .venv\Scripts\python.exe tools\test_empty_answer_resume.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
tmp = Path(tempfile.mkdtemp(prefix="muji_emptyans_"))
os.environ["ROOT_DIR"] = str(tmp)  # before importing the app

import src.config as cfg  # noqa: E402
cfg.settings.data_dir = tmp / "data"
cfg.settings.db_path = tmp / "data" / "muji.db"
cfg.settings.settings_path = tmp / "data" / "auto_approve.json"
cfg.settings.uploads_dir = tmp / "uploads"
cfg.settings.results_dir = tmp / "data" / "results"
cfg.settings.root_dir = tmp  # .env is authoritative — patch past it
for d in (cfg.settings.data_dir, cfg.settings.uploads_dir, cfg.settings.results_dir):
    d.mkdir(parents=True, exist_ok=True)

import src.db as mdb  # noqa: E402
from src import agent  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def empty_stream(messages, tools=None, thinking=None):
    """The incident: the round ends with no content and no tool call."""
    async def gen():
        yield {"type": "end", "finish_reason": "stop"}
    return gen()


def final_stream(messages, tools=None, thinking=None):
    """Control: a round that DOES produce content must stay a done frame."""
    async def gen():
        yield {"type": "token", "text": "all done, boss"}
        yield {"type": "end", "finish_reason": "stop"}
    return gen()


async def main():
    agent._llm.stream = empty_stream

    sid = mdb.create_session(None)["id"]
    mdb.add_message(sid, "user", "build the thing")

    frames = []
    async for f in agent.run_chat(sid, "build the thing", [],
                                  lambda lvl, msg: None, "act", "chill",
                                  False):
        frames.append(f)

    names = [f.split("event: ", 1)[1].split("\n", 1)[0] for f in frames]
    check("empty answer: NO error frame", "error" not in names)
    check("empty answer: terminal `stopped` frame", "stopped" in names)
    check("stopped reason names the empty answer",
          any("empty answer" in f for f in frames if "stopped" in f))
    check("run_state KEPT (resume affordance + boot auto-resume)",
          mdb.has_run_state(sid))
    check("run_state unstopped (auto-resume may fire)",
          mdb.has_unstopped_run_state(sid))

    # a stopped run must not leak a processing flag
    check("processing flag cleared",
          not (mdb.get_session(sid) or {}).get("processing"))

    # in-process auto-resume re-fire: an empty answer on a LIVE server must
    # not sit parked until the next boot — the nudge re-enters the saved
    # run_state right away, capped by the per-session budget.
    class _StubRun:
        active = False

    class _StubManager:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, sid, message, files, mode="act", roast="chill",
                    resume=False):
            self.enqueued.append((sid, message, resume))
            return _StubRun(), False

    from src import tasks as _tasks
    _stub = _StubManager()
    _tasks.set_manager(_stub)
    mdb.save_auto_approve({"auto_resume": True})
    agent._auto_resume_text = "Continue — auto-resume after empty answer."
    agent._auto_resume_fires.clear()

    frames = []
    async for f in agent.run_chat(sid, "build the thing", [],
                                  lambda lvl, msg: None, "act", "chill",
                                  False):
        frames.append(f)
    check("empty answer: auto-resume re-fired in-process",
          len(_stub.enqueued) == 1
          and _stub.enqueued[0][0] == sid
          and _stub.enqueued[0][2] is True)
    check("empty answer: re-fire re-enters saved run_state (resume=True)",
          _stub.enqueued and _stub.enqueued[0][2] is True)

    # budget cap: a permanently-empty model must not loop — after
    # _AUTO_RESUME_MAX fires the nudge stops and ↻ becomes manual
    for _ in range(agent._AUTO_RESUME_MAX + 2):
        async for _f in agent.run_chat(sid, "build the thing", [],
                                       lambda lvl, msg: None, "act", "chill",
                                       False):
            pass
    # total fires across the whole session == _AUTO_RESUME_MAX exactly
    # (budgets 0..MAX-1 fire; budget MAX is the wall)
    check("empty answer: auto-resume budget capped (no infinite loop)",
          len(_stub.enqueued) == agent._AUTO_RESUME_MAX)

    _tasks.set_manager(None)
    agent._auto_resume_text = ""
    agent._auto_resume_fires.clear()

    # control: a real final answer is still `done` and clears run_state
    agent._llm.stream = final_stream
    sid2 = mdb.create_session(None)["id"]
    mdb.add_message(sid2, "user", "say hi")
    frames2 = []
    async for f in agent.run_chat(sid2, "say hi", [],
                                  lambda lvl, msg: None, "act", "chill",
                                  False):
        frames2.append(f)
    names2 = [f.split("event: ", 1)[1].split("\n", 1)[0] for f in frames2]
    check("control: content round still `done`", "done" in names2)
    check("control: no stopped frame on a real answer", "stopped" not in names2)
    check("control: run_state cleared on done", not mdb.has_run_state(sid2))

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        sys.exit(1)
    print("all green")


asyncio.run(main())
