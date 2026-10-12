"""App settings, loaded from environment / .env file."""
from __future__ import annotations

import os
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load .env as the authoritative source for app settings.

    Values in .env override anything inherited from the parent process,
    so editing the file always takes effect on the next restart — even
    if the launcher's shell happens to carry a stale copy of a setting
    (e.g. a MAX_TURNS exported by an earlier server run)."""
    p = APP_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            os.environ[k] = v


_load_dotenv()


def _keychain_secret(service: str, username: str) -> str:
    """Read a secret from the Windows Credential Manager via `keyring`.

    On this machine keyring's backend is WinVaultKeyring — the OS
    keychain, decrypted for this user's session only, not readable from
    the file tree. Returns "" when the secret doesn't exist (or keyring
    is missing), so callers fall back to the env var."""
    try:
        import keyring
        return keyring.get_password(service, username) or ""
    except Exception:
        return ""


def _path(value: str, default: pathlib.Path) -> pathlib.Path:
    if not value:
        return default
    p = pathlib.Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_absolute() else (APP_ROOT / p).resolve()


class Settings:
    def __init__(self) -> None:
        self.title = os.environ.get("TITLE", "muji")
        self.brand = os.environ.get("BRAND", "muji")
        self.creator = os.environ.get(
            "CREATOR", "Jan (jblagana) — AI student at UP Diliman, Teaching Associate in EEEI"
        )
        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = int(os.environ.get("PORT", "8321"))

        self.base_url = os.environ.get("OPENAI_BASE_URL", "")
        # Secret resolution order: Windows Credential Manager (keychain) >
        # .env > nothing. The keychain wins so .env can hold a placeholder
        # while the real secret stays in the OS store (survives reboots,
        # not readable without this user's session, not in the file tree).
        self.api_key = (
            _keychain_secret("muji", "api_key")
            or os.environ.get("OPENAI_API_KEY", "")
            or "sk-no-key"
        )
        self.model = os.environ.get("MODEL", "")

        # Tavily web search (optional — when empty, web_search falls back to
        # the DuckDuckGo→Bing scrape chain). Secret resolution mirrors the
        # OpenAI key: keychain first, then .env.
        self.tavily_api_key = (
            _keychain_secret("muji", "tavily_api_key")
            or os.environ.get("TAVILY_API_KEY", "")
        )

        self.root_dir = _path(os.environ.get("ROOT_DIR", ""), pathlib.Path.home())
        self.fact_check = os.environ.get("FACT_CHECK", "0") == "1"
        # Local-timezone offset in hours, for the latency heatmap's hour
        # bucketing (events.ts is epoch/UTC — bucketing on raw hour splits
        # a local day across two UTC days). Default 8 = this machine (PHT).
        self.tz_offset = int(os.environ.get("TZ_OFFSET", "8"))
        self.tool_mode = os.environ.get("TOOL_MODE", "auto")  # auto | native | text
        # Tool opt-in/out (public build): comma-separated tool names to EXCLUDE
        # from the harness entirely (schema, prompt, and dispatch).
        #   - UNSET  → the personal integrations are disabled by default (opt-in):
        #     a fresh clone runs the bare core harness, no Gmail/Telegram/to-do.
        #   - SET    → exactly that list is excluded ("" = exclude nothing =
        #     enable everything). To enable one personal tool, remove its name
        #     from the list (and add its creds).
        _PERSONAL_TOOLS = (
            "gmail_search", "gmail_read",
            "tg_search", "tg_read",
            "tasks_list", "tasks_add", "tasks_done",
            "events_add", "events_list", "events_done",
        )
        if "TOOLS_DISABLED" in os.environ:
            _disabled = {s.strip() for s in os.environ["TOOLS_DISABLED"].split(",") if s.strip()}
        else:
            _disabled = set(_PERSONAL_TOOLS)
        self.tools_disabled: frozenset[str] = frozenset(_disabled)
        self.temperature = float(os.environ.get("TEMPERATURE", "0.7"))
        # Model thinking control (Qwen3-style endpoints; probed live on this
        # HPC endpoint — see tools/probe_thinking.py):
        #   "low" | "medium" | "xhigh" → reasoning_effort tier (the server
        #       defaults to xhigh — that's where the overthinking came from:
        #       ~2500 vs ~700 reasoning chars on a tiny prompt),
        #   "off" → no chain-of-thought at all (enable_thinking=False),
        #   ""    → leave the server default.
        # Probe 2026-09-28 (Qwen3.8-27B @ vLLM, tools/probe_thinking.py):
        #   honored — reasoning_effort {low|medium|xhigh} ("minimal" 400s)
        #             + chat_template_kwargs.enable_thinking (the only off:
        #             1613→0 reasoning chars on a tiny prompt).
        #   ignored — top-level enable_thinking AND thinking_budget (both
        #             accepted, both no-op) → NO server-side thinking cap
        #             exists; effort tier + off are the only levers.
        #   usage — completion_tokens yes, completion_tokens_details NO
        #           (reasoning_tokens absent) → llm_end metering uses char
        #           deltas, not token counts.
        self.thinking = os.environ.get("THINKING", "low")
        # Per-round thinking split (2026-09-28 experiment — BACKLOG
        # thinking-loop entry): the FIRST LLM round of a task (planning)
        # uses THINKING_FIRST, every later tool-loop round uses
        # THINKING_LOOP. Both default to "" → fall back to THINKING, so
        # leaving them unset is behavior-neutral. The 09-28 experiment
        # sets THINKING_LOOP=low (grunt rounds don't need the full dial);
        # revert = remove that .env line + restart.
        self.thinking_first = os.environ.get("THINKING_FIRST", "")
        self.thinking_loop = os.environ.get("THINKING_LOOP", "")
        # 0 = unlimited: context compaction (COMPACT_TRIGGER) is the pacing
        # mechanism, not a turn budget. Set >0 for a hard safety cap.
        self.max_turns = int(os.environ.get("MAX_TURNS", "0"))
        # Per-ROUND completion cap (thinking + answer tokens), 2026-10-08:
        # the AI Classes run burned ~72 min on ONE round — 497 KB of
        # degenerated thinking — because the request carried no max_tokens
        # and the server ran to its own (huge) limit. 32k is the backstop:
        # a healthy round on this workload peaks well below it, and a loop
        # that trips the thinking tripwire dies at ~4 KB anyway; this cap
        # is what bounds a round the tripwire MISSES. 0 = unlimited.
        self.max_tokens = int(os.environ.get("MAX_TOKENS", "32768"))
        # Context compaction (boss design 2026-10-11):
        #   COMPACT_BOUNDARY  = the context meter's 100% mark — the model's
        #                       real limit (Qwen via vLLM: 265k), so the
        #                       topbar shows distance to the wall.
        #   COMPACT_TRIGGER   = where the LOSSY summary pass fires: at turn
        #                       boundaries (rebuild) when context >= trigger.
        #                       Mid-turn, old tool results used to be
        #                       cleared (lossless) per round — now OFF by
        #                       default (COMPACT_MID_TURN_CLEAR), so a turn
        #                       runs on full recall until the boundary pass.
        self.compact_enabled = os.environ.get("COMPACT_ENABLED", "1") == "1"
        # PERSISTED COMPACTION (boss-approved 2026-10-09, replaces the
        # CONTEXT_WINDOW experiment): the per-turn rebuild loads the
        # session's FULL history (plus the persisted summary of everything
        # older than the marker) — no fixed 16-row tail, no token-budget
        # window. The lossy pass fires at turn boundaries (trigger); the
        # result is persisted on the session
        # (compacted_summary + compacted_through_msg_id) so it runs once
        # per growth, not per turn.
        self.compact_boundary = int(os.environ.get("COMPACT_BOUNDARY", "256000"))
        self.compact_context_limit = int(os.environ.get("COMPACT_CONTEXT_LIMIT", "265000"))
        self.compact_trigger = int(os.environ.get("COMPACT_TRIGGER", "100000"))
        self.compact_recent = int(os.environ.get("COMPACT_RECENT", "4"))
        # MID-TURN LOSSLESS CLEAR (boss-disabled 2026-10-12): the per-round
        # tool-result stubbing that ran every round once context crossed the
        # trigger. Off by default — a turn now runs on full recall until the
        # turn BOUNDARY compaction (clear + LLM summary) fires; the overflow
        # recovery's aggressive clear is untouched and remains the backstop.
        self.compact_mid_turn_clear = os.environ.get("COMPACT_MID_TURN_CLEAR", "0") == "1"
        self.command_timeout = int(os.environ.get("COMMAND_TIMEOUT", "120"))
        self.approval_timeout = int(os.environ.get("APPROVAL_TIMEOUT", "180"))
        self.question_timeout = int(os.environ.get("QUESTION_TIMEOUT", "180"))
        self.max_parallel_agents = int(os.environ.get("MAX_PARALLEL_AGENTS", "4"))

        self.data_dir = APP_ROOT / "data"
        self.uploads_dir = APP_ROOT / "uploads"
        # Pass-by-reference: tool results longer than this spill to a file and
        # the model gets a preview + the file path (read_file / search_files)
        # instead of the full text — keeps big outputs (logs, greps) out of context.
        self.results_dir = self.data_dir / "results"
        self.result_spill_threshold = int(os.environ.get("RESULT_SPILL_THRESHOLD", "6000"))
        # Local RAG (index_documents / local_search): chunk geometry and the
        # optional OpenAI-compatible embeddings model. Leave RAG_EMBED_MODEL
        # empty for BM25-only (zero config); set it to unlock dense/hybrid.
        self.rag_chunk_size = int(os.environ.get("RAG_CHUNK_SIZE", "1600"))
        self.rag_chunk_overlap = int(os.environ.get("RAG_CHUNK_OVERLAP", "200"))
        self.rag_embed_model = os.environ.get("RAG_EMBED_MODEL", "")
        self.db_path = self.data_dir / "muji.db"
        self.settings_path = self.data_dir / "auto_approve.json"
        self.learned_path = APP_ROOT / "learned.md"
        self.tool_notes_path = APP_ROOT / "tool_notes.md"
        self.instructions_path = APP_ROOT / "INSTRUCTIONS.md"
        self.instructions_archive_path = APP_ROOT / "INSTRUCTIONS-ARCHIVE.md"
        for d in (self.data_dir, self.uploads_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
