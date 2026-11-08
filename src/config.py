"""App settings, loaded from environment / .env file."""
from __future__ import annotations

import json
import os
import pathlib
import re

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
        value = (keyring.get_password(service, username) or "").strip()
    except Exception:
        return ""
    # a placeholder left in Credential Manager for the user to fill in
    # (e.g. "PASTE_YOUR_KEY_HERE") is "no key", not a bad key
    if value.upper().startswith("PASTE_"):
        return ""
    return value


# ── Model profiles (ported from muji, 2026-11-08) ──────────────────────
# A profile = one OpenAI-compatible endpoint + model + its quirks. The only
# BUILT-IN profile is "default", seeded from .env (OPENAI_BASE_URL / MODEL /
# OPENAI_API_KEY); every other provider (Groq, OpenAI, OpenRouter, Together,
# a self-hosted vLLM…) is added from the UI's "Manage profiles…" panel.
# Non-secret fields live in data/model_profiles.json; API KEYS live only in
# the OS keychain (service "muji", user "profile_key:<name>") and are never
# returned to the browser. The active profile persists in
# data/active_profile.json and switches live, no restart.
BUILTIN_PROFILES = ("default",)
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

#: How a profile's endpoint takes "thinking" control (THINKING=low|medium|high|xhigh|off):
#:   qwen        — Qwen3 on vLLM: reasoning_effort low|medium|xhigh, off =
#:                 chat_template_kwargs.enable_thinking=False (vLLM-only param)
#:   qwen_hosted — Qwen3 on a hosted API (Groq): low|medium|high, off -> "none"
#:   openai      — OpenAI-style reasoning_effort low|medium|high (gpt-oss, o-series)
#:   none        — send nothing (most non-reasoning models reject the param)
THINKING_STYLES = ("qwen", "qwen_hosted", "openai", "none")


def _compat_fields(p: dict) -> dict:
    style = p.get("thinking_style")
    if style not in THINKING_STYLES:
        style = "openai" if p.get("reasoning_effort") else "none"
    try:
        mt = max(0, int(p.get("max_tokens") or 0))
    except (TypeError, ValueError):
        mt = 0
    return {"thinking_style": style, "reasoning_effort": style != "none",
            "vision": bool(p.get("vision", False)), "max_tokens": mt}


def _default_profiles() -> dict:
    return {
        "default": {
            "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "model": os.environ.get("MODEL", ""),
            "compact_boundary": int(os.environ.get("COMPACT_BOUNDARY", "256000")),
            **_compat_fields({
                "thinking_style": os.environ.get("THINKING_STYLE", "none"),
                "vision": os.environ.get("MODEL_VISION", "0") == "1",
                "max_tokens": 0}),
        },
    }


def _profile_file() -> str:
    return str(APP_ROOT / "data" / "active_profile.json")


def _custom_profiles_file() -> str:
    return str(APP_ROOT / "data" / "model_profiles.json")


def _load_custom_profiles() -> dict:
    try:
        with open(_custom_profiles_file(), encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    for name, p in (raw or {}).items():
        if (name in BUILTIN_PROFILES or not PROFILE_NAME_RE.match(name)
                or not isinstance(p, dict) or not p.get("base_url") or not p.get("model")):
            continue
        out[name] = {"base_url": str(p["base_url"]), "model": str(p["model"]),
                     "compact_boundary": int(p.get("compact_boundary") or 131072),
                     **_compat_fields(p)}
    return out


def _save_custom_profiles(profiles: dict) -> None:
    os.makedirs(os.path.dirname(_custom_profiles_file()), exist_ok=True)
    tmp = _custom_profiles_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)
    os.replace(tmp, _custom_profiles_file())


def _load_active_profile() -> str:
    try:
        with open(_profile_file(), encoding="utf-8") as f:
            name = json.load(f).get("profile", "")
            if name in _default_profiles() or name in _load_custom_profiles():
                return name
    except Exception:
        pass
    return "default"


def _save_active_profile(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(_profile_file()), exist_ok=True)
        with open(_profile_file(), "w", encoding="utf-8") as f:
            json.dump({"profile": name}, f)
    except Exception:
        pass


def _keychain_set(service: str, username: str, secret: str) -> None:
    """Store a secret in the OS keychain. Raises on failure — callers must
    NOT fall back to writing the secret into a file."""
    import keyring
    keyring.set_password(service, username, secret)


def _keychain_delete(service: str, username: str) -> None:
    try:
        import keyring
        keyring.delete_password(service, username)
    except Exception:
        pass


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
        # model profiles (see the module-level block): the .env/keychain key
        # is the "default" profile's; the active profile overrides
        # base_url / model / api_key / limits at the end of __init__
        self._default_key = self.api_key
        self._groq_key = (_keychain_secret("muji", "groq_api_key")
                          or os.environ.get("GROQ_API_KEY", ""))
        self.profiles = _default_profiles()
        self.profiles.update(_load_custom_profiles())
        self.active_profile = _load_active_profile()
        self.reasoning_effort_ok = True
        self.thinking_style = "none"
        self.vision = False
        self.profile_max_tokens = 0

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
        #                       Mid-turn, old tool results are cleared
        #                       (lossless) per round once the trigger is
        #                       crossed (COMPACT_MID_TURN_CLEAR, on by
        #                       default) — the turn keeps full recall of
        #                       conversation text, only tool output is stubbed.
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
        # set_profile clamps the trigger below a smaller model's window
        self._compact_trigger_env = self.compact_trigger
        self.compact_recent = int(os.environ.get("COMPACT_RECENT", "4"))
        # MID-TURN LOSSLESS CLEAR (boss re-enabled 2026-10-13): the per-round
        # tool-result stubbing runs every round once context crosses the
        # trigger — ON by default. The lossy LLM summary still waits for the
        # turn BOUNDARY compaction; the overflow recovery's aggressive clear
        # is untouched and remains the backstop.
        self.compact_mid_turn_clear = os.environ.get("COMPACT_MID_TURN_CLEAR", "1") == "1"
        # TOKEN DIET (ported from muji 2026-11-08): the lossless tool-result
        # clear starts at CLEAR_AT and cuts to CLEAR_TO in ONE batch
        # (hysteresis keeps a cached prompt prefix valid for many rounds);
        # the newest KEEP_TOOL_RESULTS results always stay whole.
        self.compact_clear_at = int(os.environ.get("COMPACT_CLEAR_AT", "40000"))
        self.compact_clear_to = int(os.environ.get("COMPACT_CLEAR_TO", "25000"))
        self.compact_keep_tool_results = int(os.environ.get("COMPACT_KEEP_TOOL_RESULTS", "6"))
        # long-task brake: a wrap-up note at ROUND_WARN_AT rounds (then every
        # ROUND_WARN_EVERY) — every round re-sends the whole conversation
        self.round_warn_at = int(os.environ.get("ROUND_WARN_AT", "60"))
        self.round_warn_every = int(os.environ.get("ROUND_WARN_EVERY", "30"))
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
        self.result_spill_threshold = int(os.environ.get("RESULT_SPILL_THRESHOLD", "2500"))
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

        # apply the persisted active profile LAST (overrides the .env seeds)
        self.set_profile(self.active_profile)

    # ── model profiles ────────────────────────────────────────────────
    def set_profile(self, name: str) -> str:
        """Switch the active model profile in place (no restart); unknown
        names fall back to "default". Persisted across restarts."""
        if name not in self.profiles:
            name = "default"
        p = self.profiles[name]
        self.active_profile = name
        self.base_url = p["base_url"]
        self.model = p["model"]
        self.compact_boundary = int(p["compact_boundary"])
        self.reasoning_effort_ok = bool(p.get("reasoning_effort", False))
        self.thinking_style = p.get("thinking_style", "none")
        self.vision = bool(p.get("vision", False))
        self.profile_max_tokens = int(p.get("max_tokens") or 0)
        self.compact_trigger = min(self._compact_trigger_env, int(self.compact_boundary * 0.75))
        self.api_key = self.profile_key(name) or "sk-no-key"
        _save_active_profile(name)
        return name

    def profile_key(self, name: str) -> str:
        """API key for a profile: one saved from the UI (keychain
        "profile_key:<name>") wins; "default" falls back to OPENAI_API_KEY /
        keychain "api_key"; any Groq endpoint falls back to GROQ_API_KEY /
        keychain "groq_api_key"."""
        k = _keychain_secret("muji", "profile_key:" + name)
        if k:
            return k
        if name == "default" and self._default_key != "sk-no-key":
            return self._default_key
        p = self.profiles.get(name) or {}
        if "api.groq.com" in (p.get("base_url") or ""):
            return self._groq_key
        return ""

    def effective_max_tokens(self) -> int:
        caps = [c for c in (self.max_tokens, self.profile_max_tokens) if c > 0]
        return min(caps) if caps else 0

    def profile_public(self, name: str) -> dict:
        """Profile view for the UI — never includes the key itself."""
        p = self.profiles[name]
        key = self.profile_key(name)
        return {
            "base_url": p["base_url"], "model": p["model"],
            "compact_boundary": int(p["compact_boundary"]),
            "reasoning_effort": bool(p.get("reasoning_effort", False)),
            "thinking_style": p.get("thinking_style", "none"),
            "vision": bool(p.get("vision", False)),
            "max_tokens": int(p.get("max_tokens") or 0),
            "builtin": name in BUILTIN_PROFILES,
            "key_set": bool(key),
            "key_hint": ("…" + key[-4:]) if len(key) >= 12 else ("set" if key else ""),
        }

    def upsert_profile(self, name: str, fields: dict, api_key: str | None,
                       clear_key: bool = False) -> None:
        """Create/update a profile ("default": only its key can change here —
        its endpoint comes from .env). Blank api_key keeps the stored one."""
        if not PROFILE_NAME_RE.match(name or ""):
            raise ValueError("name must be 1-40 chars of a-z 0-9 . _ - (starting with a letter or digit)")
        entry = None
        if name not in BUILTIN_PROFILES:
            base_url = (fields.get("base_url") or "").strip().rstrip("/")
            model = (fields.get("model") or "").strip()
            if not re.match(r"^https?://[^\s/]+", base_url):
                raise ValueError("base URL must start with http:// or https://")
            if not model:
                raise ValueError("model is required")
            try:
                boundary = int(fields.get("compact_boundary") or 131072)
            except (TypeError, ValueError):
                raise ValueError("context limit must be a number")
            if not 16384 <= boundary <= 10_000_000:
                raise ValueError("context limit must be between 16,384 and 10,000,000")
            if fields.get("thinking_style") not in (None, *THINKING_STYLES):
                raise ValueError("thinking_style must be one of " + ", ".join(THINKING_STYLES))
            entry = {"base_url": base_url, "model": model,
                     "compact_boundary": boundary, **_compat_fields(fields)}
        elif name not in self.profiles:
            raise ValueError("unknown profile")
        # key first: if the keychain write fails, nothing else changes
        if clear_key:
            _keychain_delete("muji", "profile_key:" + name)
        elif api_key and api_key.strip():
            _keychain_set("muji", "profile_key:" + name, api_key.strip())
        if entry is not None:
            custom = _load_custom_profiles()
            custom[name] = entry
            _save_custom_profiles(custom)
            self.profiles[name] = entry
        if name == self.active_profile:
            self.set_profile(name)

    def delete_profile(self, name: str) -> None:
        if name in BUILTIN_PROFILES:
            raise ValueError("built-in profiles can't be deleted")
        if name not in self.profiles:
            raise KeyError(name)
        custom = _load_custom_profiles()
        custom.pop(name, None)
        _save_custom_profiles(custom)
        self.profiles.pop(name, None)
        _keychain_delete("muji", "profile_key:" + name)
        if self.active_profile == name:
            self.set_profile("default")

    def embed_endpoint(self) -> tuple[str, str]:
        """RAG embeddings stay on the "default" profile's endpoint, so
        switching the chat model doesn't break local_search."""
        p = self.profiles["default"]
        return p["base_url"], self.profile_key("default") or "sk-no-key"


settings = Settings()
