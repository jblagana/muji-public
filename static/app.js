/* muji — web UI (vanilla JS).
 * Talks to the FastAPI backend in src/api.py (SSE events:
 * token, status, tool_start, phase_end, answer_start, done, correction,
 * error, stopped, approval, approval_closed).
 */
(function () {
  "use strict";

  // Touch devices (phone/tablet/coarse pointer): HTML5 drag & drop is
  // unreliable there (long-press triggers native selection/callout instead),
  // so drag-to-move/rename is mouse-only; on touch, tap selects and the
  // ✏/🗑 buttons (visible while selected) do the same jobs.
  const COARSE = window.matchMedia("(pointer: coarse)").matches;

  // Focus the composer — but on touch, never automatically: auto-focus
  // summons the keyboard the moment a chat opens, yanking the layout up.
  // On a phone the user taps the composer when they're actually ready to type.
  function focusComposer() { if (!COARSE) el.input.focus(); }

  // ── Phase-output trimming (injected so style.css stays untouched) ──────
  // A settled tool row shows a few lines of output + a "show more" toggle, so
  // an expanded tool group stays compact instead of a wall of text. The box is
  // scrollable either way, so nothing is lost. Appended to <head> after the
  // stylesheet link, with !important, this overrides the .phase pre max-height
  // that lives in style.css.
  (function injectPhaseStyles() {
    if (document.getElementById("phase-trim-style")) return;
    const s = document.createElement("style");
    s.id = "phase-trim-style";
    s.textContent =
      ".phase pre { max-height: 5.6em !important; line-height: 1.4; " +
      "transition: max-height .18s ease; }\n" +
      ".phase pre.pre-more { max-height: 200px !important; }\n" +
      ".phase-more-btn { display: block; margin: 4px 0 2px 9px; padding: 0 8px; " +
      "font-size: 10.5px; font-family: ui-monospace, Consolas, monospace; " +
      "color: var(--muted); background: var(--code-bg); border: 1px solid var(--border); " +
      "border-radius: 999px; cursor: pointer; line-height: 1.6; }\n" +
      ".phase-more-btn:hover { color: var(--text); border-color: var(--accent); }";
    document.head.appendChild(s);
  })();

  // Files-tab "paste a folder path" row (work in a dir outside the root).
  // Injected here (like the phase styles) so style.css stays untouched.
  (function injectPasteStyles() {
    if (document.getElementById("paste-style")) return;
    const s = document.createElement("style");
    s.id = "paste-style";
    s.textContent =
      ".tree-paste { display: flex; gap: 6px; align-items: center; " +
      "padding: 6px 10px 7px; border-bottom: 1px solid var(--border); }\n" +
      ".tree-paste input { flex: 1; min-width: 0; background: var(--code-bg); " +
      "border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px; " +
      "color: var(--text); font-size: 12px; font-family: ui-monospace, Consolas, monospace; " +
      "outline: none; }\n" +
      ".tree-paste input:focus { border-color: var(--accent); }";
    document.head.appendChild(s);
  })();

  // ── State ─────────────────────────────────────────────────────
  const state = {
    config: null,
    workspaces: [],
    wsSel: null,            // workspace id; null = General
    sessions: [],
    sessionId: localStorage.getItem("muji.sid") || null,
    processing: false,
    resumable: false,      // server has saved loop state for this chat → ↻ re-enters it
    learned: { pending: [], active: [], loaded: false },  // learned.md human gate — topbar 🧠 only (composer banner retired 2026-10-12)
    toolNotes: { pending: [], active: [], loaded: false },  // tool_notes.md — topbar 🔧 only
    mode: "act",           // Plan/Act for the visible chat (Cline-style, per chat)
    roast: "chill",        // roast level for the visible chat (off|chill|full)
    attachments: [],        // [{name, url, original}]
    bootId: null,           // server process start time (from /api/health) — a
                            // different value on stream error = the server
                            // crashed and was revived → hard-reload the tab
    bootChecked: false,     // one-shot guard: only the FIRST mismatch reloads
    userScrolledUp: false,
    scrollPos: {},          // sid → {top, height} — remember scroll per chat
    pollTimer: null,
    thinkRenderTimer: null, // rAF throttle for the Thinking tab
    autoApprove: { destructive: true },
    views: {},              // sid → {es, seq, turn, done, errs, taskId} (multi-instance)
    queues: {},             // sid → [{text, files}] — messages queued while a turn ran (server is authoritative)
    finished: new Set(),    // sids whose most recent run has finished — steady green LED (seeded from server last_done_at)
    sessionSig: "",         // sidebar re-render signature
    chatFilter: "",         // sidebar search box: filter chats by title/summary
    archiveCollapsed: localStorage.getItem("muji.archiveCollapsed") === "1",
    statusTimer: null,
    progress: null,         // {mode, items, done, active, steps, start, label, timer}
    panels: {},             // sid → {term, termIds, preview, think, treePath}
    previewShown: null,     // {sid, key} — last file rendered in the Preview tab
    // right panel: two stacked panes, each with its own tab row. The upper
    // pane is Thinking | Files | Preview (Thinking auto-activates on the
    // thinking stream); the lower pane is Terminal | Editor. Old single-row
    // localStorage values ("thinking"/"terminal"/"editor") migrate to the
    // lower pane; a stale lower-pane "thinking" selection is dropped.
    rightTab: ["thinking", "files", "preview"].includes(localStorage.getItem("muji.rightTab"))
      ? localStorage.getItem("muji.rightTab") : "files",
    lowerTab: ["terminal", "editor"].includes(localStorage.getItem("muji.lowerTab"))
      ? localStorage.getItem("muji.lowerTab")
      : "terminal",
    sidebarW: parseInt(localStorage.getItem("muji.sidebarW") || "280", 10) || 280,
    rightW: parseInt(localStorage.getItem("muji.rightW") || "420", 10) || 420,
    termH: Math.min(80, Math.max(10, parseFloat(localStorage.getItem("muji.termH")) || 25)),
    editorPath: null,       // file open in the Editor pane (lower)
    termBusy: false,        // user terminal command in flight
    treePath: null,         // Files tab directory currently shown (null = workspace root)
    treeSid: null,          // which chat's saved position (panel.treePath) treePath holds
    treeRoot: null,         // "up" boundary for the Files tab
    picking: false,         // Files tab: choosing the chat's working folder
  };

  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    sidebar: $("sidebar"), sidebarClose: $("sidebar-close"), sidebarOpen: $("sidebar-open"),
    brandTitle: $("brand-title"), newChat: $("new-chat"), addWorkspace: $("add-workspace"),
    wsList: $("ws-list"), wsLabel: $("ws-label"),
    chatsMenu: $("chats-menu"), chatsMenuPop: $("chats-menu-pop"),
    clearAll: $("clear-all"), clearArchived: $("clear-archived"),
    deletedChats: $("deleted-chats"), deletedDrawer: $("deleted-drawer"),
    deletedDim: $("deleted-dim"),
    deletedList: $("deleted-list"), deletedEmpty: $("deleted-empty"),
    deletedClose: $("deleted-close"),
    chatSearch: $("chat-search"),
    tzDrawer: $("tz-drawer"), tzHead: $("tz-head"), tzTitle: $("tz-title"),
    tzCount: $("tz-count"), tzBody: $("tz-body"),
    tzTab: $("tz-tab"), tzClose: $("tz-close"),
    sessionList: $("session-list"), rootLine: $("root-line"), modelLine: $("model-line"), restartServer: $("restart-server"),
    topbarTitle: $("topbar-title"), previewToggle: $("preview-toggle"),
    ctxMeter: $("ctx-meter"), ctxFill: $("ctx-fill"), ctxPct: $("ctx-pct"),
    toks: $("toks"), toksVal: $("toks-val"),
    tbMenuBtn: $("tb-menu-btn"), tbMenu: $("tb-menu"),
    ctxNum: $("ctx-num"), ctxSub: $("ctx-sub"),
    themeToggle: $("theme-toggle"), modeToggle: $("mode-toggle"), roastToggle: $("roast-toggle"),
    progress: $("progress"), progressFill: $("progress-fill"), progressLabel: $("progress-label"),
    progressSteps: $("progress-steps"),
    jobStrip: $("job-strip"), jobText: $("job-text"),
    sessionToast: $("session-toast"), sessionToastName: $("session-toast-name"),
    approvalBanner: $("approval-banner"), bannerText: $("banner-text"), bannerView: $("banner-view"),
    bannerMark: $("banner-mark"), bannerPill: $("banner-pill"),
    chatScroll: $("chat-scroll"), welcome: $("welcome"),
    welcomeMark: $("welcome-mark"),
    messages: $("messages"),
    attachments: $("attachments"), attachBtn: $("attach-btn"), fileInput: $("file-input"),
    input: $("input"), sendBtn: $("send-btn"), stopBtn: $("stop-btn"), composerHint: $("composer-hint"),
    composerStatus: $("composer-status"), resumeChip: $("resume-chip"),
    abandonChip: $("abandon-chip"),
    queueTray: $("queue-tray"),
    scrollBottomBtn: $("scroll-bottom-btn"),
    supStrip: $("sup-strip"), supSummary: $("sup-summary"), supBoard: $("sup-board"),
    learnPanel: $("learn-panel"), learnList: $("learn-list"), learnOpen: $("learn-open"),
    wenchPanel: $("wench-panel"), wenchList: $("wench-list"), wenchOpen: $("wench-open"),
    latOpen: $("lat-open"), latDrawer: $("lat-drawer"), latDim: $("lat-dim"),
    latClose: $("lat-close"), latSub: $("lat-sub"),
    latBtnTok: $("lat-btn-tok"), latBtnMs: $("lat-btn-ms"),
    latToggle: $("lat-toggle"),
    latGrid: $("lat-grid"), latLegend: $("lat-legend"),
    latVerdict: $("lat-verdict"), latNote: $("lat-note"),
    rightPanel: $("right-panel"), rpClose: $("rp-close"),
    panelOverlay: $("panel-overlay"),
    rpTabsUpper: $("rp-tabs-upper"), rpTabsLower: $("rp-tabs-lower"),
    rpTabFiles: $("rp-tab-files"), rpTabPreview: $("rp-tab-preview"),
    rpTabThinking: $("rp-tab-thinking"), rpTabTerminal: $("rp-tab-terminal"),
    rpTabEditor: $("rp-tab-editor"),
    paneFiles: $("rp-pane-files"), paneTerminal: $("rp-pane-terminal"),
    paneThinking: $("rp-pane-thinking"), panePreview: $("rp-pane-preview"),
    paneEditor: $("rp-pane-editor"),
    termInput: $("term-input"),
    editorHead: $("editor-head"), editorName: $("editor-name"),
    editorBody: $("editor-body"), editorCode: $("editor-code"),
    editorEmpty: $("editor-empty"),
    rpSessionTitle: $("rp-session-title"),
    rpTree: $("rp-tree"), rpTreeLabel: $("rp-tree-label"),
    rpTreeUp: $("rp-tree-up"), rpTreeReveal: $("rp-tree-reveal"),
    rpTreeRefresh: $("rp-tree-refresh"),
  rpTreeDrop: $("rp-tree-dropzone"),
    rpTreeNewDir: $("rp-tree-newdir"), rpTreeNewFile: $("rp-tree-newfile"),
    rpTreeHidden: $("rp-tree-hidden"),
    rpTreeChoose: $("rp-tree-choose"), treePick: $("tree-pick"),
    treePickLabel: $("tree-pick-label"), treePickUse: $("tree-pick-use"),
    treePickCancel: $("tree-pick-cancel"),
    treePaste: $("tree-paste"), rpTreePath: $("rp-tree-path"),
    rpTreePathGo: $("rp-tree-path-go"),
    term: $("term"), thinkList: $("think"),
    previewBack: $("preview-back"), previewName: $("preview-name"),
  previewOpen: $("preview-open"),
    previewDownload: $("preview-download"), previewReveal: $("preview-reveal"),
    previewBody: $("preview-body"), previewEmpty: $("preview-empty"),
    previewZoomIn: $("preview-zoom-in"), previewZoomOut: $("preview-zoom-out"),
    previewZoomLevel: $("preview-zoom-level"),
    sidebarResize: $("sidebar-resize"), rightResize: $("right-resize"),
    termResize: $("term-resize"),
    aaToggle: $("aa-toggle"), aaLabel: $("aa-label"), aaBody: $("aa-body"), aaSub: $("aa-sub"),
    aaBox: $("auto-approve"),
  };

  marked.setOptions({ gfm: true, breaks: true });
  // Every rendered link opens in a new tab (boss's call), safe rel included.
  // mailto: links ignore target per spec, so they still open the mail client.
  marked.use({ renderer: {
    link(href, title, text) {
      const t = title ? ` title="${title}"` : "";
      return `<a href="${href || ""}"${t} target="_blank" rel="noopener noreferrer">${text}</a>`;
    },
    // GFM `~text~` → <del> (strikethrough) eats "approximately" tildes in
    // agent text ("~1.5k tok … ~550") and strikes everything between.
    // Strikethrough is never intentional in chat — render it as plain text.
    del(token) { return token.text; },
  }});

  async function api(path, opts) {
    return fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  }

  function fmtSize(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function inlineUrl(url) { return url + (url.includes("?") ? "&" : "?") + "inline=1"; }

  // Icon sprite (index.html, 2026-10-05): one stroke family for the whole UI.
  // `icon(name)` clones the <symbol> into a live <svg> — stroke follows
  // currentColor, so every icon tints per theme (ink / sage / mint) with no
  // second asset. Replaces the ~20 color emoji that fought the forest palette.
  function icon(name) {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.classList.add("mi");
    s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
    u.setAttribute("href", "#i-" + name);
    s.appendChild(u);
    return s;
  }

  // ── Markdown rendering ────────────────────────────────────────
  // Math (LaTeX) rendering: $…$ / $$…$$ are pulled out of the markdown
  // BEFORE marked sees it (see extractMathBlocks) and restored as KaTeX
  // nodes after sanitize. Inline rule: no whitespace at either end, at
  // least one char, and the closing `$` must not be followed by a digit
  // (keeps "$5 and $10" as currency, not math).
  const MATH_RE = /\$\$([\s\S]+?)\$\$|\$([^\s$](?:[^$\n]*[^\s$])?)\$(?!\d)/g;

  // Pull $…$ / $$…$$ out of the markdown BEFORE marked sees it. marked
  // (breaks:true) turns every newline inside a $$ block into <br>, which
  // would then reach KaTeX as literal HTML and fail to render — the
  // classic "raw cases environment shows up" bug. Each block is swapped
  // for a @@MATHn@@ placeholder (a standalone paragraph, so marked leaves
  // it alone) and restored as a KaTeX node after sanitize. The placeholder
  // is plain word chars + digits, so DOMPurify and innerHTML pass it
  // through untouched.
  const MATH_PH_RE = /@@MATH(\d+)@@/g;

  function extractMathBlocks(src) {
    const blocks = [];
    // Protect code first: fenced blocks and inline code keep their `$`
    // literal (a `$` in a code fence is a dollar, not math).
    const codes = [];
    let s = (src || "").replace(/```[\s\S]*?(```|$)|~~~[\s\S]*?(~~~|$)/g, (m) => {
      codes.push(m);
      return "@@CODE" + (codes.length - 1) + "@@";
    }).replace(/`[^`\n]+`/g, (m) => {
      codes.push(m);
      return "@@CODE" + (codes.length - 1) + "@@";
    });
    s = s.replace(MATH_RE, (m, disp, inline) => {
      blocks.push({ tex: (disp || inline).trim(), disp: !!disp });
      return "@@MATH" + (blocks.length - 1) + "@@";
    });
    s = s.replace(/@@CODE(\d+)@@/g, (m, i) => codes[+i]);
    return { out: s, blocks };
  }

  function restoreMathBlocks(container, blocks) {
    if (!blocks.length) return;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (n.parentElement && n.parentElement.closest("pre, code"))
          return NodeFilter.FILTER_REJECT;
        return n.nodeValue.indexOf("@@MATH") >= 0 ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((n) => {
      const frag = document.createDocumentFragment();
      let last = 0, m;
      MATH_PH_RE.lastIndex = 0;
      while ((m = MATH_PH_RE.exec(n.nodeValue))) {
        const i = +m[1];
        if (i >= blocks.length) continue; // stray placeholder — drop it
        frag.appendChild(document.createTextNode(n.nodeValue.slice(last, m.index)));
        frag.appendChild(renderMath(blocks[i].tex, blocks[i].disp));
        last = m.index + m[0].length;
      }
      frag.appendChild(document.createTextNode(n.nodeValue.slice(last)));
      n.parentNode.replaceChild(frag, n);
    });
  }

  const mathCache = new Map();
  function renderMath(tex, disp) {
    const key = (disp ? "D" : "I") + "|" + tex;
    let node = mathCache.get(key);
    if (node) return node.cloneNode(true);
    node = document.createElement(disp ? "div" : "span");
    try {
      katex.render(tex, node, { displayMode: disp, throwOnError: true });
    } catch (e) {
      // mid-stream / invalid tex: show the raw source; NOT cached, so a
      // later frame (formula complete) re-renders it for real
      node.className = "mjx-raw";
      node.textContent = (disp ? "$$" : "$") + tex + (disp ? "$$" : "$");
      return node;
    }
    mathCache.set(key, node);
    return node;
  }

  function renderMarkdown(container, text) {
    // DOMPurify's default ALLOWED_URI_REGEXP treats "C:" in a Windows path as
    // a URI scheme and strips the href — the anchor survives as a dead link
    // (text looks link-ish, click does nothing). Allow drive-letter paths:
    // "X:" + backslash / %5C / / / . (still blocks javascript: & friends).
    const FILE_URI_RE = /^(?:(?:https?|mailto|ftp|tel|file|sms|data):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$)|[A-Za-z]:[\\/%.])/i;
    const { out, blocks } = extractMathBlocks(text);
    container.innerHTML = DOMPurify.sanitize(marked.parse(out),
        { ADD_ATTR: ["target"], ALLOWED_URI_REGEXP: FILE_URI_RE });
    restoreMathBlocks(container, blocks);
    container.querySelectorAll("img[src]").forEach((img) => {
      const src = img.getAttribute("src") || "";
      if (src.startsWith("/uploads/")) img.src = inlineUrl(src);
      img.loading = "lazy";
    });
    container.querySelectorAll("pre > code").forEach((code) => {
      try { hljs.highlightElement(code); } catch (e) { /* ignore */ }
    });
    // visual cue: local file links look different from web links
    container.querySelectorAll("a[href]").forEach((a) => {
      if (looksLikeFilePath(a.getAttribute("href") || ""))
        a.classList.add("file-link");
    });
  }

  // ── Action chips ──────────────────────────────────────────────
  // The model ends final summaries/plans with a "## Chips" section — 2–6
  // short action lines for things the boss must decide, approve, or do
  // next. The UI renders them as click-to-send buttons and strips the
  // section from the visible answer (it would otherwise read as dead
  // boilerplate). Returns { text, chips }.
  function extractChips(text) {
    const t = text || "";
    const m = t.match(/\n[ \t]*##[ \t]+Chips[ \t]*\r?\n([\s\S]*?)(?=\n[ \t]*#{1,6}[ \t]|\s*$)/i);
    if (!m) return { text: t, chips: [] };
    const chips = (m[1].match(/^\s*[-*]\s+(.+)$/gm) || [])
      .map((l) => {
        let c = l.replace(/^\s*[-*]\s+/, "").trim();
        let kind = "act";
        const pm = c.match(/^(act|plan)\s*:\s*(.+)$/i);
        if (pm) { kind = pm[1].toLowerCase(); c = pm[2].trim(); }
        return { label: c, kind };
      })
      .filter((c) => c.label && c.label.length <= 120)
      .slice(0, 6);
    return { text: t.slice(0, m.index) + t.slice(m.index + m[0].length), chips };
  }

  // Clickable chip row under a finished answer. Clicking a chip sends that
  // line as the next user message (queued if a run is in flight — the same
  // path as typing it). Chips only render on FINISHED turns: a mid-stream
  // partial section would flash a row of buttons then vanish.
  // Chip kinds: "act" (green — do this) / "plan" (yellow — decide/draft).
  function addChips(turn, chips) {
    if (!chips.length) return;
    const wrap = document.createElement("div");
    wrap.className = "chip-row";
    chips.forEach((chip) => {
      const label = typeof chip === "string" ? chip : chip.label;
      const kind = (typeof chip === "string" ? "act" : chip.kind) || "act";
      const b = document.createElement("button");
      b.className = "chip " + (kind === "plan" ? "chip-plan" : "chip-act");
      b.textContent = label;
      b.title = kind === "plan" ? "Click to send (plan)" : "Click to send (act)";
      b.addEventListener("click", () => {
        b.classList.add("sent");
        send(label);
      });
      wrap.appendChild(b);
    });
    turn.body.insertBefore(wrap, turn.actions);
  }

  // Cline-style link vetting: external links stay unclickable until the
  // server confirms they resolve; mailto must appear in the session sources.
  async function verifyLinks(scope, sessionId) {
    const links = [];
    scope.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href") || "";
      if (/^https?:\/\//i.test(href) || href.toLowerCase().startsWith("mailto:")) links.push(href);
    });
    if (!links.length) return;
    const uniq = Array.from(new Set(links));
    try {
      const res = await api("/api/verify_links", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, links: uniq }),
      });
      const data = await res.json();
      scope.querySelectorAll("a[href]").forEach((a) => {
        const href = a.getAttribute("href") || "";
        if (!(href in data.ok)) return;
        if (data.ok[href]) { a.classList.add("link-verified"); return; }
        const isMail = href.toLowerCase().startsWith("mailto:");
        const span = document.createElement("span");
        span.className = "link-broken";
        span.title = isMail ? "Address not found in any source" : "Link could not be verified";
        span.textContent = a.textContent;
        a.replaceWith(span);
      });
    } catch (e) { /* keep links as-is */ }
  }

  // ── File links in chat text ───────────────────────────────────
  // Muji's replies reference local files as raw paths (e.g.
  // `C:\Users\Jan\report.md` or `static/app.js`). A single delegated
  // listener on the chat scroller turns any link whose href looks like a
  // local path into a Files-tab jump: the right panel opens on Files, the
  // tree navigates to the file's folder, and the file row is selected.
  const WIN_PATH_RE = /^[A-Za-z]:[\\\/]/;
  const UNIQ_PATH_RE = /^(?:[A-Za-z]:)?(?:[\\\/][\w.-]+){2,}[\\\/][\w.-]+$/;
  const REL_PATH_RE  = /^[A-Za-z0-9._-]+(?:[\\/][A-Za-z0-9._-]+){1,4}$/;
  function looksLikeFilePath(href) {
    if (!href) return false;
    // marked percent-encodes backslashes in Windows paths (C:%5CUsers%5C…),
    // which defeats every regex below — decode first (no-op for plain hrefs)
    if (href.includes("%")) { try { href = decodeURIComponent(href); } catch (e) { /* keep raw */ } }
    if (href.startsWith("/") && !href.startsWith("//") &&
        (href.match(/\//g) || []).length >= 2 &&
        /[/\\][A-Za-z0-9._-]+$/i.test(href)) return true;
    if (WIN_PATH_RE.test(href)) return true;
    if (UNIQ_PATH_RE.test(href)) return true;
    return REL_PATH_RE.test(href);
  }
  function pathFromHref(href) {
    let p = href;
    if (p.includes("%")) { try { p = decodeURIComponent(p); } catch (e) { /* keep raw */ } }
    if (WIN_PATH_RE.test(p)) {
      const norm = p.replace(/\//g, "\\");
      const root = state.config && state.config.root_dir;
      if (root) {
        const lower = (x) => x.toLowerCase();
        const r = root.replace(/\//g, "\\");
        if (lower(norm) === lower(r)) return r;
        if (lower(norm).startsWith(lower(r) + "\\")) return norm;
      }
      return norm;
    }
    if (p.startsWith("/")) return p.slice(1);
    return p;
  }
  el.chatScroll.addEventListener("click", async (ev) => {
    const a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
    if (!a) return;
    // anchors with their own behaviour (attachment thumbs, download/open
    // buttons on file chips) keep it — this only handles plain text links
    if (a.closest(".msg-attach, .file-chip, .msg-actions")) return;
    const href = a.getAttribute("href") || "";
    if (!looksLikeFilePath(href)) return;  // real links keep default (new tab)
    ev.preventDefault();
    const p = pathFromHref(href);
    const dir = p.replace(/[\\\/][^\\\/]+$/, "");
    const base = (p.split(/[\\\/]/).pop() || "").toLowerCase();
    setRightPanelOpen(true);
    setUpperTab("files");
    if (dir) enterFolder(dir);  // pushState + loadTree, then select below
    // wait for the tree rows to render, then select the file row
    // (exact path match first, basename fallback for relative hrefs)
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 50));
      const rows = [...el.rpTree.querySelectorAll(".tree-row")];
      const row = rows.find((r) => {
        const t = (r.querySelector(".tr-name") || {}).title || "";
        return t === p || t.toLowerCase().endsWith(p.toLowerCase());
      }) || rows.find((r) =>
        ((r.querySelector(".tr-name") || {}).title || "").split(/[\\\/]/).pop().toLowerCase() === base);
      if (row) {
        el.rpTree.querySelectorAll(".tree-row.selected")
          .forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        row.scrollIntoView({ block: "nearest" });
        return;
      }
    }
  });


  function hideWelcome() { el.welcome.hidden = true; }
  function showWelcome() { el.welcome.hidden = false; }

  // ── Welcome robot (fixed at max size, no − / ＋) ───────────────
  const WM_MAX = 160;
  (function applyWelcomeMark() {
    const img = el.welcomeMark.querySelector("img");
    img.style.width = img.style.height = WM_MAX + "px";
  })();
  function scrollToBottom(force) {
    if (force || !state.userScrolledUp) {
      if (force)
        // instant jump (e.g. after loading history / switching chats) —
        // "instant" overrides the CSS scroll-behavior:smooth (note:
        // behavior:"auto" does NOT — it resolves to the CSS value)
        el.chatScroll.scrollTo({ top: el.chatScroll.scrollHeight, behavior: "instant" });
      else
        el.chatScroll.scrollTop = el.chatScroll.scrollHeight;
    }
  }

  // "10:42" today, "Sep 20 · 10:42" on other days (ts = epoch seconds or ms)
  function fmtMsgTime(ts) {
    if (!ts) return "";
    const t = new Date(ts < 1e12 ? ts * 1000 : ts);
    if (isNaN(t)) return "";
    const now = new Date();
    const hm = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (t.toDateString() === now.toDateString()) return hm;
    return t.toLocaleDateString([], { month: "short", day: "numeric" }) + " · " + hm;
  }

  function addUserMessage(text, files, ts) {
    hideWelcome();
    const msg = document.createElement("div");
    msg.className = "msg msg-user";
    const row = document.createElement("div");
    row.className = "msg-user-row";
    const hasFiles = (files || []).some((f) => f && (f.name || f.url));
    // image-only sends have NO words — the thumbnail row IS the message,
    // so skip the (empty) bubble instead of rendering a ghost "(attachment)"
    const t = (text || "").toString();
    // per-bubble timestamp chip — mid-row, OUTSIDE the bubble: user = LEFT
    // of the bubble, muji = right (same .msg-time class, hover-revealed,
    // every bubble keeps its own even for consecutive messages)
    const tstr = fmtMsgTime(ts);
    const tm = tstr ? document.createElement("time") : null;  // no ts → no chip (an empty pill is a bug, not a feature)
    if (tm) {
      tm.className = "msg-time";
      tm.textContent = tstr;
      tm.title = new Date(ts < 1e12 ? ts * 1000 : ts).toLocaleString();
    }
    if (t || !hasFiles) {
      // hover-revealed copy button (left of the bubble, after the chip)
      const cp = document.createElement("button");
      cp.className = "msg-copy";
      cp.textContent = "⧉";
      cp.title = "Copy message";
      cp.addEventListener("click", () => copyText(t, cp));
      const b = document.createElement("div");
      b.className = "msg-bubble";
      renderMarkdown(b, t);
      if (tm) row.appendChild(tm);
      row.append(cp, b);
      msg.appendChild(row);
    } else if (tm) {
      // image-only send: no bubble row, but the chip still gets its own row
      row.appendChild(tm);
      msg.appendChild(row);
    }
    // attachments: images render as thumbnails (that's what muji saw —
    // its pixels were inlined into the model request), other files as chips
    const all = (files || []).filter((f) => f && (f.name || f.url));
    if (all.length) {
      const arow = document.createElement("div");
      arow.className = "msg-attach";
      all.forEach((f) => {
        const kind = f.kind || guessKind(f.name || f.path || "");
        if (kind === "image" && f.url) {
          const a = document.createElement("a");
          a.className = "msg-attach-img";
          a.href = inlineUrl(f.url);
          a.title = (f.original || f.name) + " — click to open full size";
          const im = document.createElement("img");
          im.src = inlineUrl(f.url);
          im.alt = f.original || f.name;
          im.loading = "lazy";
          a.appendChild(im);
          a.addEventListener("click", (ev) => { ev.preventDefault(); openPreview(f); });
          arow.appendChild(a);
        } else {
          const c = document.createElement("span");
          c.className = "attach-chip";
          c.appendChild(icon("paperclip"));
          c.appendChild(document.createTextNode(" " + (f.original || f.name)));
          c.title = f.url || f.path || "";
          c.addEventListener("click", () => openPreview(f));
          arow.appendChild(c);
        }
      });
      msg.appendChild(arow);
    }
    el.messages.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  // The resume nudge goes to the model in full, but the chat shows a compact
  // marker instead of a big "Continue — …" user bubble
  function addResumeMarker() {
    hideWelcome();
    const msg = document.createElement("div");
    msg.className = "msg msg-resume";
    msg.textContent = "↻ Resumed — continuing where the run stopped";
    el.messages.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  // Plan→Act auto-execute marker — compact, like the resume marker
  function addPlanMarker() {
    hideWelcome();
    const msg = document.createElement("div");
    msg.className = "msg msg-resume";
    msg.textContent = "▶ Act mode — executing the plan above";
    el.messages.appendChild(msg);
    scrollToBottom();
    return msg;
  }

  // The turn's time — every bubble in the turn chips it (addSegment reads
  // turn.time at build time), so this also refreshes the chips already in
  // the DOM (the draft snapshot's created_at replaces the client-side guess).
  function setTurnTime(turn, ts) {
    turn.time = ts;
    const s = fmtMsgTime(ts);
    const full = new Date(ts < 1e12 ? ts * 1000 : ts).toLocaleString();
    (turn.root ? turn.root.querySelectorAll(".msg-time") : []).forEach((c) => {
      if (!s) { c.textContent = ""; c.removeAttribute("title"); return; }
      c.textContent = s;
      c.title = full;
    });
  }

  function addAssistantTurn() {
    hideWelcome();
    const turn = { rawText: "", streamStarted: false,
                   time: Date.now() / 1000 };  // this turn's time — every bubble in it chips it
    const root = document.createElement("div");
    root.className = "msg msg-assistant";
    turn.root = root;

    // status group: avatar + "Working…" chip + thinking hint — ONE unit that
    // GLIDES just ABOVE whichever element is active (streaming bubble →
    // running tool group → final answer). Absolutely positioned in the turn
    // root; JS sets `top` + a transition. Consecutive turns' avatar is
    // hidden (the body keeps a 34px gutter so bubbles align under the first).
    const prev = el.messages.lastElementChild;
    const consecutive = !!(prev && prev.classList.contains("msg-assistant"));
    const group = document.createElement("div");
    group.className = "turn-status";
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar" + (consecutive ? " msg-avatar-spacer" : "");
    const img = document.createElement("img");
    img.src = "/static/favicon.png?v=20";
    img.alt = "";
    img.width = 34;
    img.height = 34;
    avatar.appendChild(img);
    group.appendChild(avatar);
    // Boss: "working on top, thinking below" — the status column stacks the
    // Working/Done chip over the thinking hint (one clickable unit)
    const col = document.createElement("div");
    col.className = "think-col";
    const row = document.createElement("div");
    row.className = "think-row";
    row.hidden = true;
    const chip = document.createElement("span");
    chip.className = "think-chip";
    const dot = document.createElement("span");
    dot.className = "dot";
    const lab = document.createElement("span");
    lab.className = "think-label";
    lab.textContent = "Working…";
    chip.append(dot, lab);
    row.appendChild(chip);
    // thinking (model reasoning_content) streams to the right-panel Thinking
    // tab; the chat keeps only a tiny hint chip per thinking burst
    turn.hint = document.createElement("a");
    turn.hint.className = "think-hint";
    turn.hint.hidden = true;
    turn.hint.innerHTML = '<span class="mi mi-brain"></span> thinking…';
    turn.hint.addEventListener("click", () => {
      setRightPanelOpen(true);
      setUpperTab("thinking");
    });
    col.append(row, turn.hint);
    group.appendChild(col);
    root.appendChild(group);
    turn.group = group;
    turn.row = row;
    turn.chip = chip;
    turn.hintStart = 0;
    turn.lastEventWasThinking = false;
    turn.thinkAutoed = false;  // lower pane auto-switches to Thinking once per turn

    // body column: everything that used to be the turn root
    const body = document.createElement("div");
    body.className = "msg-turn-body";
    turn.body = body;
    root.appendChild(body);

    // timeline — text segments and tool runs in the order they occurred:
    //   text → runs → text → runs → … → final answer
    const timeline = document.createElement("div");
    timeline.className = "msg-timeline";
    turn.timeline = timeline;
    body.appendChild(timeline);
    turn.seg = null;         // text segment currently streaming (or null)
    turn.answerSeg = null;   // segment that holds the final answer
    turn.mode = state.mode;  // mode this turn ran in → tints the final bubble

    // "files touched this turn" — collapsed by default: one count line
    // (📁 6 files changed) that expands to the chips. A flat chip dump
    // was a wall of cards on long self-edit turns.
    const filesRow = document.createElement("details");
    filesRow.className = "msg-files";
    filesRow.hidden = true;
    const filesSum = document.createElement("summary");
    filesRow.appendChild(filesSum);
    const filesBody = document.createElement("div");
    filesBody.className = "msg-files-body";
    filesRow.appendChild(filesBody);
    turn.filesRow = filesRow;
    turn.filesSum = filesSum;
    turn.filesBody = filesBody;
    body.appendChild(filesRow);

    const actions = document.createElement("div");
    actions.className = "msg-actions";
    turn.actions = actions;
    body.appendChild(actions);

    // timestamp: per-bubble chips now (addSegment) — no more footer time
    el.messages.appendChild(root);
    watchGroup(turn);  // 💭 hint appearing grows the group → --lane + re-park
    scrollToBottom();
    return turn;
  }

  // The status group's lane height in px — the group's LIVE height, not a
  // constant: the 💭 hint chip appearing makes the group taller than the
  // 52px avatar-only lane, and a fixed lane let the group paint over the
  // bubble below (the overlap bug). laneH(turn) reads the group's rendered
  // height; the per-turn root's --lane custom property feeds the same value
  // into .msg-turn-body's padding-top and .lane-above's margin-top, so the
  // lane always matches the group exactly.
  // Measure the think-col's CONTENT, NOT the group: .turn-status is
  // min-height: 52px, so when the chip+hint column grows past that the
  // group's box stays at 52 while the content overflows it — reading the
  // group under-measures the lane and the overflow paints over the bubble
  // below. Sum the col's children + gaps (the col's own bounding rect
  // includes the flex gap, which would over-measure by a few px).
  const laneH = (turn) => {
    if (!turn.group) return 52;
    let h = 0;
    const col = turn.group.querySelector(".think-col");
    if (col) {
      const gap = 3;  // .think-col gap
      let n = 0;
      for (const c of col.children) {
        if (c.hidden) continue;
        h += Math.ceil(c.getBoundingClientRect().height) + (n ? gap : 0);
        n++;
      }
    }
    return Math.max(52, h);
  };
  // Keep the turn root's --lane in sync with the group's live height (the
  // hint chip toggling changes it) and re-park, since the lane moved.
  function syncLane(turn) {
    if (!turn.group || !turn.root) return;
    const h = laneH(turn);
    if (turn.root.style.getPropertyValue("--lane") !== h + "px") {
      turn.root.style.setProperty("--lane", h + "px");
      // re-park: the lane moved. If no target yet (history rebuild shows
      // the 💭 hint BEFORE any timeline element exists), park at the first
      // one so the group isn't stranded at the top with a stale lane.
      const t = turn.group._target;
      if (t && t.isConnected) moveAvatar(turn, t, false);
      else if (turn.timeline.firstElementChild)
        moveAvatar(turn, turn.timeline.firstElementChild, false);
    }
  }
  // One observer for every status group: fires when the 💭 hint appears or
  // disappears (think-col height changes) → update --lane + re-park.
  let laneObserver = null;
  function watchGroup(turn) {
    if (!turn.group || !turn.root) return;
    if (!laneObserver) {
      laneObserver = new ResizeObserver((entries) => {
        for (const e of entries) {
          const t = e.target._turn;
          if (t) syncLane(t);
        }
      });
    }
    const col = turn.group.querySelector(".think-col");
    // watch the col — the group's min-height box would never resize when
    // the col overflows it. _turn also stays on the group: reparkAvatars
    // iterates .turn-status nodes and needs the back-reference.
    turn.group._turn = turn;
    (col || turn.group)._turn = turn;
    laneObserver.observe(col || turn.group);
    syncLane(turn);
  }
  // Park the status group (avatar + Working/Done chip on top, thinking hint
  // below) just ABOVE the currently active element of the turn (streaming
  // bubble → running tool group → final answer). The group is absolutely
  // positioned in the turn root, so the target's offsetTop is in the same
  // coordinate space (root = offsetParent).
  //
  // The lane TRAVELS with the group: the parked target reserves a LANE px
  // margin above itself (.lane-above) and every other timeline element loses
  // it, so the group always sits in empty space and can never cover the
  // element above the target (v=40 shipped the overlap: the lane was only
  // the body's top padding, so parking above a later element covered
  // whatever sat above it). The first timeline element needs no class — the
  // body's LANE px top padding is already its lane.
  function moveAvatar(turn, target, smooth) {
    if (!turn.group || !target) return;
    // a text segment is wrapped in its .msg-seg-row (the time-chip row) —
    // park on the ROW: it's the timeline child that owns the lane margin
    if (target.classList && target.classList.contains("msg-content"))
      target = target.parentElement;
    if (!target || !target.isConnected) return;
    const g = turn.group;
    const tl = turn.timeline;
    tl.querySelectorAll(".lane-above").forEach((n) => n.classList.remove("lane-above"));
    // Two-phase park. Phase 1 (now): swap the lane classes. Phase 2 (next
    // frame): after the caller's pending --lane change (syncLane) has
    // reflowed, re-read the target's top (lane ON) and set the group's top.
    //
    // Why the single-phase version (measure-before-class, f6168f7) still
    // overlapped: syncLane's setProperty("--lane") and this moveAvatar run
    // in the SAME synchronous block, so the class toggle flushed layout
    // BEFORE the new --lane was applied — the .lane-above margin reflowed
    // at the OLD (52px) size while laneH() already returned the NEW grown
    // height, and y landed short by the growth (the 💭 hint). Deferring
    // the final measure+write to rAF guarantees the margin has reflowed at
    // the final lane. (A background tab throttles rAF to ~1Hz — the park
    // just settles a beat late; the next moveAvatar/resize re-parks.)
    if (target !== tl.firstElementChild) target.classList.add("lane-above");
    g._target = target;  // remembered for resize re-parking
    requestAnimationFrame(() => {
      if (!g.isConnected || !target.isConnected) return;
      // -1px safety: parking with the group's bottom edge EXACTLY on the
      // target's top edge lets sub-pixel layout rounding leak a 1–3px
      // overlap (measured live: oy up to 0.46px at the shared edge)
      const y = Math.max(0, target.offsetTop - laneH(turn) - 1);
      if (g.dataset.y !== String(y)) {
        g.dataset.y = String(y);
        if (smooth) g.style.transition = "top .28s cubic-bezier(.3,.8,.3,1)";
        g.style.top = y + "px";
      }
    });
  }
  // window resize reflows the bubbles → re-park every status group above its
  // current active element (no animation; the layout just shifted). The DOM
  // is the source of truth (group._target, with its .lane-above lane still
  // on), so this also covers history turns that aren't in any live view.
  function reparkAvatars() {
    el.messages.querySelectorAll(".turn-status").forEach((g) => {
      if (!g._target || !g._target.isConnected) return;
      const y = Math.max(0, g._target.offsetTop - (g._turn ? laneH(g._turn) : 52));
      g.dataset.y = String(y);
      g.style.transition = "none";
      g.style.top = y + "px";
    });
  }

  // one hover time chip per bubble — the turn's time on every bubble it
  // contains (consecutive turns keep their own chips; no dedup). The chip
  // lives in the seg ROW (right of the bubble, mid-row), not the bubble.
  // one hover time chip per bubble — the turn's time on every bubble it
  // contains (consecutive turns keep their own chips; no dedup). The chip
  // lives in the seg ROW (right of the bubble, mid-row), not the bubble.
  function makeTimeChip(ts) {
    const s = fmtMsgTime(ts);
    if (!s) return null;  // no time → no chip (an empty pill is a bug, not a feature)
    const chip = document.createElement("time");
    chip.className = "msg-time";
    chip.textContent = s;
    chip.title = new Date(ts < 1e12 ? ts * 1000 : ts).toLocaleString();
    return chip;
  }

  // new text segment in the turn timeline (raw buffer resets per segment).
  // Wrapped in a .msg-seg-row so the time chip can sit mid-row to the
  // RIGHT of the bubble (the row is the layout unit, the seg is the bubble).
  // ts: optional explicit timestamp. Callers that rebuild from stored data
  // (history / draft parts) pass the part's own ts (or the turn's as
  // fallback); live streaming calls pass nothing → now, the moment this
  // segment was created. That's what gives each bubble in a long run its
  // OWN time instead of the whole turn sharing the start time.
  function addSegment(turn, ts) {
    const row = document.createElement("div");
    row.className = "msg-seg-row";
    const seg = document.createElement("div");
    seg.className = "msg-content bubble";
    const t = ts || (Date.now() / 1000);
    row.append(seg, makeTimeChip(t));  // chip AFTER the bubble = right side
    turn.timeline.appendChild(row);
    turn.rawText = "";
    return seg;
  }

  // streaming: accumulate raw tokens in the current segment, re-render
  // markdown at most per frame. A token that doesn't belong to the current
  // segment (a tool run happened in between) starts a NEW segment, so the
  // timeline keeps the natural order the events happened in. The raw buffer
  // lives on the segment itself: a frame that fires late (a tool_start
  // arrived first) must render ITS text into ITS segment, never the new one.
  function appendToken(turn, text) {
    if (!turn.seg) {
      turn.seg = addSegment(turn);
      if (!turn.streamStarted) {
        turn.streamStarted = true;
        turn.row.hidden = false;
        thinkLabel(turn, "Answering…");
      }
      moveAvatar(turn, turn.seg, true);  // avatar glides to the streaming bubble
    }
    const seg = turn.seg;
    seg._raw = (seg._raw || "") + text;
    turn.rawText = seg._raw;
    // Long answers: re-parsing + re-sanitizing + re-highlighting the ENTIRE
    // buffer per token is O(n²) — past ~25KB the per-frame cost is big
    // enough to hitch a phone's main thread (the PWA "freeze" feel). Stop
    // live re-rendering at the cap; the final `done` frame re-renders the
    // full text via renderAnswer, so nothing is lost.
    if (seg._raw.length > 25000) {
      if (seg._liveStopped) return;
      seg._liveStopped = true;
      seg._renderPending = false;
      renderMarkdown(seg, seg._raw);
      const cur = seg.lastElementChild;
      if (cur && (cur.tagName === "P" || cur.tagName === "DIV" ||
                  cur.tagName === "LI"))
        cur.textContent += " …";
      scrollToBottom();
      return;
    }
    if (seg._renderPending) return;
    seg._renderPending = true;
    requestAnimationFrame(() => {
      seg._renderPending = false;
      renderMarkdown(seg, seg._raw);
      scrollToBottom();
    });
  }

  // render the authoritative final answer into its segment (the live one if
  // the stream is still open, the one answer_start marked, else a new one).
  // ts: optional — the final answer's OWN time (history: m.answer_ts);
  // the live path lands on the already-streamed segment (chipped when it
  // was created), so ts only matters for the new-segment fallback.
  function renderAnswer(turn, content, ts) {
    const seg = turn.answerSeg || turn.seg || addSegment(turn, ts || turn.time);
    turn.answerSeg = seg;
    // tint the final bubble only when the turn was real work: a plan
    // (plan mode) or an execution (tools ran). Plain chat stays neutral —
    // color is reserved for plans + final summaries of execution.
    if (turn.mode === "plan" || turn.toolsUsed)
      seg.classList.add("mode-" + (turn.mode || "act"));
    seg._raw = content;
    turn.rawText = content;
    seg.innerHTML = "";
    renderMarkdown(seg, content);
    return seg;
  }

  function thinkLabel(turn, text) {
    const lab = turn.chip && turn.chip.querySelector(".think-label");
    if (lab) lab.textContent = text;
  }

  function settleThink(turn) {
    if (!turn.chip) return;
    const dot = turn.chip.querySelector(".dot");
    if (dot) dot.style.animation = "none";
    const secs = turn.start ? Math.round((Date.now() - turn.start) / 1000) : 0;
    thinkLabel(turn, secs > 0 ? "Worked " + secs + "s" : "Done");
  }

  // ── Thinking: the full text goes to the right-panel Thinking ───
  // ── tab; the chat shows only a tiny hint line per turn. ────────
  function appendThinking(turn, text) {
    if (!text) return;
    if (!turn.lastEventWasThinking) {
      turn.hintStart = Date.now();
      turn.hint.hidden = false;
      turn.hint.innerHTML = '<span class="mi mi-brain"></span> thinking…';
      turn.row.hidden = false;
      syncLane(turn);  // the hint grew the group — the lane must move NOW,
                       // not on the ResizeObserver's async tick
    }
    turn.lastEventWasThinking = true;
  }

  function finalizeThinkingHint(turn) {
    if (turn.lastEventWasThinking && turn.hintStart) {
      const secs = Math.max(1, Math.round((Date.now() - turn.hintStart) / 1000));
      turn.hint.innerHTML = '<span class="mi mi-brain"></span> thought ' + secs + 's → Thinking tab';
      turn.hintStart = 0;
    }
    turn.lastEventWasThinking = false;
    syncLane(turn);  // the finalized text can wrap to a new line → taller col
  }

  function renderThoughtHistory(turn) {
    const dot = turn.chip.querySelector(".dot");
    if (dot) dot.style.animation = "none";
    const lab = turn.chip.querySelector(".think-label");
    if (lab) lab.textContent = "Done";
    turn.hint.hidden = false;
    turn.hint.innerHTML = '<span class="mi mi-brain"></span> thought → Thinking tab';
    turn.row.hidden = false;
    // In loadHistory the root isn't in the DOM yet (the col would measure
    // 0) — watchGroup's syncLane after appendChild sets the real --lane
    // before the first moveAvatar. When the root IS live (resume/replay),
    // syncLane updates --lane NOW so the next moveAvatar parks on the
    // grown lane instead of the stale 52.
    if (turn.root && turn.root.isConnected) syncLane(turn);
  }


  // ── Progress bar (plan-driven, honest fallback) ────────────────
  function resetProgress() {
    if (state.progress && state.progress.timer) clearInterval(state.progress.timer);
    state.progress = null;
    el.progress.hidden = true;
    el.progress.classList.remove("indeterminate", "complete");
    el.progressFill.style.width = "0%";
    el.progressLabel.textContent = "";
    el.progressSteps.innerHTML = "";
  }

  function startProgress(items) {
    resetProgress();
    state.progress = { mode: "plan", items: items, done: 0, active: -1,
                       steps: 0, start: Date.now(), timer: null };
    renderProgressChips();
    el.progress.hidden = false;
    tickProgress();
  }

  // One pill per STEP: line — pending (dim) / active (accent + pulse) /
  // done (green ✓ + strikethrough). Rebuilt only on start; tickProgress
  // just flips classes so the row never reflows mid-run.
  function renderProgressChips() {
    const p = state.progress;
    if (!p || !p.items.length) return;
    el.progressSteps.innerHTML = "";
    p.chips = p.items.map((label) => {
      const chip = document.createElement("span");
      chip.className = "pstep";
      const dot = document.createElement("span");
      dot.className = "pstep-dot";
      dot.textContent = "○";
      const txt = document.createElement("span");
      txt.className = "pstep-txt";
      txt.textContent = label;
      chip.append(dot, txt);
      el.progressSteps.appendChild(chip);
      return chip;
    });
  }

  function paintProgressChips() {
    const p = state.progress;
    if (!p || !p.chips) return;
    p.chips.forEach((chip, i) => {
      chip.classList.toggle("done", i < p.done);
      chip.classList.toggle("active", i === p.active && i >= p.done);
      chip.querySelector(".pstep-dot").textContent =
        i < p.done ? "✓" : (i === p.active && i >= p.done ? "●" : "○");
    });
  }

  function progressPhase(label) {
    // the plan bar wins; otherwise show an honest indeterminate phase bar
    if (state.progress && state.progress.mode === "plan") return;
    if (!state.progress) {
      resetProgress();
      state.progress = { mode: "phase", items: [], done: 0, active: -1,
                         steps: 0, start: Date.now(), label: label || "Working", timer: null };
      el.progress.classList.add("indeterminate");
    }
    if (label) state.progress.label = label;
    el.progress.hidden = false;
    tickProgress();
  }

  function updateProgress(d) {
    const p = state.progress;
    if (!p || p.mode !== "plan") return;
    if (d.status === "active") p.active = d.index;
    p.done = d.done || 0;
    tickProgress();
  }

  function tickProgress() {
    const p = state.progress;
    if (!p) return;
    const secs = Math.round((Date.now() - p.start) / 1000);
    if (p.mode === "plan" && p.items.length) {
      el.progressFill.style.width = (p.done / p.items.length * 100) + "%";
      paintProgressChips();
      el.progressLabel.textContent =
        "Step " + Math.min(p.done + 1, p.items.length) + " of " + p.items.length +
        "  ·  " + secs + "s";
    } else {
      el.progressLabel.textContent = (p.label || "Working") +
        "  ·  tool step " + p.steps + "  ·  " + secs + "s";
    }
    if (!p.timer) p.timer = setInterval(tickProgress, 1000);
  }

  function finishProgress() {
    const p = state.progress;
    if (!p) return;
    if (p.timer) clearInterval(p.timer);
    p.timer = null;
    if (p.mode === "plan") {
      el.progressFill.style.width = "100%";
      // tick the last chip even if the backend never sent it as active
      p.done = p.items.length;
      p.active = -1;
      paintProgressChips();
    }
    el.progress.classList.add("complete");
    const secs = Math.round((Date.now() - p.start) / 1000);
    el.progressLabel.textContent = "✓ Done in " + secs + "s";
    doneToast(secs);
    setTimeout(resetProgress, 2500);
  }

  // Bottom toast for a finished run — same visual language as the
  // question/approval toasts, green accent, auto-dismisses in 3s.
  let _doneToastTimer = null;
  function doneToast(secs) {
    const old = document.querySelector(".wait-toast.done");
    if (old) old.remove();
    if (_doneToastTimer) { clearTimeout(_doneToastTimer); _doneToastTimer = null; }
    const b = document.createElement("div");
    b.className = "wait-toast done";
    const pill = document.createElement("span");
    pill.className = "wt-pill";
    pill.textContent = "✓";
    const s = document.createElement("span");
    s.textContent = "Done in " + secs + "s";
    b.append(pill, s);
    document.body.appendChild(b);
    _doneToastTimer = setTimeout(() => { b.remove(); _doneToastTimer = null; }, 3000);
  }

  // Transient job-strip label for a resume run — setJob() treats this
  // specially: it shows while the resume is in flight but never clobbers
  // the pinned goal (v.jobGoal), which stays until the run settles.
  const RESUME_LABEL = "↻ Resuming where the run stopped";
  const EXECUTE_PLAN_LABEL = "▶ Executing the plan above";

  // Sent when the user flips Plan → Act after a plan turn: execute the plan
  // that just appeared in chat, verbatim, without re-planning. Goes to the
  // model only — the chat shows the compact ▶ marker instead of a user
  // bubble (same treatment as the resume nudge).
  const EXECUTE_PLAN_TEXT =
    "Execute the plan below, exactly as written, in order. You are now in " +
    "Act mode — file edits and commands are allowed. Do not re-plan or " +
    "re-research; start with step 1. If a step turns out to be wrong or " +
    "blocked mid-run, stop, explain what you found, and propose the minimal " +
    "adjustment before continuing.\n\nPLAN:";

  // Pinned job strip: goal of the run in flight (below the progress bar).
  // Long goals wrap up to 3 lines, then scroll. It stays pinned through the
  // run's life AND after it settles — `setJobDone()` marks it finished
  // (green ✓, no pulse) so the user always sees what the last run worked on;
  // it only clears on a NEW run, chat switch, or clearView().
  function setJob(text) {
    if (text) {
      // The resume/execute-plan labels are transient — they must NOT clobber
      // the pinned goal (a real run's text does: that's the new goal of the
      // run in flight).
      if (text !== RESUME_LABEL && text !== EXECUTE_PLAN_LABEL) {
        const v = state.sessionId ? view(state.sessionId) : null;
        if (v) v.jobGoal = text;
      }
      el.jobStrip.classList.remove("done");  // in-flight again: pulse back on
      el.jobText.textContent = text;
      el.jobText.scrollTop = 0;  // fresh run: start at the top of the goal
      el.jobStrip.hidden = false;
    } else {
      el.jobStrip.hidden = true;
      el.jobText.textContent = "";
    }
  }

  // Settle the strip: keep the last goal visible, stop the pulse. `checked`
  // adds a ✓ prefix (the run actually finished; stopped/errored runs settle
  // without one). No-op if the strip is already hidden.
  function setJobDone(checked) {
    if (el.jobStrip.hidden) return;
    el.jobStrip.classList.add("done");
    if (checked) {
      const t = el.jobText.textContent.trim();
      if (t && !t.startsWith("✓ ")) el.jobText.textContent = "✓ " + t;
    }
  }

  // The goal a resume should keep pinned: this view's last real run goal,
  // else the last user message in the rendered chat (covers a page reload —
  // the view object is gone but the DOM history is not).
  function resumeGoal() {
    const v = state.sessionId ? view(state.sessionId) : null;
    if (v && v.jobGoal) return v.jobGoal;
    const rows = el.messages.querySelectorAll(".msg-user .msg-bubble");
    for (let i = rows.length - 1; i >= 0; i--) {
      const t = (rows[i].textContent || "").trim();
      if (t) return t;
    }
    return null;
  }

  // Trim a settled phase's output to a few lines + a "show more" toggle, so an
  // expanded tool group stays compact instead of a wall of text. Short output
  // shows in full; the box is scrollable either way (nothing is lost).
  const PHASE_PRE_LINES = 4;
  function phaseOutput(det, text) {
    let pre = det.querySelector("pre");
    if (!pre) { pre = document.createElement("pre"); det.appendChild(pre); }
    pre.classList.remove("pre-more");
    pre.textContent = text || "";
    const oldBtn = det.querySelector(".phase-more-btn");
    if (oldBtn) oldBtn.remove();
    const lines = (text || "").split("\n");
    if (lines.length > PHASE_PRE_LINES) {
      const b = document.createElement("button");
      b.className = "phase-more-btn";
      const more = lines.length - PHASE_PRE_LINES;
      b.textContent = "⌄ show more (+" + more + " lines)";
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        b.textContent = pre.classList.toggle("pre-more")
          ? "⌃ show less"
          : "⌄ show more (+" + more + " lines)";
      });
      det.appendChild(b);
    }
  }

  // one tool-run row. `done` rows (history / settled) show ok/err + output.
  // The label is a human phrase ("Reading app.js") — raw name/args live in
  // the Terminal tab; the row's <details> body still carries the output.
  // `rawArgs` is the RAW args JSON (phrase target comes from it); `argsStr`
  // is the display fallback (shortenArgs output) used when the JSON is
  // missing/unparseable.
  // phase tag for the mockup-style pill: plan (reading/exploring) /
  // verify (test & check commands) / tool (everything else). The pill is
  // the first thing in the row — the row reads [plan] (file-text icon) Reading app.js ✓
  function phaseOf(name, argsStr) {
    const plan = ["read_file", "list_dir", "search_files", "local_search",
                  "index_documents", "web_search", "fetch_url", "browser"];
    if (plan.includes(name)) return "plan";
    if (name === "run_command") {
      let cmd = "";
      try { cmd = String((JSON.parse(argsStr || "").command) || "").toLowerCase(); }
      catch (e) { cmd = String(argsStr || "").toLowerCase(); }
      if (/(test|check|verify|pytest|lint|node\s+--check)/.test(cmd)) return "verify";
      // ship verbs get their own dimmed tag (mockup "done" phase)
      if (/git (add|commit|push)/.test(cmd)) return "done";
    }
    return "tool";
  }
  // sprite names (index.html <symbol id="i-…">) — stroke tints per theme
  function phaseIcon(name, ph) {
    const byTool = {
      read_file: "file-text", list_dir: "folder", search_files: "search",
      local_search: "search", index_documents: "layers", web_search: "globe",
      fetch_url: "globe", browser: "globe", write_file: "pen", edit_file: "pen",
      run_command: "terminal", ask_user: "help-circle",
    };
    return byTool[name] || (ph === "plan" ? "search" : "terminal");
  }
  function makePhase(name, argsStr, done, d, rawArgs) {
    const det = document.createElement("details");
    det.className = "phase";
    det.dataset.tool = name;
    det.dataset.args = rawArgs || argsStr || "";
    const ph = phaseOf(name, rawArgs || argsStr || "");
    const sum = document.createElement("summary");
    const tag = document.createElement("span");
    tag.className = "tl-tag " + ph;
    tag.textContent = ph;
    const ico = document.createElement("span");
    ico.className = "tl-ico";
    ico.appendChild(icon(phaseIcon(name, ph)));
    const label = document.createElement("span");
    label.className = "phase-title";
    const phrase = toolPhrase(name, rawArgs || argsStr || "");
    const verb = document.createElement("span");
    verb.className = "phase-verb";
    verb.textContent = phrase.verb;
    label.appendChild(verb);
    if (phrase.target) {
      const tgt = document.createElement("span");
      tgt.className = "phase-target";
      tgt.textContent = phrase.target;
      label.appendChild(tgt);
    }
    const st = document.createElement("span");
    st.className = "tl-st" + (done ? (d.ok ? " ok" : " err") : " run");
    st.textContent = done ? (d.ok ? "✓" : "✗") : "…";
    sum.append(tag, ico, label, st);
    // status is COLOR on the verb too — green = done, red = failed,
    // accent = running (the ✓/✗ glyph is the quick-scan, the color the glance)
    if (done) verb.classList.add(d.ok ? "ok" : "err");
    det.appendChild(sum);
    phaseOutput(det, done
      ? ((d.detail || "") + (d.ms != null ? "  (" + d.ms + " ms)" : ""))
      : "… running");
    if (done) det.dataset.done = "1";
    return det;
  }

  // Consecutive tool runs group into one row (less clutter):
  //   4 steps · Reading ×2 · Running   ✓
  // A group is a <details> whose body holds the individual .phase rows.
  // OPEN by default: the steps are visible, not a mystery box. More than
  // GROUP_MAX_ROWS rows → the body scrolls (CSS cap) and the footer shows
  // "↑ N more" — the summary row stays the only way to collapse.
  // matches the CSS cap: 68px = exactly 4 full rows (measured — see
  // style.css .phase-group-body). Drives the "↑ N more" footer count.
  const GROUP_MAX_ROWS = 4;
  // CSS scroll-snap alone can't fix a mid-row rest: proximity only engages
  // when NEAR a boundary (a fast flick can land far from one and stay —
  // that's the clip the boss kept seeing), and programmatic smooth scrolls
  // (the auto-follow) don't snap at all (headless-verified: scrollTop=36
  // read back 36, no correction, proximity AND mandatory). So the real
  // snap is JS: when a scroll settles (scrollend), nudge to the nearest
  // row TOP — the box must never rest with a row half-cut at the top edge.
  // (Row tops, not multiples of row-height: rows 2+ carry a 4px padding-top,
  // so the pitch is uneven — measure, don't assume.)
  function groupRowTops(body) {
    const boxTop = body.getBoundingClientRect().top;
    return [...body.querySelectorAll(":scope > .phase")].map((r) =>
      Math.round(r.getBoundingClientRect().top - boxTop + body.scrollTop));
  }
  function snapGroupToRow(body) {
    const tops = groupRowTops(body);
    if (tops.length < 2) return;
    let best = tops[0], bd = Infinity;
    for (const p of tops) {
      const d = Math.abs(p - body.scrollTop);
      if (d < bd) { bd = d; best = p; }
    }
    if (bd > 0.5) body.scrollTo({ top: best, behavior: "smooth" });
  }
  function makePhaseGroup(open) {
    const det = document.createElement("details");
    det.className = "phase phase-group";
    det.open = open !== false;  // open by default
    const sum = document.createElement("summary");
    const label = document.createElement("span");
    label.className = "phase-title";
    sum.appendChild(label);
    const chip = document.createElement("span");
    chip.className = "phase-chip";
    sum.appendChild(chip);
    det.appendChild(sum);
    const body = document.createElement("div");
    body.className = "phase-group-body";
    // settle every scroll rest (flick inertia, auto-follow) onto a row edge
    if ("onscrollend" in document) {
      body.addEventListener("scrollend", () => snapGroupToRow(body));
    } else {
      let t;
      body.addEventListener("scroll", () => {
        clearTimeout(t);
        t = setTimeout(() => snapGroupToRow(body), 120);
      });
    }
    const footer = document.createElement("div");
    footer.className = "phase-group-footer";
    footer.hidden = true;
    const more = document.createElement("span");
    more.className = "phase-group-more";
    footer.appendChild(more);
    // The footer is OUTSIDE the scroll body (a sibling after it) — sticky
    // bottom INSIDE the body made it float over the last row. As a sibling
    // it sits below the capped list and can never overlap a step.
    det.appendChild(body);
    det.appendChild(footer);
    return det;
  }
  function refreshGroup(group, scrollNew = false) {
    const footer = group.querySelector(".phase-group-footer");
    const body = group.querySelector(".phase-group-body");
    const rows = [...group.querySelectorAll(".phase-group-body > details.phase")];
    const counts = {};
    let ok = 0, err = 0, running = 0;
    rows.forEach((r) => {
      const v = (r.querySelector(".phase-verb") || {}).textContent ||
        (r.dataset.tool || "?");
      counts[v] = (counts[v] || 0) + 1;
      const cls = (r.querySelector(".phase-verb") || r).classList;
      if (cls.contains("err")) err++;
      else if (cls.contains("ok")) ok++;
      else running++;
    });
    const parts = Object.entries(counts).map(([t, n]) => n > 1 ? t + " ×" + n : t);
    const title = group.querySelector(".phase-title");
    title.textContent = rows.length + " step" + (rows.length > 1 ? "s" : "") +
      (parts.length ? " · " + parts.join(" · ") : "");
    const chip = group.querySelector(".phase-chip");
    if (running) { chip.textContent = "…"; chip.className = "phase-chip run"; }
    else if (err) { chip.textContent = "✗ " + err; chip.className = "phase-chip err"; }
    else { chip.textContent = "✓"; chip.className = "phase-chip ok"; }
    // overflow footer: only when the body is actually capped (more rows
    // than fit) — the CSS max-height decides, so measure it
    const overflow = rows.length > GROUP_MAX_ROWS &&
      body.scrollHeight > body.clientHeight + 4;
    footer.hidden = !overflow;
    if (overflow) {
      const hiddenN = Math.max(1, rows.length - GROUP_MAX_ROWS);
      footer.querySelector(".phase-group-more").textContent =
        "↑ " + hiddenN + " more — scroll for the rest";
    }
    if (scrollNew) {
      // auto-follow the newest row while the run is live — land exactly on
      // the LAST row's top (scrollHeight overshoots to a mid-row max, and
      // smooth programmatic scrolls don't snap; the scrollend nudge would
      // fix it, but the double-hop is visible, so aim true now)
      const rows = body.querySelectorAll(":scope > .phase");
      const last = rows[rows.length - 1];
      const top = last
        ? Math.max(0, Math.round(last.getBoundingClientRect().top -
            body.getBoundingClientRect().top + body.scrollTop))
        : body.scrollHeight;
      body.scrollTo({ top, behavior: "smooth" });
    }
  }

  function settleGroups(turn) {
    // groups stay OPEN after the turn (the boss wants the steps visible);
    // refresh once so the footer hint settles to its final state
    turn.timeline.querySelectorAll("details.phase-group").forEach((g) => {
      refreshGroup(g);
    });
  }

  function addPhase(turn, name, argsStr, rawArgs) {
    turn.row.hidden = false;
    // close the current text segment — the next tokens start a NEW one, so
    // the timeline reads in natural order: text → runs → text → runs → …
    turn.seg = null;
    turn.rawText = "";
    turn.streamStarted = false;
    // rawArgs = the RAW args JSON from the tool_start frame — the phrase
    // target is derived from it (makePhase's 5th param); argsStr is only
    // the display fallback for when the JSON is missing/unparseable
    const row = makePhase(name, argsStr, false, null, rawArgs);
    const last = turn.timeline.lastElementChild;
    let target = row;  // where the avatar parks
    if (last && last.classList.contains("phase-group")) {
      // extend the group — stays OPEN while running (steps visible); the
      // body scrolls past GROUP_MAX_ROWS and auto-follows the newest row
      last.querySelector(".phase-group-body").appendChild(row);
      refreshGroup(last, true);
      target = last;
    } else if (last && last.classList.contains("phase") && !last.classList.contains("phase-group")) {
      // second consecutive run — fold the lone row into a new group
      const g = makePhaseGroup();
      last.replaceWith(g);
      g.querySelector(".phase-group-body").appendChild(last);
      g.querySelector(".phase-group-body").appendChild(row);
      refreshGroup(g, true);
      target = g;
    } else {
      turn.timeline.appendChild(row);
    }
    thinkLabel(turn, "Working…");
    moveAvatar(turn, target, true);  // avatar glides to the running tool group
    scrollToBottom();
  }

  // replay a finished tool run from message.parts (history)
  function addPhaseDone(turn, p) {
    turn.row.hidden = false;
    // p.args is the RAW args JSON (persisted on the message) — the phrase
    // target is derived from it, not from the shortened display string
    const row = makePhase(p.tool, shortenArgs(p.args || ""), true, p, p.args);
    const last = turn.timeline.lastElementChild;
    if (last && last.classList.contains("phase-group")) {
      last.querySelector(".phase-group-body").appendChild(row);
      refreshGroup(last);
    } else if (last && last.classList.contains("phase") && !last.classList.contains("phase-group")) {
      const g = makePhaseGroup();
      last.replaceWith(g);
      g.querySelector(".phase-group-body").appendChild(last);
      g.querySelector(".phase-group-body").appendChild(row);
      refreshGroup(g);
    } else {
      turn.timeline.appendChild(row);
    }
  }

  function settlePhase(turn, name, d) {
    const phases = turn.timeline.querySelectorAll("details.phase");
    let target = null;
    for (const p of phases) {
      // match by dataset.tool — the title is now a human phrase, not the
      // raw name
      if (p.dataset.tool === name && !p.dataset.done) { target = p; break; }
    }
    if (!target) return;
    target.dataset.done = "1";
    const verb = target.querySelector(".phase-verb");
    if (verb) verb.classList.add(d.ok ? "ok" : "err");
    const st = target.querySelector(".tl-st");
    if (st) {
      st.className = "tl-st " + (d.ok ? "ok" : "err");
      st.textContent = d.ok ? "✓" : "✗";
    }
    phaseOutput(target,
      (d.detail || "") + (d.ms != null ? "  (" + d.ms + " ms)" : ""));
    const group = target.closest("details.phase-group");
    if (group) refreshGroup(group);
  }

  const approvals = new Map(); // approval_id → {card, timer}
  const questions = new Map(); // question_id → {card, timer}

  function settleApproval(card, label) {
    if (card.dataset.answered) return;
    card.dataset.answered = "1";
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    const c = card.querySelector(".approval-count");
    if (c) c.textContent = label;
  }

  function settleQuestion(card, label) {
    if (card.dataset.answered) return;
    card.dataset.answered = "1";
    card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    const c = card.querySelector(".question-count");
    if (c) c.textContent = label;
    // dress the countdown down: pill off, strip to zero
    card.dispatchEvent(new CustomEvent("qc-settled"));
  }

  // number-key answer: 1–5 picks on the open question card of the CURRENT
  // session (only when not typing) — the card's footer advertises it
  document.addEventListener("keydown", (e) => {
    if (e.key.length !== 1 || e.key < "1" || e.key > "9" ||
        e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT" ||
        (document.activeElement && document.activeElement.isContentEditable))
      return;
    const card = document.querySelector(
      ".question-card:not([data-answered])");
    if (!card || !card.isConnected) return;
    const btns = card.querySelectorAll(".qc-opt");
    const i = Number(e.key) - 1;
    if (i < btns.length) { e.preventDefault(); btns[i].click(); }
  });

  // The approval frame carries the tool's RAW args string (e.g.
  // `{"command": "...", "cwd": "..."}`) — extract the human part.
  function approvalCommand(d) {
    if (typeof d.command === "string" && d.command) {
      try {
        const o = JSON.parse(d.command);
        if (o && typeof o === "object") {
          return o.command || o.path || o.url || d.command;
        }
      } catch (e) { /* not JSON — show as-is */ }
      return d.command;
    }
    return d.detail || "";
  }

  function renderApproval(turn, d) {
    const card = document.createElement("div");
    card.className = "approval-card";
    const title = document.createElement("div");
    title.className = "approval-title";
    title.innerHTML = '<span class="mi mi-triangle-alert"></span> Approval needed — ' +
      String(d.title || "this command is not read-only")
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const pre = document.createElement("pre");
    pre.textContent = approvalCommand(d);
    const reason = document.createElement("div");
    reason.className = "approval-reason";
    reason.textContent = d.reason || "";
    if (!reason.textContent) reason.hidden = true;
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const approve = document.createElement("button");
    approve.className = "btn primary";
    approve.textContent = "Approve & run";
    const deny = document.createElement("button");
    deny.className = "btn";
    deny.textContent = "Deny";
    const count = document.createElement("span");
    count.className = "approval-count";
    // same deadline rule as the question card: server-stamped ms epoch,
    // so a re-render resumes the countdown instead of resetting it
    const deadline = d.deadline_ms || (Date.now() + (d.timeout || 180) * 1000);
    let secs = Math.ceil((deadline - Date.now()) / 1000);
    count.textContent = "expires in " + secs + "s";
    const timer = setInterval(() => {
      secs = Math.ceil((deadline - Date.now()) / 1000);
      if (secs <= 0) { clearInterval(timer); settleApproval(card, "expired"); }
      else count.textContent = "expires in " + secs + "s";
    }, 1000);
    async function answer(decision, label) {
      settleApproval(card, label);
      clearInterval(timer);
      try {
        await api("/api/approvals/" + d.approval_id, {
          method: "POST", body: JSON.stringify({ decision }),
        });
      } catch (e) { count.textContent = "answer failed"; }
    }
    approve.addEventListener("click", () => answer("approved", "Approved — running…"));
    deny.addEventListener("click", () => answer("denied", "Denied."));
    actions.append(approve, deny, count);
    card.append(title, pre, reason, actions);
    turn.body.appendChild(card);
    approvals.set(d.approval_id, { card, timer });
    scrollToBottom();
  }

  // Settled question card from a persisted part (history reload / draft
  // re-attach): the LIVE card is gone by then (question_closed already
  // fired), so this is the only place the answered question survives.
  // Same DOM shape as the live card — CSS keys off .question-card +
  // [data-answered], so it inherits the settled styling for free.
  function renderQuestionSettled(turn, p) {
    if (p.question_id &&
        turn.timeline.querySelector(
          '.question-card[data-qid="' + p.question_id + '"]'))
      return;  // the live card is already on screen — one card per question
    const card = document.createElement("div");
    card.className = "question-card";
    card.dataset.answered = "1";
    if (p.question_id) card.dataset.qid = p.question_id;
    const head = document.createElement("div");
    head.className = "qc-head";
    const mark = document.createElement("span");
    mark.className = "mark";
    mark.appendChild(icon("help-circle"));
    const title = document.createElement("span");
    title.className = "question-title";
    title.textContent = p.question || "A quick question";
    head.append(mark, title);
    const actions = document.createElement("div");
    actions.className = "question-options";
    (p.options || []).forEach((opt, i) => {
      const b = document.createElement("button");
      b.className = "qc-opt" + (i === p.recommended ? " recommended" : "") +
        (i === p.choice ? " chosen" : "");
      b.disabled = true;
      const key = document.createElement("span");
      key.className = "key";
      key.textContent = i + 1;
      const txt = document.createElement("span");
      txt.className = "opt-text";
      txt.textContent = opt;
      b.append(key, txt);
      actions.appendChild(b);
    });
    const count = document.createElement("span");
    count.className = "question-count";
    count.textContent = p.source === "timeout"
      ? "Auto-picked: " + (p.options || [])[p.choice]
      : "Chose: " + (p.options || [])[p.choice];
    card.append(head, actions, count);
    turn.timeline.appendChild(card);
  }

  function renderQuestion(turn, d) {
    // replayFromDb + live ring can both carry the same frame (the question
    // event is durable now) — one card per question_id, ever
    if (d.question_id) {
      const prev = questions.get(d.question_id);
      if (prev) {
        if (prev.card.isConnected) return;  // the live card is still on screen
        // the DOM was rebuilt (chat switch / reload) — drop the stale card
        // and its countdown, then re-render below: a question that is STILL
        // OPEN must stay answerable after a re-attach
        clearInterval(prev.timer);
        questions.delete(d.question_id);
      }
    }
    const card = document.createElement("div");
    card.className = "question-card";
    if (d.question_id) card.dataset.qid = d.question_id;
    const head = document.createElement("div");
    head.className = "qc-head";
    const mark = document.createElement("span");
    mark.className = "mark";
    mark.appendChild(icon("help-circle"));
    const title = document.createElement("span");
    title.className = "question-title";
    title.textContent = d.question || "A quick question";
    const pill = document.createElement("span");
    pill.className = "qc-pill";
    const actions = document.createElement("div");
    actions.className = "question-options";
    const count = document.createElement("span");
    count.className = "question-count";
    // server-stamped deadline (ms epoch) — the single source of truth.
    // Re-rendering this card (chat switch / reload) derives the remaining
    // time from it, so the countdown RESUMES instead of resetting to full.
    // No deadline (very old persisted frames) → local clock from now.
    const deadline = d.deadline_ms || (Date.now() + (d.timeout || 180) * 1000);
    const totalMs = Math.max(1, deadline - Date.now());
    let secs = Math.ceil(totalMs / 1000);
    count.textContent = "auto-picks in " + secs + "s";
    const timer = setInterval(() => {
      secs = Math.ceil((deadline - Date.now()) / 1000);
      if (secs <= 0) { clearInterval(timer); settleQuestion(card, "auto-picked"); }
      else count.textContent = "auto-picks in " + secs + "s";
    }, 1000);
    async function pick(index, label) {
      settleQuestion(card, label);
      clearInterval(timer);
      try {
        await api("/api/questions/" + d.question_id, {
          method: "POST", body: JSON.stringify({ choice: index }),
        });
      } catch (e) { count.textContent = "answer failed"; }
    }
    (d.options || []).forEach((opt, i) => {
      const b = document.createElement("button");
      b.className = "qc-opt" + (i === d.recommended ? " recommended" : "");
      const key = document.createElement("span");
      key.className = "key";
      key.textContent = i + 1;
      const txt = document.createElement("span");
      txt.className = "opt-text";
      txt.textContent = opt;
      b.append(key, txt);
      if (i === d.recommended) {
        const badge = document.createElement("span");
        badge.className = "rec-badge";
        badge.textContent = "recommended";
        b.appendChild(badge);
      }
      b.addEventListener("click", () => pick(i, "Chose: " + opt));
      actions.appendChild(b);
    });
    const foot = document.createElement("div");
    foot.className = "qc-foot";
    foot.innerHTML = "<span>auto-picks <kbd>" +
      (d.recommended != null ? d.recommended + 1 : "—") +
      "</kbd> when timer ends</span><span>press <kbd>1</kbd>–<kbd>" +
      (d.options || []).length + "</kbd> to answer</span>";
    card.append(head, actions, foot, count);
    head.append(mark, title, pill);
    // countdown: pill ticks down; frozen on settle (settleQuestion hides it)
    pill.textContent = secs + "s";
    const tick = setInterval(() => {
      pill.textContent = Math.max(0, secs) + "s";
      if (secs <= 0) clearInterval(tick);
    }, 250);
    card.addEventListener("qc-settled", () => {
      clearInterval(tick);
      pill.remove();
    });
    // timeline, not body: the card must sit where the question happened
    // (before the answer that follows it), not at the bottom of the turn
    turn.timeline.appendChild(card);
    questions.set(d.question_id, { card, timer });
    // the notice stack (ticking title / repeating notification) needs the
    // real deadline — this is the only place that knows it. announced:false
    // so the ding+toast fire when the user SWITCHES AWAY, not now (the
    // question is on screen — he can see it)
    let wrec = _waitState.get(state.sessionId);
    if (!wrec) {
      wrec = { mark: "❓", deadline: null, lastNotify: 0,
               toast: null, toastTimer: null, announced: false };
      _waitState.set(state.sessionId, wrec);
    }
    wrec.deadline = deadline;
    wrec.totalMs = totalMs;  // toast strip scales against the full span
    scrollToBottom();
  }

  function addFileChips(turn, files) {
    if (!files || !files.length) return;
    turn.filesRow.hidden = false;
    turn.filesSum.innerHTML = '<span class="mi mi-folder"></span> ' +
      files.length + " file" + (files.length > 1 ? "s" : "") + " changed";
    (files).forEach((f) => {
      const chip = document.createElement("div");
      chip.className = "file-chip";
      const ico = FILE_ICONS[f.kind] || "file";
      const name = document.createElement("span");
      name.className = "fc-name";
      name.appendChild(icon(ico));
      name.appendChild(document.createTextNode(" " + (f.name || "file")));
      name.title = f.path || f.name;
      chip.appendChild(name);
      if (f.size != null) {
        const size = document.createElement("span");
        size.className = "fc-size";
        size.textContent = fmtSize(f.size);
        chip.appendChild(size);
      }
      const pv = document.createElement("button");
      pv.textContent = "Preview";
      pv.addEventListener("click", () => openPreview(f));
      chip.appendChild(pv);
      if (f.path || f.url) {
        const dl = document.createElement("a");
        dl.textContent = "⬇";
        dl.href = f.url || ("/api/files/download?path=" + encodeURIComponent(f.path || ""));
        dl.title = "Download";
        chip.appendChild(dl);
      }
      if (f.kind === "html" && f.preview_url) {
        const open = document.createElement("a");
        open.textContent = "Open ↗";
        open.href = f.preview_url;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        chip.appendChild(open);
      }
      turn.filesBody.appendChild(chip);
    });
  }

  function addActions(turn, text) {
    const copy = document.createElement("button");
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text || "");
        copy.textContent = "Copied ✓";
      } catch (e) { copy.textContent = "Copy failed"; }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    turn.actions.appendChild(copy);
  }

  // ── History / sessions / workspaces ───────────────────────────
  // Render ONE history message into el.messages. Extracted from the old
  // inline loop so the windowed loader (below) can render the recent tail
  // immediately and backfill older messages in small chunks. Every
  // message-adding helper appends to el.messages, so temporarily pointing
  // el.messages at a DocumentFragment collects a chunk off-screen.
  function renderHistoryMsg(m, sid) {
    if (m.role === "user") {
      if (m.content === RESUME_TEXT) { addResumeMarker(); return; }
      // execute-plan nudge — the plan is already visible as the assistant
      // message above; show the compact marker, not a duplicate bubble
      if (typeof m.content === "string" && m.content.startsWith(EXECUTE_PLAN_TEXT)) {
        addPlanMarker(); return;
      }
      addUserMessage(m.content, m.files, m.created_at);
    } else {
      const turn = addAssistantTurn();
      turn.mode = m.mode === "plan" ? "plan" : "act";  // tint the final bubble
      if (m.created_at) turn.time = m.created_at;  // history: the turn's real time
      if (m.thinking) renderThoughtHistory(turn);
      // rebuild the turn in natural order: intermediate text, the tool
      // runs that followed it, then the final answer
      for (const p of (m.parts || [])) {
        if (p.t === "text") {
          if (!(p.text || "").trim()) continue;
          // p.ts = when this text was produced (per-part, not the turn's
          // start). Old rows have no ts → turn.time (the turn's start),
          // NEVER Date.now() (that would stamp the reload time on an old
          // message and make it look brand-new)
          renderMarkdown(addSegment(turn, p.ts || turn.time), p.text);
        } else if (p.t === "tool") {
          turn.toolsUsed = true;  // history rebuild: same tint rule as live
          addPhaseDone(turn, p);
        } else if (p.t === "question") {
          // answered question card — closed cards are skipped on SSE
          // replay, so this persisted part is where it survives reload
          renderQuestionSettled(turn, p);
        }
      }
      // final answer — only when there's text. Mirrors the live finishTurn()
      // guard: an empty answer (model ended after tools with no content) must
      // not render a blank bubble + a dead Copy button on reload.
      const answer = (m.content || "").trim();
      if (answer) {
        const { text, chips } = extractChips(m.content);
        // m.answer_ts = when the final answer was produced (per-bubble
        // time); old rows have none → renderAnswer falls back to turn.time
        const seg = renderAnswer(turn, text, m.answer_ts);
        moveAvatar(turn, seg, false);  // history: park at the final answer
        addActions(turn, text);
        addChips(turn, chips);
        verifyLinks(seg, sid);
      } else {
        // no text answer: park the avatar above the last timeline element
        // (the final tool run) so the status group doesn't float at the top.
        const last = turn.timeline.lastElementChild;
        if (last) moveAvatar(turn, last, false);
      }
      addFileChips(turn, m.files);
    }
  }

  // "Load earlier" button for windowed history. Each click backfills one
  // small chunk (a dozen messages) so the tab never blocks; the hidden
  // count ticks down and the button removes itself when nothing's left.
  function makeLoadEarlierBtn(onMore) {
    const btn = document.createElement("button");
    btn.className = "load-earlier";
    btn.addEventListener("click", () => onMore(btn));
    return btn;
  }

  // SERVER-SIDE windowed backfill (chat-switch freeze, 2026-09-28): each
  // click fetches ONE small chunk (a dozen messages, before_id cursor) from
  // the server and prepends it — the switch payload no longer carries the
  // full history, so nothing big is ever parsed up front. st = { hidden,
  // beforeId } is SHARED state: each fetch re-reads the server's hidden
  // count and advances the cursor, so the label ticks down and the button
  // removes itself at zero. (A const captured in the closure would freeze it.)
  // the switch render window (server-side, 2026-09-28) + backfill chunk size
  const RECENT = 20;
  const EARLIER_CHUNK = 12;
  // B: messages built per animation frame (chat-switch freeze, 2026-09-29)
  const RENDER_CHUNK = 5;

  // CHUNKED DOM BUILD - option B, the third chat-switch lever (2026-09-29).
  // A windowed the payload, C capped the re-attach draft, but the render
  // itself was still ONE synchronous burst: every renderHistoryMsg (markdown
  // parse + DOM rows) in a single frame, which a phone CPU turns into a
  // visible hitch. This spreads the build across frames - RENDER_CHUNK
  // messages per requestAnimationFrame - so the tab stays tappable while
  // the window appears in stages (RECENT=20 is ~4 frames at 60fps).
  // The el.messages redirect stays SYNCHRONOUS per chunk (the add* helpers
  // append to el.messages): nothing ever points at the temp fragment across
  // frames. The gen guard kills a stale chunker - a newer loadHistory/resume
  // bumps view(sid).gen while frames are queued, so a superseded build
  // aborts instead of appending stale rows over the fresh settle.
  // Returns a promise that settles with the build (or its abort); loadHistory
  // awaits it so the re-attach block below the build sees the history
  // already in the DOM (its consecutive-avatar check reads the container's
  // last child, and the live turn row must land with it present).
  function buildMessagesChunked(list, sid, gen, onDone) {
    const frag = document.createDocumentFragment();
    let settled = false;
    const live = () =>
      sid === state.sessionId && (view(sid).gen || 0) === gen;
    const finish = (ok) => {
      settled = true;
      if (ok && live()) onDone(frag);
    };
    const step = (i) => {
      if (!live()) { finish(false); return; }
      const tmp = el.messages;
      el.messages = frag;
      const end = Math.min(i + RENDER_CHUNK, list.length);
      for (let k = i; k < end; k++) renderHistoryMsg(list[k], sid);
      el.messages = tmp;
      if (end < list.length) requestAnimationFrame(() => step(end));
      else finish(true);
    };
    step(0);  // first chunk synchronously - don't delay the first paint
    return new Promise((res) => {
      const wait = () => {
        if (settled) res(frag);
        else requestAnimationFrame(wait);
      };
      wait();
    });
  }

  async function loadEarlierChunk(btn, st, sid) {
    if (btn.disabled || st.building) return;
    btn.disabled = true;
    const cs = el.chatScroll;
    const oldH = cs.scrollHeight, oldTop = cs.scrollTop;
    try {
      const res = await api("/api/history?session_id=" + encodeURIComponent(sid) +
        "&limit=" + EARLIER_CHUNK + "&before_id=" + st.beforeId);
      if (!res.ok || sid !== state.sessionId) return;
      const data = await res.json();
      // B (rAF chunking, 2026-09-29): the 12-msg build spreads across the
      // next few frames. gen is the stale-abort tripwire (a newer loadHistory
      // for this chat bumps it, and the queued chunker dies in step());
      // st.building blocks a second click while frames are pending - the
      // finally below re-enables when THIS fn returns, so it alone can't
      // guard the build.
      const gen = view(sid).gen || 0;
      st.building = true;
      buildMessagesChunked(data.messages, sid, gen, (frag) => {
        st.building = false;
        if (!btn.isConnected) return;  // a re-settle took the container
        // btn.AFTER, not btn.before: the button sits at the TOP of the loaded
        // region (loadHistory inserts it before firstChild), so each new
        // chunk must land just BELOW it - above the previously loaded chunk.
        // btn.before (the pre-B shipped line) stacked chunks in CLICK order,
        // putting the OLDEST chunk in the middle after the 2nd tap.
        btn.after(frag);
        st.hidden = data.hidden_count || 0;
        if (data.messages.length) st.beforeId = data.messages[0].id;
        // re-anchor EVERY frame, not once at the end: the chunk grows above
        // the fold as it lands, so a single final anchor would let the
        // visible area drift between frames
        cs.scrollTop = oldTop + (cs.scrollHeight - oldH);
        if (st.hidden <= 0) { btn.remove(); return; }
        btn.textContent = st.hidden <= EARLIER_CHUNK
          ? "Load the last " + st.hidden + " earlier"
          : "Load earlier · " + st.hidden + " hidden";
      });
    } finally { btn.disabled = false; }
  }

  async function loadHistory() {
    if (!state.sessionId) return;
    const sid = state.sessionId;
    // generation counter: the settle at the bottom is async — a resume
    // clicked while THIS fetch is in flight bumps the gen, so the stale
    // settle below sees a mismatch and skips instead of clobbering the
    // resume's chip/processing state
    const gen = (view(sid).gen || 0) + 1;
    view(sid).gen = gen;
    // limit=RECENT: the server ships only the render window (+ hidden
    // count) — the old full-history payload (1.4–2.9 MB) froze every switch
    const res = await api("/api/history?session_id=" + encodeURIComponent(sid) +
      "&limit=" + RECENT);
    if (!res.ok) return;
    if (sid !== state.sessionId) return;  // switched away while loading
    const data = await res.json();
    // stale-settle guard: a resume() click (or a newer loadHistory) bumped
    // the gen while THIS response was in flight — skip the whole settle
    // before touching the DOM, so it can't clobber the resume's state
    if (view(sid).gen !== gen) return;
    el.messages.innerHTML = "";
    if (!data.messages.length) { showWelcome(); }
    else {
      // WINDOWED (server-side, 2026-09-28) + CHUNKED (option B, 2026-09-29):
      // the payload IS the render window (last RECENT), and the build
      // spreads across rAF frames (RENDER_CHUNK msgs/frame) so no single
      // frame pays the whole window's markdown+DOM cost. The await keeps the
      // re-attach block (below) running AFTER the rows are in - its
      // consecutive-avatar detection reads el.messages' last child, so the
      // live turn row must land with the history already present.
      const built = buildMessagesChunked(data.messages, sid, gen, (frag) => {
        el.messages.appendChild(frag);
        firePendingSessionToast();  // thread is on screen - pill names a real chat
        const hidden = data.hidden_count || 0;
        if (hidden > 0) {
          // shared mutable cursor state (see loadEarlierChunk)
          const st = { hidden, building: false,
                       beforeId: data.messages.length ? data.messages[0].id : 0 };
          const btn = makeLoadEarlierBtn((b) => loadEarlierChunk(b, st, sid));
          btn.textContent = hidden <= EARLIER_CHUNK
            ? "Load the last " + hidden + " earlier"
            : "Load earlier · " + hidden + " hidden";
          el.messages.insertBefore(btn, el.messages.firstChild);
        }
      });
      await built;
      // the build spans frames - a newer loadHistory/resume may have bumped
      // the gen (or the user switched away) while it ran; don't settle over it
      if (sid !== state.sessionId || view(sid).gen !== gen) return;
      scrollToBottom(true);
      state.userScrolledUp = false;  // a fresh load pins to the newest message
    }
    // right panel: rebuild this chat's state from the history
    const p = panel(sid);
    // Thinking tab rehydrates from the server's bounded tail (recent 100,
    // count + byte capped) — the windowed payload only carries its own
    // messages' thinking
    p.think = (data.thinking_recent || [])
      .map((t) => ({ text: t, live: false }));
    if (p.think.length > 100) p.think.splice(0, p.think.length - 100);
    syncTerminalFromDb(sid);
    refreshPanel();
    const v = view(sid);
    // capture the in-flight turn BEFORE the null: if this loadHistory fired
    // while we were already WATCHING this run (the spurious replay_gap a
    // fresh run trips on every send — see the handler — or a settle
    // refresh), the turn and its streamed content are the truth. The
    // processing branch below keeps them; re-drawing would double every
    // frame the old stream delivered in the meantime (duplicate-reply bug).
    // firstSeq ≥ run_start_seq = the turn belongs to the CURRENT run (an
    // older unfinished turn — e.g. a crash that auto-resumed a new run —
    // falls through to the re-draw).
    const keepTurn = (v.turn && !v.turn.finished && v.turn.firstSeq
        && v.turn.firstSeq >= (data.run_start_seq || 0)) ? v.turn : null;
    // NOT kept → the old turn row dies with the wipe. Kept → v.turn must
    // stay live through the fetch below: the stream keeps delivering into
    // it the whole time (nulling it here would drop every frame that lands
    // before the branch restores it — a hole in the middle of the answer).
    if (!keepTurn) v.turn = null;
    v.draining = false;
    setMode(data.mode || "act");
    setRoast(data.roast || "chill");
    if (data.processing) {
      // job is running in the background — re-attach and watch it live
      setProcessing(true);
      v.done = false;
      v.knownActive = true;  // server confirms a live job — don't settle on a blank stream
      progressPhase("Working…");
      updateResumeChip(false);
      // server confirms the run is live — the pre-active gap is over, so
      // the resume guard can come down (a later mid-run stop re-derives
      // the chip normally)
      view(sid).pendingResume = false;
      // pinned job: the run_state's goal is authoritative — for long runs
      // it's OLDER than the window, so it may not be in `messages` (fall
      // back to the last visible user message; nudges — resume / execute-
      // plan — are not goals)
      const goalMsg = (data.messages || []).slice().reverse()
        .find((m) => m.role === "user" && m.content !== RESUME_TEXT
                     && !m.content.startsWith(EXECUTE_PLAN_TEXT));
      const goal = data.run_goal || (goalMsg && goalMsg.content) || null;
      if (goal) view(sid).jobGoal = goal;
      setJob(goal ? goal : "Working…");
      if (keepTurn && !v.gapReattach) {
        // KEEP THE LIVE TURN (duplicate-reply fix, 2026-09-30): this
        // loadHistory fired while we were ALREADY watching this run — the
        // spurious replay_gap (a fresh run's seq line is anchored above
        // the previous run, so sendNow's openStream(sid, 0) trips the
        // server's gap check on EVERY send), or a settle refresh. The
        // stream drew this turn's timeline all along; the row was wiped
        // by the innerHTML reset above but is intact in memory.
        // Re-append it and keep the SAME subscription — a fresh
        // optimistic turn + snapshot/replay re-draw would double every
        // frame the old stream delivered while the re-attach fetch ran
        // (duplicate tool rows + the answer's tail rendered twice).
        v.turn = keepTurn;
        keepTurn.row.hidden = false;
        el.messages.appendChild(keepTurn.row);
        reparkAvatars();  // the row moved — re-park the avatar on it
        if (!v.es) openStream(sid, v.seq);  // dead stream → from where we were
      } else {
        v.gapReattach = false;  // consumed: a genuine gap forced the re-draw
        // CLOSE THE OLD STREAM FIRST (duplicate-reply fix, 2026-09-30):
        // while the re-attach fetches below it would keep delivering
        // frames to the fresh turn — and the re-attach re-opens from the
        // SNAPSHOT's seq (older), so every frame in between lands TWICE.
        // One subscription per run, no matter how we got here.
        closeStream(sid);
        v.turn = addAssistantTurn();
        v.turn.row.hidden = false;  // optimistic "Working…" until replay catches up
        const draft = data.draft;
        const hasSnapshot = draft &&
          (((draft.parts || []).length > 0) || (draft.content || "").trim());
        if (hasSnapshot) {
        // The server sent the in-flight turn's durable snapshot (the draft
        // row, refreshed at every tool boundary + every ~2s of typing):
        // render it as the live turn, then stream from exactly where the
        // snapshot ends (draft_seq) — the in-flight text continues with no
        // gap and no double render. The structural replay only re-applies
        // what the snapshot doesn't hold (progress bar, open question card,
        // fact-check note) — never the timeline (that would double-draw).
        renderDraftTurn(v.turn, draft, sid, data.draft_omitted_tools || 0);
        v.seq = data.draft_seq || 0;
        replayFromDb(sid, data.run_start_seq || 0, true).then(() => {
          if (state.sessionId === sid && !view(sid).done)
            openStream(sid, data.draft_seq || view(sid).seq);
        }).catch(() => {
          if (state.sessionId === sid && !view(sid).done)
            openStream(sid, data.draft_seq || 0);
        });
      } else {
        // No snapshot yet (run just started): the in-memory ring only holds
        // the TAIL of a long run, so first replay the durable structural
        // events (+ thinking bursts) from the DB, then stream from the
        // highest seq we've seen — the ring's live tail lands after that.
        replayFromDb(sid, data.run_start_seq || 0).then(() => {
          if (state.sessionId === sid && !view(sid).done)
            openStream(sid, view(sid).seq);
        }).catch(() => {
          if (state.sessionId === sid && !view(sid).done)
            openStream(sid, 0);  // replay failed — fall back to the ring
        });
      }
          syncQueueFromServer(sid);  // queue tray: the server is authoritative
        }  // end re-attach (genuine gap / switch / reload) vs keep-turn
    } else {
      setProcessing(false);
      closeStream(sid);
      // not processing — the queue is server-authoritative too (a durable
      // row can exist with no live run); sync, don't assume empty
      syncQueueFromServer(sid);
      // stopped mid-air? — and state.resumable tells ↻ whether the server
      // can re-enter the saved loop state (true) or will do a plain
      // fresh-turn nudge (false).
      // The chip must mirror the sidebar continue-symbol (needs_continue,
      // has_run_state) so symbol and chip can never disagree. The server's
      // `resumable` is being aligned to has_run_state in api.py (takes
      // effect on the next restart) — until then it's stricter (last
      // message must be the user's; a hard stop finalizes the draft as an
      // EMPTY assistant row), so the client ORs in needs_continue from
      // /api/sessions as the authoritative trigger. After the restart the
      // OR is redundant but harmless — collapse to data.resumable then.
      const last = data.messages[data.messages.length - 1];
      state.resumable = !!data.resumable;
      const v0 = view(sid);
      if (v0.resumeAt) v0.resumeAt = 0;  // settled — the pre-active gap is over
      v0.pendingResume = false;
      v0.gapReattach = false;  // run is over — the gap flag dies with it
      const snc = (state.sessions.find((x) => x.id === sid) || {}).needs_continue;
      updateResumeChip(!!last && (data.resumable || snc));
      // Re-pin the last run's goal as SETTLED (page reload / chat switch /
      // poll-settle all land here) — the strip must not vanish just because
      // the run already finished. No last assistant message = the run never
      // produced anything → nothing to pin.
      const v2 = view(sid);
      const lastAsst = (data.messages || []).slice().reverse()
        .find((m) => m.role === "assistant");
      if (lastAsst) {
        if (!v2.jobGoal) v2.jobGoal = resumeGoal() || "Last run";
        setJob(v2.jobGoal);
        // ✓ only when the last message is an answer; last = user means the
        // run stopped mid-air → settle without the check
        setJobDone(last.role === "assistant");
      } else {
        setJob(null);  // this chat never produced a run — no goal to pin
      }
    }
    // re-pin: the state settle above can grow the composer (resume chip,
    // stop button, attachments row) AFTER the first scrollToBottom(true)
    // — without this the bottom of the chat (composer + newest message)
    // ends up below the fold, e.g. behind the Windows taskbar in F11
    scrollToBottom(true);
    loadLearned();  // a task may have just logged a new learned pattern
  }

  function pollWhileProcessing() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      try {
        const data = await (await api(
          "/api/history?session_id=" + encodeURIComponent(state.sessionId))).json();
        if (!data.processing) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          setProcessing(false);
          await loadHistory();
          refreshSessions();
        }
      } catch (e) { /* keep polling */ }
    }, 2000);
  }

  function updateBanner() {
    // global fallback: a BACKGROUND chat is waiting for an approval
    // or an ask_user answer (questions outrank — they're the newer,
    // more common "needs you" and carry a 3-min auto-run)
    const waiting = state.sessions.find(
      (s) => s.waiting_question && s.id !== state.sessionId) ||
      state.sessions.find(
        (s) => s.waiting_approval && s.id !== state.sessionId);
    if (!waiting) {
      el.approvalBanner.hidden = true;
      el.bannerPill.hidden = true;
      if (state.bannerTick) { clearInterval(state.bannerTick); state.bannerTick = null; }
      return;
    }
    const isQ = !!waiting.waiting_question;
    el.approvalBanner.classList.toggle("a", !isQ);
    el.bannerMark.replaceChildren(icon(isQ ? "help-circle" : "triangle-alert"));
    el.bannerText.textContent = (waiting.title || "A chat") +
      (isQ ? " is waiting for your answer"
           : " is waiting for your approval");
    el.bannerView.onclick = () => switchSession(waiting.id);
    // countdown pill (same math as the question card): the server stamps
    // a deadline on the open wait, so the banner shows it live even
    // though the card itself is in the other chat
    const dl = isQ ? waiting.question_deadline : waiting.approval_deadline;
    const tickBanner = () => {
      if (!dl) { el.bannerPill.hidden = true; return; }
      const left = Math.max(0, dl - Date.now());
      el.bannerPill.hidden = left <= 0;
      const s = Math.ceil(left / 1000);
      el.bannerPill.textContent =
        Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    };
    tickBanner();
    if (!state.bannerTick) state.bannerTick = setInterval(tickBanner, 500);
    el.approvalBanner.hidden = false;
  }

  // ── "needs you" attention: tab title + browser notification ──
  // A question/approval in a chat you're NOT looking at should be
  // noticeable from other tabs/apps: the document title carries a
  // marker (visible in every tab bar) and, if the page is hidden and
  // the user granted permission, a Notification fires.
  // ── notice stack: ding + toast + ticking title + repeating notification ──
  // A question/approval in a chat you're NOT looking at gets:
  //  · a short WebAudio ding (no asset file; the AudioContext is created
  //    lazily — browsers block audio before a user gesture, and by the
  //    time a question fires you've definitely clicked something)
  //  · a 6s toast with a "View" button that jumps to the chat
  //  · a tab title that TICKS DOWN (❓ 2:47 muji) — the deadline is
  //    visible from any tab, and it doubles as the "something's waiting"
  //    marker (replaces the old static ❓)
  //  · a browser Notification that REPEATS every 30s until settled
  //    (the old one-shot fired once per sid and went quiet)
  const _waitState = new Map();  // sid → {mark, deadline, lastNotify, toast}
  function waitDing() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = ding.ctx || (ding.ctx = new AC());
      if (ctx.state === "suspended") ctx.resume().catch(() => {});
      const t = ctx.currentTime;
      // two soft sine pings (E6 → A6) — short enough not to annoy
      [1318.5, 1760].forEach((freq, i) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t + i * 0.14);
        g.gain.exponentialRampToValueAtTime(0.22, t + i * 0.14 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.14 + 0.13);
        o.connect(g).connect(ctx.destination);
        o.start(t + i * 0.14);
        o.stop(t + i * 0.14 + 0.15);
      });
    } catch (e) { /* audio unavailable — the rest of the stack still works */ }
  }
  const ding = { ctx: null };
  // (the bottom toast is removed by design — the top banner is the single
  //  "needs you" surface; it kept duplicating it)
  function waitToast() { return; }
  function killToast(sid) {
    const t = _waitState.get(sid);
    if (!t) return;
    if (t.toastTimer) { clearTimeout(t.toastTimer); t.toastTimer = null; }
    if (t.toastTick) { clearInterval(t.toastTick); t.toastTick = null; }
    if (t.toast) { t.toast.remove(); t.toast = null; }
  }
  function attentionForWaiting() {
    const w = state.sessions.find(
      (s) => (s.waiting_question || s.waiting_approval) &&
             s.id !== state.sessionId);
    const base = (state.config && state.config.title) || "muji";
    // settle every wait that's gone (keep the one map tidy)
    for (const sid of [..._waitState.keys()]) {
      const s = state.sessions.find((x) => x.id === sid);
      if (!s || (!s.waiting_question && !s.waiting_approval)) {
        killToast(sid);
        _waitState.delete(sid);
      }
    }
    if (!w) {
      if (document.title !== base) document.title = base;
      return;
    }
    const mark = w.waiting_question ? "❓" : "⚠";
    let t = _waitState.get(w.id);
    if (!t) {
      t = { mark, deadline: null, lastNotify: 0, toast: null, toastTimer: null };
      _waitState.set(w.id, t);
    }
    t.mark = mark;
    // ding + toast fire the moment this chat becomes "background" — for a
    // question fired in a chat you weren't in, that's now; for one fired in
    // the chat you WERE in, it's the moment you switch away (the card was
    // on screen, no need to nag)
    if (!t.announced) { waitDing(); waitToast(w, mark); t.announced = true; }
    // deadline for the toast pill + ticking title: the server stamps it on
    // the open wait (question → question_*, approval → approval_*), so the
    // countdown survives a page refresh. renderQuestion also stores it from
    // the SSE frame — same value, and it lands first (SSE beats the 3s
    // poll), so the pill ticks from second zero, not second ~3.
    if (w.waiting_question && w.question_deadline) {
      t.deadline = w.question_deadline;
      t.totalMs = (w.question_total || 180) * 1000;
    } else if (!w.waiting_question && w.waiting_approval && w.approval_deadline) {
      t.deadline = w.approval_deadline;
      t.totalMs = (w.approval_total || 180) * 1000;
    }
    // ticking title: ❓ 2:47 muji — without a deadline, no countdown,
    // just the marker
    const now = Date.now();
    let title = mark + " " + base;
    if (t.deadline && t.deadline > now) {
      const s = Math.ceil((t.deadline - now) / 1000);
      title = mark + " " + Math.floor(s / 60) + ":" +
              String(s % 60).padStart(2, "0") + " " + base;
    }
    if (document.title !== title) document.title = title;
    // repeating browser notification: first fire immediately, then every
    // 30s while the page is hidden — same tag, so it REPLACES instead of
    // stacking a notification per minute
    if (document.hidden &&
        "Notification" in window &&
        Notification.permission === "granted" &&
        now - (t.lastNotify || 0) >= 30000) {
      t.lastNotify = now;
      try {
        const n = new Notification(
          (w.waiting_question ? "❓ " : "⚠ ") + base,
          { body: (w.title || "A chat") +
                  (w.waiting_question
                   ? " is waiting for your answer"
                   : " is waiting for your approval"),
            tag: "muji-wait-" + w.id });
        n.onclick = () => { window.focus(); n.close(); };
      } catch (e) { /* some platforms restrict Notification */ }
    }
  }
  function askNotifyPermission() {
    if ("Notification" in window && Notification.permission === "default")
      Notification.requestPermission().catch(() => {});
  }

  // Relative timestamp for the sidebar ("now", "2m", "3h", "Yest", "Mon",
  // "Sep 2"). updated_at is epoch seconds from the server.
  function relTime(ts) {
    if (!ts) return "";
    const d = Date.now() / 1000 - ts;
    if (d < 60) return "now";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    const t = new Date(ts * 1000);
    const today = new Date();
    if (t.toDateString() === today.toDateString())
      return t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const yest = new Date(today); yest.setDate(today.getDate() - 1);
    if (t.toDateString() === yest.toDateString()) return "Yest";
    if (d < 7 * 86400) return t.toLocaleDateString([], { weekday: "short" });
    return t.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  // Auto-archive threshold: non-pinned chats idle 14+ days sink into the
  // collapsed Archive section instead of dying.
  const ARCHIVE_MS = 14 * 86400 * 1000;

  // Status icon for a chat row — replaces the old ambiguous dots:
  // ⟳ running (spin) · ⚠ approval · ? question · ▶ interrupted · ✓ done
  // The 4th element (present only on "cont") marks the icon as a direct
  // resume trigger — clicking it resumes the run without opening the chat.
  // Priority: running wins over needs_continue — a chat with a saved
  // run_state that ALSO has a live job (a new run started after the
  // interruption) shows the spinner, not a second resume affordance.
  function statusIcon(s) {
    if (s.status === "running" || s.status === "queued")
      return ["⟳", "running", "Running in the background"];
    if (s.waiting_approval)
      return ["⚠", "approval", "Waiting for approval"];
    if (s.waiting_question)
      return ["?", "question", "Waiting for your answer"];
    if (s.needs_continue)
      return ["▶", "cont",
              "Interrupted — no final summary. Click to continue.",
              { resume: true }];
    if (state.finished.has(s.id))
      return ["✓", "done", "Task done"];
    return null;
  }

  // Session-row action menu: one ⋯ button per row (hover) opens a small
  // dropdown (Pin / Rename / Delete). Replaces the old 3-button trio that
  // reflowed the row on hover and shoved the status icon under the cursor.
  function closeSessionMenus(except) {
    document.querySelectorAll(".s-menu.open").forEach((m) => {
      if (m === except) return;
      m.classList.remove("open");
      m.closest(".s-actions")?.classList.remove("menu-open");
      m.closest(".session-item")?.classList.remove("menu-open");
    });
  }
  document.addEventListener("click", () => closeSessionMenus(null));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSessionMenus(null);
  });

  function makeSessionItem(s) {
    const item = document.createElement("div");
    let cls = "session-item";
    if (s.id === state.sessionId) cls += " active";
    if (s.waiting_approval) cls += " needs-approval";
    if (s.waiting_question) cls += " needs-question";
    if (s.pinned) cls += " pinned";
    item.className = cls;
    item.dataset.sid = s.id;
    const col = document.createElement("span");
    col.className = "s-col";
    // title + TL;DR flag badge share one row: the badge sits right after
    // the title (the chat's count of final answers that shipped without a
    // TL;DR — server-side deterministic check; 0 = no badge)
    const titleRow = document.createElement("span");
    titleRow.className = "s-title-row";
    const title = document.createElement("span");
    title.className = "s-title";
    if (s.pinned) title.appendChild(icon("pin"));
    title.appendChild(document.createTextNode((s.pinned ? " " : "") + (s.title || "New chat")));
    titleRow.appendChild(title);
    if (s.tldr_flags > 0) {
      const flag = document.createElement("span");
      flag.className = "s-tldr-flag";
      flag.textContent = "⚑" + (s.tldr_flags > 1 ? " " + s.tldr_flags : "");
      flag.title = s.tldr_flags + " final answer(s) shipped without a TL;DR since the last rule-following one — cleared by the next final answer that carries a TL;DR (the rule: every final summary + every multi-paragraph reply ends with a one-line TL;DR)";
      titleRow.appendChild(flag);
    }
    col.appendChild(titleRow);
    const sum = document.createElement("span");
    sum.className = "s-sum";
    // model-generated activity phrase (3-7 words) — labels the latest
    // user turn: new task / "Continuing …" / question-topic; empty
    // until the chat's first turn finishes
    sum.textContent = s.summary || "";
    if (!s.summary) sum.classList.add("empty");
    col.appendChild(sum);
    item.appendChild(col);
    const ic = statusIcon(s);
    if (ic) {
      const dot = document.createElement("span");
      dot.className = "s-ic " + ic[1];
      dot.textContent = ic[0];
      dot.title = ic[2];
      if (ic[3] && ic[3].resume) {
        // clickable resume trigger: stop the row's open-chat handler and
        // resume directly — the chat opens as a side effect of resume()
        dot.classList.add("s-ic-btn");
        dot.addEventListener("click", (ev) => {
          ev.stopPropagation();
          resumeSidebar(s.id, s);
        });
      }
      item.appendChild(dot);
    }
    const tm = document.createElement("span");
    tm.className = "s-time";
    tm.textContent = relTime(s.updated_at);
    tm.title = s.updated_at
      ? new Date(s.updated_at * 1000).toLocaleString() : "";
    item.appendChild(tm);
    const acts = document.createElement("span");
    acts.className = "s-actions";
    const menu = document.createElement("div");
    menu.className = "s-menu";
    const mkItem = (label, danger) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (danger) b.classList.add("danger");
      return b;
    };
    const pinBtn = mkItem(s.pinned ? "Unpin" : "Pin to top");
    pinBtn.prepend(icon("pin"), document.createTextNode(" "));
    const renBtn = mkItem("✎ Rename…");
    const delBtn = mkItem("✕ Delete", true);
    pinBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeSessionMenus(null);
      api("/api/sessions/pin", {
        method: "POST", body: JSON.stringify({ session_id: s.id }),
      }).then(() => refreshSessions());
    });
    renBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeSessionMenus(null);
      const t = prompt("New chat title:", s.title || "");
      if (t === null) return;
      api("/api/sessions/rename", {
        method: "POST", body: JSON.stringify({ session_id: s.id, title: t }),
      }).then(() => refreshSessions());
    });
    delBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      closeSessionMenus(null);
      if (!confirm("Delete this chat?")) return;
      api("/api/sessions/" + s.id, { method: "DELETE" }).then(async () => {
        delete state.panels[s.id];
        const wasVisible = state.sessionId === s.id;
        if (wasVisible) {
          closeStream(s.id);
          state.sessionId = null;
        }
        await refreshSessions();
        if (wasVisible) {
          // switch to a chat that still exists — only start a new one if none remain
          const remaining = state.wsSel === null
            ? state.sessions.filter((x) => !x.workspace_id)
            : state.sessions.filter((x) => x.workspace_id === state.wsSel);
          if (remaining.length) switchSession(remaining[0].id);
          else newSession();
        }
      });
    });
    menu.append(pinBtn, renBtn, delBtn);
    const more = document.createElement("button");
    more.className = "s-more";
    more.textContent = "⋯";
    more.title = "Chat actions";
    more.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const wasOpen = menu.classList.contains("open");
      closeSessionMenus(null);
      if (!wasOpen) {
        menu.classList.add("open");
        acts.classList.add("menu-open");
        item.classList.add("menu-open");
        // flip upward when the row sits near the bottom of the scroll list
        // (the menu is absolutely positioned and the list clips overflow)
        const list = item.closest(".session-list");
        if (list) {
          const r = item.getBoundingClientRect();
          const lr = list.getBoundingClientRect();
          menu.classList.toggle("up", lr.bottom - r.bottom < 140);
        }
      }
    });
    acts.append(more, menu);
    item.appendChild(acts);
    item.addEventListener("click", () => {
      // fromSidebar=true: on mobile this tap also slides the sidebar away
      // (the list is an overlay, not a persistent column) — one gesture.
      switchSession(s.id, true);
    });
    return item;
  }

  function dayHeader(label) {
    const h = document.createElement("div");
    h.className = "s-day";
    h.textContent = label;
    return h;
  }

  async function refreshSessions() {
    let data;
    try { data = await (await api("/api/sessions")).json(); } catch (e) { return; }
    state.sessions = data.sessions;
    renderSessionTitle();  // panel header tracks renames/pins live
    // Revival blind spot: the stream died while this chat was open and the
    // settle path left it CLOSED (noJob after a crash, a `stopped` frame
    // before the boot auto-resume re-enqueued a turn). The health check in
    // openStream's onerror ran during the down-gap, so the revival reload
    // never fired — the poll is the only live signal that a run is going
    // with no stream attached. Re-open it so the frames land instead of
    // the pane freezing on the pre-crash state.
    if (state.sessionId) {
      const v = view(state.sessionId);
      if (!v.es && !v.done) {
        const rs = data.sessions.find((x) => x.id === state.sessionId);
        if (rs && rs.status === "running") openStream(state.sessionId, v.seq);
      }
    }
    // steady green done-LED: "finished AND not opened since" — seeded from
    // the server's last_done_at vs last_viewed_at stamps, so it survives
    // page refreshes; cleared the moment the user opens the chat or a new
    // run starts in it
    state.finished.clear();  // prune stale entries before re-seeding
    for (const s of data.sessions) {
      if (s.status === "running" || s.status === "queued") continue;
      if (s.last_done_at &&
          (!s.last_viewed_at || s.last_done_at > s.last_viewed_at))
        state.finished.add(s.id);
    }
    let shown = state.wsSel === null
      ? data.sessions.filter((s) => !s.workspace_id)
      : data.sessions.filter((s) => s.workspace_id === state.wsSel);
    // search filter: title + summary, case-insensitive substring
    const q = state.chatFilter.trim().toLowerCase();
    if (q) shown = shown.filter((s) =>
      (s.title || "").toLowerCase().includes(q) ||
      (s.summary || "").toLowerCase().includes(q));
    const sig = JSON.stringify([state.wsSel, state.sessionId, q,
      shown.map((s) => [s.id, s.title, s.summary, s.pinned, s.status,
        s.waiting_approval, s.waiting_question, s.needs_continue,
        state.finished.has(s.id)])]);
    updateBanner();
    attentionForWaiting();
    updateMenuCounts();
    ctxMeterFromSession(true);  // 3 s poll: refresh the meter, keep an
                                // in-flight compacting flag from flickering
    if (sig === state.sessionSig) return;
    state.sessionSig = sig;
    el.sessionList.classList.toggle("archived-collapsed", state.archiveCollapsed);
    el.sessionList.innerHTML = "";
    // groups: Pinned / Today / Yesterday / Earlier / Archive — the server
    // already orders pinned DESC, updated_at DESC, so each group keeps that
    const now = Date.now();
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const yest = new Date(today); yest.setDate(today.getDate() - 1);
    const groups = { pinned: [], today: [], yest: [], earlier: [], archive: [] };
    for (const s of shown) {
      const ts = (s.updated_at || 0) * 1000;
      if (s.pinned) groups.pinned.push(s);
      else if (now - ts > ARCHIVE_MS) groups.archive.push(s);
      else if (ts >= +today) groups.today.push(s);
      else if (ts >= +yest) groups.yest.push(s);
      else groups.earlier.push(s);
    }
    const addGroup = (label, list, extraCls) => {
      if (!list.length) return;
      if (extraCls === "archive") {
        // collapsible archive header — click toggles the whole section
        const h = dayHeader("");
        h.classList.add("archive");
        const btn = document.createElement("button");
        btn.textContent = (state.archiveCollapsed ? "▸ " : "▾ ") +
                          "Archive (" + list.length + ")";
        btn.title = "Old chats (14+ days, unpinned) — click to toggle";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          state.archiveCollapsed = !state.archiveCollapsed;
          localStorage.setItem("muji.archiveCollapsed",
                               state.archiveCollapsed ? "1" : "0");
          state.sessionSig = "";  // force re-render with the new arrow
          refreshSessions();
        });
        h.appendChild(btn);
        el.sessionList.appendChild(h);
      } else {
        el.sessionList.appendChild(dayHeader(label));
      }
      for (const s of list) el.sessionList.appendChild(makeSessionItem(s));
    };
    addGroup("Pinned", groups.pinned);
    addGroup("Today", groups.today);
    addGroup("Yesterday", groups.yest);
    addGroup("Earlier", groups.earlier);
    addGroup("Archive", groups.archive, "archive");
    if (!shown.length && q) {
      const none = dayHeader("No chats match " + q);
      el.sessionList.appendChild(none);
    }
  }

  async function loadWorkspaces() {
    let data;
    try { data = await (await api("/api/workspaces")).json(); } catch (e) { return; }
    state.workspaces = data.workspaces;
    el.wsList.innerHTML = "";
    const gen = document.createElement("div");
    gen.className = "ws-item" + (state.wsSel === null ? " active" : "");
    const gic = document.createElement("span");
    gic.className = "ws-ic";
    gic.textContent = "◆";
    gen.appendChild(gic);
    const gn = document.createElement("span");
    gn.className = "ws-name";
    gn.textContent = "General";
    gen.appendChild(gn);
    gen.title = state.config ? "General chats (working folder: " + state.config.root_dir + ")" : "";
    gen.addEventListener("click", () => selectWorkspace(null));
    el.wsList.appendChild(gen);
    for (const w of data.workspaces) {
      const item = document.createElement("div");
      item.className = "ws-item" + (state.wsSel === w.id ? " active" : "");
      const ic = document.createElement("span");
      ic.className = "ws-ic";
      ic.appendChild(icon("folder"));
      item.appendChild(ic);
      const nm = document.createElement("span");
      nm.className = "ws-name";
      nm.textContent = w.name;
      item.appendChild(nm);
      const pth = document.createElement("span");
      pth.className = "ws-path";
      pth.textContent = w.path;
      item.appendChild(pth);
      item.addEventListener("click", () => selectWorkspace(w.id));
      el.wsList.appendChild(item);
    }
  }

  // Per-workspace "last chat I was in" — snapshot the open session whenever
  // the workspace changes, so clicking back restores THAT chat, not merely
  // the most-recently-updated one. Key: "g" = General, else the workspace id.
  function rememberWsSession() {
    try {
      const k = "muji.ws.last." + (state.wsSel === null ? "g" : state.wsSel);
      if (state.sessionId) localStorage.setItem(k, state.sessionId);
      else localStorage.removeItem(k);
    } catch (e) { /* private mode — landing falls back to most recent */ }
  }

  function selectWorkspace(wid) {
    if (wid !== state.wsSel) rememberWsSession();  // leaving: snapshot
    state.wsSel = wid;
    state.sessionId = null;
    localStorage.removeItem("muji.sid");
    resetTree();
    pickUI();  // 📂 hidden while no chat is selected
    const ws = state.workspaces.find((w) => w.id === wid);
    el.wsLabel.textContent = ws ? "Chats · " + ws.name : "Chats (General)";
    loadWorkspaces();
    refreshSessions().then(() => {
      // landing chat: the last one open in THIS workspace (rememberWsSession),
      // if it still exists here; else the most recent
      let land = null;
      try {
        const k = "muji.ws.last." + (wid === null ? "g" : wid);
        const last = localStorage.getItem(k);
        if (last) land = state.sessions.find((s) => s.id === last);
      } catch (e) { /* fall through to most recent */ }
      if (!land) land = state.sessions[0];
      if (land) switchSession(land.id);
      else { clearView(); focusComposer(); }
      restoreDraft();  // workspace restore also lands on a session
    });
  }

  // Restore a composer draft the reload (or a refresh) left behind — only
  // if it belongs to the session we ended up on. Runs on every boot path
  // (last-session, workspace, fresh) so no branch skips it.
  function restoreDraft() {
    try {
      const d = JSON.parse(localStorage.getItem("muji.draft") || "null");
      if (d && d.text && d.sid === state.sessionId && !el.input.value) {
        el.input.value = d.text;
        autosize();
        updateSendEnabled();
      }
      if (d) localStorage.removeItem("muji.draft");
    } catch (e) { /* malformed draft — drop it */ }
  }

  function markViewed(sid) {
    api("/api/sessions/" + encodeURIComponent(sid) + "/viewed",
        { method: "POST" }).catch(() => {});
  }

  // fromSidebar: the user tapped a chat row — on mobile that gesture also
  // slides the sidebar away. The boot-time restore passes false: the app
  // should OPEN with the sidebar expanded, not auto-collapse it.
  // Session-switch toast: brief pill above the composer naming the chat
  // you just landed in — sanity check for lookalike chat boxes.
  // Event-driven (fires only on switch); one ~1.6s CSS transition, no timers
  // outliving the fade.
  let sessionToastTimer = null;
  function showSessionToast(sid) {
    const s = state.sessions.find((x) => x.id === sid);
    const name = (s && s.title) || "New chat";
    el.sessionToastName.textContent = name;
    el.sessionToast.classList.add("show");
    clearTimeout(sessionToastTimer);
    sessionToastTimer = setTimeout(() => el.sessionToast.classList.remove("show"), 1600);
  }
  // The pill must name the chat you're ALREADY looking at — firing it in
  // switchSession (before loadHistory's async fetch resolves) shows it over
  // a blank thread. So switchSession only sets the flag; the toast fires
  // from loadHistory's settle, after the first render. loadHistory also
  // runs from poll-settle / draft-restore / in-chat paths, which must NOT
  // re-toast — the flag is consumed exactly once per user switch.
  let pendingSessionToast = null;
  function queueSessionToast(sid) { pendingSessionToast = sid; }
  function firePendingSessionToast() {
    if (!pendingSessionToast) return;
    const sid = pendingSessionToast;
    pendingSessionToast = null;
    if (sid === state.sessionId) showSessionToast(sid);
  }

  function switchSession(sid, fromSidebar) {
    if (state.sessionId && state.sessionId !== sid) {
      // detach only the VIEW — the server-side task keeps running
      closeStream(state.sessionId);
      view(state.sessionId).turn = null;
      normalizeHistoryFor(sid);
    }
    state.sessionId = sid;
    if (state.picking) { state.picking = false; }
    localStorage.setItem("muji.sid", sid);
    // opening the chat = the finished run is now seen → LED off (durable)
    state.finished.delete(sid);
    markViewed(sid);
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    resetProgress();
    refreshSessions();
    loadHistory();
    queueSessionToast(sid);  // pill fires at loadHistory's settle, post-render
    pickUI();  // 📂 shows for chats without a chosen working folder
    focusComposer();
    ctxMeterFromSession();  // topbar meter follows the chat (or hides)
    // Mobile: picking a chat is the whole point — don't leave the
    // overlay sidebar covering the conversation. Only for a real row
    // tap (fromSidebar); the boot-time restore keeps it expanded.
    if (window.innerWidth <= 900 && fromSidebar) {
      el.sidebar.classList.add("collapsed");
      el.sidebarOpen.hidden = false;
      updatePanelOverlay();
    }
  }

  function clearView() {
    el.messages.innerHTML = "";
    showWelcome();
    state.processing = false;
    setProcessing(false);
    resetProgress();
    setJob(null);
    state.previewShown = null;
    state.treeSid = null;   // force the Files tab to resync to the (new) chat
    renderTerminal();
    renderSessionTitle();
    ctxMeterFromSession();  // no chat open → meter hides
    if (state.rightTab === "files") { syncTreeToChat(); syncHiddenToggle(); }
    updateResumeChip(false);
    if (state.rightTab === "thinking") renderThinkingPanel();
    el.previewBody.innerHTML = "";
    el.previewEmpty.hidden = false;
  }

  async function newSession() {
    const res = await api("/api/sessions/new", {
      method: "POST", body: JSON.stringify({ workspace_id: state.wsSel || undefined }),
    });
    if (!res.ok) return;
    const s = await res.json();
    state.sessionId = s.id;
    localStorage.setItem("muji.sid", s.id);
    normalizeHistoryFor(s.id);
    clearView();
    refreshSessions();
    pickUI();  // 📂 shows for the fresh chat
    focusComposer();
    ctxMeterFromSession();  // fresh chat → no data yet → meter hides
  }

  async function addWorkspace() {
    const path = prompt("Workspace folder (must be under the root):",
      (state.config && state.config.root_dir) || "");
    if (!path) return;
    const name = prompt("Display name (optional):",
      path.split(/[\\/]/).filter(Boolean).pop() || "");
    if (name === null) return;
    const res = await api("/api/workspaces", {
      method: "POST", body: JSON.stringify({ path, name: name || undefined }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not add workspace");
      return;
    }
    const ws = await res.json();
    selectWorkspace(ws.id);
  }

  // ── Send / SSE ────────────────────────────────────────────────
  // Multi-instance: per-session view state + resumable EventSource streams.
  // The agent runs server-side; this is only the browser's subscription.
  function view(sid) {
    if (!state.views[sid])
      state.views[sid] = { es: null, seq: 0, turn: null, done: false,
                           knownActive: false, errs: 0, lastPlan: null,
                           taskId: null, draining: false };
    return state.views[sid];
  }

  function closeStream(sid) {
    const v = state.views[sid];
    if (v && v.es) { v.es.close(); v.es = null; }
  }

  const SSE_EVENTS = ["token", "thinking", "status", "tool_start", "tool_end", "phase_end",
    "llm_end", "answer_start", "done", "correction", "error", "stopped",
    "approval", "approval_closed", "question", "question_closed",
    "plan", "plan_update", "queued", "replay_gap"];

  // ── Re-attach replay: durable events → DOM ─────────────────────
  // When a chat is reopened while its turn is still running, the
  // in-memory ring buffer may have evicted the run's early frames
  // (maxlen 4000 — a long run burns through that in token deltas
  // alone). The DB `events` table holds every STRUCTURAL event the
  // run produced (status/tool_start/tool_end/phase_end/answer_start/
  // plan/plan_update/approval/done/…) plus one row per thinking
  // burst, so we can rebuild the in-flight turn's progress no matter
  // how long the run is. Live token/thinking deltas that are still in
  // the ring arrive right after (openStream from max seq).
  // Reattach: draw the server's in-flight draft snapshot into the live
  // turn. Same shape as the history render (m.parts → segments + done tool
  // rows), but the turn stays LIVE: the in-flight text becomes the segment
  // the stream continues into (turn.seg + its raw buffer), and the thinking
  // keeps a live burst so continuation deltas append to it.
  function renderDraftTurn(turn, draft, sid, omittedTools = 0) {
    if (draft && draft.created_at) setTurnTime(turn, draft.created_at);
    if (omittedTools > 0) {
      // the server capped the snapshot's tool timeline (chat-switch freeze,
      // 2026-09-28) — the full one still lives in the events DB
      const cap = document.createElement("div");
      cap.className = "draft-omitted";
      cap.textContent = "… " + omittedTools +
        (omittedTools === 1 ? " earlier step" : " earlier steps") + " (capped)";
      turn.timeline.prepend(cap);
    }
    for (const p of (draft.parts || [])) {
      if (p.t === "text") {
        if (!(p.text || "").trim()) continue;
        renderMarkdown(addSegment(turn, p.ts || turn.time), p.text);
      } else if (p.t === "tool") {
        turn.toolsUsed = true;
        addPhaseDone(turn, p);
      } else if (p.t === "question") {
        // settled question from the draft snapshot — one card per qid
        renderQuestionSettled(turn, p);
      }
    }
    const c = (draft.content || "").trim();
    const ui = draft.ui || {};
    if (c.startsWith("⏳")) {
      // a tool is mid-run: its tool_start frame still replays from the
      // stream and draws the row (and parks the avatar on it)
      thinkLabel(turn, ui.label || c.replace(/^⏳\s*/, "").trim() || "Working…");
    } else if (c) {
      // in-flight text: the live tokens after the snapshot continue it
      const seg = addSegment(turn);
      seg._raw = c;
      turn.seg = seg;
      turn.rawText = c;
      turn.streamStarted = true;
      turn.row.hidden = false;
      renderMarkdown(seg, c);
      thinkLabel(turn, ui.label || "Answering…");
      moveAvatar(turn, seg, true);
    } else {
      thinkLabel(turn, ui.label || "Working…");
    }
    // avatar: "stream" already parked it on the in-flight segment; any other
    // snapshot state parks it where the timeline ended (the running tool
    // row, once its frame replays, pulls it there again via addPhase)
    if (ui.avatar !== "stream" && turn.timeline.lastElementChild)
      moveAvatar(turn, turn.timeline.lastElementChild, false);
    if (draft.thinking) {
      turn.thinkAutoed = true;  // don't auto-flip the tab on later deltas
      turn.hint.hidden = false;
      if (ui.thinking) {
        // mid-thinking at snapshot time — live thinking frames continue the
        // same hint (the next non-thinking frame finalizes it as usual)
        turn.hint.innerHTML = '<span class="mi mi-brain"></span> thinking…';
        turn.lastEventWasThinking = true;
        turn.hintStart = Date.now();
      } else {
        turn.hint.innerHTML = '<span class="mi mi-brain"></span> thought → Thinking tab';
      }
      const p = panel(sid);
      p.think.push({ text: draft.thinking, live: true, start: Date.now() });
      if (p.think.length > 100) p.think.splice(0, p.think.length - 100);
      syncLane(turn);  // resume: hint shown before the timeline settles
    }
    if (draft.files && draft.files.length)
      addFileChips(turn, draft.files);
    turn.row.hidden = false;
  }

  async function replayFromDb(sid, after = 0, covered = false) {
    let data;
    try {
      data = await (await api(
        "/api/sessions/" + encodeURIComponent(sid) +
        "/events_log?after=" + (after || 0))).json();
    } catch (e) { return; }
    if (state.sessionId !== sid) return;  // user switched again mid-fetch
    const v = view(sid);
    const turn = v.turn;  // the optimistic "Working…" turn loadHistory made
    let lastStatus = "";
    // Question cards on replay: only the question the run is STILL WAITING
    // ON earns a card — the newest `question` event in the window whose
    // `question_closed` never followed (the answer, the ~3-min timeout, and
    // a stop all emit question_closed). Anything older is history:
    // re-rendering it would fire already-answered cards back into the
    // timeline with a live countdown — the same dead-card trap approvals
    // are skipped for.
    // NOT "the last event in the window is a question": a thinking_burst
    // row can share the question's seq (burst rows are stamped with the
    // closing frame's seq, which can be the question frame itself) and
    // SQLite's `ORDER BY seq` breaks that tie by rowid — so the burst can
    // land AFTER the question row and steal "last", silently dropping the
    // open card on every re-attach. The closed-event scan is tie-proof.
    const events = data.events || [];
    const closedQids = new Set();
    let openQid = null;
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      const d = ev.data || {};
      if (ev.event === "question_closed") { closedQids.add(d.question_id); continue; }
      if (ev.event === "question") {
        // the newest question is the only one that can still be open (the
        // agent blocks on _wait_question — one open at a time)
        if (!closedQids.has(d.question_id)) openQid = d.question_id;
        break;
      }
    }
    for (const e of events) {
      const d = e.data || {};
      if (typeof e.seq === "number") v.seq = Math.max(v.seq, e.seq);
      if (covered && e.event !== "plan" && e.event !== "plan_update" &&
          e.event !== "question" && e.event !== "question_closed" &&
          e.event !== "correction") continue;
      // ^ covered: the draft snapshot already drew this turn's timeline
      //   (text + tool rows + thinking) — re-apply ONLY what it doesn't
      //   hold (progress bar, the still-open question card, fact-check
      //   notes). Anything else would double-draw.
      switch (e.event) {
        case "status":
          if (turn) {
            thinkLabel(turn, d.text || "Working…");
            progressPhase(d.text || "Working…");
          }
          lastStatus = d.text || "";
          break;
        case "thinking_burst": {
          // the run's reasoning so far — same shape as a closed live burst
          const p = panel(sid);
          const text = d.text || "";
          if (text) p.think.push({ text, live: false });
          if (p.think.length > 100) p.think.splice(0, p.think.length - 100);
          if (turn) {
            turn.thinkAutoed = true;
            setUpperTab("thinking", { auto: true, btn: el.rpTabThinking });
            renderThoughtHistory(turn);
          }
          break;
        }
        case "tool_start":
          if (turn) {
            finalizeThinkingHint(turn);
            turn.toolsUsed = true;  // replay path: same tint rule as live
            addPhase(turn, d.tool, shortenArgs(d.args || ""), d.args);
          }
          if (state.progress) state.progress.steps += 1;
          // sidebar keeps the TASK phrase (plan step / LLM summary) — tool
          // names no longer overwrite it
          break;
        case "phase_end":
          if (turn) settlePhase(turn, d.tool, d);
          break;
        case "answer_start":
          if (turn) {
            finalizeThinkingHint(turn);
            turn.row.hidden = false;
            thinkLabel(turn, "Answering…");
            turn.answerSeg = turn.seg;
          }
          break;
        case "plan":
          startProgress(d.items || []);
          break;
        case "plan_update":
          updateProgress(d);
          break;
        // approval / approval_closed: intentionally NOT replayed.
        // The DB holds stale frames from the pre-destruction-only
        // era ("Open browser", non-destructive run_command) — replaying
        // them re-renders dead cards. Live frames still render.
        case "question":
          if (turn && d.question_id === openQid) renderQuestion(turn, d);
          break;
        case "question_closed": {
          const qrec = questions.get(d.question_id);
          if (qrec) {
            if (!qrec.card.dataset.answered) settleQuestion(qrec.card, "resolved");
            clearInterval(qrec.timer);
            questions.delete(d.question_id);
          }
          break;
        }
        case "correction":
          if (turn && (d.note || "").trim()) {
            const note = document.createElement("div");
            note.className = "msg-correction";
            const lab = document.createElement("span");
            lab.className = "msg-correction-label";
            lab.textContent = "Fact-check note:";
            const body = document.createElement("span");
            body.textContent = d.note;
            note.append(lab, body);
            turn.body.insertBefore(note, turn.actions);
          }
          break;
        case "done":
        case "stopped":
        case "error":
          // the run ended while we were fetching — the stream will settle
          // it (or loadHistory's next poll will); nothing to render here
          break;
        default:
          break;
      }
    }
    // restore the latest status label (a burst/tool replay overwrote it)
    if (turn && lastStatus) thinkLabel(turn, lastStatus);
    refreshPanel();
  }

  function openStream(sid, after) {
    closeStream(sid);
    const v = view(sid);
    const es = new EventSource(
      "/api/sessions/" + encodeURIComponent(sid) + "/events?after=" + after);
    v.es = es;
    v.errs = 0;
    for (const name of SSE_EVENTS) {
      es.addEventListener(name, (m) => {
        let d = {};
        try { d = JSON.parse(m.data); } catch (e) { /* ignore */ }
        if (typeof d.seq === "number") {
          v.seq = Math.max(v.seq, d.seq);
          // the live turn's FIRST frame = where its run's seq line began —
          // the replay_gap spurious filter compares the ring's oldest
          // against it (frames older than that belong to a PREVIOUS run)
          if (v.turn && !v.turn.firstSeq) v.turn.firstSeq = d.seq;
        }
        handleEvent(sid, name, d);
      });
    }
    es.onerror = async () => {
      es.close();
      v.es = null;
      if (v.done) return;  // task finished; the server closed the stream
      // REVIVAL CHECK (first, and nuclear): if the server process changed
      // since this tab opened, the stream we held was minted against a DEAD
      // server and a fresh one (empty in-memory event ring) is now serving —
      // every piece of in-tab state is suspect (chat pane, sidebar, stream,
      // right panel). Hard-reload the whole tab and re-converge from the
      // durable source in one shot. A same-process blip keeps the same
      // boot_id and falls through to the surgical paths below. The claim is
      // set SYNCHRONOUSLY (before the await) so N concurrent stream errors
      // on a revival trigger exactly ONE reload, not N.
      if (state.bootId && !state.bootChecked) {
        state.bootChecked = true;
        let revived = false;
        try {
          const h = await (await fetch("/api/health", { cache: "no-store" })).json();
          revived = !!(h.boot_id && h.boot_id !== state.bootId);
        } catch (e) { /* server still down → not a revival, keep retrying */ }
        if (revived) { location.reload(); return; }
        state.bootChecked = false;  // not a revival — release for next time
      }
      const s = state.sessions.find((x) => x.id === sid);
      // A stream that closes with zero frames has no job behind it (a live
      // job always replays at least its first `status` event); `knownActive`
      // covers the instant after Send, before the first frame arrived.
      const noJob = (s && s.status === "idle") ||
                    (v.seq === 0 && !v.knownActive);
      if (noJob) {
        // connection dropped and no work is actually ongoing (server
        // restarted, stale busy flag, or the stream never had a job).
        // The revived server's in-memory ring is EMPTY — the frames for
        // any auto-resumed turn live in the DB, not here — so settle from
        // the durable source: loadHistory re-renders the real final state
        // (finished answer, or the ↻ chip if it genuinely didn't resume)
        // and re-attaches the stream if the session IS processing.
        v.knownActive = false;
        loadHistory();
        refreshSessions();
        return;
      }
      v.errs += 1;
      if (v.errs > 8) { fallbackPoll(sid); return; }
      // reconnect from where we stopped — the server replays from `after`
      setTimeout(() => {
        if (state.sessionId === sid && !view(sid).done) openStream(sid, view(sid).seq);
      }, 800);
    };
    return es;
  }

  function handleEvent(sid, name, d) {
    // Right-panel state accumulates per chat — even for background sessions.
    if (name !== "thinking") closeThinkBurst(sid);
    if (name === "thinking") panelThinking(sid, d.text || "");
    if (name === "tool_end") { panelToolEnd(sid, d); return; }
    if (name === "queued") {
      // a message joined/leaved this session's queue — the server is
      // authoritative, so re-sync the real entries (text + files)
      // `fired` (drain-announce) = the text of the entry that JUST left the
      // queue to run. Stash it: the next `status` frame (the fired run's
      // first event) draws its user bubble from this — the AUTHORITATIVE
      // source, because the client's local peek (v._nextJob / state.queues)
      // is empty when the item was queued from ANOTHER tab. This event rides
      // the new run's buffer and is replayed BEFORE the status frame, so the
      // stash is set by the time the status branch runs.
      if (d.fired) view(sid)._firedText = d.fired;
      syncQueueFromServer(sid);
      return;
    }
    if (name === "replay_gap") {
      // Our seq was older than the ring's oldest frame — the in-memory
      // replay we just got is TRUNCATED (iOS froze this tab for hours,
      // then the reconnect replayed only the tail). Re-attach from the
      // durable source: loadHistory pulls the draft snapshot + events_log
      // and reopens the stream from the true seq. 10s cooldown so a
      // pathological loop (run longer than the ring) can't re-fetch
      // history in a tight cycle.
      // SPURIOUS variant (duplicate-reply bug, 2026-09-30): a FRESH run's
      // seq line is anchored ABOVE the previous run (tasks._anchor_seq),
      // so sendNow's openStream(sid, 0) trips the server's gap check on
      // EVERY send (0 < the new run's first seq) even though we received
      // the new run in full — the "missed" frames belong to the FINISHED
      // previous run, which history already holds. Re-attaching on those
      // wiped the live turn mid-run and re-subscribed from a stale cursor
      // (double delivery → the reply's tail rendered twice, until reload).
      // The ring's oldest at/after this turn's FIRST frame = the turn
      // holds the whole run → nothing missed → ignore.
      const v = view(sid);
      const t = v.turn;
      if (t && t.firstSeq && d.oldest <= t.firstSeq) return;
      if (sid === state.sessionId &&
          Date.now() - (v._gapAt || 0) > 10000) {
        v._gapAt = Date.now();
        v.gapReattach = true;  // genuine gap: loadHistory must re-draw the turn
        loadHistory();
      }
      return;
    }
    if (sid !== state.sessionId) return;  // render only the visible chat
    const v = view(sid);
    const turn = v.turn;
    switch (name) {
      case "thinking":
        if (turn) {
          if (!turn.thinkAutoed) {
            turn.thinkAutoed = true;
            setUpperTab("thinking", { auto: true, btn: el.rpTabThinking });
          }
          appendThinking(turn, d.text || "");
        }
        break;
      case "token":
        if (turn) { finalizeThinkingHint(turn); appendToken(turn, d.text || ""); }
        break;
      case "status":
        if (v.draining && (!turn || turn.finished)) {
          // the server drained the queue and the NEXT run just started —
          // this status frame is its first event, so the real turn for it
          // is created HERE (the placeholder was dropped in drainQueue)
          // The fired message's USER bubble is created HERE too: the busy
          // branch of send() no longer draws one (no bubble while queued),
          // so without this the fired turn's answer would sit above the
          // PREVIOUS user bubble — the "no user bubble until reload" bug.
          // drainQueue() peeked the head's text into v._nextJob before the
          // server popped it; the server's durable history row is the
          // fallback if the local peek was empty.
          // Text source, in order of authority: (1) the drain-announce
          // stash — the server told us exactly what fired, replayed before
          // this status frame; (2) the local peek (v._nextJob, set by
          // drainQueue) for same-tab sends; (3) the local queue head —
          // the server pops in its finally a beat AFTER our `done` frame,
          // so for a missed drain-announce the head still holds the entry.
          const firedEntry = (state.queues[sid] || [])[0];
          const fired = v._firedText
            || v._nextJob
            || (firedEntry && (firedEntry.text || "(attachment)"))
            || "(attachment)";
          v._firedText = null;  // consumed — one bubble per fired entry
          // internal nudges (auto-resume / execute-plan) render as their
          // compact markers, not as raw user bubbles — same rule as history
          if (fired === RESUME_TEXT) addResumeMarker();
          else if (fired === EXECUTE_PLAN_TEXT) addPlanMarker();
          else addUserMessage(fired);
          const nt = addAssistantTurn();
          nt.firstSeq = d.seq;  // this run's first frame (replay_gap filter)
          nt.start = Date.now();
          nt.row.hidden = false;
          v.turn = nt;
          v.done = false;
          v.knownActive = true;
          v.draining = false;
          setProcessing(true);
          resetProgress();
          // resume run: the server's status text is the resume nudge —
          // keep the original goal pinned instead of showing it
          setJob(v._nextJob || (resumeGoal() || d.text || "Working…"));
          v._nextJob = null;
          thinkLabel(nt, d.text || "Working…");
          progressPhase(d.text || "Working…");
          // Avatar handoff: addAssistantTurn's consecutive-avatar check saw
          // the previous assistant row and hid this turn's avatar
          // (msg-avatar-spacer) — designed for back-to-back assistant turns,
          // wrong for a queue drain: the fired message is a NEW user send,
          // it deserves its own avatar, or the old turn's avatar looks
          // "stuck" (the "avatar didn't slide" bug). Show it.
          nt.group.querySelector(".msg-avatar")
            ?.classList.remove("msg-avatar-spacer");
          scrollToBottom(true);
        } else {
          if (turn) thinkLabel(turn, d.text || "Working…");
          progressPhase(d.text || "Working…");
          // context meter: the compaction step flags its start
          // (`compacting: true`); any other status frame clears the flag
          if (d.compacting) {
            const s = state.sessions.find((x) => x.id === sid);
            renderCtxMeter((s && s.ctx_tokens) || state.ctxToks, true);
          } else if (el.ctxMeter.classList.contains("compacting")) {
            ctxMeterFromSession();
          }
        }
        break;
      case "llm_end":
        // context meter: the model's own prompt_tokens for THIS call is
        // the real context size — update the topbar pill live (the value
        // is also persisted server-side, so a reload shows it too)
        if (d.prompt_tokens) {
          const s = state.sessions.find((x) => x.id === sid);
          if (s) s.ctx_tokens = d.prompt_tokens;
          renderCtxMeter(d.prompt_tokens, false);
        }
        renderToks(d.tok_s);  // last real decode speed of this chat
        break;
      case "plan":
        startProgress(d.items || []);
        break;
      case "plan_update":
        updateProgress(d);
        break;
      case "tool_start":
        if (turn) {
          finalizeThinkingHint(turn);
          turn.toolsUsed = true;  // execution happened → final bubble gets tinted
          addPhase(turn, d.tool, shortenArgs(d.args || ""), d.args);
        }
        if (state.progress) state.progress.steps += 1;
        autoTabForTool(d.tool, d.args);
        // sidebar keeps the TASK phrase (plan step / LLM summary) — tool
        // names no longer overwrite it
        break;
      case "phase_end":
        if (turn) settlePhase(turn, d.tool, d);
        break;
      case "answer_start":
        if (turn) {
          finalizeThinkingHint(turn);
          turn.row.hidden = false;
          thinkLabel(turn, "Answering…");
          turn.answerSeg = turn.seg;  // the in-flight segment is the answer
        }
        break;
      case "approval":
        if (turn) renderApproval(turn, d);
        break;
      case "approval_closed": {
        const rec = approvals.get(d.approval_id);
        if (rec) {
          if (!rec.card.dataset.answered) settleApproval(rec.card, "expired");
          clearInterval(rec.timer);
          approvals.delete(d.approval_id);
        }
        break;
      }
      case "question":
        if (turn) renderQuestion(turn, d);
        break;
      case "question_closed": {
        const qrec = questions.get(d.question_id);
        if (qrec) {
          if (!qrec.card.dataset.answered) settleQuestion(qrec.card, "resolved");
          clearInterval(qrec.timer);
          questions.delete(d.question_id);
        }
        break;
      }
      case "correction":
        // note-only: the streamed answer stays intact, we just append a flag below it
        if (turn && (d.note || "").trim()) {
          const note = document.createElement("div");
          note.className = "msg-correction";
          const lab = document.createElement("span");
          lab.className = "msg-correction-label";
          lab.textContent = "Fact-check note:";
          const body = document.createElement("span");
          body.textContent = d.note;
          note.append(lab, body);
          turn.body.insertBefore(note, turn.actions);
        }
        break;
      case "done":
        v.done = true;
        v.knownActive = false;
        ctxMeterReset();  // run ended — clear any stuck compacting flag
        if (turn && !turn.finished) {
          finishTurn(turn, d.content, d.files, sid);
          // ended with NO final answer (empty content, no files) — treat it
          // like a stop: offer the chip + inline ↻ (only when nothing is
          // queued behind it; a draining queue will start the next run)
          if (!(d.content || "").trim() && !(d.files || []).length && !v.draining)
            offerResume(sid, turn);
        }
        // Plan mode: stash the final answer so a Plan→Act flip can auto-execute it.
        // Only the most recent plan turn matters — an Act turn clears the stash.
        if (turn && turn.mode === "plan" && d.content && d.content.trim())
          v.lastPlan = d.content;
        else if (turn && turn.mode === "act")
          v.lastPlan = null;
        setProcessing(false);
        finishProgress();
        setJobDone(true);  // run finished — keep the goal pinned as done
        // finishing in the OPEN chat = the user is looking at it → the
        // result is already seen, so no done-LED (it's for OTHER chats)
        if (sid === state.sessionId) markViewed(sid);
        refreshSessions();
        // Pop the next queued message NOW (sets v._nextJob + drops the
        // placeholder) so the job strip can show it during the handoff gap.
        // The server is authoritative — if the local queue is empty (another
        // tab queued it), ask the server before deciding to close the stream.
        drainQueue(sid);
        // Drain resync: the server pops the head in its own finally (a
        // moment after our `done` frame) and announces it on the `queued`
        // channel — but that event rides the NEW run's buffer, which this
        // stream may not replay (the onerror reconnect re-subscribes from
        // the old run's seq; the replay only carries it if the client
        // reattaches after the new run's first frame). A delayed resync
        // after the handoff gap makes the server's post-drain list
        // authoritative again — the tray row (or its absence) settles
        // either way. NOTE: the guard must NOT be v.draining — the next
        // run's `status` frame clears it well before this timer fires,
        // which is exactly when the resync is still needed.
        if ((state.queues[sid] || []).length || v._nextJob) {
          setTimeout(() => {
            if (state.sessionId === sid) syncQueueFromServer(sid);
          }, 1500);
        }
        // Pre-flip: the user toggled Plan→Act WHILE this plan turn was still
        // generating. The toggle only changes the mode (execution needs a
        // finished plan), so the flip never fired — and the Act button is
        // already active, so clicking it again is a no-op. The plan would
        // sit stashed forever. Run it here instead.
        if (v.lastPlan && sid === state.sessionId && state.mode === "act"
            && !state.processing)
          executePlan();  // pre-flip: the plan runs directly — no user bubble
        const handoff = (jobText) => {
          // A queued message follows: the server auto-drains it. The stream
          // closes on run A's sentinel and the natural onerror reconnect
          // re-subscribes to run B (replay from `after`); the NEXT run's
          // `status` frame creates the real turn (stop face up via draining).
          v.draining = true;
          v.done = false;
          v.knownActive = true;
          setProcessing(true);
          setJob(jobText);
          if (v.drainFallback) clearTimeout(v.drainFallback);
          v.drainFallback = setTimeout(() => {
            // the next run's first frame never came on this stream —
            // reconnect from scratch (the server replays from `after`)
            if (state.sessionId === sid && view(sid).draining)
              openStream(sid, view(sid).seq);
          }, 4000);
        };
        // Drain resync: the server pops the head in its own finally (a
        // moment after our `done` frame) and announces it on the `queued`
        // channel — but that event rides the NEW run's buffer, which this
        // stream may not replay (the onerror reconnect re-subscribes from
        // the old run's seq; the replay only carries it if the client
        // reattaches after the new run's first frame). A delayed resync
        // after the handoff gap makes the server's post-drain list
        // authoritative again — the tray row (or its absence) settles
        // either way. NOTE: the guard must NOT be v.draining — the next
        // run's `status` frame clears it well before this timer fires,
        // which is exactly when the resync is still needed.
        if ((state.queues[sid] || []).length || v._nextJob) {
          setTimeout(() => {
            if (state.sessionId === sid) syncQueueFromServer(sid);
          }, 1500);
        }
        if (v._nextJob) handoff(v._nextJob);
        else if ((state.queues[sid] || []).length) handoff("Working…");
        else syncQueueFromServer(sid).then(() => {
          if (v.draining) return;  // a handoff already happened meanwhile
          if ((state.queues[sid] || []).length && state.sessionId === sid)
            handoff("Working…");
          else closeStream(sid);
        });
        break;
      case "stopped":
        v.done = true;
        v.knownActive = false;
        ctxMeterReset();  // run ended — clear any stuck compacting flag
        if (turn && !turn.finished) {
          const note = document.createElement("div");
          note.className = "msg-stopped";
          note.textContent = (d.reason && d.reason !== "stopped by user")
            ? "⏹ Stopped — " + d.reason + "." : "⏹ Stopped.";
          turn.body.insertBefore(note, turn.actions);
          finishTurn(turn, turn.rawText, null);
          offerResume(sid, turn);   // chip + inline button, every stop
        }
        closeStream(sid);
        setProcessing(false);
        finishProgress();
        setJobDone(false);  // stopped mid-run — keep the goal, no ✓
        if (sid === state.sessionId) markViewed(sid);
        refreshSessions();
        // Stop = stop everything: the server dropped the queue too
        // (queued_dropped), so the tray rows are gone as well.
        state.queues[sid] = [];
        if (queueEditIndex >= 0) queueEditIndex = -1;
        renderQueueTray(sid);
        break;
      case "error":
        v.done = true;
        v.knownActive = false;
        if (turn && !turn.finished) {
          const err = document.createElement("div");
          err.className = "msg-error";
          err.textContent = d.message || "Something went wrong.";
          turn.body.insertBefore(err, turn.actions);
          settleThink(turn);
          settleGroups(turn);
          reparkAvatars();    // collapse shrank the timeline -> re-park the avatars
          offerResume(sid, turn);   // no final answer -> chip + inline button
        }
        setProcessing(false);
        finishProgress();
        setJobDone(false);  // errored — keep the goal, no ✓
        if (sid === state.sessionId) markViewed(sid);
        refreshSessions();
        if ((state.queues[sid] || []).length) {
          // an error is not a stop — the queue keeps running: same
          // handoff as `done` (stream stays open, next `status` starts it)
          v.draining = true;
          v.done = false;
          v.knownActive = true;
          setProcessing(true);
          if (v.drainFallback) clearTimeout(v.drainFallback);
          v.drainFallback = setTimeout(() => {
            if (state.sessionId === sid && view(sid).draining)
              openStream(sid, view(sid).seq);
          }, 4000);
        } else {
          closeStream(sid);
        }
        drainQueue(sid);
        break;
    }
    if (turn) scrollToBottom();
  }

  function shortenArgs(s) {
    try {
      const o = JSON.parse(s);
      const parts = [];
      for (const v of Object.values(o)) {
        let vs = typeof v === "string" ? v : JSON.stringify(v);
        if (vs.length > 60) vs = vs.slice(0, 60) + "…";
        parts.push(vs);
      }
      return parts.length ? "· " + parts.join(" · ") : "";
    } catch (e) { return s ? "· " + s.slice(0, 80) : ""; }
  }

  // Human phrase for a tool row — mirrors the server's _tool_phrase
  // (agent.py): verb + short target, so the chat reads "Reading app.js"
  // instead of "⚙ read_file …". Raw names stay in the Terminal tab.
  const TOOL_PHRASES = {
    read_file: "Reading", list_dir: "Browsing", write_file: "Writing",
    edit_file: "Editing", search_files: "Searching",
    local_search: "Searching notes", index_documents: "Indexing",
    run_command: "Running", web_search: "Searching web",
    fetch_url: "Fetching", browser: "Browsing",
    ask_user: "Waiting for your pick",
  };
  // One-line, never mid-token: cut at the last space/pipe/quote boundary
  // that fits, then "…" — a hard slice used to leave "…|ph" ghosts.
  function clipPhrase(s, max) {
    s = String(s || "").replace(/\s+/g, " ").trim();
    if (s.length <= max) return s;
    const cut = s.slice(0, max);
    const at = Math.max(cut.lastIndexOf(" "), cut.lastIndexOf("|"), cut.lastIndexOf('"'));
    return (at >= 8 ? cut.slice(0, at) : cut) + "…";
  }
  function toolPhrase(name, argsStr) {
    let a = {};
    try { a = JSON.parse(argsStr) || {}; } catch (e) { /* not JSON */ }
    if (typeof a !== "object" || a === null) a = {};
    const base = TOOL_PHRASES[name] || "Working on it";
    let target = "";
    if (["read_file", "write_file", "edit_file", "list_dir"].includes(name)) {
      const p = String(a.path || "");
      // last NON-EMPTY segment — a trailing slash used to pop "" and leave
      // the verb alone in the row
      const segs = p.split(/[\\/]/).filter(Boolean);
      target = clipPhrase(segs.length ? segs[segs.length - 1] : p, 30);
    } else if (name === "run_command") {
      target = clipPhrase(a.command, 40);
    } else if (["web_search", "fetch_url", "local_search", "search_files"].includes(name)) {
      target = clipPhrase(a.query || a.url || a.pattern, 40);
    }
    return { verb: base, target };
  }

  function setProcessing(flag) {
    state.processing = flag;
    el.sendBtn.hidden = flag;
    el.stopBtn.hidden = !flag;
  }

  function finishTurn(turn, content, files, sid) {
    turn.finished = true;
    closeThinkBurst(sid || state.sessionId);  // belt-and-braces: run ended, nothing may stay "live"
    finalizeThinkingHint(turn);
    settleThink(turn);
    settleGroups(turn);        // collapse finished tool groups FIRST, so the lane /
                               // offsetTop math below measures the settled (shorter)
                               // layout. Parking before the collapse left the group a
                               // whole group-height too low, over the final answer.
    if (content && content.trim()) {
      const { text, chips } = extractChips(content);
      const seg = renderAnswer(turn, text);
      moveAvatar(turn, seg, true);  // park above the answer AFTER the collapse
      addActions(turn, text);
      addChips(turn, chips);
      verifyLinks(seg, state.sessionId);
    } else {
      reparkAvatars();        // no final answer -> re-park the current target
    }
    addFileChips(turn, files || []);
    if (files && files.length) {
      openPreview(files[0]);   // generated files open in the right panel
    }
    scrollToBottom();
  }

  // EventSource kept failing — fall back to dumb history polling
  function fallbackPoll(sid) {
    if (sid !== state.sessionId) return;
    const v = view(sid);
    if (v.turn && !v.turn.finished) {
      settleThink(v.turn);
      thinkLabel(v.turn, "Reconnecting…");
    }
    pollWhileProcessing();
  }

  let sendInFlight = false;
  async function send(overrideText, opts) {
    // Enter mashing / double-click race: the first call is still awaiting
    // /api/sessions/new (bubble not added yet, state.processing not set),
    // so the second call would add a duplicate ghost bubble. One in-flight
    // send per tab, full stop.
    if (sendInFlight) return;
    sendInFlight = true;
    const isResume = !!(opts && opts.resume);
    // only a real string counts — a click event handed in by a listener
    // must never be treated as the message (it used to ship as "{}" and
    // 500 the server while showing "[object PointerEvent]" in the bubble)
    const text = (typeof overrideText === "string" && overrideText)
      ? overrideText : el.input.value.trim();
    if (!text && !state.attachments.length) { sendInFlight = false; return; }
    try {
      // Busy? Queue it: the message waits in the per-session FIFO and fires
      // the moment the current run fully ends (server-side drain).
      if (state.processing && state.sessionId) {
        if (!isResume) {
          const files = state.attachments.slice();
          el.input.value = "";
          autosize();
          clearAttachments();
          try { localStorage.removeItem("muji.draft"); } catch (e) {}
          // No transcript bubble while queued — the queue is pipeline
          // state and the tray is its only view. (The old bubble was
          // in-memory and vanished on reload anyway; the tray survives.)
          const entry = { text, files };
          (state.queues[state.sessionId] =
           state.queues[state.sessionId] || []).push(entry);
          renderQueueTray(state.sessionId);
          // The SERVER is the queue — ship the message to it NOW. If we only
          // kept it in state.queues, the drain (which runs server-side the
          // moment the current run ends) would fire the NEXT queued message
          // while this one sat in the client's memory, unexecuted.
          api("/api/chat", {
            method: "POST",
            body: JSON.stringify({ session_id: state.sessionId, message: text,
                                   files, mode: state.mode, roast: state.roast }),
          }).then((res) => res.json().catch(() => ({})))
            .then((body) => {
              if (body && body.ok) {
                // the server accepted it — its `queued` event (and the tray
                // sync it triggers) is the live confirmation
                syncQueueFromServer(state.sessionId);
              } else {
                // the server rejected it (offline, bad session) — the tray
                // is a lie now; drop the entry and say so
                const q = state.queues[state.sessionId] || [];
                const i = q.indexOf(entry);
                if (i >= 0) q.splice(i, 1);
                renderQueueTray(state.sessionId);
                const err = document.createElement("div");
                err.className = "msg-error";
                err.textContent = "Couldn't queue this message — " +
                  ((body && body.detail) || "the server didn't accept it.");
                el.messages.appendChild(err);
                scrollToBottom(true);
              }
            })
            .catch(() => {
              const q = state.queues[state.sessionId] || [];
              const i = q.indexOf(entry);
              if (i >= 0) q.splice(i, 1);
              renderQueueTray(state.sessionId);
            });
        }
        return;
      }
      if (!state.sessionId) {
        const res = await api("/api/sessions/new", {
          method: "POST", body: JSON.stringify({ workspace_id: state.wsSel || undefined }),
        });
        const s = await res.json();
        state.sessionId = s.id;
        localStorage.setItem("muji.sid", s.id);
        saveMode();  // persist the current Plan/Act choice for this new chat
        saveRoast(); // …and the roast level
      }
      const files = state.attachments.slice();
      el.input.value = "";
      autosize();
      clearAttachments();
      try { localStorage.removeItem("muji.draft"); } catch (e) {}
      addUserMessage(text || "(attachment)", files, Date.now());
      // sendNow's sync prefix sets state.processing=true before its first
      // await, so from here on the queue path is the duplicate guard —
      // the flag only covers the gap up to this point.
      return sendNow(text, files, isResume);
    } finally {
      sendInFlight = false;
    }
  }

  // ── Message queue: FIFO per session, drained server-side ─────────
  // While a run is in flight, new messages wait in state.queues[sid] (and
  // the server's own queue, which is authoritative). The moment a run
  // fully ends, drainQueue() fires the next one. The server pushes a
  // `queued` event whenever the queue changes, so the tray stays in sync
  // even when the message came from ANOTHER tab.
  //
  // The tray is the ONLY queue view: it renders from state.queues, which
  // syncQueueFromServer() rebuilds from GET /queue — so the ✎/✕/clear-all
  // controls survive a tab reload by construction. (The old transcript
  // placeholders were in-memory and died on reload, leaving only a dumb
  // non-cancellable chip behind — that was the bug this replaces.)
  let queueExpanded = false;  // the tray is COLLAPSED by default (boss call 2026-10-12, reversed)
  let queueEditIndex = -1;    // the item currently in inline-edit mode

  function renderQueueTray(sid) {
    const tray = el.queueTray;
    const q = state.queues[sid] || [];
    const n = q.length;
    if (!n) {
      tray.hidden = true;
      tray.innerHTML = "";
      queueEditIndex = -1;
      return;
    }
    tray.hidden = false;
    tray.innerHTML = "";

    // The head IS the fold — clicking it (anywhere except clear-all)
    // toggles expand/collapse. Collapsed = ONE line with the "next:"
    // preview.
    const head = document.createElement("div");
    head.className = "qt-head qt-foldable";
    const arrow = document.createElement("span");
    arrow.className = "qt-arrow";
    arrow.textContent = queueExpanded ? "▾" : "▸";
    head.appendChild(arrow);
    const label = document.createElement("span");
    label.className = "qt-count";
    label.textContent = (n === 1 ? "1 queued" : n + " queued")
      + (queueExpanded ? "" : " · next: " +
        ((q[0] && q[0].text || "(attachment)").slice(0, 80)));
    head.appendChild(label);
    const clearBtn = document.createElement("button");
    clearBtn.className = "qt-clear";
    clearBtn.textContent = "clear all";
    clearBtn.title = "Drop every queued message";
    clearBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      clearQueueAll(sid);
    });
    head.appendChild(clearBtn);
    head.title = queueExpanded ? "Collapse the queue" : "Expand the queue";
    head.addEventListener("click", () => {
      queueExpanded = !queueExpanded;
      renderQueueTray(sid);
    });
    tray.appendChild(head);

    if (!queueExpanded) return;  // collapsed: head line only

    const list = document.createElement("div");
    list.className = "qt-list";
    q.forEach((entry, i) => list.appendChild(queueItemRow(sid, entry, i)));
    tray.appendChild(list);
  }

  function queueItemRow(sid, entry, i) {
    const row = document.createElement("div");
    row.className = "qt-item" + (queueEditIndex === i ? " qt-editing" : "");
    const num = document.createElement("span");
    num.className = "qt-num";
    num.textContent = i + 1;
    row.appendChild(num);

    if (queueEditIndex === i) {
      const ta = document.createElement("textarea");
      ta.className = "qt-edit";
      ta.value = entry.text || "";
      ta.rows = 1;
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          saveQueueEdit(sid, i, ta.value);
        } else if (e.key === "Escape") {
          queueEditIndex = -1;
          renderQueueTray(sid);
        }
      });
      row.appendChild(ta);
      const save = document.createElement("button");
      save.className = "qt-btn qt-save";
      save.textContent = "save";
      save.title = "Save (↵)";
      save.addEventListener("click", () => saveQueueEdit(sid, i, ta.value));
      row.appendChild(save);
      const cancel = document.createElement("button");
      cancel.className = "qt-btn";
      cancel.textContent = "cancel";
      cancel.title = "Cancel (esc)";
      cancel.addEventListener("click", () => {
        queueEditIndex = -1;
        renderQueueTray(sid);
      });
      row.appendChild(cancel);
      requestAnimationFrame(() => {
        ta.focus();
        ta.selectionStart = ta.value.length;
      });
    } else {
      const txt = document.createElement("span");
      txt.className = "qt-text" + (i === 0 ? " qt-next" : "");
      txt.textContent = entry.text || "(attachment)";
      txt.title = (entry.text || "") + " — click to edit";
      txt.addEventListener("click", () => {
        queueEditIndex = i;
        renderQueueTray(sid);
      });
      row.appendChild(txt);
      if (i === 0) {
        const hint = document.createElement("span");
        hint.className = "qt-hint";
        hint.textContent = "runs next";
        row.appendChild(hint);
      }
      const edit = document.createElement("button");
      edit.className = "qt-btn";
      edit.textContent = "✎";
      edit.title = "Edit this queued message";
      edit.addEventListener("click", () => {
        queueEditIndex = i;
        renderQueueTray(sid);
      });
      row.appendChild(edit);
    }

    const x = document.createElement("button");
    x.className = "qt-btn qt-x";
    x.textContent = "✕";
    x.title = "Cancel this queued message";
    x.addEventListener("click", () => cancelQueued(sid, i));
    row.appendChild(x);
    return row;
  }

  function saveQueueEdit(sid, i, text) {
    const clean = (text || "").trim();
    queueEditIndex = -1;
    if (!clean) { renderQueueTray(sid); return; }  // empty → keep old text
    const q = state.queues[sid] || [];
    if (!q[i]) { renderQueueTray(sid); return; }
    q[i].text = clean;  // optimistic — the sync below settles it
    renderQueueTray(sid);
    api("/api/sessions/" + encodeURIComponent(sid) + "/queue/update", {
      method: "POST", body: JSON.stringify({ index: i, text: clean }),
    }).then((res) => res.json().catch(() => ({}))).then((body) => {
      if (body && body.drained) {
        // the item ran between load and save — the OLD text went out;
        // offer the edit back as a composer draft so nothing is lost
        el.input.value = clean;
        autosize();
        const note = document.createElement("div");
        note.className = "msg-error";
        note.textContent = "That queued message already started running — " +
          "your edit is back in the composer.";
        el.messages.appendChild(note);
        scrollToBottom(true);
      }
      syncQueueFromServer(sid);
    }).catch(() => syncQueueFromServer(sid));
  }

  function cancelQueued(sid, i) {
    const q = state.queues[sid] || [];
    if (i < 0 || i >= q.length) return;
    q.splice(i, 1);
    if (queueEditIndex === i) queueEditIndex = -1;
    else if (queueEditIndex > i) queueEditIndex--;  // edit slid down one slot
    renderQueueTray(sid);
    // server-side drop (the server's queue is authoritative — re-sync after)
    api("/api/sessions/" + encodeURIComponent(sid) + "/queue/remove", {
      method: "POST", body: JSON.stringify({ index: i }),
    }).then(() => syncQueueFromServer(sid)).catch(() => {});
  }

  function clearQueueAll(sid) {
    const q = state.queues[sid] || [];
    if (!q.length) return;
    state.queues[sid] = [];
    queueEditIndex = -1;
    renderQueueTray(sid);
    api("/api/sessions/" + encodeURIComponent(sid) + "/queue/clear", {
      method: "POST",
    }).then(() => syncQueueFromServer(sid)).catch(() => {});
  }

  async function syncQueueFromServer(sid) {
    try {
      const data = await (await api(
        "/api/sessions/" + encodeURIComponent(sid) + "/queue")).json();
      const server = data.queued || [];
      // Full rebuild, not a merge: the tray has no per-entry DOM refs to
      // preserve (rows are rebuilt every render), and a rebuild is what
      // makes reload-proofness free — the server's list IS the view.
      state.queues[sid] = server.map((se) => ({
        text: se.text || "", files: se.files || [],
      }));
      // an edit that no longer maps to a slot (item drained/removed) dies
      if (queueEditIndex >= state.queues[sid].length) queueEditIndex = -1;
      if (sid === state.sessionId) renderQueueTray(sid);
    } catch (e) { /* offline — the tray is cosmetic, the server is authoritative */ }
  }

  // Called from every terminal settle (done / stopped / error) + on stream
  // settle. The SERVER is the queue driver — it auto-drains the moment a
  // run fully ends (so queued messages run even if this tab is closed).
  // Here we only PREPARE the UI: peek the head (pinned-job text for the
  // handoff strip) and hold the stop face up (v.draining) until the next
  // run's `status` event arrives on the same stream — THAT is what creates
  // the real turn. We deliberately do NOT pop the local entry: the server
  // pops in its own finally (after our `done` frame) and announces it on
  // the `queued` channel — THAT is what drops the tray row. Popping
  // optimistically here made the tray vanish a beat before the server
  // drained (and, once the queue hit 0, hide it entirely until the next
  // sync — the "rack disappears until reload" bug).
  function drainQueue(sid) {
    if (sid !== state.sessionId) return;  // background chats: the server
                                           // drains on its own; the tray
                                           // resyncs via loadHistory/refresh
    const entry = (state.queues[sid] || [])[0];
    if (!entry) return;  // nothing local — the delayed resync asks the server
    const v = view(sid);
    v.draining = true;  // keep the stop face up across the run handoff
    v._nextJob = entry.text || "(attachment)";  // pinned-job text for the new run
    renderQueueTray(sid);  // row STAYS; the drain-announce drops it live
  }

  // The actual dispatch — the user bubble is added by the caller (send()
  // for fresh messages; queued messages already have theirs from the busy
  // branch). Queued follow-ups are NOT dispatched from here: the server
  // auto-drains them and their turn is created on the next `status` frame.
  async function sendNow(text, files, isResume) {
    const sid = state.sessionId;
    if (isResume) addResumeMarker();

    const turn = addAssistantTurn();
    turn.start = Date.now();
    turn.row.hidden = false;  // optimistic "Working…" chip — no dead time before the first event
    const v = view(sid);
    v.turn = turn;
    v.done = false;
    setProcessing(true);
    resetProgress();
    progressPhase("Working…");  // indeterminate bar from t=0 until a plan/phase event arrives
    updateResumeChip(false);
    // a new run supersedes the old ↻ affordances — the inline resume
    // buttons on earlier stopped turns are stale now, take them down
    el.messages.querySelectorAll(".resume-btn").forEach((b) => b.remove());
    setJob(isResume ? RESUME_LABEL : (text || "(attachment)"));
    // The optimistic UI above (user bubble + "Working…" chip + progress bar)
    // grows/shrinks the layout AFTER the smooth scrolls fired in
    // addUserMessage()/addAssistantTurn(), so their animation target is stale
    // and the view rests ~one progress-bar height short — the new bubble
    // looks hidden below the fold. Re-pin instantly to the true bottom.
    scrollToBottom(true);

    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ session_id: sid, message: text, files,
                               mode: state.mode, roast: state.roast }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        const err = document.createElement("div");
        err.className = "msg-error";
        err.textContent = errBody.detail || "Request failed (" + res.status + ")";
        turn.body.insertBefore(err, turn.actions);
        settleThink(turn);
        v.knownActive = false;
        setProcessing(false);
        setJobDone(false);  // request failed — keep the goal, no ✓
        return;
      }
      // The job now runs in the background — subscribe to its event stream.
      // Switching away from the chat (or closing the tab) does NOT stop it.
      v.knownActive = true;
      const body = await res.json().catch(() => ({}));
      if (body.queued) {
        // The message WAITED behind an in-flight run. Reopening from 0 would
        // make the server re-replay that run's whole ring tail into THIS
        // turn — duplicate phases (the step count balloons), duplicate
        // thinking bursts, and the run's OLD question cards firing back to
        // life. Keep the live stream; if it's gone, attach from where we are.
        if (!v.es) openStream(sid, v.seq);
      } else {
        // A NEW run started: its seq line begins fresh and its ring holds
        // only its own frames, so replay from 0 (the run's opening frames).
        openStream(sid, 0);
      }
    } catch (e) {
      const err = document.createElement("div");
      err.className = "msg-error";
      err.textContent = "Could not reach the server.";
      turn.body.insertBefore(err, turn.actions);
      settleThink(turn);
      v.knownActive = false;
      setProcessing(false);
      setJobDone(false);  // offline — keep the goal, no ✓
    }
  }

  // ── Plan/Act mode (Cline-style, per chat) + Resume ────────────
  function setMode(mode) {
    state.mode = mode === "plan" ? "plan" : "act";
    el.modeToggle.querySelectorAll(".mode-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === state.mode));
    // Composer outline follows the active mode (gold = plan, green = act).
    document.body.dataset.mode = state.mode;
    // No re-tint on toggle: final bubbles keep the mode they were produced in.
    renderComposerStatus();
  }

  async function saveMode() {
    if (!state.sessionId) return;
    try {
      await api("/api/sessions/mode", {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId, mode: state.mode }),
      });
      const s = state.sessions.find((x) => x.id === state.sessionId);
      if (s) s.mode = state.mode;
    } catch (e) { /* server defaults to act for this turn */ }
  }

  // ── Roast level (per chat): 😴 off · 😏 chill · 🔥 full ─────────
  function setRoast(level) {
    state.roast = (level === "off" || level === "full") ? level : "chill";
    el.roastToggle.querySelectorAll(".mode-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.roast === state.roast));
  }

  async function saveRoast() {
    if (!state.sessionId) return;
    try {
      await api("/api/sessions/roast", {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId, roast: state.roast }),
      });
    } catch (e) { /* server defaults to chill for this turn */ }
  }

  function renderComposerStatus() {
    // the status row hosts the resume/abandon chips + the queue tray — the
    // Plan/Act toggle itself shows the active mode (accent color), no
    // separate chip
    el.composerStatus.hidden = el.resumeChip.hidden && el.abandonChip.hidden
      && el.queueTray.hidden;
  }

  function updateResumeChip(show) {
    if (!show) state.resumable = false;  // chip gone → stateful resume gone
    el.resumeChip.hidden = !show;
    el.abandonChip.hidden = !show;  // same trigger — an unfinished run to drop
    renderComposerStatus();
  }

  // Any run that ended WITHOUT a final answer (stopped, error, dropped
  // stream, empty done) gets BOTH the composer chip and an inline ↻ button
  // on the turn — whichever is in view, the user never types "resume".
  // Idempotent: the flag guards double-inserts across the settle paths.
  function offerResume(sid, turn) {
    updateResumeChip(true);
    if (turn && !turn._resumeBtn) {
      const rb = document.createElement("button");
      rb.className = "resume-btn";
      rb.textContent = "↻ Resume";
      rb.title = "Continue where the previous run stopped";
      rb.addEventListener("click", () => resume());
      turn.body.insertBefore(rb, turn.actions);
      turn._resumeBtn = rb;
    }
  }

  // ── the human gate: two topbar buttons, two panels, two record stores ──
  // 🧠 brain  = learned.md    (my failures) — /api/learned
  // 🔧 wench  = tool_notes.md (verified tool quirks) — /api/tool_notes
  // No composer banner (retired 2026-10-12, boss call): new pending entries
  // don't pop above the composer — the list is only shown when the boss
  // opens it via the topbar buttons. A dot on the button marks pending.
  // Deterministic + app-level (not the model) so it's reliable and free.
  // Save rewrites the file via its own decide endpoint — the model still
  // never self-activates; the file is the single source of truth.
  function srcState(src) { return src === "toolnote" ? state.toolNotes : state.learned; }
  function srcEl(src) {
    return src === "toolnote"
      ? { panel: el.wenchPanel, list: el.wenchList, open: el.wenchOpen }
      : { panel: el.learnPanel, list: el.learnList, open: el.learnOpen };
  }
  function allPending(src) {
    const st = srcState(src);
    return st.pending.map((t) => ({ src, text: t }));
  }
  function allActive(src) {
    const st = srcState(src);
    return st.active.map((t) => ({ src, text: t }));
  }
  async function loadLearned() {
    let data;
    try { data = await (await api("/api/learned")).json(); } catch (e) { return; }
    state.learned.pending = data.pending || [];
    state.learned.active = data.active || [];
    state.learned.loaded = true;
    let tn;
    try { tn = await (await api("/api/tool_notes")).json(); } catch (e) { /* endpoint absent (pre-restart) — learned-only */ }
    if (tn) {
      state.toolNotes.pending = tn.pending || [];
      state.toolNotes.active = tn.active || [];
      state.toolNotes.loaded = true;
    }
    renderLearnedDot("learned");
    if (state.toolNotes.loaded) renderLearnedDot("toolnote");
  }

  // No banner: the only ambient signal is a dot on the topbar button while
  // its file has pending entries. The list itself opens on demand.
  function renderLearnedDot(src) {
    const e = srcEl(src);
    e.open.classList.toggle("has-pending", allPending(src).length > 0);
  }

  function learnDecideButtons() {
    const seg = document.createElement("span");
    seg.className = "learn-seg";
    ["activate", "keep", "veto"].forEach((a) => {
      const b = document.createElement("button");
      b.dataset.a = a; b.textContent = a[0].toUpperCase() + a.slice(1);
      b.addEventListener("click", () =>
        seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)));
      seg.appendChild(b);
    });
    seg.querySelector('[data-a="keep"]').classList.add("on");  // safe default: no change
    return seg;
  }

  function sourceTag(src) {
    const tag = document.createElement("span");
    tag.className = "learn-src" + (src === "toolnote" ? " src-tool" : "");
    tag.textContent = src === "toolnote" ? "tool" : "learned";
    tag.title = src === "toolnote"
      ? "Tool quirk (tool_notes.md) — verified API behavior with a fix + recheck rule"
      : "Learned pattern (learned.md) — a failure muji logged";
    return tag;
  }

  function buildLearnedList(src) {
    const L = srcEl(src).list;
    L.innerHTML = "";
    const pend = document.createElement("div");
    pend.className = "learn-section-label";
    pend.textContent = allPending(src).length + " pending";
    L.appendChild(pend);
    allPending(src).forEach(({ text: t }) => {
      const row = document.createElement("div");
      row.className = "learn-row";
      row.dataset.src = src;
      const txt = document.createElement("div");
      txt.className = "learn-text"; txt.textContent = t;
      row.append(sourceTag(src), txt, learnDecideButtons());
      L.appendChild(row);
    });
    const actives = allActive(src);
    if (actives.length) {
      const act = document.createElement("div");
      act.className = "learn-section-label";
      act.textContent = actives.length + " active (currently injected)";
      L.appendChild(act);
      actives.forEach(({ text: t }) => {
        const row = document.createElement("div");
        row.className = "learn-row active";
        row.dataset.src = src;
        const txt = document.createElement("div");
        txt.className = "learn-text"; txt.textContent = t;
        const x = document.createElement("button");
        x.className = "learn-x"; x.textContent = "\u2715"; x.title = "Remove from active";
        x.addEventListener("click", () => {
          if (x.dataset.deactivate) { delete x.dataset.deactivate; x.style.background = ""; x.style.color = ""; }
          else { x.dataset.deactivate = t; x.style.background = "var(--panel-2)"; x.style.color = "var(--warn)"; }
        });
        row.append(sourceTag(src), txt, x);
        L.appendChild(row);
      });
    }
    // Sticky action bar: the list scrolls (max-height) but Save/Cancel stay
    // reachable no matter how long the pending list gets (Boss rule,
    // 2026-09-25) — it pins to the bottom of the list's viewport.
    const actions = document.createElement("div");
    actions.className = "learn-actions";
    const save = document.createElement("button");
    save.className = "btn primary"; save.textContent = "Save decisions";
    save.addEventListener("click", () => saveLearned(src));
    const cancel = document.createElement("button");
    cancel.className = "btn"; cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => closeLearnedList(src));
    actions.append(save, cancel);
    L.appendChild(actions);
  }

  function openLearnedList(src) {
    buildLearnedList(src);
    const e = srcEl(src);
    // The panel floats above the composer (banner retired 2026-10-12) —
    // shown only while open; new pending entries never pop it on their own.
    e.panel.hidden = false;
  }
  // Cancel: close the panel. The topbar dot marks pending until it's cleared.
  function closeLearnedList(src) {
    const e = srcEl(src);
    e.panel.hidden = true;
    renderLearnedDot(src);
  }

  async function saveLearned(src) {
    const e = srcEl(src);
    const decisions = [];
    e.list.querySelectorAll(".learn-row").forEach((row) => {
      if (row.classList.contains("active")) {
        const x = row.querySelector(".learn-x");
        if (x && x.dataset.deactivate) decisions.push({ text: x.dataset.deactivate, action: "deactivate" });
        return;
      }
      const on = row.querySelector(".learn-seg button.on");
      if (on) decisions.push({ text: row.querySelector(".learn-text").textContent, action: on.dataset.a });
    });
    const changed = decisions.filter((d) => d.action !== "keep");
    let totals = { activated: 0, vetoed: 0, deactivated: 0 };
    if (changed.length) {
      try {
        const url = src === "toolnote" ? "/api/tool_notes/decide" : "/api/learned/decide";
        const res = await (await api(url, {
          method: "POST", body: JSON.stringify({ decisions: changed }),
        })).json();
        const st = srcState(src);
        st.pending = res.pending || [];
        st.active = res.active || [];
        totals.activated += res.activated || 0;
        totals.vetoed += res.vetoed || 0;
        totals.deactivated += res.deactivated || 0;
      } catch (err) { learnToast("Save failed — try again"); return; }
    }
    e.panel.hidden = true;
    learnToast(totals.activated + " activated, " + totals.vetoed + " vetoed, " +
               totals.deactivated + " deactivated");
    renderLearnedDot(src);
  }

  let learnToastEl = null;
  function learnToast(msg) {
    if (!learnToastEl) {
      learnToastEl = document.createElement("div");
      learnToastEl.className = "learn-toasts";
      document.body.appendChild(learnToastEl);
    }
    const t = document.createElement("div");
    t.className = "learn-toast"; t.textContent = msg;
    learnToastEl.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, 2600);
  }

  const RESUME_TEXT =
    "Continue — the previous run was stopped mid-task. Pick up exactly where " +
    "you left off (build on what was already done, don't redo it) and finish the job.";



  async function resume(sid) {
    sid = sid || state.sessionId;
    if (state.processing || !sid) return;
    updateResumeChip(false);
    addResumeMarker();
    const turn = addAssistantTurn();
    turn.row.hidden = false;
    const v = view(sid);
    v.turn = turn;
    v.done = false;
    // in-flight resume: a settle that lands BEFORE the run is active
    // (stale /api/history response, or the gap between POST and enqueue)
    // would otherwise re-show the chip off the still-saved run_state —
    // pendingResume + resumeAt suppress settles that predate this click,
    // and the gen bump makes an in-flight loadHistory skip its settle
    v.pendingResume = true;
    v.resumeAt = Date.now();
    v.gen = (v.gen || 0) + 1;
    setProcessing(true);
    resetProgress();
    progressPhase(state.resumable ? "Resuming…" : "Working…");
    // Keep the original task pinned — the user sees the real goal, not
    // a "Resuming…" label. Falls back to the label only if the goal is
    // somehow unknown (shouldn't happen in practice).
    setJob(resumeGoal() || RESUME_LABEL);
    scrollToBottom(true);
    try {
      // stateful resume: the server re-enters the saved loop state when it
      // exists (crash/restart, hard stop, max-turns); otherwise it degrades
      // to a plain fresh turn with the nudge — same UX either way
      const res = await api("/api/chat/resume", {
        method: "POST",
        body: JSON.stringify({ session_id: sid, message: RESUME_TEXT,
                               mode: state.mode, roast: state.roast }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        const err = document.createElement("div");
        err.className = "msg-error";
        err.textContent = errBody.detail || "Resume failed (" + res.status + ")";
        turn.body.insertBefore(err, turn.actions);
        settleThink(turn);
        v.knownActive = false;
        v.pendingResume = false;  // no run started — chip may come back on settle
        v.resumeAt = 0;
        setProcessing(false);
        setJobDone(false);  // resume failed — keep the goal, no ✓
        return;
      }
      state.resumable = false;  // consumed — the next ↻ (if any) is a fresh turn
      v.knownActive = true;
      v.pendingResume = false;  // the run is live — the pre-active gap is over
      openStream(sid, 0);
    } catch (e) {
      const err = document.createElement("div");
      err.className = "msg-error";
      err.textContent = "Could not reach the server.";
      turn.body.insertBefore(err, turn.actions);
      settleThink(turn);
      v.knownActive = false;
      v.resumeAt = 0;  // offline — chip may come back on settle
      setProcessing(false);
      setJobDone(false);  // offline — keep the goal, no ✓
    }
  }

  // Sidebar continue-symbol click: resume from the list, no need to enter
  // the chat first. Opens the chat (so the run is visible while it flies)
  // and fires the same resume() path — the in-chat ↻ chip shows up too.
  function resumeSidebar(sid, s) {
    if (s.status === "running" || s.status === "queued") return;  // busy — row just opens
    if (state.sessionId !== sid) switchSession(sid);
    resume(sid);
  }

  // ✕ Start fresh: the opposite of resume — drop the unfinished task
  // entirely. The chat + its history stay; the saved run_state is cleared
  // so the ↻ affordances disappear, the inline resume button comes off the
  // stopped turn, and the composer is ready for something different. No
  // nudge goes to the model — the next message is a plain fresh turn.
  async function abandonRun() {
    const sid = state.sessionId;
    if (!sid || state.processing) return;
    try {
      await api("/api/sessions/" + encodeURIComponent(sid) + "/abandon", {
        method: "POST", body: JSON.stringify({ session_id: sid }),
      });
    } catch (e) { /* offline — chips clear locally; the server's state
                       (if any) will resync on the next settle */ }
    state.resumable = false;
    updateResumeChip(false);
    // remove the inline ↻ buttons this chat rendered
    el.messages.querySelectorAll(".resume-btn").forEach((b) => b.remove());
    // the sidebar's continue symbol mirrors has_run_state — force a
    // re-render so symbol and chip can't disagree
    state.sessionSig = "";
    refreshSessions();
  }

  // Plan→Act flip: execute the stashed plan directly — same treatment as
  // the resume chip (compact ▶ marker in chat, nudge + plan go to the model
  // only). No user bubble: the plan is already visible above.
  async function executePlan() {
    if (state.processing || !state.sessionId) return;
    const v = view(state.sessionId);
    const plan = v.lastPlan;
    if (!plan) return;
    v.lastPlan = null;
    addPlanMarker();
    const turn = addAssistantTurn();
    turn.row.hidden = false;
    v.turn = turn;
    v.done = false;
    setProcessing(true);
    resetProgress();
    progressPhase("Working…");
    setJob(EXECUTE_PLAN_LABEL);
    scrollToBottom(true);
    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId,
                               message: EXECUTE_PLAN_TEXT + "\n\n" + plan,
                               files: [],
                               mode: state.mode, roast: state.roast }),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        const err = document.createElement("div");
        err.className = "msg-error";
        err.textContent = errBody.detail || "Execute failed (" + res.status + ")";
        turn.body.insertBefore(err, turn.actions);
        settleThink(turn);
        v.knownActive = false;
        setProcessing(false);
        setJobDone(false);
        return;
      }
      v.knownActive = true;
      const body = await res.json().catch(() => ({}));
      if (body.queued) {
        // behind an in-flight run — keep the live stream, it'll drain
        if (!v.es) openStream(state.sessionId, v.seq);
      } else {
        openStream(state.sessionId, 0);
      }
    } catch (e) {
      const err = document.createElement("div");
      err.className = "msg-error";
      err.textContent = "Could not reach the server.";
      turn.body.insertBefore(err, turn.actions);
      settleThink(turn);
      v.knownActive = false;
      setProcessing(false);
      setJobDone(false);
    }
  }

  // ── Stop / uploads / attachments ──────────────────────────────
  async function stop() {
    if (!state.sessionId) return;
    const sid = state.sessionId;
    try {
      const res = await api("/api/chat/stop", {
        method: "POST", body: JSON.stringify({ session_id: sid }),
      });
      if (!res.ok) return;
      const data = await res.json().catch(() => ({}));
      if (data.stopped && sid === state.sessionId)
        return;  // a live job was stopped — its `stopped` event settles the UI
      // The server has no live job for this chat, so no `stopped` event is
      // coming — settle now: stop face hidden, resume chip re-derived.
      const v = view(sid);
      v.done = true;
      v.knownActive = false;
      closeStream(sid);
      if (sid === state.sessionId) await loadHistory();
      refreshSessions();
    } catch (e) { /* offline — stream reconnect or the poller will settle it */ }
  }

  function renderAttachments() {
    el.attachments.innerHTML = "";
    state.attachments.forEach((a, i) => {
      const chip = document.createElement("span");
      chip.className = "attach-chip";
      if (guessKind(a.name) === "image" && a.url) {
        // image chip: mini thumbnail + name, so you see what you're sending
        const im = document.createElement("img");
        im.src = a.url;
        im.alt = a.original || a.name;
        im.className = "attach-thumb";
        chip.appendChild(im);
        const nm = document.createElement("span");
        nm.textContent = a.original || a.name;
        chip.appendChild(nm);
      } else {
        chip.appendChild(icon("paperclip"));
        chip.appendChild(document.createTextNode(" " + (a.original || a.name)));
      }
      const rm = document.createElement("button");
      rm.textContent = " ✕";
      rm.addEventListener("click", () => {
        state.attachments.splice(i, 1);
        renderAttachments();
        updateSendEnabled();
      });
      chip.appendChild(rm);
      el.attachments.appendChild(chip);
    });
  }

  function clearAttachments() {
    state.attachments = [];
    renderAttachments();
  }

  async function uploadFile(file) {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/upload", { method: "POST", body: form });
    if (!res.ok) { alert("Upload failed"); return; }
    const data = await res.json();
    state.attachments.push({ name: data.name, url: data.url, original: data.original });
    renderAttachments();
    updateSendEnabled();
  }

  function updateSendEnabled() {
    el.sendBtn.disabled = !el.input.value.trim() && !state.attachments.length;
  }

  // ── Theme ─────────────────────────────────────────────────────
  // 2 themes, cycled by the topbar toggle: forest ↔ forest-dark
  // (old light/dark dropped 2026-10-05; their saved values remap below).
  const THEMES = ["forest", "forest-dark"];

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const dark = theme.includes("dark");
    $("hljs-light").media = dark ? "not all" : "all";
    $("hljs-dark").media = dark ? "all" : "not all";
    localStorage.setItem("muji.theme", theme);
  }

  function initTheme() {
    const saved = localStorage.getItem("muji.theme");
    const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "forest-dark" : "forest";
    // remap dropped themes: dark → forest-dark, light → forest
    const mapped = saved === "dark" ? "forest-dark"
                 : saved === "light" ? "forest" : saved;
    applyTheme(THEMES.includes(mapped) ? mapped : preferred);
  }

  // ── Preview panel ─────────────────────────────────────────────
  // Syntax-color `text` into <code> with a line-number column. The whole
  // file is highlighted in ONE pass (hljs keeps the newlines) and the .ln
  // column is spliced back in afterwards — hljs.highlightElement() reads
  // textContent and would bake the line numbers into the highlighted code.
  function renderCodeInto(code, text, name) {
    const lang = (String(name || "").split(".").pop() || "").toLowerCase();
    let html;
    try {
      html = lang
        ? hljs.highlight(text, { language: lang, ignoreIllegals: true }).value
        : (text.length < 20000
            ? hljs.highlightAuto(text).value
            : text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"));
    } catch (e) {  // unknown language → plain escaped text
      html = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    code.innerHTML = html.split("\n")
      .map((ln, i) => '<span class="ln">' + (i + 1) + "</span>" + (ln || "&nbsp;"))
      .join("\n");
  }

  // ── Preview zoom ─────────────────────────────────────────────────
  // Font-size scaling on the preview body: em-based children (markdown,
  // code) track it automatically; images/iframes scale via a transform
  // wrapper so they never overflow their box.
  const ZOOM_MIN = 0.5, ZOOM_MAX = 3;
  const zoomByFile = {};  // file key → zoom level
  function previewZoomKey() {
    return state.previewShown ? state.previewShown.key : null;
  }
  function applyPreviewZoom(z) {
    z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 100) / 100));
    const k = previewZoomKey();
    if (k) zoomByFile[k] = z;
    el.previewBody.style.fontSize = (z * 100) + "%";
    el.previewZoomLevel.textContent = Math.round(z * 100) + "%";
    el.previewBody.querySelectorAll(".img-view, .web-frame-box").forEach((n) => {
      const w = n.parentElement;
      if (w.classList.contains("preview-body")) {
        if (z === 1) w.removeAttribute("style");
        else w.style.cssText =
          "transform: scale(" + z + "); transform-origin: top left; width: " +
          (100 / z) + "%;";
      }
    });
  }
  function previewZoomStep(dir) {
    const k = previewZoomKey();
    const cur = k ? (zoomByFile[k] || 1) : 1;
    applyPreviewZoom(cur + dir * 0.1);
  }
  el.previewZoomIn.addEventListener("click", () => previewZoomStep(1));
  el.previewZoomOut.addEventListener("click", () => previewZoomStep(-1));
  el.previewZoomLevel.addEventListener("click", () => applyPreviewZoom(1));
  // Ctrl/Cmd + wheel = zoom (trackpad pinch sends the same event),
  // plain wheel = scroll as before
  el.previewBody.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    previewZoomStep(e.deltaY < 0 ? 1 : -1);
  }, { passive: false });

  async function openPreview(f) {
    if (!f) return;
    if (!f.kind) f.kind = guessKind(f.name || f.path);
    if (state.sessionId) panel(state.sessionId).preview = f;
    state.previewShown = { sid: state.sessionId, key: f.path || f.name };
    setRightPanelOpen(true);
    if (state.rightTab !== "preview") setUpperTab("preview");
    el.previewEmpty.hidden = true;
    el.previewName.textContent = f.name || "preview";
    el.previewBody.innerHTML = '<div class="preview-truncated">Loading…</div>';
    el.previewDownload.href = f.url ||
      ("/api/files/download?path=" + encodeURIComponent(f.path || ""));
    el.previewDownload.hidden = !f.path && !f.url;
    el.previewReveal.hidden = !f.path;
    el.previewReveal.onclick = () => revealInExplorer(f.path);
    const po = el.previewOpen;
    if (f.kind === "html" || f.kind === "ipynb") {
      po.href = f.preview_url || previewUrlFor(f.path);
      po.hidden = !po.href;
    }
    else po.hidden = true;

    const body = el.previewBody;
    try {
      if (f.kind === "markdown" || f.kind === "code" || f.kind === "text") {
        const res = await api("/api/files/raw?path=" + encodeURIComponent(f.path || ""));
        if (!res.ok) throw new Error("could not load file");
        const data = await res.json();
        body.innerHTML = "";
        if (f.kind === "markdown") {
          const wrap = document.createElement("div");
          wrap.className = "md-preview";
          renderMarkdown(wrap, data.text);
          body.appendChild(wrap);
        } else {
          const pre = document.createElement("pre");
          pre.className = "code-view";
          const code = document.createElement("code");
          code.className = "language-" + ((f.name || "").split(".").pop() || "");
          renderCodeInto(code, data.text.replace(/\n$/, ""), f.name);
          pre.appendChild(code);
          body.appendChild(pre);
        }
        if (data.truncated) {
          const note = document.createElement("div");
          note.className = "preview-truncated";
          note.textContent = "Preview truncated — download the file for the rest.";
          body.appendChild(note);
        }
      } else if (f.kind === "html" || f.kind === "ipynb") {
        body.innerHTML = "";
        const box = document.createElement("div");
        box.className = "web-frame-box";
        const frame = document.createElement("iframe");
        frame.className = "web-frame";
        if (f.kind === "ipynb") {
          // server-rendered notebook HTML — static, no scripts needed
          frame.setAttribute("sandbox", "allow-downloads");
        } else {
          frame.setAttribute("sandbox",
            "allow-scripts allow-modals allow-forms allow-popups allow-pointer-lock allow-downloads");
        }
        frame.setAttribute("referrerpolicy", "no-referrer");
        frame.title = f.name;
        frame.src = f.kind === "ipynb"
          ? (previewUrlFor(f.path) || (f.preview_url || ""))
          : (f.preview_url ||
             ("/api/files/blob?path=" + encodeURIComponent(f.path || "")));
        box.appendChild(frame);
        body.appendChild(box);
        if (f.kind === "ipynb") {
          const note = document.createElement("div");
          note.className = "preview-truncated";
          note.textContent = "Static notebook render — cells and outputs as saved, not executed.";
          body.appendChild(note);
        } else {
          const note = document.createElement("div");
          note.className = "preview-truncated";
          note.textContent = "Sandboxed preview — this page cannot access your data or cookies.";
          body.appendChild(note);
        }
      } else if (f.kind === "image") {
        body.innerHTML = "";
        const img = document.createElement("img");
        img.className = "img-view";
        img.src = f.url ? inlineUrl(f.url)
          : "/api/files/blob?path=" + encodeURIComponent(f.path || "");
        img.alt = f.name;
        body.appendChild(img);
      } else {
        body.innerHTML = "";
        const note = document.createElement("div");
        note.className = "preview-truncated";
        note.textContent = "No inline preview for this type — use Download.";
        body.appendChild(note);
      }
    } catch (e) {
      body.innerHTML = "";
      const err = document.createElement("div");
      err.className = "preview-truncated";
      err.textContent = "Could not preview: " + e.message;
      body.appendChild(err);
    }
    applyPreviewZoom(zoomByFile[f.path || f.name] || 1);
  }

  function setRightPanelOpen(open) {
    if (open) syncRightWidthToSidebar();  // match the left panel's width
    el.rightPanel.hidden = !open;
    el.rightResize.hidden = !open;
    el.termResize.hidden = !open;
    updatePanelOverlay();
  }

  // The right panel opens at the sidebar's current width — on phone the
  // two panels should feel like the same size, not 280 vs 420.
  function syncRightWidthToSidebar() {
    const sb = el.sidebar.getBoundingClientRect().width || state.sidebarW;
    state.rightW = clampRightW(sb);
    applyWidths();
    localStorage.setItem("muji.rightW", String(Math.round(sb)));
  }

  // Mobile: tapping OUTSIDE an open panel (the dimmed area) closes it.
  // The overlay only renders on narrow screens (CSS), so desktop is
  // untouched — panels there are always-on columns, not overlays.
  function updatePanelOverlay() {
    const narrow = window.innerWidth <= 900;
    el.panelOverlay.hidden = !(narrow &&
      (!el.rightPanel.hidden || !el.sidebar.classList.contains("collapsed")));
  }
  el.panelOverlay.addEventListener("click", () => {
    if (!el.rightPanel.hidden) setRightPanelOpen(false);
    else {
      el.sidebar.classList.add("collapsed");
      el.sidebarOpen.hidden = false;
    }
  });

  // ── Right panel: per-chat state ─────────────────────────────────
  // The Terminal tab is DB-backed: GET /api/sessions/{sid}/tool_log is the
  // source of truth; live tool_end events carry event_id for dedupe.
  // Chats whose tool_log has already been fetched (lazy sync, 2026-09-26):
  // a re-visit must not re-pull the 260–810 KB payload — the panel's
  // p.term + live events are current enough.
  const termLoaded = new Set();
  function panel(sid) {
    if (!state.panels[sid])
      state.panels[sid] = { term: [], termIds: new Set(),
                            preview: null,
                            think: [], treePath: null, showHidden: false };
    return state.panels[sid];
  }

  // ── Right panel: Thinking tab (per-chat live bursts) ───────────
  // Consecutive thinking events form one burst; any other event closes
  // it. History rehydrates these from m.thinking in loadHistory().
  function panelThinking(sid, text) {
    if (!sid || !text) return;
    const p = panel(sid);
    const last = p.think[p.think.length - 1];
    if (!last || !last.live) p.think.push({ text, live: true, start: Date.now() });
    else last.text += text;
    if (p.think.length > 100) p.think.splice(0, p.think.length - 100);
    scheduleThinkRender();
  }

  function closeThinkBurst(sid) {
    const p = state.panels[sid];
    if (!p) return;
    const last = p.think[p.think.length - 1];
    if (last && last.live) {
      last.live = false; last.start = 0;
      // re-render NOW — without this the last-drawn "live" block keeps
      // pulsing after the burst settled (no later thinking event to
      // trigger a render, so the stale DOM sat there until a tab switch)
      scheduleThinkRender();
    }
  }

  function scheduleThinkRender() {
    if (state.rightTab !== "thinking") return;
    if (state.thinkRenderTimer) return;
    state.thinkRenderTimer = requestAnimationFrame(() => {
      state.thinkRenderTimer = null;
      renderThinkingPanel();
    });
  }

  function thinkBlock(t) {
    const det = document.createElement("details");
    det.className = "think-block" + (t.live ? " live" : "");
    const sum = document.createElement("summary");
    sum.className = "think-head";
    const lab = document.createElement("span");
    lab.appendChild(icon("brain"));  // brain = thinking; pulses while live
    lab.appendChild(document.createTextNode(" " + (t.live ? "Thinking…" : "Thought")));
    sum.appendChild(lab);
    const body = document.createElement("div");
    body.className = "think-body";
    body.textContent = t.text;
    det.open = !!t.live;  // streaming block open, settled ones collapsed
    det.append(sum, body);
    return det;
  }

  function renderThinkingPanel() {
    const box = el.thinkList;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const items = (state.sessionId && state.panels[state.sessionId] || {}).think || [];
    box.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "term-empty";
      empty.textContent = "No thinking yet — the model's chain-of-thought " +
        "shows up here as it happens.";
      box.appendChild(empty);
      return;
    }
    for (const t of items) box.appendChild(thinkBlock(t));
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function panelToolEnd(sid, d) {
    if (!sid) return;
    const p = panel(sid);
    const id = d.event_id != null ? String(d.event_id) : null;
    if (id && p.termIds.has(id)) return;
    p.term.push({ tool: d.tool, ok: d.ok, ms: d.ms, args: d.args || "",
                  output: d.output || "", id: id });
    if (id) p.termIds.add(id);
    if (p.term.length > 400) {
      for (const t of p.term.splice(0, p.term.length - 400))
        if (t.id) p.termIds.delete(t.id);
    }
    if (sid === state.sessionId) {
      if (state.lowerTab === "terminal") renderTerminal();
      // an edit just finished on the file the Editor shows → refresh it
      if (d.tool === "write_file" || d.tool === "edit_file") {
        const p = d.path || editorPathFromArgs(d.args);
        if (p && p === state.editorPath) openEditor(p, { silent: true });
      }
    }
  }

  async function syncTerminalFromDb(sid) {
    if (!sid) return;
    // LAZY (chat-switch freeze, 2026-09-26): tool_log is 260–810 KB per chat
    // (400 tool rows with full args + output) and was fetched on EVERY chat
    // switch even when the Terminal tab was closed and the pane empty. Now
    // the fetch only happens when the Terminal tab is actually visible;
    // opening the tab later triggers the sync (setLowerTab). Live
    // tool_end events keep accumulating into p.term meanwhile, so a tab
    // opened mid-run still fills from the DB (this sync) without losing
    // anything.
    if (state.lowerTab !== "terminal" || termLoaded.has(sid)) return;
    try {
      const res = await api("/api/sessions/" + encodeURIComponent(sid) + "/tool_log");
      if (!res.ok || sid !== state.sessionId) return;
      const data = await res.json();
      const p = panel(sid);
      const rows = (data.events || []).map((e) => ({
        tool: e.data.tool, ok: e.data.ok, ms: e.data.ms,
        args: e.data.args || "", output: e.data.output || "",
        id: e.id != null ? String(e.id) : null,
      }));
      if (!p.term.length) {
        // first fill: the DB is the whole story
        p.term = rows;
      } else {
        // live tool_end events landed before the fetch resolved — the DB
        // already contains them (tool_end rows persist at event time), so
        // the DB rows win and the duplicates are dropped by id
        const ids = new Set(rows.map((t) => t.id).filter(Boolean));
        p.term = rows.concat(p.term.filter((t) => t.id && !ids.has(t.id)));
      }
      p.termIds = new Set(p.term.map((t) => t.id).filter(Boolean));
      termLoaded.add(sid);
      if (state.lowerTab === "terminal") renderTerminal();
    } catch (e) { /* live events keep accumulating */ }
  }

  function termBlock(t) {
    const div = document.createElement("div");
    div.className = "term-block";
    let cmd = "";
    try {
      const a = JSON.parse(t.args || "{}");
      cmd = a.command || a.path || a.pattern || a.url || a.query || "";
    } catch (e) { cmd = ""; }
    if (!cmd && t.args) cmd = t.args.slice(0, 120);
    // flat PS-style row (mockup): accent prompt (PS> for shell commands,
    // the tool name for agent tool calls) + command + status, no card chrome
    const isCmd = t.tool === "you" || (t.args || "").includes('"command"');
    const row = document.createElement("div");
    row.className = "term-line term-cmd-line";
    const ps = document.createElement("span");
    ps.className = "ps";
    ps.textContent = isCmd ? "PS>" : (t.tool || "tool");
    row.appendChild(ps);
    if (cmd) {
      const c = document.createElement("span");
      c.className = "tc";
      c.textContent = (isCmd ? " " : "  ") + cmd;
      row.appendChild(c);
    }
    const st = document.createElement("span");
    st.className = "term-status " + (t.ok === false ? "err" : "ok");
    st.textContent = t.ok === null ? "…" : (t.ok ? "✓" : "✗");
    row.appendChild(st);
    if (t.ms != null) {
      const ms = document.createElement("span");
      ms.className = "term-ms";
      ms.textContent = t.ms + " ms";
      row.appendChild(ms);
    }
    div.appendChild(row);
    const p = state.sessionId ? state.panels[state.sessionId] : null;
    const outText = (t.output || "").trim();
    if (!outText) return div;
    const out = document.createElement("div");
    out.className = "term-out";
    out.textContent = outText;
    if (outText.split("\n").length <= 5) {
      // short enough to show fully — no chrome (Cline's magic threshold)
      div.appendChild(out);
      return div;
    }
    // long output: collapsed to 75px by default (fade signals "more");
    // the notch expands to a 200px scrollable box. Expansion survives
    // re-renders via p.termExpanded (renderTerminal rebuilds the DOM).
    const expanded0 = !!(p && t.id && p.termExpanded && p.termExpanded.has(t.id));
    out.classList.add("clamped");
    if (expanded0) out.classList.add("expanded");
    div.appendChild(out);
    const notch = document.createElement("button");
    notch.className = "term-expand";
    notch.textContent = expanded0 ? "▴ collapse" : "▾ expand";
    notch.addEventListener("click", () => {
      const open = out.classList.toggle("expanded");
      notch.textContent = open ? "▴ collapse" : "▾ expand";
      if (p && t.id) {
        if (!p.termExpanded) p.termExpanded = new Set();
        if (open) p.termExpanded.add(t.id); else p.termExpanded.delete(t.id);
      }
    });
    div.appendChild(notch);
    return div;
  }

  function renderTerminal() {
    const term = el.term;
    const nearBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 80;
    term.innerHTML = "";
    const lines = (state.sessionId && state.panels[state.sessionId] || {}).term || [];
    if (!lines.length) {
      const empty = document.createElement("div");
      empty.className = "term-empty";
      empty.textContent = "No activity yet for this chat — the agent's commands and file edits, plus your own PowerShell commands, show up here as they happen.";
      term.appendChild(empty);
      return;
    }
    for (const t of lines) term.appendChild(termBlock(t));
    if (nearBottom) term.scrollTop = term.scrollHeight;
  }

  // ── Editor pane: read-only, syntax-colored view of the file being
  //    edited. Auto-opens on write_file / edit_file tool starts; the
  //    content refreshes when that tool finishes.
  let editorSeq = 0;
  function editorPathFromArgs(args) {
    if (!args) return null;
    try { return JSON.parse(args).path || null; }
    catch (e) {
      const m = /"path"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(args);
      return m ? m[1] : null;
    }
  }
  async function openEditor(path, opts = {}) {
    if (!path) return;
    const seq = ++editorSeq;
    state.editorPath = path;
    el.editorEmpty.hidden = true;
    el.editorHead.hidden = false;
    el.editorName.textContent = path;
    el.editorBody.hidden = false;
    el.editorCode.className = "language-" + ((path.split(".").pop() || ""));
    el.editorCode.innerHTML = "";
    if (!opts.silent) setLowerTab("editor", { auto: true, btn: el.rpTabEditor });
    try {
      const res = await api("/api/files/raw?path=" + encodeURIComponent(path));
      if (seq !== editorSeq) return;  // a newer file already took over
      if (!res.ok) throw new Error("could not load file");
      const data = await res.json();
      if (seq !== editorSeq) return;
      renderCodeInto(el.editorCode, data.text.replace(/\n$/, ""), path);
      el.editorName.textContent = path + (data.truncated ? "  (truncated)" : "");
      el.editorBody.scrollTop = 0;
    } catch (e) {
      if (seq !== editorSeq) return;
      el.editorCode.textContent = "could not load " + path;
    }
  }

  // Cline-style auto-activation: the lower pane follows the action —
  // any tool runs → Terminal, a file is edited → Editor (thinking →
  // Thinking is handled on the thinking event).
  function autoTabForTool(tool, args) {
    if (tool === "write_file" || tool === "edit_file") {
      setLowerTab("editor", { auto: true, btn: el.rpTabEditor });
      openEditor(editorPathFromArgs(args));
    } else {
      setLowerTab("terminal", { auto: true, btn: el.rpTabTerminal });
    }
  }

  // ── User terminal: real PowerShell, runs in the chat's working
  //    folder, no approval gate (the user's own hands).
  let termSeq = 0;
  async function runUserCommand() {
    const cmd = el.termInput.value.trim();
    if (!cmd || state.termBusy) return;
    const sid = state.sessionId;
    const p = sid ? panel(sid) : null;
    state.termBusy = true;
    const id = "user-" + (++termSeq) + "-" + Date.now();
    if (p) p.term.push({ tool: "you", ok: null, ms: null,
                          args: JSON.stringify({ command: cmd }),
                          output: "running…", id });
    if (sid === state.sessionId && state.lowerTab === "terminal") renderTerminal();
    el.termInput.value = "";
    let ok = true, out = "";
    try {
      const res = await api("/api/terminal/run", {
        method: "POST",
        body: JSON.stringify({ session_id: sid, command: cmd }),
      });
      const data = await res.json().catch(() => ({}));
      out = data.output || (res.ok ? "(no output)" : (data.detail || "command failed"));
      ok = res.ok && data.ok !== false;
    } catch (e) {
      ok = false;
      out = "error: " + e.message;
    }
    if (p) {
      const t = p.term.find((x) => x.id === id);
      if (t) { t.ok = ok; t.output = out; }
    }
    state.termBusy = false;
    if (sid === state.sessionId && state.lowerTab === "terminal") {
      renderTerminal();
      el.term.scrollTop = el.term.scrollHeight;
    }
  }

  // ── Composer ──────────────────────────────────────────────────
  // sprite names (index.html <symbol id="i-…">) — stroke tints per theme
  const FILE_ICONS = { markdown: "file-pen", html: "globe", ipynb: "file-text",
                       image: "image", code: "terminal", text: "file" };

  const KIND_EXT = {
    md: "markdown", markdown: "markdown", html: "html", htm: "html", ipynb: "ipynb",
    png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
    svg: "image", bmp: "image", ico: "image",
    py: "code", js: "code", mjs: "code", ts: "code", tsx: "code", jsx: "code",
    json: "code", css: "code", scss: "code", sh: "code", ps1: "code", bat: "code",
    cmd: "code", yaml: "code", yml: "code", toml: "code", ini: "code", cfg: "code",
    conf: "code", xml: "code", sql: "code", c: "code", h: "code", cpp: "code",
    hpp: "code", rs: "code", go: "code", java: "code", rb: "code", php: "code",
    lua: "code", env: "code", txt: "code", csv: "code", tsv: "code", log: "code",
    svelte: "code", vue: "code",
  };
  function guessKind(name) {
    const ext = (String(name || "").split(".").pop() || "").toLowerCase();
    return KIND_EXT[ext] || "text";
  }

  // Top of the right panel: the current chat's name (replaces the old
  // "Files in this chat" header position — the tab row moved down).
  function renderSessionTitle() {
    const s = state.sessionId
      ? state.sessions.find((x) => x.id === state.sessionId) : null;
    const name = (s && s.title) || "New chat";
    el.rpSessionTitle.replaceChildren(
      s && s.pinned ? icon("pin") : null,
      document.createTextNode((s && s.pinned ? " " : "") + name));
    el.rpSessionTitle.title = name;
  }

  // ── Context meter (topbar, far left) ─────────────────────────
  // Fill of this chat's context vs the model's real limit (boundary =
  // the 100% mark). `toks` is the chat's last known context size (the
  // model's prompt_tokens, persisted on sessions.ctx_tokens). Hidden for
  // a fresh chat (no data yet). The sublabel shows where compaction
  // actually fires (trigger, at turn boundaries). The compacting flag
  // dims it + swaps the % for a label while the compaction step runs
  // (server's status frame carries `compacting: true`).
  function renderCtxMeter(toks, compacting) {
    const trig = (state.config && state.config.compact_trigger) || 0;
    const bound = (state.config && state.config.compact_boundary) || trig || 0;
    if (toks) state.ctxToks = toks;  // remember for the compacting flip
    if (!bound || !toks || toks <= 0) {
      el.ctxMeter.hidden = true;
      return;
    }
    const pct = Math.min(100, Math.round((toks / bound) * 100));
    el.ctxMeter.hidden = false;
    el.ctxMeter.classList.toggle("warn", !compacting && pct >= 60 && pct < 85);
    el.ctxMeter.classList.toggle("hot", !compacting && pct >= 85);
    el.ctxMeter.classList.toggle("compacting", !!compacting);
    el.ctxFill.style.width = pct + "%";
    el.ctxPct.textContent = compacting ? "compacting…" : pct + "%";
    const fmt = (n) => (n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(n));
    el.ctxNum.textContent = fmt(toks) + " / " + fmt(bound);
    el.ctxSub.textContent = compacting ? "summarizing now"
      : (trig ? "compacts at " + fmt(trig) : "model limit");
  }
  // Feed the meter from the current session row (session switch / refresh).
  // A session row never knows about in-flight compaction, so by default
  // this CLEARS the flag — a live compacting status frame re-sets it.
  // keepCompacting (the 3 s sidebar poll) preserves an in-flight flag:
  // the poll must not flicker the meter back to normal mid-compaction.
  function ctxMeterFromSession(keepCompacting) {
    const s = state.sessionId
      ? state.sessions.find((x) => x.id === state.sessionId) : null;
    renderCtxMeter(s && s.ctx_tokens,
                   keepCompacting && el.ctxMeter.classList.contains("compacting"));
  }
  // End-of-run reset: done/stopped clear the compacting flag (a run that
  // died mid-compaction leaves it stuck otherwise).
  function ctxMeterReset() {
    if (el.ctxMeter.classList.contains("compacting")) ctxMeterFromSession();
  }

  // ── tok/s readout (topbar, right of the ctx meter) ────────────
  // The last REAL decode speed: each llm_end frame carries tok_s
  // (server-computed from the stream span). No polling — the number only
  // moves when a model call finishes, and it dims >10 s after that so a
  // tool-call gap reads "last measured," not "currently." Color
  // thresholds from the boss's last-400-row distribution (2026-10-08:
  // median 68.7, p10 44.9): green ≥55 = normal, yellow 30–55 = slower
  // than usual, red <30 = degraded.
  const TOKS_STALE_MS = 10000;
  let toksStaleTimer = null;
  // Boot seed: the readout is hidden until the first llm_end of THIS tab,
  // so a reload before any model call shows no indicator at all. Seed it
  // from the latest llm_end across all sessions. Best-effort and
  // non-blocking — boot must not wait on the full llm_end scan.
  function seedToks() {
    fetch("/api/latency", { cache: "no-store" }).then((r) => r.json())
      .then((D) => {
        // newest day with data, its last hour with data = the latest reading
        const lastDay = (D.days || []).filter((d) =>
          Object.keys(D.n_tok[d] || {}).length).pop();
        if (!lastDay) return;
        const hours = Object.keys(D.n_tok[lastDay]).map(Number)
          .sort((a, b) => a - b);
        if (!hours.length) return;
        const h = hours[hours.length - 1];
        renderToks(D.tok_s[lastDay][h]);
        // it's a historical reading, not a live one — let the stale dim
        // do its job (renderToks already armed the 10 s timer)
      }).catch(() => {});  // server busy — the first llm_end will paint it
  }
  function renderToks(tok_s) {
    if (!tok_s || tok_s <= 0) return;  // no usage chunk / no content — keep last
    el.toks.hidden = false;
    el.toksVal.textContent = Math.round(tok_s);
    el.toks.classList.toggle("g", tok_s >= 55);
    el.toks.classList.toggle("y", tok_s >= 30 && tok_s < 55);
    el.toks.classList.toggle("r", tok_s < 30);
    el.toks.classList.remove("stale");
    if (toksStaleTimer) clearTimeout(toksStaleTimer);
    toksStaleTimer = setTimeout(() => el.toks.classList.add("stale"),
                                 TOKS_STALE_MS);
  }

  function resetTree() {
    const ws = state.workspaces.find((w) => w.id === state.wsSel);
    state.treePath = null;
    state.treeSid = null;
    state.treeRoot = ws ? ws.path : (state.config ? state.config.root_dir : null);
  }

  // Per-chat file-explorer position: panel(sid).treePath is the source of
  // truth (null = this chat's home). A chat with a chosen working folder
  // (Files tab → 📂) opens there and it doubles as the "up" boundary;
  // other chats keep the old behaviour (root list, workspace "up" boundary).
  function syncTreeToChat() {
    const sid = state.sessionId;
    const s = sid ? state.sessions.find((x) => x.id === sid) : null;
    const ws = state.workspaces.find(
      (w) => w.id === (s ? s.workspace_id : state.wsSel));
    const home = s && s.cwd
      ? s.cwd
      : (ws ? ws.path : (state.config ? state.config.root_dir : null));
    state.treeRoot = home;
    const want = sid ? (panel(sid).treePath || (s && s.cwd ? s.cwd : null)) : null;
    if (state.treeSid === sid && state.treePath === want) return;
    state.treeSid = sid;
    state.treePath = want;
    loadTree();
  }

  // Per-chat hidden-dirs toggle (Files tab): panel(sid).showHidden
  function syncHiddenToggle() {
    const on = !!(state.sessionId && panel(state.sessionId).showHidden);
    el.rpTreeHidden.classList.toggle("active", on);
    el.rpTreeHidden.title = on
      ? "Hide hidden files and folders"
      : "Show hidden files and folders (.git, .venv, …)";
  }

  // ── Tree history: every folder drill-in is a real browser-history entry,
  // so the (mouse) back/forward buttons walk the folder stack natively — one
  // press = one folder — and muji is only left once the stack is down to the
  // plain page-load entry. (preventDefault on the mouse back button is not
  // honoured by browsers, which is why intercepting mousedown used to let the
  // page navigate away in the same stroke.)
  let treeUpPending = undefined;  // parent a ↑ click expects back() to reach
  function enterFolder(path) {
    state.treePath = path;
    history.pushState({ treePath: path, sid: state.sessionId || null }, "");
    loadTree();
  }
  window.addEventListener("popstate", (e) => {
    const st = e.state;  // null = the plain muji entry → this chat's root
    // The Preview tab is a history step of its own: landing on a preview
    // entry shows it; leaving it (folder entry / plain entry) returns to the
    // Files tab — that's what makes (mouse) back walk Preview → Files → folders
    const suppress = previewBackSuppress; previewBackSuppress = false;
    const toPreview = isPreviewEntry(st);
    if (suppress) {
      // setUpperTab("thinking") consumed the preview entry — keep the tab the
      // app already switched to (thinking) instead of snapping back to Files
    } else if (toPreview && state.rightTab !== "preview") {
      previewNav = true; setUpperTab("preview"); previewNav = false;
    } else if (!toPreview && state.rightTab === "preview") {
      previewNav = true; setUpperTab("files"); previewNav = false;
    }
    const actual = st && st.treePath != null ? st.treePath : null;
    if (treeUpPending !== undefined) {
      const want = treeUpPending; treeUpPending = undefined;
      if (actual !== want) {
        // entry below isn't the parent (chats were switched): fix it up so
        // back/forward stay consistent with the visible folder
        try { history.replaceState({ treePath: want, sid: state.sessionId || null }, ""); } catch (err) { /* ignore */ }
      }
      state.treePath = want;
    } else if (!st || st.sid === (state.sessionId || null)) {
      state.treePath = actual;
    } else {
      return;  // entry belongs to another chat's tree — consume it quietly
    }
    loadTree();
  });

  async function loadTree() {
    const sid = state.sessionId;
    let q = "";
    if (state.treePath) q += "?path=" + encodeURIComponent(state.treePath);
    if (sid && panel(sid).showHidden) q += (q ? "&" : "?") + "hidden=1";
    if (state.picking) q += (q ? "&" : "?") + "dirs=1";
    let data;
    try { data = await (await api("/api/tree" + q)).json(); } catch (e) { return; }
    if (!data.entries) return;
    if (sid && sid !== state.sessionId) return;  // switched chats mid-fetch
    state.treePath = data.path;
    state.treeSid = sid;
    if (sid) panel(sid).treePath = data.path;
    el.rpTreeLabel.textContent = data.path;
    el.rpTreeUp.hidden = !state.treeRoot || data.path === state.treeRoot;
    // "move out" dropzone: visible only when this folder has a parent
    // (i.e. the ↑ button is available) — drag a row onto it to move the
    // item up one level.
    el.rpTreeDrop.hidden = state.picking || el.rpTreeUp.hidden;
    if (state.picking)
      el.treePickLabel.textContent = "Use this folder: " + data.path;
    el.rpTree.innerHTML = "";
    if (!data.entries.length) {
      const note = document.createElement("div");
      note.className = "term-empty";
      note.textContent = "(empty folder)";
      el.rpTree.appendChild(note);
      return;
    }
    for (const e of data.entries) {
      const row = document.createElement("div");
      row.className = "tree-row";
      const nm = document.createElement("span");
      nm.className = "tr-name";
      nm.appendChild(icon(e.is_dir ? "folder" : (FILE_ICONS[e.kind] || "file")));
      nm.appendChild(document.createTextNode(" " + e.name));
      nm.title = e.path;
      row.appendChild(nm);
      if (!e.is_dir && e.size != null) {
        const sz = document.createElement("span");
        sz.className = "tr-size";
        sz.textContent = fmtSize(e.size);
        row.appendChild(sz);
      }
      // per-row actions (hover): ⧉ copy path, ✏ rename, 🗑 delete,
      // files also ⬇ download
      const acts = document.createElement("span");
      acts.className = "tr-acts";
      acts.addEventListener("click", (ev) => ev.stopPropagation());
      const mkBtn = (txt, title, fn) => {
        const b = document.createElement("button");
        b.className = "mini-btn";
        if (typeof txt === "string") b.textContent = txt;
        else b.appendChild(txt);
        b.title = title;
        b.addEventListener("click", fn);
        acts.appendChild(b);
      };
      mkBtn("⧉", "Copy path", () => copyText(e.path, null));
      mkBtn("↗", "Reveal in File Explorer", () => revealInExplorer(e.path));
      mkBtn(icon("pen"), "Rename (or drag this row onto itself)", () => askRename(e));
      mkBtn(icon("trash"), "Delete", () => askDelete(e));
      if (!e.is_dir) {
        const dl = document.createElement("a");
        dl.className = "mini-btn";
        dl.textContent = "⬇";
        dl.title = "Download";
        dl.href = "/api/files/download?path=" + encodeURIComponent(e.path);
        dl.addEventListener("click", (ev) => ev.stopPropagation());
        acts.appendChild(dl);
      }
      row.appendChild(acts);
      const enter = () => {
        if (e.is_dir) enterFolder(e.path);
        else openPreview({ name: e.name, path: e.path, kind: e.kind });
      };
      // single click selects, double click enters the folder / opens the file.
      // Touch has no dblclick → tap selects, tap the selected row again to enter
      // (the ✏/🗑 buttons are visible while selected, so those stay one tap away).
      row.addEventListener("click", () => {
        const wasSelected = row.classList.contains("selected");
        el.rpTree.querySelectorAll(".tree-row.selected").forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        if (COARSE && wasSelected) enter();
      });
      row.addEventListener("dblclick", enter);
      // drag to move: row → folder row = move into; row → itself = rename.
      // Internal drags only — OS file drops are handled by the pane-level
      // drop listener (upload), which this never interferes with.
      if (!COARSE) {
      row.draggable = true;
      row.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData("text/muji-path", e.path);
        ev.dataTransfer.effectAllowed = "move";
        row.classList.add("dragging");
      });
      row.addEventListener("dragend", () => {
        row.classList.remove("dragging");
        el.rpTree.querySelectorAll(".tree-row.drop-target")
          .forEach((r) => r.classList.remove("drop-target"));
      });
      row.addEventListener("dragover", (ev) => {
        // getData() is empty during dragover (HTML5 only exposes it on drop), so
        // detect the drag by type — same as the move-out dropzone below. The drop
        // handler reads getData where it works.
        if (state.picking || !ev.dataTransfer.types.includes("text/muji-path")) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        if (e.is_dir) row.classList.add("drop-target");
      });
      row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
      row.addEventListener("drop", (ev) => {
        const dragging = ev.dataTransfer.getData("text/muji-path");
        row.classList.remove("drop-target");
        if (!dragging || state.picking) return;
        ev.preventDefault();
        ev.stopPropagation();
        if (dragging === e.path) askRename({ name: e.name, path: e.path });
        else if (e.is_dir) doMove(dragging, e.path);
      });
      }  // !COARSE
      el.rpTree.appendChild(row);
    }
  }

  // ── Choose a working folder for the chat (Files tab → 📂) ──────
  // Browse the dirs-only listing (double-click down, ↑ up), then "Use this
  // folder" pins it as the chat's cwd: the agent works there, and the Files
  // tab opens there from then on.
  function pickUI() {
    const s = state.sessionId
      ? state.sessions.find((x) => x.id === state.sessionId) : null;
    el.rpTreeChoose.hidden = !state.sessionId || !!(s && s.cwd);
    // paste-a-path row: available whenever a chat is open (and not mid-browse)
    el.treePaste.hidden = !state.sessionId || state.picking;
    el.treePick.hidden = !state.picking;
  }

  function startPick() {
    if (!state.sessionId || state.picking) return;
    state.picking = true;
    state.treeSid = state.sessionId;
    state.treeRoot = state.config ? state.config.root_dir : null;  // browse the whole root
    state.treePath = null;
    pickUI();
    loadTree();
  }

  function cancelPick() {
    if (!state.picking) return;
    state.picking = false;
    pickUI();
    syncTreeToChat();
  }

  async function usePick() {
    const sid = state.sessionId;
    if (!sid || !state.picking) return;
    const target = state.treePath || (state.config && state.config.root_dir);
    if (!target) return;
    el.treePickUse.disabled = true;
    const res = await api("/api/sessions/" + sid + "/cwd", {
      method: "POST", body: JSON.stringify({ path: target }),
    });
    el.treePickUse.disabled = false;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not set the working folder");
      return;
    }
    const data = await res.json().catch(() => ({}));
    const s = state.sessions.find((x) => x.id === sid);
    if (s) s.cwd = data.cwd || target;
    panel(sid).treePath = null;  // the Files tab opens at the chosen folder
    state.picking = false;
    pickUI();
    refreshSessions();
    syncTreeToChat();
  }

  // Paste a folder path (anywhere on disk, incl. outside the agent root) and
  // open it as this chat's working folder — same effect as the 📂 browse pick,
  // but the user types the path instead of double-clicking through the tree.
  async function openPastedPath() {
    const sid = state.sessionId;
    if (!sid) return;
    const raw = (el.rpTreePath.value || "").trim();
    if (!raw) return;
    el.rpTreePathGo.disabled = true;
    const res = await api("/api/sessions/" + sid + "/cwd", {
      method: "POST", body: JSON.stringify({ path: raw }),
    });
    el.rpTreePathGo.disabled = false;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not open that folder");
      return;
    }
    const data = await res.json().catch(() => ({}));
    const s = state.sessions.find((x) => x.id === sid);
    if (s) s.cwd = data.cwd || raw;
    panel(sid).treePath = null;  // the Files tab opens at the chosen folder
    el.rpTreePath.value = "";
    pickUI();
    refreshSessions();
    syncTreeToChat();
  }

  // ── Files tab: copy paths, create entries, drag & drop ──────────
  // Reveal a path in the OS file manager. The server (the user's own
  // machine) opens File Explorer — files get selected in their parent,
  // folders open themselves. Browser-only, no popup: the server spawns
  // explorer.exe, so no window.open to be blocked.
  async function revealInExplorer(path) {
    if (!path) return;
    try {
      const res = await api("/api/files/reveal?path=" + encodeURIComponent(path));
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert("Could not reveal: " + (data.detail || "not found"));
      }
    } catch (e) { /* server offline — nothing to reveal */ }
  }

  function copyText(text, btn) {
    const done = () => {
      if (!btn) return;
      const old = btn.textContent;
      btn.textContent = "✓";
      setTimeout(() => { btn.textContent = old; }, 900);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* ignore */ }
      ta.remove();
      done();
    };
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    else fallback();
  }

  // ── Rename / move / delete (Files tab) ──────────────────────────
  // prompt() is the existing UI convention here (createEntry) and is
  // modal — no Enter-to-confirm races, unlike a custom inline input.
  function confirmFileOp(promptText, fallbackText) {
    const a = prompt(promptText);
    if (a === null) return false;
    return a === "" ? !!confirm(fallbackText) : true;
  }

  async function askRename(e) {
    const name = prompt("Rename to:", e.name);
    if (name === null || !name.trim() || name.trim() === e.name) return;
    const res = await api("/api/files/rename", {
      method: "POST",
      body: JSON.stringify({ path: e.path, name: name.trim() }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not rename");
      return;
    }
    loadTree();
  }

  async function askDelete(e) {
    const ok = e.is_dir
      ? confirm(`Delete folder "${e.name}" and everything in it?`)
      : confirm(`Delete "${e.name}"?`);
    if (!ok) return;
    const res = await api("/api/files/delete", {
      method: "POST", body: JSON.stringify({ path: e.path }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not delete");
      return;
    }
    loadTree();
  }

  // Move `src` into the folder `destDir`. On a name clash (409) ask once
  // whether to rename into the destination instead.
  async function doMove(src, destDir) {
    const res = await api("/api/files/move", {
      method: "POST",
      body: JSON.stringify({ path: src, dest: destDir }),
    });
    if (res.ok) { loadTree(); return; }
    const err = await res.json().catch(() => ({}));
    if (res.status === 409) {
      const alt = prompt(
        `"${src}" already exists in the destination.\n` +
        "Enter a new name to move it as that, or cancel:", src);
      if (!alt || !alt.trim() || alt.trim() === src) return;
      const r2 = await api("/api/files/move", {
        method: "POST",
        body: JSON.stringify({ path: src, dest: destDir, name: alt.trim() }),
      });
      if (!r2.ok) {
        const e2 = await r2.json().catch(() => ({}));
        alert(e2.detail || "Could not move");
        return;
      }
    } else {
      alert(err.detail || "Could not move");
      return;
    }
    loadTree();
  }

  // 📁＋ / 📄＋ — create a folder or empty file in the directory shown
  async function createEntry(kind) {
    if (state.picking) return;
    const dir = state.treePath || (state.config && state.config.root_dir);
    if (!dir) return;
    const name = prompt(kind === "dir" ? "New folder name:" : "New file name:");
    if (!name) return;
    const res = await api("/api/files/create", {
      method: "POST",
      body: JSON.stringify({ path: dir, name, kind }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Could not create " + (kind === "dir" ? "folder" : "file"));
      return;
    }
    loadTree();
  }

  // Drag & drop: drop files anywhere in the Files tab → saved into the
  // directory currently shown (existing names are skipped, never overwritten)
  el.paneFiles.addEventListener("dragover", (ev) => {
    if (state.picking) return;
    // internal row-to-row moves (rename/move) — the row highlights itself
    if (ev.dataTransfer && ev.dataTransfer.types.includes("text/muji-path")) return;
    ev.preventDefault();
    el.paneFiles.classList.add("drop");
  });
  el.paneFiles.addEventListener("dragleave", (ev) => {
    if (!ev.relatedTarget || !el.paneFiles.contains(ev.relatedTarget))
      el.paneFiles.classList.remove("drop");
  });
  el.paneFiles.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    el.paneFiles.classList.remove("drop");
    if (state.picking) return;
    const files = ev.dataTransfer && ev.dataTransfer.files
      ? [...ev.dataTransfer.files] : [];
    const dir = state.treePath || (state.config && state.config.root_dir);
    if (!files.length || !dir) return;
    let saved = 0, skipped = 0;
    for (const f of files) {
      const form = new FormData();
      form.append("file", f);
      const res = await fetch("/api/fs/upload?path=" + encodeURIComponent(dir), {
        method: "POST", body: form,
      });
      if (res.ok) {
        const d = await res.json().catch(() => ({}));
        if (d.skipped) skipped++; else saved++;
      } else skipped++;
    }
    loadTree();
    if (skipped) alert(saved + " saved, " + skipped + " skipped (already exist or failed)");
  });

  const RP_UPPER_TABS = ["thinking", "files", "preview"];
  const RP_LOWER_TABS = ["terminal", "editor"];

  // ── Preview history: entering the Preview tab is a real browser-history
  // entry, so the (mouse) back/forward buttons walk
  //   … folder → folder → Files tab → Preview
  // one press = one step — the same gesture that already walks the folder
  // stack in the Files tab (enterFolder below). The ← button and the
  // Files-tab click consume that entry via history.back(), so in-app back
  // and mouse-back can never disagree.
  let previewNav = false;  // history is driving the tab switch — no push/back
  let previewBackSuppress = false;  // we left preview for a non-history tab (thinking) — popstate must not re-set the tab
  function docState() { try { return history.state; } catch (e) { return null; } }
  function isPreviewEntry(st) {
    return !!(st && st.preview && st.sid === (state.sessionId || null));
  }
  // A Preview entry belongs to the chat that opened it — leaving that chat
  // (or starting a fresh one) consumes it so back never re-enters a dead
  // chat's preview
  function normalizeHistoryFor(sid) {
    const st = docState();
    if (st && st.preview)
      try { history.replaceState({ treePath: null, sid: sid || null }, ""); }
      catch (e) { /* ignore */ }
  }

  // Upper pane: Thinking | Files | Preview (manual + browser-history
  // integration for the preview step). Thinking auto-activates when the model
  // streams reasoning (opts.auto) and is a live overlay tab — it never pushes
  // or consumes a history entry of its own.
  function setUpperTab(tab, opts = {}) {
    if (!RP_UPPER_TABS.includes(tab)) tab = "files";
    const prev = state.rightTab;
    state.rightTab = tab;
    localStorage.setItem("muji.rightTab", tab);
    el.rpTabsUpper.querySelectorAll(".rp-tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.ru === tab));
    el.paneThinking.hidden = tab !== "thinking";
    el.paneFiles.hidden = tab !== "files";
    el.panePreview.hidden = tab !== "preview";
    if (opts.auto && opts.btn) flashTab(opts.btn);
    refreshPanel();
    if (previewNav) return;  // popstate already moved us — the entry matches
    if (tab === "preview" && prev !== "preview" && state.sessionId)
      history.pushState({ treePath: state.treePath, sid: state.sessionId,
                          preview: true }, "");
    else if (tab !== "preview" && prev === "preview" && isPreviewEntry(docState())) {
      // Leaving the preview step: Files re-syncs itself in the popstate
      // handler, but a non-history tab (thinking) must keep the tab it just
      // switched to — so suppress that re-set for the thinking case.
      previewBackSuppress = tab !== "files";
      history.back();  // consume the preview entry; popstate keeps sync
    }
  }

  // Lower pane: Terminal | Editor. Cline-style auto-activation: a tool runs →
  // Terminal, a file is edited → Editor. Manual clicks work the same way; the
  // next event just takes over the tab again (and flashes it, so the switch is
  // visible). (Thinking lives in the upper pane now — see setUpperTab.)
  function setLowerTab(tab, opts = {}) {
    if (!RP_LOWER_TABS.includes(tab)) tab = "terminal";
    state.lowerTab = tab;
    localStorage.setItem("muji.lowerTab", tab);
    el.rpTabsLower.querySelectorAll(".rp-tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.rl === tab));
    el.paneTerminal.hidden = tab !== "terminal";
    el.paneEditor.hidden = tab !== "editor";
    if (opts.auto && opts.btn) flashTab(opts.btn);
    // lazy tool_log: opening the Terminal tab is the moment the fetch is
    // worth paying (the sync is a no-op once termLoaded has the chat)
    if (tab === "terminal" && state.sessionId) syncTerminalFromDb(state.sessionId);
    refreshPanel();
  }

  function flashTab(btn) {
    btn.classList.remove("auto-flash");
    void btn.offsetWidth;  // restart the CSS animation
    btn.classList.add("auto-flash");
  }

  function refreshPanel() {
    if (state.lowerTab === "terminal") renderTerminal();
    if (state.rightTab === "files") {
      syncTreeToChat();
      syncHiddenToggle();
    } else if (state.rightTab === "preview") {
      const p = state.sessionId ? state.panels[state.sessionId] : null;
      const f = p ? p.preview : null;
      const shown = state.previewShown;
      if (f && (!shown || shown.sid !== state.sessionId || shown.key !== (f.path || f.name)))
        openPreview(f);
      else if (!f) { el.previewBody.innerHTML = ""; el.previewEmpty.hidden = false; }
    } else if (state.rightTab === "thinking") {
      renderThinkingPanel();
    }
  }

  // src/tools.preview_token mirrored in JS: base64url(path), no padding
  function previewUrlFor(path) {
    if (!path) return "";
    try {
      const b = btoa(unescape(encodeURIComponent(path)));
      return "/preview/" + b.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    } catch (e) { return ""; }
  }

  function autosize() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 220) + "px";
  }

  // ── Boot ──────────────────────────────────────────────────────
  // Multi-instance: poll all sessions for background status (dots + banner)
  // ── Draggable resizers (sidebar | chat | right panel) ───────────
  function applyWidths() {
    const app = document.getElementById("app");
    app.style.setProperty("--sidebar-w", state.sidebarW + "px");
    app.style.setProperty("--right-w", state.rightW + "px");
    app.style.setProperty("--term-h", state.termH + "%");
  }

  function clampRightW(w) {
    const max = Math.max(Math.floor(window.innerWidth * 0.6), 320);
    return Math.min(Math.max(w, 320), max);
  }

  function makeResizer(handle, opts) {
    let dragging = false, start0 = 0, startV = 0, span = 1;
    const pos = (ev) => (opts.vertical ? ev.clientY : ev.clientX);
    const cls = () => (opts.vertical ? "resizing-h" : "resizing");
    handle.addEventListener("pointerdown", (ev) => {
      if (ev.pointerType === "touch" && ev.isPrimary === false) return;
      if (handle.hidden) return;
      dragging = true;
      start0 = pos(ev);
      startV = opts.get();
      span = opts.span ? opts.span() : 1;
      try { handle.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      document.body.classList.add(cls());
      ev.preventDefault();
    });
    handle.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      const d = pos(ev) - start0;
      const scale = opts.vertical ? 100 / span : 1;  // % of the panel height
      opts.set(opts.clamp(startV + (opts.growRight ? d : -d) * scale));
    });
    const end = () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove(cls());
      opts.save(opts.get());
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    handle.addEventListener("dblclick", () => opts.reset());
  }

  function initResizers() {
    applyWidths();
    makeResizer(el.sidebarResize, {
      growRight: true,   // dragging right widens the sidebar
      get: () => state.sidebarW,
      clamp: (w) => Math.min(Math.max(w, 200), 560),
      set: (w) => {
        state.sidebarW = w; applyWidths();
        if (state.tzOnSidebarW) state.tzOnSidebarW();  // legacy hook (v4 drawer tracks --sidebar-w via CSS)
      },
      save: (w) => localStorage.setItem("muji.sidebarW", String(Math.round(w))),
      reset: () => {
        state.sidebarW = 280; applyWidths();
        localStorage.setItem("muji.sidebarW", "280");
      },
    });
    makeResizer(el.rightResize, {
      growRight: false,  // dragging right widens the RIGHT panel
      get: () => state.rightW,
      clamp: clampRightW,
      set: (w) => { state.rightW = w; applyWidths(); },
      save: (w) => localStorage.setItem("muji.rightW", String(Math.round(w))),
      reset: () => {
        state.rightW = 420; applyWidths();
        localStorage.setItem("muji.rightW", "420");
      },
    });
    makeResizer(el.termResize, {
      vertical: true,
      growRight: false,  // dragging the handle DOWN shrinks the terminal
      span: () => Math.max(el.rightPanel.clientHeight, 1),
      get: () => state.termH,
      clamp: (h) => Math.min(Math.max(h, 10), 80),
      set: (h) => { state.termH = h; applyWidths(); },
      save: (h) => localStorage.setItem("muji.termH", String(Math.round(h))),
      reset: () => {
        state.termH = 25; applyWidths();
        localStorage.setItem("muji.termH", "25");
      },
    });
    window.addEventListener("resize", () => {
      state.rightW = clampRightW(state.rightW);
      applyWidths();
      reparkAvatars();  // reflow re-parked the bubbles → re-park the avatars
      updatePanelOverlay();  // crossing 900px toggles the tap-outside layer
    });
  }

  // ── Today zone (option A step 4) ─────────────────────────────────────
  // Fixed collapsible block above the session list: mini-cal (day FILTER,
  // not a destination), events, tasks, quick-add. Rows act in place —
  // chk toggles, ✕ deletes, Enter creates — the zone never navigates.
  // The chat stays the command surface (agent tools write the same store);
  // the zone is the status surface. Collapse state persists like
  // sidebarW; the body glides (grid-template-rows, CSS).
  const tz = {
    collapsed: localStorage.getItem("muji.tzCollapsed") === "1",
    autoCollapsed: false,   // narrow-sidebar auto-collapse (restores on widen)
    day: null,              // selected day (local Date @ midnight) — null = Today
    month: null,            // mini-cal month (Date @ 1st) — null = current
    tasks: [], events: [],
    more: false,            // "+N more" expanded
    sig: "",
    timer: null,
  };

  function tzDayKey(d) {  // local YYYY-MM-DD for a Date
    return d.getFullYear() + "-" +
           String(d.getMonth() + 1).padStart(2, "0") + "-" +
           String(d.getDate()).padStart(2, "0");
  }
  function tzToday() { const d = new Date(); d.setHours(0, 0, 0, 0); return d; }
  function tzViewDay() { return tz.day || tzToday(); }

  function tzFetch() {
    return Promise.all([
      api("/api/tasks?include_done=1&max_results=100").then((r) => r.json()),
      // include_done=1: done events are HISTORY, not noise — they render
      // struck-through in the "Done" section (Boss: "is different if u
      // just mark it done so its in history?")
      api("/api/events?limit=50&include_done=1").then((r) => r.json()),
    ]).then(([t, e]) => {
      tz.tasks = t.tasks || [];
      tz.events = e.events || [];
      tzRender();
    }).catch(() => {});  // offline / server down → zone keeps last state
  }

  // items in scope for the current view (Today = today's events + open
  // tasks by due; a picked day = that day's events + due tasks)
  function tzScoped() {
    const vd = tzViewDay();
    const key = tzDayKey(vd);
    const isToday = !tz.day;
    const evts = tz.events.filter((e) => {
      if (!e.start_iso || e.done) return false;
      const d = new Date(e.start_iso);
      return tzDayKey(d) === key;
    });
    // done events: history, shown struck-through under a "Done" section
    const evtsDone = tz.events.filter((e) => {
      if (!e.start_iso || !e.done) return false;
      const d = new Date(e.start_iso);
      return tzDayKey(d) === key;
    });
    const open = tz.tasks.filter((t) => !t.done);
    const tasks = open
      .filter((t) => t.due_iso && tzDayKey(new Date(t.due_iso)) === key)
      .concat(isToday ? open.filter((t) => !t.due_iso) : []);
    // Today view only: open tasks due AFTER today get their own section —
    // before this they were invisible unless you hunted the mini-cal to
    // their due day (Boss: "am i not seeing it in the task list")
    const upcoming = isToday
      ? open.filter((t) => t.due_iso && tzDayKey(new Date(t.due_iso)) > key)
           .sort((a, b) => new Date(a.due_iso) - new Date(b.due_iso))
      : [];
    return { evts, evtsDone, tasks, upcoming };
  }

  function tzWhenLabel(iso) {
    const d = new Date(iso);
    const t = tzToday();
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const yest = new Date(t); yest.setDate(t.getDate() - 1);
    const tmr = new Date(t); tmr.setDate(t.getDate() + 1);
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (day.getTime() < t.getTime()) {
      // overdue: name the day so the red "when" reads as a missed slot
      return (day.getTime() === yest.getTime() ? "yesterday · " : "") + time;
    }
    if (day.getTime() === t.getTime()) return time;
    if (day.getTime() === tmr.getTime()) return "tomorrow";
    // future: name the weekday so "Sat Oct 3" reads as a real slot, not a mystery
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }
  function tzWhenCls(iso) {
    const d = new Date(iso);
    if (d.getTime() < Date.now()) return " overdue";
    if (d.getTime() - Date.now() < 36e5) return " due";  // within 1h
    return "";
  }

  function tzMiniCal() {
    const wrap = document.createElement("div");
    wrap.className = "tz-mini";
    const m = tz.month || new Date();
    const mFirst = new Date(m.getFullYear(), m.getMonth(), 1);
    // scope keys for the dots: tasks due / events starting per local day
    const tKeys = new Set(tz.tasks.filter((t) => !t.done && t.due_iso)
      .map((t) => tzDayKey(new Date(t.due_iso))));
    const eKeys = new Set(tz.events.filter((e) => e.start_iso)
      .map((e) => tzDayKey(new Date(e.start_iso))));
    const head = document.createElement("div");
    head.className = "tz-mc-head";
    const mo = document.createElement("span");
    mo.className = "tz-mc-month";
    mo.textContent = mFirst.toLocaleDateString([], { month: "long", year: "numeric" });
    const navs = document.createElement("span");
    for (const [dir, label] of [[-1, "‹"], [1, "›"]]) {
      const b = document.createElement("button");
      b.className = "tz-mc-nav";
      b.textContent = label;
      b.title = dir < 0 ? "Previous month" : "Next month";
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        tz.month = new Date(mFirst.getFullYear(), mFirst.getMonth() + dir, 1);
        tzRender();
      });
      navs.appendChild(b);
    }
    head.append(mo, navs);
    wrap.appendChild(head);
    const grid = document.createElement("div");
    grid.className = "tz-mc-grid";
    for (const dow of ["S", "M", "T", "W", "T", "F", "S"]) {
      const c = document.createElement("div");
      c.className = "tz-mc-dow";
      c.textContent = dow;
      grid.appendChild(c);
    }
    const todayKey = tzDayKey(tzToday());
    const selKey = tzDayKey(tzViewDay());
    const startDow = mFirst.getDay();
    const dim = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    const prevDim = new Date(m.getFullYear(), m.getMonth(), 0).getDate();
    for (let i = 0; i < 35; i++) {
      const dn = i - startDow + 1;
      let cell, other = false;
      if (dn < 1) { cell = prevDim + dn; other = true; }
      else if (dn > dim) { cell = dn - dim; other = true; }
      else cell = dn;
      const d = new Date(m.getFullYear(), m.getMonth(), dn);  // normalizes out-of-range
      const b = document.createElement("div");
      b.className = "tz-mc-day" + (other ? " other" : "") +
        (tzDayKey(d) === selKey ? " sel" : "");
      b.textContent = String(d.getDate());
      const k = tzDayKey(d);
      if (tKeys.has(k)) { const dot = document.createElement("span"); dot.className = "dot"; b.appendChild(dot); }
      if (eKeys.has(k)) { const dot = document.createElement("span"); dot.className = "dot evt"; b.appendChild(dot); }
      b.title = d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        // day filter in place — Today when it's today, that day otherwise
        tz.day = k === todayKey ? null : d;
        tz.month = new Date(d.getFullYear(), d.getMonth(), 1);
        tz.more = false;
        tzRender();
      });
      grid.appendChild(b);
    }
    wrap.appendChild(grid);
    return wrap;
  }

  function tzRow(kind, item) {
    const row = document.createElement("div");
    row.className = (kind === "event" ? "tz-e-row" : "tz-t-row")
      + (item.done ? " done" : "");
    if (kind === "event") {
      const bar = document.createElement("span");
      bar.className = "e-bar";
      row.appendChild(bar);
    }
    // both kinds get a chk: done events stay as history (struck through),
    // they are NOT deleted — delete is the ✕, done is the ✓
    const chk = document.createElement("button");
    chk.className = "chk";
    chk.title = item.done ? "Reopen" : "Mark done";
    chk.addEventListener("click", (ev) => {
      ev.stopPropagation();
      api("/api/" + (kind === "event" ? "events" : "tasks") + "/"
          + item.id + "/toggle", {
        method: "POST", body: JSON.stringify({ done: !item.done }),
      }).then(tzFetch).catch(() => {});
    });
    row.appendChild(chk);
    const txt = document.createElement("span");
    txt.className = "r-txt";
    txt.textContent = item.title;
    row.appendChild(txt);
    const when = document.createElement("span");
    when.className = "r-when" + (kind === "task" && item.due_iso ? tzWhenCls(item.due_iso) : "");
    when.textContent = item.due_iso || item.start_iso
      ? tzWhenLabel(item.due_iso || item.start_iso)
      : "no due";
    when.title = (item.due_iso || item.start_iso || "") + (item.note ? "\n" + item.note : "");
    row.appendChild(when);
    // expand affordance: ▸ reveals on hover, rotates when the row is open
    // (clicking the row — not its buttons — toggles the full title + note)
    const chev = document.createElement("span");
    chev.className = "r-chev";
    chev.textContent = "▸";
    // click the row (buttons stopPropagation above) → expand to reveal the
    // full title + note; the chev rotates, the title stops truncating
    row.addEventListener("click", () => {
      row.classList.toggle("expanded");
    });
    // ✏ = edit (tasks only for now — events get the same treatment later);
    // ✕ = hard delete (the row disappears from history too); ✓ = done
    // (stays, struck through) — the verbs do different things on purpose
    // (Boss: "is different if u just mark it done so its in history?",
    // "make my tasks editable in the ui so i can modify it myself")
    if (kind === "task") {
      const ed = document.createElement("button");
      ed.className = "r-edit";
      ed.appendChild(icon("pen"));
      ed.title = "Edit task";
      ed.addEventListener("click", (ev) => {
        ev.stopPropagation();
        tzEditTask(item);
      });
      row.appendChild(ed);
    }
    const del = document.createElement("button");
    del.className = "r-del";
    del.textContent = "✕";
    del.title = kind === "event" ? "Delete event" : "Delete task";
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      api("/api/" + (kind === "event" ? "events" : "tasks") + "/"
          + item.id + "/delete", { method: "POST" })
        .then(tzFetch).catch(() => {});
    });
    row.appendChild(del);
    row.appendChild(chev);
    return row;
  }

  // ── task edit modal (Boss: "make my tasks editable in the ui so i can
  //    modify it myself") — title / note / due, all editable, due clearable.
  //    Only tasks for now; events can reuse this with a start/end pair.
  function tzEditTask(item) {
    const overlay = document.createElement("div");
    overlay.className = "tz-edit-ovl";
    const box = document.createElement("div");
    box.className = "tz-edit";
    const h = document.createElement("div");
    h.className = "tz-edit-h";
    h.textContent = "EDIT TASK";
    box.appendChild(h);
    const mk = (cls, ph) => {
      const i = document.createElement("input");
      i.className = cls;
      i.placeholder = ph;
      return i;
    };
    const fTitle = mk("tz-e-title", "Title");
    fTitle.value = item.title || "";
    const fNote = mk("tz-e-note", "Note (optional)");
    fNote.value = item.note || "";
    const fDue = mk("tz-e-due", "");
    fDue.type = "datetime-local";
    fDue.value = tzToDTLocal(item.due_iso);
    const dClear = document.createElement("button");
    dClear.type = "button";
    dClear.className = "tz-e-clear";
    dClear.textContent = "clear";
    dClear.hidden = !fDue.value;
    dClear.addEventListener("click", () => {
      fDue.value = "";
      dClear.hidden = true;
      fDue.focus();
    });
    const dueRow = document.createElement("div");
    dueRow.className = "tz-e-duerow";
    fDue.addEventListener("input", () => {
      dClear.hidden = !fDue.value;
    });
    dueRow.append(fDue, dClear);
    box.appendChild(dueRow);
    box.appendChild(fTitle);
    box.appendChild(fNote);
    const err = document.createElement("div");
    err.className = "tz-e-err";
    box.appendChild(err);
    const actions = document.createElement("div");
    actions.className = "tz-e-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "tz-e-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", () => overlay.remove());
    const save = document.createElement("button");
    save.type = "button";
    save.className = "tz-e-save";
    save.textContent = "Save";
    save.addEventListener("click", () => {
      const title = fTitle.value.trim();
      if (!title) { err.textContent = "title required"; fTitle.focus(); return; }
      // datetime-local is naive local time — stamp it with the local UTC
      // offset (toISOString would store the UTC wall time, shifting the
      // toast by the offset) so the stored ISO matches what the poller expects
      const due = fDue.value
        ? tzLocalIso(new Date(fDue.value))
        : "";
      api("/api/tasks/" + item.id + "/update", {
        method: "POST",
        body: JSON.stringify({ title, note: fNote.value, due }),
      }).then(() => {
        overlay.remove();
        tzFetch();
      }).catch((e) => {
        err.textContent = "save failed: " + (e && e.message || e);
      });
    });
    actions.append(cancel, save);
    box.appendChild(actions);
    overlay.appendChild(box);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) overlay.remove();
    });
    document.addEventListener("keydown", function onKey(ev) {
      if (ev.key === "Escape") {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
      }
    });
    document.body.appendChild(overlay);
    fTitle.focus();
    fTitle.select();
  }
  // local Date → ISO 8601 WITH the local UTC offset (the store's format)
  function tzLocalIso(d) {
    const p = (n) => String(n).padStart(2, "0");
    const off = -d.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const oh = p(Math.floor(Math.abs(off) / 60));
    const om = p(Math.abs(off) % 60);
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
      + "T" + p(d.getHours()) + ":" + p(d.getMinutes()) + ":"
      + p(d.getSeconds()) + sign + oh + ":" + om;
  }
  // ISO (with offset) → datetime-local value (naive local) for the input
  function tzToDTLocal(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0") + "T" +
      String(d.getHours()).padStart(2, "0") + ":" +
      String(d.getMinutes()).padStart(2, "0");
  }

  function tzRender() {
    const drawer = el.tzDrawer;
    if (!drawer) return;
    const { evts, evtsDone, tasks, upcoming } = tzScoped();
    const vd = tzViewDay();
    // header: identical in both states (Boss: no morphing row)
    el.tzTitle.textContent = (!tz.day
      ? "TODAY · " + vd.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
      : vd.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }));
    const n = evts.length + tasks.length + upcoming.length;  // done = history, not count
    el.tzCount.hidden = n === 0;
    el.tzCount.textContent = String(n);
    // v4 drawer: open/closed is the whole state — tab toggles, ✕ closes
    drawer.classList.toggle("open", !tz.collapsed);
    // While the drawer is open, the composer's sticky bottom bar hides the
    // drawer's last rows (both paint at z:3, the drawer's box spans the full
    // viewport height). Hide the composer for the drawer's lifetime — the
    // drawer is the status surface, the chat is the command surface, and
    // clicking outside (or the tab / ✕) brings the composer straight back.
    document.body.classList.toggle("tz-open", !tz.collapsed);
    // signature: skip the body rebuild when nothing changed
    const sig = JSON.stringify([tzDayKey(vd), tz.month && tzDayKey(tz.month), tz.more,
      tasks.map((t) => [t.id, t.done]), upcoming.map((t) => t.id),
      evts.map((e) => e.id), evtsDone.map((e) => [e.id, e.done])]);
    if (sig === tz.sig) return;
    tz.sig = sig;
    const body = el.tzBody;
    body.innerHTML = "";
    body.appendChild(tzMiniCal());
    if (evts.length) {
      const lab = document.createElement("div");
      lab.className = "tz-sec";
      lab.textContent = "Events";
      body.appendChild(lab);
      const show = tz.more ? evts : evts.slice(0, 3);
      for (const e of show) body.appendChild(tzRow("event", e));
      if (evts.length > 3) body.appendChild(tzMoreBtn(evts.length - show.length));
    }
    if (tasks.length) {
      const lab = document.createElement("div");
      lab.className = "tz-sec";
      lab.textContent = "Tasks";
      body.appendChild(lab);
      const show = tz.more ? tasks : tasks.slice(0, 3);
      for (const t of show) body.appendChild(tzRow("task", t));
      if (tasks.length > 3) body.appendChild(tzMoreBtn(tasks.length - show.length));
    }
    if (upcoming.length) {
      const lab = document.createElement("div");
      lab.className = "tz-sec";
      lab.textContent = "Upcoming";
      body.appendChild(lab);
      const show = tz.more ? upcoming : upcoming.slice(0, 3);
      for (const t of show) body.appendChild(tzRow("task", t));
      if (upcoming.length > 3) body.appendChild(tzMoreBtn(upcoming.length - show.length));
    }
    if (evtsDone.length) {
      const lab = document.createElement("div");
      lab.className = "tz-sec";
      lab.textContent = "Done";
      body.appendChild(lab);
      const show = tz.more ? evtsDone : evtsDone.slice(0, 3);
      for (const e of show) body.appendChild(tzRow("event", e));
      if (evtsDone.length > 3) body.appendChild(tzMoreBtn(evtsDone.length - show.length));
    }
    if (!evts.length && !tasks.length && !upcoming.length) {
      const emp = document.createElement("div");
      emp.className = "tz-empty";
      emp.textContent = "nothing here — add one below or ask muji";
      body.appendChild(emp);
    }
    // quick add: one input, Enter → task (no due; the agent tool is the
    // full-surface for "due 10:20" phrasing)
    const qa = document.createElement("div");
    qa.className = "tz-quick";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "＋ add a task…";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    inp.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      const title = inp.value.trim();
      if (!title) return;
      api("/api/tasks", { method: "POST", body: JSON.stringify({ title }) })
        .then(tzFetch).catch(() => {});
    });
    const btn = document.createElement("button");
    btn.textContent = "＋";
    btn.title = "Add task (Enter)";
    btn.addEventListener("click", () => inp.focus());
    qa.append(inp, btn);
    body.appendChild(qa);
  }

  function tzMoreBtn(left) {
    const b = document.createElement("button");
    b.className = "tz-more";
    b.textContent = (tz.more ? "− show less" : "+ " + left + " more");
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      tz.more = !tz.more;
      tzRender();
    });
    return b;
  }

  function initTodayZone() {
    const drawer = el.tzDrawer;
    if (!drawer) return;
    tz.month = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
    // v4 drawer: the tab and the ✕ both toggle; the header row itself is
    // no longer clickable (it holds the ✕ button — a head click would
    // double-fire when hitting the ✕). State persists like sidebarW.
    const toggle = () => {
      tz.collapsed = !tz.collapsed;
      localStorage.setItem("muji.tzCollapsed", tz.collapsed ? "1" : "0");
      tzRender();
    };
    el.tzTab.addEventListener("click", toggle);
    el.tzClose.addEventListener("click", () => {
      if (!tz.collapsed) toggle();
    });
    tzRender();  // apply the persisted open/closed state
    tzFetch();
    // the agent writes the same store via its tools — 30s keeps the zone
    // honest without hammering the API (rows also refresh on every local
    // action immediately)
    tz.timer = setInterval(tzFetch, 30000);
  }

  // ── watch strip (the supervisor) ───────────────────────────────
  // Polls /api/watch every 5 s: the server classifies every live run
  // (running / waiting / stuck / queued) and ships pre-phrased rows, so
  // the client only renders. The strip is the ghost — one quiet line
  // when healthy, amber/red pulse when something needs a look. The
  // board's `stop + resume` is the manual lever; since 2026-09-28 the
  // supervisor ALSO auto stop-resumes a stuck run after 2 consecutive
  // ticks (tool-in-flight grace, budget 2/episode) — `waiting` rows
  // stay the user's click only.
  const sup = {
    rows: null, open: localStorage.getItem("muji.supOpen") === "1",
    busy: false,
  };
  const SUP_RANK = { stuck: 3, waiting: 2, running: 1, queued: 0, idle: -1 };

  function supRender() {
    const rows = sup.rows;
    const strip = el.supStrip;
    if (!rows) return;
    const n = (st) => rows.filter((r) => r.status === st);
    const stuck = n("stuck"), waiting = n("waiting"),
          running = n("running"), queued = n("queued");
    const worst = rows.length
      ? rows.slice().sort((a, b) => SUP_RANK[b.status] - SUP_RANK[a.status])[0].status
      : "idle";
    strip.classList.toggle("open", sup.open);
    el.supBoard.classList.toggle("open", sup.open);
    strip.classList.remove("ok", "amber", "red", "idle");
    if (worst === "stuck") strip.classList.add("red");
    else if (worst === "waiting" || (queued.length && queued.some((r) => r.why)))
      strip.classList.add("amber");
    else if (rows.length) strip.classList.add("ok");
    else strip.classList.add("idle");
    // summary: worst state first, then the rest in short form
    const parts = [];
    if (stuck.length) parts.push('<span class="bad">' + stuck.length + ' stuck</span>' +
      stuck.map((r) => " · " + r.title + " " + (r.why || "")).join(""));
    if (waiting.length) parts.push('<span class="amb">needs you</span>' +
      waiting.map((r) => " · " + r.title + " waiting " + r.waiting).join(""));
    if (running.length) parts.push('<span class="okc">' + running.length + " running</span>" +
      running.map((r) => " · " + r.title + " " + r.age).join(""));
    if (queued.length) parts.push(queued.length + " queued");
    el.supSummary.innerHTML = parts.length ? parts.join(" · ")
      : "idle — nothing running";
    // board: one row per run, worst first (server already sorts)
    el.supBoard.innerHTML = "";
    for (const r of rows) {
      const row = document.createElement("div");
      row.className = "run-row " + r.status;
      const dot = document.createElement("span");
      dot.className = "run-dot " + r.status;
      const name = document.createElement("span");
      name.className = "run-name"; name.textContent = r.title; name.title = r.title;
      const what = document.createElement("span");
      what.className = "run-what"; what.textContent = r.what || "";
      row.append(dot, name, what);
      if (r.why) {
        const why = document.createElement("span");
        why.className = "run-why"; why.textContent = r.why;
        row.append(why);
      }
      const age = document.createElement("span");
      age.className = "run-age"; age.textContent = r.age;
      row.append(age);
      const open = document.createElement("button");
      open.className = "run-act"; open.textContent = "open";
      open.addEventListener("click", (e) => {
        e.stopPropagation();
        switchSession(r.sid, false);
      });
      row.append(open);
      if (r.status === "stuck") {
        // the ONLY lever, and always the user's click (mockup: stop+resume
        // on stuck rows only — waiting rows are answered in the chat)
        const stop = document.createElement("button");
        stop.className = "run-act danger";
        stop.textContent = "stop + resume";
        stop.title = "Stops the run and queues a continuation nudge — the manual lever (the supervisor's auto action does the same, 2 ticks in)";
        stop.addEventListener("click", (e) => {
          e.stopPropagation();
          if (sup.busy) return;
          if (!confirm('Stop "' + r.title + '" and resume it? The run is cancelled and a continuation message is queued.')) return;
          sup.busy = true;
          stop.disabled = true;
          api("/api/watch/stop-resume", { method: "POST",
            body: JSON.stringify({ session_id: r.sid }) })
            .then((res) => res.json().then((j) => ({ ok: res.ok, j })))
            .catch(() => ({ ok: false, j: {} }))
            .then(({ ok, j }) => {
              if (!ok) {
                alert((j && j.detail) || "stop + resume failed");
                sup.busy = false; stop.disabled = false;
              }
              // success: the new run appears on the next tick — leave
              // `busy` armed until then (the button is gone by then anyway)
            });
        });
        row.append(stop);
      }
      el.supBoard.append(row);
    }
  }

  async function supTick() {
    try {
      const d = await (await api("/api/watch")).json();
      sup.rows = d.rows || [];
      supRender();
    } catch (e) { /* server blip — keep the last frame */ }
  }

  function initWatch() {
    el.supStrip.addEventListener("click", () => {
      sup.open = !sup.open;
      localStorage.setItem("muji.supOpen", sup.open ? "1" : "0");
      supRender();
    });
    supTick();
    setInterval(supTick, 5000);
  }

  function startStatusPoll() {
    if (state.statusTimer) return;
    state.statusTimer = setInterval(refreshSessions, 3000);
    // the ticking title (❓ 2:47 muji) must advance even when this tab is
    // HIDDEN — Chrome throttles hidden-tab timers, and the 3s poll alone
    // left the tab title frozen (or never set) while you were elsewhere.
    // A 1s interval re-derives the title from the stored deadline; it's a
    // pure string compare + assign, so the throttling cost is nothing.
    setInterval(() => {
      const w = state.sessions.find(
        (s) => (s.waiting_question || s.waiting_approval) &&
               s.id !== state.sessionId);
      if (!w) return;  // attentionForWaiting resets the title when settled
      const t = _waitState.get(w.id);
      const base = (state.config && state.config.title) || "muji";
      const mark = w.waiting_question ? "❓" : "⚠";
      const now = Date.now();
      let title = mark + " " + base;
      if (t && t.deadline && t.deadline > now) {
        const s = Math.ceil((t.deadline - now) / 1000);
        title = mark + " " + Math.floor(s / 60) + ":" +
                String(s % 60).padStart(2, "0") + " " + base;
      }
      if (document.title !== title) document.title = title;
    }, 1000);
  }

  // PWA (Add to Home Screen): notch/dynamic island eats the top strip, so
  // .pwa shifts the app below the safe area (CSS). The sidebar starts
  // OPEN on phone-sized screens (Boss: the app should open with the left
  // panel expanded) — it's an overlay there, and tap-outside / ✕ closes it.
  const IS_PWA = window.matchMedia("(display-mode: standalone)").matches;
  if (IS_PWA) el.app.classList.add("pwa");
  // Narrow screens (PWA OR mobile browser): the sidebar starts OPEN (Boss:
  // the app should open with the left panel expanded) — it's an overlay
  // there, and tap-outside / ✕ closes it. The right panel must start
  // CLOSED: as a full-width overlay it would cover the whole chat.
  // (Previously PWA-only — a plain mobile browser booted with the right
  // panel on top of everything.)
  if (window.innerWidth <= 900) {
    el.sidebar.classList.remove("collapsed");
    el.sidebarOpen.hidden = true;
    setRightPanelOpen(false);
    updatePanelOverlay();  // sidebar is open → the dimmed tap-outside layer shows
  }

  // ── First-run setup overlay ─────────────────────────────────────
  // Shown when /api/setup/status says the model endpoint isn't configured.
  // Collects base URL / API key / model, writes them to .env via
  // POST /api/setup/save, then reloads the page. The server picks up
  // the new env vars on restart.
  function initSetup() {
    const setup = $("setup");
    if (!setup) return;
    setup.hidden = false;
    const base = $("setup-base");
    const key = $("setup-key");
    const model = $("setup-model");
    const modelCustom = $("setup-model-custom");
    const detect = $("setup-detect");
    const modelHint = $("setup-model-hint");
    const go = $("setup-go");
    const status = $("setup-status");
    const presets = $("setup-presets");

    // Auto-detect models from the endpoint (proxied by /api/models).
    // Debounced: fires 600 ms after the last Base-URL keystroke, or
    // immediately on preset click. Local endpoints (Ollama etc.) work
    // without a key; hosted ones need one (a 401 just returns []).
    let detectTimer;
    function autoDetect() {
      const url = base.value.trim().replace(/\/+$/, "");
      if (!url) {
        setModelList([], "");
        detect.className = "setup-detect";
        detect.textContent = "";
        modelHint.textContent = "Paste a Base URL to auto-detect models.";
        return;
      }
      detect.className = "setup-detect loading";
      detect.textContent = "detecting…";
      const params = new URLSearchParams({ base: url });
      const k = key.value.trim();
      if (k) params.set("key", k);
      fetch("/api/models?" + params.toString(), { cache: "no-store" })
        .then((r) => r.json())
        .then((d) => {
          const list = d.models || [];
          setModelList(list, list[0] || "");
          if (list.length) {
            detect.className = "setup-detect show";
            detect.textContent = "auto-detected";
            modelHint.textContent =
              list.length + " model" + (list.length > 1 ? "s" : "") +
              " found at this endpoint.";
          } else {
            detect.className = "setup-detect";
            detect.textContent = "";
            modelHint.textContent = d.error
              ? "Couldn't detect models (" + d.error + ") — type a name below."
              : "No models found — type a model name below.";
            modelCustom.style.display = "block";
          }
        })
        .catch(() => {
          detect.className = "setup-detect";
          detect.textContent = "";
          modelHint.textContent = "Couldn't reach the endpoint — type a model name below.";
          modelCustom.style.display = "block";
        });
    }

    function setModelList(list, selected) {
      model.innerHTML = '<option value="">— pick a model —</option>';
      list.forEach((m) => {
        const o = document.createElement("option");
        o.value = m;
        o.textContent = m;
        if (m === selected) o.selected = true;
        model.appendChild(o);
      });
      const custom = document.createElement("option");
      custom.value = "__custom__";
      custom.textContent = "…or type a model name";
      model.appendChild(custom);
      modelCustom.style.display = "none";
    }

    // Preset click: fill Base URL, trigger detect.
    // Custom: clear Base URL, focus it, drop to type-your-own model.
    presets.addEventListener("click", (e) => {
      const p = e.target.closest(".setup-preset");
      if (!p) return;
      presets.querySelectorAll(".setup-preset").forEach((x) => x.classList.remove("active"));
      p.classList.add("active");
      base.value = p.dataset.base;
      if (p.dataset.base === "") {
        setModelList([], "");
        modelCustom.style.display = "block";
        detect.className = "setup-detect";
        detect.textContent = "";
        modelHint.textContent = "Paste your Base URL above and type a model name below.";
        base.focus();
      } else {
        clearTimeout(detectTimer);
        detectTimer = setTimeout(autoDetect, 100);
      }
    });

    // Base URL change → re-detect (debounced).
    base.addEventListener("input", () => {
      presets.querySelectorAll(".setup-preset").forEach((x) => x.classList.remove("active"));
      clearTimeout(detectTimer);
      detectTimer = setTimeout(autoDetect, 600);
    });

    // Key change → re-detect if a model list is already showing
    // (hosted endpoints need the key to list models).
    key.addEventListener("input", () => {
      if (model.options.length > 2) { // has detected models
        clearTimeout(detectTimer);
        detectTimer = setTimeout(autoDetect, 600);
      }
    });

    // Model select: "…or type a model name" shows the custom input.
    model.addEventListener("change", () => {
      if (model.value === "__custom__") {
        modelCustom.style.display = "block";
        modelCustom.focus();
      } else {
        modelCustom.style.display = "none";
      }
    });
    modelCustom.addEventListener("input", () => {
      presets.querySelectorAll(".setup-preset").forEach((x) => x.classList.remove("active"));
    });

    function getModel() {
      if (model.value === "__custom__") return modelCustom.value.trim();
      return model.value;
    }

    // Save & Connect: validate, POST to /api/setup/save, reload.
    go.addEventListener("click", () => {
      const b = base.value.trim().replace(/\/+$/, "");
      const k = key.value.trim();
      const m = getModel();
      if (!b) { showStatus("err", "✕ Enter a Base URL."); return; }
      if (!m) { showStatus("err", "✕ Pick or type a model name."); return; }
      // Local endpoints don't need a real key; hosted ones do.
      const looksLocal = /localhost|127\.0\.0\.1|0\.0\.0\.0|192\.168\.|10\./.test(b);
      if (!k && !looksLocal) { showStatus("err", "✕ Enter an API key to continue."); return; }

      go.disabled = true;
      showStatus("loading", "Saving…");
      fetch("/api/setup/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: b, api_key: k, model: m }),
      })
        .then((r) => {
          if (!r.ok) return r.json().then((d) => { throw new Error(d.detail || r.status); });
          return r.json();
        })
        .then(() => {
          // .env is read at import time — the running server still sees the
          // old (unset) key, so a plain reload would just re-show this card.
          // Restart the server (user-initiated save = explicit consent), wait
          // for it to come back, then reload into a configured app.
          showStatus("loading", "Saved — restarting server…");
          return fetch("/api/restart", { method: "POST" })
            .catch(() => {}) // server exits immediately after spawning its successor
            .then(() => new Promise((resolve) => {
              const t0 = Date.now();
              const poll = () => {
                fetch("/api/health", { cache: "no-store" })
                  .then((r) => (r.ok ? resolve() : poll()))
                  .catch(() => (Date.now() - t0 > 30000 ? resolve() : poll()));
              };
              setTimeout(poll, 1200);
            }));
        })
        .then(() => {
          showStatus("ok", "✓ Connected — loading…");
          setTimeout(() => location.reload(), 400);
        })
        .catch((e) => {
          showStatus("err", "✕ " + e.message);
          go.disabled = false;
        });
    });

    function showStatus(kind, msg) {
      status.className = "setup-status show" + (kind === "loading" ? "" : " " + kind);
      if (kind === "loading") {
        status.innerHTML = '<span class="spin"></span> ' + msg;
      } else {
        status.textContent = msg;
      }
    }

    // Initial: OpenAI preset is active in the HTML; fire detect on load.
    autoDetect();
  }

  async function boot() {
    initTheme();
    let config;
    try {
      config = await (await fetch("/api/config")).json();
    } catch (e) {
      document.body.innerHTML =
        "<p style='padding:40px;font-family:sans-serif'>" +
        "muji server unreachable — start it with: python server.py</p>";
      return;
    }
    state.config = config;
    // First-run: if no model endpoint is configured yet, show the setup
    // overlay and stop here — the rest of boot (sessions, workspaces, etc.)
    // is pointless until a model is connected.
    let setupOk = true;
    try {
      const st = await (await fetch("/api/setup/status", { cache: "no-store" })).json();
      setupOk = st.configured;
    } catch (e) { /* endpoint missing (old server) — assume configured */ }
    if (!setupOk) {
      initSetup();
      return;
    }
    // The server process we're attached to — the revival detector's baseline
    // (see openStream's onerror). Fetched best-effort: if it fails, the
    // detector simply stays armed-off and the surgical paths cover the tab.
    try {
      const h = await (await fetch("/api/health", { cache: "no-store" })).json();
      if (h.boot_id) state.bootId = h.boot_id;
    } catch (e) { /* server flaky at boot — skip the detector */ }
    // Review panels: the brain (🧠 learned.md) and wench (🔧 tool_notes.md)
    // topbar buttons each toggle their own panel — opens it on demand,
    // closes it when it's visible. No composer banner (retired 2026-10-12):
    // new pending entries only light the button's dot.
    const wireReviewButton = (src) => (async () => {
      // toggle: panel visible → close it; hidden → open it
      const e = srcEl(src);
      if (!e.panel.hidden) { closeLearnedList(src); return; }
      if (!srcState(src).loaded) await loadLearned();
      // Always open the list — 0 pending + 0 active still shows the empty
      // panel (with a toast), and 0 pending + N active shows the active
      // section, which is viewable/deactivatable.
      if (!allPending(src).length && !allActive(src).length) {
        learnToast(src === "toolnote" ? "No quirks logged yet"
                                      : "No patterns logged yet");
      }
      openLearnedList(src);
    });
    el.learnOpen.addEventListener("click", wireReviewButton("learned"));
    el.wenchOpen.addEventListener("click", wireReviewButton("toolnote"));
    loadLearned();
    document.title = config.title || "muji";
    el.brandTitle.textContent = config.brand || "muji";
    el.composerHint.textContent =
      (config.brand || "muji") + " can make mistakes. Verify important results.";
    el.input.placeholder = "Message " + (config.brand || "muji") + "…";
    el.rootLine.textContent = "root: " + config.root_dir;
    el.modelLine.textContent = "model: " + (config.model || "?");
    el.aaSub.textContent =
      "Let " + (config.brand || "muji") + " take these actions without asking for approval:";
    await loadSettings();
    await loadWorkspaces();
    resetTree();
    setUpperTab(state.rightTab);
    setLowerTab(state.lowerTab);
    initResizers();
    startStatusPoll();
    seedToks();            // topbar readout survives a reload (hidden otherwise)
    initWatch();           // the supervisor strip: 5s poll, ghost by default
    initTodayZone();       // option A step 4: Today zone (tasks + mini-cal)
    askNotifyPermission();  // one-time: lets cross-tab "needs you" ping

    // restore last session if it still exists
    if (state.sessionId) {
      try {
        const all = await (await api("/api/sessions")).json();
        const mine = all.sessions.find((s) => s.id === state.sessionId);
        if (mine) {
          if (mine.workspace_id) {
            const ws = state.workspaces.find((w) => w.id === mine.workspace_id);
            if (ws) { selectWorkspace(ws.id); return; }
          }
          switchSession(state.sessionId);
          restoreDraft();
          return;
        }
      } catch (e) { /* fall through to a fresh session */ }
    }
    await refreshSessions();
    const first = state.sessions[0];
    if (first) switchSession(first.id);
    else newSession();
    restoreDraft();
    focusComposer();
  }

  // ── Wiring ────────────────────────────────────────────────────
  el.input.addEventListener("input", () => {
    autosize(); updateSendEnabled();
    // draft persistence: a revival reload (or a refresh) must not eat the
    // composer text — the only state a full tab reload would otherwise lose
    try {
      const d = el.input.value;
      localStorage.setItem("muji.draft",
        JSON.stringify({ sid: state.sessionId, text: d }));
      if (!d) localStorage.removeItem("muji.draft");
    } catch (e) { /* storage full/blocked — cosmetic only */ }
  });
  el.input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); }
  });
  // arrow wrapper: a bare `send` here would receive the click EVENT as
  // overrideText (see the guard in send())
  el.sendBtn.addEventListener("click", () => send());
  el.stopBtn.addEventListener("click", stop);
  el.attachBtn.addEventListener("click", () => el.fileInput.click());
  el.fileInput.addEventListener("change", () => {
    if (el.fileInput.files.length) uploadFile(el.fileInput.files[0]);
    el.fileInput.value = "";
  });
  // paste images (Ctrl+V) into the composer → same upload flow as 📎
  el.input.addEventListener("paste", (ev) => {
    const files = Array.from((ev.clipboardData && ev.clipboardData.files) || []);
    const imgs = files.filter((f) => f.type.startsWith("image/"));
    if (!imgs.length) return;
    ev.preventDefault();
    const room = Math.max(1, 4 - state.attachments.length);
    imgs.slice(0, room).forEach(uploadFile);
  });
  el.newChat.addEventListener("click", newSession);
  el.rpTreeChoose.addEventListener("click", startPick);
  el.rpTreePathGo.addEventListener("click", openPastedPath);
  el.rpTreePath.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); openPastedPath(); }
  });
  el.rpTreeNewDir.addEventListener("click", () => createEntry("dir"));
  el.rpTreeNewFile.addEventListener("click", () => createEntry("file"));
  el.treePickUse.addEventListener("click", usePick);
  el.treePickCancel.addEventListener("click", cancelPick);
  // ── Chats ⋯ menu: safer destructive actions ──────────────────
  // Two-step arming: first click arms the item ("Sure? N chats — click
  // again") for 4s; a second click within the window executes. No
  // accidental one-tap wipe of the whole history.
  function armDestructive(btn, count, onConfirm) {
    if (btn._armed) {
      clearTimeout(btn._armTimer);
      btn._armed = false;
      btn.classList.remove("armed");
      btn.textContent = btn.dataset.label;
      onConfirm();
      return;
    }
    btn._armed = true;
    btn.classList.add("armed");
    btn.textContent = "Sure? " + count + " chat" + (count === 1 ? "" : "s") + " — click again";
    btn._armTimer = setTimeout(() => {
      btn._armed = false;
      btn.classList.remove("armed");
      btn.textContent = btn.dataset.label;
    }, 4000);
  }
  // live counts in the menu labels (updated on every sidebar refresh)
  function updateMenuCounts() {
    const all = state.sessions.length;
    const cutoff = Date.now() - ARCHIVE_MS;
    const arch = state.sessions.filter((s) => !s.pinned &&
      (s.updated_at || 0) * 1000 < cutoff).length;
    el.clearAll.dataset.label = "Clear all" + (all ? " (" + all + ")" : "");
    el.clearAll.textContent = el.clearAll._armed
      ? el.clearAll.textContent : el.clearAll.dataset.label;
    el.clearArchived.dataset.label = "Clear archived" + (arch ? " (" + arch + ")" : "");
    el.clearArchived.textContent = el.clearArchived._armed
      ? el.clearArchived.textContent : el.clearArchived.dataset.label;
    el.clearArchived.disabled = arch === 0;
    el.deletedChats.dataset.label = "Deleted chats" +
      (state.deletedCount ? " (" + state.deletedCount + ")" : "");
    el.deletedChats.textContent = el.deletedChats.dataset.label;
  }
  // ── Deleted-chats drawer: 30-day tombstone with true restore ─────
  // The server snapshots every delete (single / Clear all / Clear
  // archived) into deleted_sessions WITH its messages; this renders
  // the list and the per-row restore (re-inserts the chat under its
  // original id and switches to it).
  const DD_SOURCES = {
    delete: "single", clear_all: "clear all", clear_archived: "archived",
  };
  async function openDeletedDrawer() {
    el.chatsMenuPop.hidden = true;
    el.deletedDim.hidden = false;
    el.deletedDrawer.hidden = false;
    el.deletedList.innerHTML = "";
    el.deletedEmpty.hidden = true;
    try {
      const data = await (await api("/api/sessions/deleted")).json();
      const rows = data.deleted || [];
      state.deletedCount = rows.length;
      updateMenuCounts();
      el.deletedEmpty.hidden = rows.length > 0;
      for (const r of rows) {
        const row = document.createElement("div");
        row.className = "dd-row";
        const t = document.createElement("div");
        t.className = "dd-title2" + (r.title ? "" : " unnamed");
        t.textContent = r.title || "(untitled chat)";
        row.appendChild(t);
        const meta = document.createElement("div");
        meta.className = "dd-meta";
        const when = document.createElement("span");
        when.textContent = fmtDeletedWhen(r.deleted_at * 1000);
        const src = document.createElement("span");
        src.className = "dd-src";
        src.textContent = DD_SOURCES[r.source] || r.source || "?";
        const msgs = document.createElement("span");
        msgs.textContent = r.msg_count + " msg" + (r.msg_count === 1 ? "" : "s");
        meta.appendChild(when); meta.appendChild(src); meta.appendChild(msgs);
        row.appendChild(meta);
        const rb = document.createElement("button");
        rb.className = "dd-restore";
        rb.textContent = "Restore";
        rb.title = "Bring this chat back (same id, all messages)";
        rb.addEventListener("click", (ev) => {
          ev.stopPropagation();
          restoreDeleted(r, rb);
        });
        row.appendChild(rb);
        el.deletedList.appendChild(row);
      }
    } catch (e) {
      el.deletedEmpty.hidden = false;
      el.deletedEmpty.textContent = "Couldn't load the deleted list.";
    }
  }
  async function restoreDeleted(r, btn) {
    // single-flight per row: a double-tap must not hit the endpoint twice
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api(
        "/api/sessions/deleted/" + encodeURIComponent(r.session_id) +
        "/restore", { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "restore failed");
      }
      const s = (await res.json()).session;
      // the chat comes back under its ORIGINAL id — if it belonged to a
      // workspace the sidebar isn't showing, switch to it first
      if (s.workspace_id && s.workspace_id !== state.wsSel)
        selectWorkspace(s.workspace_id);
      closeDeletedDrawer();
      await refreshSessions();
      switchSession(s.id);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Restore";
      alert("Couldn't restore: " + e.message);
    }
  }
  function closeDeletedDrawer() {
    el.deletedDrawer.hidden = true;
    el.deletedDim.hidden = true;
    el.deletedEmpty.textContent = "Nothing deleted in the last 30 days.";
  }
  function fmtDeletedWhen(ms) {
    const d = new Date(ms);
    const day = d.toLocaleDateString(undefined,
      { month: "short", day: "numeric" });
    const tod = new Date(); tod.setHours(0, 0, 0, 0);
    const yest = new Date(tod); yest.setDate(tod.getDate() - 1);
    const prefix = d >= tod ? "Today" : d >= yest ? "Yesterday" : day;
    const time = d.toLocaleTimeString(undefined,
      { hour: "2-digit", minute: "2-digit" });
    return prefix + " " + time;
  }
  el.chatsMenu.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const pop = el.chatsMenuPop;
    const show = pop.hidden;
    pop.hidden = !show;
    if (show) updateMenuCounts();
  });
  document.addEventListener("click", (ev) => {
    if (!el.chatsMenuPop.hidden &&
        !el.chatsMenuPop.contains(ev.target) &&
        ev.target !== el.chatsMenu)
      el.chatsMenuPop.hidden = true;
  });
  el.clearAll.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const n = state.sessions.length;
    if (!n) return;
    armDestructive(el.clearAll, n, async () => {
      el.chatsMenuPop.hidden = true;
      await api("/api/sessions/clear", { method: "POST" });
      state.panels = {};
      state.sessionId = null;
      localStorage.removeItem("muji.sid");
      refreshSessions().then(newSession);
    });
  });
  el.clearArchived.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const cutoff = Date.now() - ARCHIVE_MS;
    const n = state.sessions.filter((s) => !s.pinned &&
      (s.updated_at || 0) * 1000 < cutoff).length;
    if (!n) return;
    armDestructive(el.clearArchived, n, async () => {
      el.chatsMenuPop.hidden = true;
      await api("/api/sessions/clear-archived", {
        method: "POST", body: JSON.stringify({ days: 14 }),
      });
      // archived chats can't be the visible one (they're 14+ days old and
      // unpinned) — just re-render
      refreshSessions();
    });
  });
  el.deletedChats.addEventListener("click", (ev) => {
    ev.stopPropagation();
    openDeletedDrawer();
  });
  el.deletedClose.addEventListener("click", closeDeletedDrawer);
  document.addEventListener("click", (ev) => {
    if (!el.deletedDrawer.hidden &&
        !el.deletedDrawer.contains(ev.target) &&
        !el.chatsMenuPop.contains(ev.target))
      closeDeletedDrawer();
  });
  // ── Latency heatmap drawer (topbar ⏱): day × hour speed, live from /api/latency ──
  // One click recomputes — no baked-in cache, so the numbers are always
  // current (the standalone HTML file's stale-data bug, fixed by design).
  const LAT_START_H = 6; // day starts at 6 AM (dead overnight hours wrap right)
  let latData = null;
  let latMetric = "tok_s";
  function latFmtHour(h) {
    if (h === 0) return "12am";
    if (h < 12) return h + "am";
    if (h === 12) return "12pm";
    return (h - 12) + "pm";
  }
  function latColor(t) {
    const stops = [[185,28,28],[217,119,6],[234,179,8],[101,163,13],[22,163,74]];
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    const i = Math.min(Math.floor(x), stops.length - 2);
    const f = x - i;
    const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * f));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  function latRenderDay() {
    const D = latData;
    const now = new Date();
    const nowH = now.getHours();
    // LOCAL date — the data is bucketed in local time (the UTC bucketing
    // was the original heatmap bug). toISOString() would be UTC.
    const todayStr = now.getFullYear() + "-" +
      String(now.getMonth() + 1).padStart(2, "0") + "-" +
      String(now.getDate()).padStart(2, "0");
    const recent = D.days.slice(-7);  // 7-day moving window (parked backlog, applied 2026-10-08)
    const proj = [], projN = [];
    for (let h = 0; h < 24; h++) {
      const vals = [];
      for (const d of recent) {
        const v = D.tok_s[d][h];
        const n = D.n_tok[d][String(h)] || 0;
        if (v != null && n) { for (let i = 0; i < n; i++) vals.push(v); }
      }
      projN[h] = vals.length;
      proj[h] = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    }
    const today = D.days.includes(todayStr) ? D.days.indexOf(todayStr) : -1;
    const grid = el.latGrid;
    grid.innerHTML = "";
    grid.classList.add("daychart");
    grid.style.gridTemplateColumns = `repeat(24, 1fr)`;
    const todayArr = today >= 0 ? D.tok_s[todayStr] : null;  // keyed by date string
    const all = [];
    for (let h = 0; h < 24; h++) {
      const v = todayArr ? todayArr[h] : null;
      const p = proj[h];
      if (v != null) all.push(v);
      if (p != null) all.push(p);
    }
    if (!all.length) {
      grid.classList.remove("daychart");
      el.latVerdict.textContent = "No tok/s data yet — use muji a bit and come back.";
      return;
    }
    const lo = Math.min(...all), hi = Math.max(...all);
    const span = hi - lo || 1;
    const pAll = proj.filter(v => v != null);
    const dayMed = pAll.length ? pAll.reduce((a, b) => a + b, 0) / pAll.length : null;
    for (let h = 0; h < 24; h++) {
      const col = document.createElement("div");
      col.className = "dcol" + (h === nowH ? " now" : "");
      const track = document.createElement("div");
      track.className = "dtrack";
      const v = todayArr ? todayArr[h] : null;
      const p = proj[h];
      const shown = v != null ? v : p;
      const bar = document.createElement("div");
      bar.className = "dbar" + (v != null ? " actual" : (p == null ? " na" : " proj"));
      if (shown != null) {
        bar.style.height = Math.max(6, ((shown - lo) / span) * 100) + "%";
        bar.style.background = latColor((shown - lo) / span);
        bar.title = latFmtHour(h) + (v != null ? "" : " (projected)");
      } else {
        bar.style.height = "6%";
        bar.title = latFmtHour(h) + " — no history";
      }
      track.appendChild(bar);
      if (dayMed != null) {
        const base = document.createElement("div");
        base.className = "dbase";
        base.style.bottom = Math.max(0, Math.min(100, ((dayMed - lo) / span) * 100)) + "%";
        track.appendChild(base);
      }
      col.appendChild(track);
      const lbl = document.createElement("div");
      lbl.className = "dlab";
      lbl.textContent = h % 3 === 0 ? latFmtHour(h) : "";
      col.appendChild(lbl);
      grid.appendChild(col);
    }
    const lg = el.latLegend;
    lg.innerHTML = "";
    const l1 = document.createElement("span");
    l1.textContent = lo.toFixed(0) + " tok/s";
    const bar = document.createElement("div");
    bar.className = "bar";
    const l2 = document.createElement("span");
    l2.textContent = hi.toFixed(0) + " tok/s";
    lg.append(l1, bar, l2);
    const nActual = todayArr ? todayArr.filter(v => v != null).length : 0;
    el.latSub.textContent =
      `${D.days.length} days · ${recent.length}-day projection · ${nActual}/24 hours actual today`;
    // Verdict: work now or later?
    const pNow = proj[nowH];
    const rank = pAll.slice().sort((a, b) => a - b);
    const pctile = pNow != null && rank.length
      ? Math.round((rank.filter(x => x < pNow).length / rank.length) * 100) : null;
    let verdict;
    if (pNow == null) {
      verdict = `No history for ${latFmtHour(nowH)} — can't project this hour. ` +
        `Best window so far: see the greenest bars below.`;
    } else if (dayMed != null && pNow >= dayMed * 0.97) {
      verdict = `Now is <b>good</b> — ${latFmtHour(nowH)} projects <b>${pNow.toFixed(0)} tok/s</b>` +
        (pctile != null ? ` (top ${100 - pctile}% of the day)` : "") +
        `, at/above the day median of ${dayMed.toFixed(0)}. Work now.`;
    } else {
      let bestH = -1, bestV = -1;
      for (let h = 0; h < 24; h++)
        if (proj[h] != null && proj[h] > bestV) { bestV = proj[h]; bestH = h; }
      const diff = dayMed != null ? Math.round((1 - pNow / dayMed) * 100) : null;
      verdict = `Now is <b>slow</b> — ${latFmtHour(nowH)} projects <b>${pNow.toFixed(0)} tok/s</b>` +
        (diff != null ? ` (${diff}% under the day median)` : "") +
        `. Best window: <b>${latFmtHour(bestH)} — ${bestV.toFixed(0)} tok/s</b>.`;
    }
    el.latVerdict.innerHTML = verdict;
    el.latNote.textContent =
      "Solid bars = today's actual medians so far. Faded bars = projection " +
      "(7-day moving window, round-weighted per hour). Dashed line = projected day median. " +
      "Gray stub = no history that hour. Projection is a historical average, " +
      "not a guarantee — a heavy load right now can still slow things down.";
  }
  function latRender() {
    if (!latData) return;
    const D = latData;
    el.latGrid.classList.remove("daychart");
    if (latMetric === "day") { latRenderDay(); return; }
    const order = [...D.hours.slice(LAT_START_H), ...D.hours.slice(0, LAT_START_H)];
    const grid = el.latGrid;
    grid.innerHTML = "";
    grid.style.gridTemplateColumns = `88px repeat(${order.length}, 1fr)`;
    grid.appendChild(document.createElement("div"));
    for (const h of order) {
      const el = document.createElement("div");
      el.className = "hlabel";
      el.textContent = latFmtHour(h);
      grid.appendChild(el);
    }
    const vals = [];
    for (const d of D.days) for (const h of order) {
      const v = D[latMetric][d][h];
      if (v != null) vals.push(v);
    }
    if (!vals.length) {
      el.latVerdict.textContent = "No data yet — use muji a bit and come back.";
      return;
    }
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = hi - lo || 1;
    for (const d of D.days) {
      const dl = document.createElement("div");
      dl.className = "dlabel";
      const dt = new Date(d + "T00:00:00");
      dl.innerHTML = `<b>${dt.toLocaleDateString("en", { weekday: "short" })} ${d.slice(5)}</b>`;
      grid.appendChild(dl);
      for (const h of order) {
        const v = D[latMetric][d][h];
        const n = D["n_" + (latMetric === "tok_s" ? "tok" : "ms")][d][String(h)];
        const cell = document.createElement("div");
        if (v == null || !n) {
          cell.className = "cell na";
          cell.textContent = "·";
        } else {
          const t = latMetric === "tok_s" ? (v - lo) / span : 1 - (v - lo) / span;
          cell.className = "cell";
          cell.style.background = latColor(t);
          cell.textContent = latMetric === "tok_s"
            ? v.toFixed(0) : (v / 1000).toFixed(1) + "s";
          const nn = document.createElement("span");
          nn.className = "n";
          nn.textContent = n;
          cell.appendChild(nn);
        }
        grid.appendChild(cell);
      }
    }
    const lg = el.latLegend;
    lg.innerHTML = "";
    const l1 = document.createElement("span");
    l1.textContent = latMetric === "tok_s" ? `${lo.toFixed(0)} tok/s` : `${(hi/1000).toFixed(1)}s`;
    const bar = document.createElement("div");
    bar.className = "bar";
    if (latMetric === "ms")
      bar.style.background = "linear-gradient(90deg,#16a34a,#65a30d,#eab308,#d97706,#b91c1c)";
    const l2 = document.createElement("span");
    l2.textContent = latMetric === "tok_s" ? `${hi.toFixed(0)} tok/s` : `${(lo/1000).toFixed(1)}s`;
    lg.append(l1, bar, l2);
    const fmtD = d => new Date(d + "T00:00:00").toLocaleDateString("en", { month: "short", day: "numeric" });
    if (latMetric === "tok_s") {
      let best = null, worst = null;
      for (const d of D.days) for (const h of order) {
        const val = D.tok_s[d][h];
        const n = D.n_tok[d][String(h)];
        if (val == null || n < 10) continue;
        if (!best || val > best.val) best = { d, h, val, n };
        if (!worst || val < worst.val) worst = { d, h, val, n };
      }
      const tokDays = D.days.filter(d => D.day_tot[d].med_tok != null);
      if (tokDays.length && best && worst) {
        const f = tokDays[0], l = tokDays[tokDays.length - 1];
        const slowDay = tokDays.slice().sort((a,b) => D.day_tot[a].med_tok - D.day_tot[b].med_tok)[0];
        el.latVerdict.innerHTML =
          `Fastest: <b>${fmtD(best.d)} ${latFmtHour(best.h)} — ${best.val.toFixed(0)} tok/s</b>. ` +
          `Slowest: ${fmtD(worst.d)} ${latFmtHour(worst.h)} — ${worst.val.toFixed(0)}. ` +
          `Day medians: ${D.day_tot[f].med_tok.toFixed(0)} on ${fmtD(f)} → ${D.day_tot[l].med_tok.toFixed(0)} on ${fmtD(l)}. ` +
          `Sagged to its slowest on ${fmtD(slowDay)}, then recovered.`;
      } else {
        el.latVerdict.textContent = "Not enough tok/s data yet for a verdict.";
      }
    } else {
      el.latVerdict.innerHTML =
        `Median round time: ${(D.day_tot[D.days[0]].med_ms/1000).toFixed(1)}s on ` +
        `${fmtD(D.days[0])} → ${(D.day_tot[D.days[D.days.length-1]].med_ms/1000).toFixed(1)}s on ` +
        `${fmtD(D.days[D.days.length-1])}. Earlier days can't be compared to later ones — tok/s is the honest metric from Sep 26 onward.`;
    }
    el.latNote.textContent =
      "Cells with < 10 rounds are shown but statistically thin. Empty (·) = no data that hour. " +
      "Sep 22–25 have no tok/s: the tracker started logging it on Sep 26.";
  }
  async function openLatDrawer() {
    el.latDim.hidden = false;
    el.latDrawer.hidden = false;
    el.latVerdict.textContent = "Recomputing from the events table…";
    el.latGrid.innerHTML = "";
    el.latLegend.innerHTML = "";
    try {
      latData = await (await api("/api/latency")).json();
      const total = latData.days.reduce((s, d) => s + (latData.day_tot[d].n || 0), 0);
      el.latSub.textContent = `${latData.days.length} days · ${total} rounds`;
      latRender();
    } catch (e) {
      el.latVerdict.textContent = "Couldn't load latency data: " + e.message;
    }
  }
  function closeLatDrawer() {
    el.latDrawer.hidden = true;
    el.latDim.hidden = true;
  }
  el.latOpen.addEventListener("click", openLatDrawer);
  el.latClose.addEventListener("click", closeLatDrawer);
  // The topbar tok/s readout is a shortcut into the projection tab:
  // click → open the latency drawer on "today + projection"; click again
  // (while it's open on that tab) → collapse it.
  el.toks.addEventListener("click", async () => {
    if (!el.latDrawer.hidden && latMetric === "day") { closeLatDrawer(); return; }
    await openLatDrawer();
    latMetric = "day";
    el.latToggle.querySelectorAll("button").forEach((x) =>
      x.classList.toggle("active", x.dataset.metric === "day"));
    latRender();
  });
  document.addEventListener("click", (ev) => {
    if (!el.latDrawer.hidden &&
        !el.latDrawer.contains(ev.target) &&
        ev.target !== el.latOpen &&
        !el.toks.contains(ev.target))  // toks is the projection shortcut — its own click handler owns it
      closeLatDrawer();
  });
  // Tab buttons: data-metric drives latMetric; "day" is the 12am-11:59pm
  // projection view (latRenderDay).
  el.latToggle.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      latMetric = b.dataset.metric;
      el.latToggle.querySelectorAll("button").forEach((x) =>
        x.classList.toggle("active", x === b));
      latRender();
    });
  });
  // chat search/filter — client-side, filters title + summary
  el.chatSearch.addEventListener("input", () => {
    state.chatFilter = el.chatSearch.value;
    state.sessionSig = "";  // force re-render (filter is part of the sig)
    refreshSessions();
  });
  el.addWorkspace.addEventListener("click", addWorkspace);
  el.themeToggle.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "forest";
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    applyTheme(next);
  });
  // ⋯ menu (V3): the low-frequency review drawers. Toggle on click; close
  // on Escape, outside click, or after an item is chosen.
  el.tbMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    el.tbMenu.hidden = !el.tbMenu.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!el.tbMenu.hidden && !e.target.closest(".tb-menu-wrap"))
      el.tbMenu.hidden = true;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el.tbMenu.hidden) el.tbMenu.hidden = true;
  });
  el.tbMenu.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => { el.tbMenu.hidden = true; });
  });
  el.modeToggle.querySelectorAll(".mode-btn").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.mode === state.mode) return;
      const wasPlan = state.mode === "plan";
      setMode(b.dataset.mode);
      saveMode();
      // Plan → Act: if a plan turn just finished in this chat, execute it
      // automatically — no extra message needed from the user.
      if (wasPlan && state.mode === "act" && state.sessionId && !state.processing)
        executePlan();  // the stashed plan runs directly — no user bubble
    });
  });
  el.roastToggle.querySelectorAll(".mode-btn").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.roast === state.roast) return;
      setRoast(b.dataset.roast);
      saveRoast();
    });
  });
  el.resumeChip.addEventListener("click", () => resume());
  el.abandonChip.addEventListener("click", abandonRun);
  el.previewToggle.addEventListener("click", () => setRightPanelOpen(el.rightPanel.hidden));
  el.rpClose.addEventListener("click", () => setRightPanelOpen(false));
  el.rpTabsUpper.querySelectorAll(".rp-tab").forEach((b) =>
    b.addEventListener("click", () => setUpperTab(b.dataset.ru)));
  el.rpTabsLower.querySelectorAll(".rp-tab").forEach((b) =>
    b.addEventListener("click", () => setLowerTab(b.dataset.rl)));
  el.previewBack.addEventListener("click", () => setUpperTab("files"));
  el.termInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); runUserCommand(); }
  });
  el.rpTreeRefresh.addEventListener("click", () => loadTree());
// 🗂 = open the folder currently shown in File Explorer
el.rpTreeReveal.addEventListener("click", () => {
  revealInExplorer(state.treePath || (state.config && state.config.root_dir));
});
  el.rpTreeHidden.addEventListener("click", () => {
    if (!state.sessionId) return;
    const p = panel(state.sessionId);
    p.showHidden = !p.showHidden;
    syncHiddenToggle();
    loadTree();
  });
  el.rpTreeUp.addEventListener("click", () => {
    if (!state.treePath) return;
    const trimmed = state.treePath.replace(/[\\/]+$/, "");
    const i = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
    if (i <= 2) return;  // top of this drive/path
    treeUpPending = trimmed.slice(0, i);
    history.back();  // the popstate handler renders the parent folder
  });

  // Dropzone: drop a row on it to move the item OUT of the current folder
  // (up one level). Same 409 rename flow as folder-to-folder moves.
  el.rpTreeDrop.addEventListener("dragover", (ev) => {
    if (state.picking || !ev.dataTransfer.types.includes("text/muji-path")) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    el.rpTreeDrop.classList.add("drop-target");
  });
  el.rpTreeDrop.addEventListener("dragleave", () =>
    el.rpTreeDrop.classList.remove("drop-target"));
  el.rpTreeDrop.addEventListener("drop", (ev) => {
    const dragging = ev.dataTransfer.getData("text/muji-path");
    el.rpTreeDrop.classList.remove("drop-target");
    if (!dragging || state.picking) return;
    ev.preventDefault();
    ev.stopPropagation();
    const cur = state.treePath.replace(/[\\/]+$/, "");
    const i = Math.max(cur.lastIndexOf("\\"), cur.lastIndexOf("/"));
    if (i <= 2) return;  // no parent to move up to
    doMove(dragging, cur.slice(0, i));
  });

  // ── Approvals panel (destructive-only) ───────────────────────────
  function aaSummary() {
    return state.autoApprove.destructive
      ? "Approvals: destructive only"
      : "Approvals: off";
  }

  function renderAutoApprove() {
    el.aaLabel.textContent = aaSummary();
    el.aaBox.querySelectorAll("input[data-cat]").forEach((cb) => {
      cb.checked = !!state.autoApprove[cb.dataset.cat];
    });
  }

  async function loadSettings() {
    try {
      const data = await (await api("/api/settings")).json();
      Object.assign(state.autoApprove, data.auto_approve || {});
    } catch (e) { /* keep defaults */ }
    renderAutoApprove();
  }

  async function saveAutoApprove(cat, value) {
    state.autoApprove[cat] = value;
    renderAutoApprove();
    try {
      const data = await (await api("/api/settings", {
        method: "POST", body: JSON.stringify({ auto_approve: { [cat]: value } }),
      })).json();
      Object.assign(state.autoApprove, data.auto_approve || {});
    } catch (e) { /* reload from server on next boot */ }
  }

  // ── Server restart (one-click, guaranteed reload of the latest code) ──────
  let restarting = false;
  let restartOverlay = null;
  function showRestartOverlay(msg, failed) {
    if (!restartOverlay) {
      const st = document.createElement("style");
      st.textContent =
        ".restart-overlay{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
        "background:rgba(9,11,15,.93);backdrop-filter:blur(3px);color:#e8e8e8;font-family:system-ui,sans-serif;}" +
        ".restart-box{text-align:center;max-width:440px;padding:0 24px;}" +
        ".restart-spin{font-size:44px;line-height:1;margin-bottom:16px;display:inline-block;animation:muji-spin 1s linear infinite;}" +
        ".restart-msg{font-size:15px;line-height:1.55;white-space:pre-wrap;}" +
        ".restart-overlay.failed .restart-spin{animation:none;opacity:.5;}" +
        "@keyframes muji-spin{to{transform:rotate(360deg)}}";
      document.head.appendChild(st);
      restartOverlay = document.createElement("div");
      restartOverlay.className = "restart-overlay";
      restartOverlay.innerHTML =
        '<div class="restart-box"><span class="restart-spin">⟳</span><div class="restart-msg"></div></div>';
      document.body.appendChild(restartOverlay);
    }
    restartOverlay.querySelector(".restart-msg").textContent = msg;
    restartOverlay.classList.toggle("failed", !!failed);
  }

  async function restartServer() {
    if (restarting) return;
    if (!confirm("Restart the muji server? This reloads the latest code and stops any in-flight chat turn. The page reloads automatically once the server is back up.")) return;
    restarting = true;
    showRestartOverlay("Restarting server…");
    // parent_boot_token = the boot we just asked to die. The reload must wait
    // for a FRESH boot_token, not the first 200: the old code reloaded on any
    // live /api/sessions, which the DYING parent (still serving for ~0.5s) or
    // a sibling checkout on the same port can answer — so the page could come
    // back running the very code we just tried to replace.
    let parentToken = null;
    try {
      const r = await fetch("/api/restart", { method: "POST" });
      if (r.ok) parentToken = (await r.json()).parent_boot_token || null;
    } catch (e) { /* the server exits before it can always reply — expected */ }
    const t0 = Date.now();
    const tick = async () => {
      try {
        const r = await fetch("/api/health", { cache: "no-store" });
        if (r.ok) {
          const h = await r.json();
          if (!parentToken || (h.boot_token && h.boot_token !== parentToken)) { location.reload(); return; }
        }
      } catch (e) { /* still down */ }
      if (Date.now() - t0 > 30000) {
        showRestartOverlay("The server didn't come back within 30s. Reload the page to reconnect — or run python server.py again if it's not starting.", true);
        restarting = false;
        return;
      }
      setTimeout(tick, 800);
    };
    setTimeout(tick, 1200);
  }

  el.restartServer.addEventListener("click", restartServer);

  el.aaToggle.addEventListener("click", () => {
    el.aaBody.hidden = !el.aaBody.hidden;
    el.aaBox.classList.toggle("open", !el.aaBody.hidden);
  });
  el.aaBox.querySelectorAll("input[data-cat]").forEach((cb) => {
    cb.addEventListener("change", () => saveAutoApprove(cb.dataset.cat, cb.checked));
  });
  el.sidebarClose.addEventListener("click", () => {
    el.sidebar.classList.add("collapsed");
    el.sidebarOpen.hidden = false;
    updatePanelOverlay();
  });
  el.sidebarOpen.addEventListener("click", () => {
    el.sidebar.classList.remove("collapsed");
    el.sidebarOpen.hidden = true;
    updatePanelOverlay();
  });
  el.chatScroll.addEventListener("scroll", () => {
    const sc = el.chatScroll;
    const gap = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
    state.userScrolledUp = gap > 60;
    // Floating "scroll to bottom": show only when there's a real gap
    // (not at the bottom AND not a non-scrollable short chat)
    el.scrollBottomBtn.hidden = gap <= 60 || sc.scrollHeight <= sc.clientHeight + 4;
  });
  el.scrollBottomBtn.addEventListener("click", () => {
    state.userScrolledUp = false;
    el.chatScroll.scrollTo({ top: el.chatScroll.scrollHeight, behavior: "smooth" });
    el.scrollBottomBtn.hidden = true;  // hide immediately; the scroll event re-evaluates
  });
  document.addEventListener("dragover", (ev) => ev.preventDefault());
  document.addEventListener("drop", (ev) => {
    ev.preventDefault();
    if (ev.dataTransfer.files.length) uploadFile(ev.dataTransfer.files[0]);
  });

  // ── PWA: service worker registration ─────────────────────────────
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/static/sw.js", { scope: "/" }).catch(() => {});
    });
    // The SW posts "muji-update" when it detects a newer shell deploy on
    // navigation — this page is still running the stale app.js. The banner
    // is PERSISTENT (no auto-dismiss): a 15s toast got missed on the PWA
    // and the app kept running a pre-fix shell — that's how Boss's PWA
    // stayed on 2-tap sidebar code long after the 1-tap fix shipped.
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data === "muji-update" && !window.__mujiUpdateNotified) {
        window.__mujiUpdateNotified = true;
        const b = document.createElement("div");
        b.id = "ui-update-banner";
        b.style.cssText =
          "position:fixed;bottom:14px;left:50%;transform:translateX(-50%);" +
          "z-index:10001;display:flex;gap:10px;align-items:center;" +
          "background:var(--panel-2,#222);color:var(--text,#eee);" +
          "border:1px solid var(--accent,#4a9);border-radius:10px;" +
          "padding:8px 12px;font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.25);" +
          "max-width:92vw;";
        const s = document.createElement("span");
        s.textContent = "New muji UI available";
        const btn = document.createElement("button");
        btn.className = "btn primary"; btn.textContent = "Reload";
        btn.style.margin = "0";
        btn.onclick = () => location.reload();
        b.append(s, btn);
        document.body.appendChild(b);
      }
    });
  }

  // ── PWA: offline banner ──────────────────────────────────────────
  (function offlineBanner() {
    const el = document.getElementById("offline-banner");
    if (!el) return;
    let timer = null;

    function check() {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 3000);
      fetch("/api/health", { signal: ctrl.signal, cache: "no-store" })
        .then((r) => {
          clearTimeout(t);
          el.hidden = true;
          if (timer) { clearInterval(timer); timer = null; }
        })
        .catch(() => {
          clearTimeout(t);
          el.hidden = false;
          if (!timer) timer = setInterval(check, 15000);
        });
    }

    window.addEventListener("online", check);
    window.addEventListener("offline", () => { el.hidden = false; });
    check(); // initial
  })();

  boot().catch(() => {}).finally(hideSplash);
  // Safety: even if boot throws before the .finally path (e.g. a
  // top-level error in an earlier listener), never leave the splash
  // covering the app forever.
  setTimeout(hideSplash, 4000);
  function hideSplash() {
    const s = document.getElementById("splash");
    if (!s || s.classList.contains("gone")) return;
    s.classList.add("gone");
    setTimeout(() => s.remove(), 250);
  }
})();

// ── Taskbar safe zone (browser F11 fullscreen) ───────────────────────
// In F11 fullscreen the CSS viewport extends to the physical screen
// edge — under the Windows taskbar — so a 100vh app puts its composer +
// newest response in the covered strip (the reported bug).
//
// v1 (2cd1423) applied the inset when "fullscreen" was detected via
// window.outerHeight ≥ screen.height. Measured live in Edge F11 (CDP):
// outerHeight reports 850 in F11 on this 864px screen (816 maximized)
// — the heuristic NEVER fires, the inset stays 0px, bug persists.
// (F11 is browser-window fullscreen, not the Fullscreen API:
// document.fullscreenElement stays null.)
//
// v2 tried pure CSS 100dvh. Measured: in Edge F11 dvh == vh == the
// full physical screen (864px) — the taskbar is not "browser UI" as
// far as dvh is concerned, so it did not lift the app either.
//
// v3 = robust detection + periodic re-measure. "Covered" = the CSS
// viewport actually spans the whole physical screen (innerHeight ≥
// screen.height — true in F11: 864 ≥ 864, false maximized: 732,
// false normal), or the Fullscreen API is active, or outerHeight ≥
// screen.height (kept for browser variants). When covered, inset =
// screen.height − screen.availHeight (the taskbar strip; 0 when the
// taskbar is auto-hidden). A 1 s interval re-applies, so the state is
// corrected no matter how it was reached (F11 before page load, missed
// events, taskbar shown/hidden under the window). keepPinned re-pins
// a bottom-scrolled chat after the app shrinks.
(() => {
  const taskbarH = () => Math.max(0, window.screen.height - window.screen.availHeight);
  // Desktop-only: on a phone, innerHeight == screen.height is the NORMAL
  // state (no taskbar exists), so the old heuristic subtracted the
  // phone's own "taskbar strip" (screen.height - availHeight, e.g. 34px)
  // and left the composer floating above the real bottom of the screen.
  // The taskbar is a Windows-desktop concept — coarse pointers skip it.
  const covered = () =>
    window.matchMedia("(pointer: coarse)").matches ? false :
    !!document.fullscreenElement ||
    window.innerHeight >= window.screen.height ||
    window.outerHeight >= window.screen.height;
  let prev = -1;
  function keepPinned() {
    const sc = document.querySelector(".chat-scroll");
    if (!sc) return;
    if (sc.scrollHeight - sc.scrollTop - sc.clientHeight < 60) sc.scrollTop = sc.scrollHeight;
  }
  function apply() {
    const cover = covered() ? taskbarH() : 0;
    if (cover === prev) return;
    document.documentElement.style.setProperty("--taskbar-inset", cover + "px");
    if (cover > prev) setTimeout(keepPinned, 200);
    prev = cover;
  }
  window.addEventListener("resize", apply);
  document.addEventListener("fullscreenchange", apply);
  window.addEventListener("screenchange", apply);
  window.setInterval(apply, 1000);  // catch state changes no event reports
  apply();
})();
