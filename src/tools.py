"""Agent tools: filesystem, shell (approval-gated), web search/fetch."""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import fnmatch
import html as htmllib
import json
import os
import re
import subprocess
import urllib.parse
import uuid
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .config import settings, APP_ROOT
from . import db
from . import gmail as gmail_mod
from . import gtasks as gtasks
from . import taskstore

MAX_READ = 100_000      # bytes readable in one read_file call
MAX_WRITE = 500_000     # bytes writable in one write_file call
MAX_OUTPUT = 20_000     # tool output cap fed back to the model
MAX_SEARCH = 50         # default max search hits
RESULT_PREVIEW = 800    # chars of a spilled result shown inline (the rest is on disk; was 2000 — token diet 2026-11-08)


class ToolError(Exception):
    pass


class NeedsScope(ToolError):
    """A tool reached a path in a gated scope: 'self' (muji's own repo — the
    Self-edit tick) or 'outside' (everything else — the Work-outside-root
    tick). Carries the resolved path + scope so the agent can raise the
    matching approval and retry with it granted."""

    def __init__(self, path: Path, scope: str):
        self.path = path
        self.scope = scope
        super().__init__(f"path in {scope} scope: {path}")


@dataclasses.dataclass
class ToolCtx:
    """Per-run context shared by all tool calls."""
    cwd: Path
    sources: list = dataclasses.field(default_factory=list)    # grounding text
    generated: list = dataclasses.field(default_factory=list)  # files written
    scope_approved: set = dataclasses.field(default_factory=set)  # gated-scope paths (self/outside) granted this run
    session_id: str = ""  # this run's chat — lets tools address "the current conversation" (search_transcript)


# ── path safety ─────────────────────────────────────────────────────

def _under_root(p: Path) -> bool:
    root = settings.root_dir
    return p == root or root in p.parents


def _under_app(p: Path) -> bool:
    """True if *p* sits under the app's own repo (APP_ROOT — muji's home)."""
    return p == APP_ROOT or APP_ROOT in p.parents


def _scope(p: Path) -> str:
    """Which access scope a path falls into (checked in this order):

    - "self"    — under muji's repo (APP_ROOT); gated by the Self-edit tick.
      Checked FIRST so an over-broad root (e.g. the home dir) can't swallow the
      repo and silently auto-approve self-edits.
    - "free"    — under the user root (settings.root_dir); read/edit ticks apply.
    - "outside" — anything else; gated by the Work-outside-root tick.
    """
    if _under_app(p):
        return "self"
    if _under_root(p):
        return "free"
    return "outside"


def resolve_path(ctx: ToolCtx, path: str) -> Path:
    """Resolve a model-supplied path, gated by access scope.

    "free" (under the user root) always passes. "self" (muji's repo) and
    "outside" (everything else) need a per-run grant: if the boss allowed that
    scope this run, the agent adds the path to ctx.scope_approved and retries;
    otherwise this raises NeedsScope and the agent turns it into the matching
    approval card (Self-edit / Work outside root)."""
    raw = (path or "").strip()
    if not raw:
        raise ToolError("empty path")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(os.path.expanduser(raw)) if raw.startswith("~") else ctx.cwd / p
    rp = p.resolve()
    if _scope(rp) == "free" or rp in ctx.scope_approved:
        return rp
    raise NeedsScope(rp, _scope(rp))


def preview_token(path: Path) -> str:
    return base64.urlsafe_b64encode(str(path).encode("utf-8")).decode().rstrip("=")


def decode_token(token: str) -> Path | None:
    try:
        pad = "=" * (-len(token) % 4)
        p = Path(base64.urlsafe_b64decode(token + pad).decode("utf-8")).resolve()
    except Exception:
        return None
    # a preview token may point at any file: the chat's working folder can be
    # outside the agent root, and tokens are only ever minted by this server
    return p


def _nb_cell_html(cell: dict) -> str:
    """Render one notebook cell as static HTML (source + outputs)."""
    src = cell.get("source") or ""
    if isinstance(src, list):
        # each element already ends in "\n" (except maybe the last)
        src = "".join(src)
    kind = cell.get("cell_type", "code")
    parts = [f'<div class="nb-cell nb-{kind}">']
    if kind == "markdown":
        parts.append(f'<div class="nb-md">{md_to_html(src)}</div>')
    else:
        if kind == "code":
            ec = cell.get("execution_count")
            badge = f'<span class="nb-ec">In [{ec if ec else " "}]</span>'
            parts.append(f'<div class="nb-code">{badge}<pre><code>'
                         + _code_lines_html(src.rstrip("\n")) + "</code></pre></div>")
            for out in cell.get("outputs") or []:
                ot = out.get("output_type")
                if ot in ("stream", "display_data", "execute_result"):
                    # images first (matplotlib figures are display_data
                    # with image/png only — no text/plain)
                    if ot in ("display_data", "execute_result"):
                        data = out.get("data") or {}
                        for mime in ("image/png", "image/jpeg", "image/svg+xml", "image/gif"):
                            if mime in data:
                                b64 = "".join(data[mime]) if isinstance(data[mime], list) else data[mime]
                                parts.append(f'<div class="nb-out"><img src="data:{mime};base64,{b64}" alt="figure"></div>')
                                break
                    text = out.get("text")
                    if text is None and ot in ("display_data", "execute_result"):
                        text = (out.get("data") or {}).get("text/plain")
                    if isinstance(text, list):
                        text = "".join(text)
                    if text:
                        cls = "nb-out nb-err" if (ot == "stream" and out.get("name") == "stderr") else "nb-out"
                        parts.append(f'<div class="{cls}"><pre><code>'
                                     + _code_lines_html(text.rstrip("\n")) + "</code></pre></div>")
                elif ot == "error":
                    msg = "\n".join(out.get("traceback") or [out.get("ename", "error")])
                    parts.append(f'<div class="nb-out nb-err"><pre><code>'
                                 + _code_lines_html(msg.rstrip("\n")) + "</code></pre></div>")
    parts.append("</div>")
    return "".join(parts)


_MD_LINK_SAFE = re.compile(
    r"^(?:https?://|mailto:|/|[A-Za-z0-9._~-][^\s:]*[\\/][^\s:]*|[A-Za-z]:[\\/])",
    re.I)


def _md_inline(s: str) -> str:
    """Inline markdown → HTML on already-escaped text: `code`, **bold**,
    *italic*, ~~strike~~, [text](url), ![alt](url), bare autolinks.
    Code spans are masked first so their content is never re-processed
    (a `*` inside `code` stays literal)."""
    codes = []

    def _stash(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", _stash, s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"~~([^~\n]+)~~", r"<del>\1</del>", s)

    def _link(m):
        text, url = m.group(1), m.group(2)
        if not _MD_LINK_SAFE.match(url):
            return f"{text}({htmllib.escape(url)})"
        return f'<a href="{htmllib.escape(url, quote=True)}">{text}</a>'

    def _image(m):
        alt, url = m.group(1), m.group(2)
        if not _MD_LINK_SAFE.match(url):
            return f"[{alt}]({htmllib.escape(url)})"
        return (f'<img src="{htmllib.escape(url, quote=True)}" '
                f'alt="{htmllib.escape(alt, quote=True)}">')

    tags = []

    def _stash_tag(html):
        tags.append(html)
        return f"\x01{len(tags) - 1}\x01"

    s = re.sub(r"!\[([^\]\n]*)\]\(([^)\s]+)\)",
               lambda m: _stash_tag(_image(m)), s)
    s = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)",
               lambda m: _stash_tag(_link(m)), s)
    # bare autolinks (https?://…) — tags are stashed, so nothing already
    # rendered can be double-linked
    s = re.sub(r"(https?://[^\s\x00\x01<>\"']+)",
               lambda m: _stash_tag(
                   f'<a href="{htmllib.escape(m.group(1), quote=True)}">'
                   f"{m.group(1)}</a>"), s)
    s = re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], s)
    s = re.sub("\x01(\\d+)\x01", lambda m: tags[int(m.group(1))], s)
    return s


def _md_table_html(header: str, delim: str, rows: list) -> str:
    """Pipe table (GFM-style). Alignment from the delimiter row."""
    def cells(line):
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def aligns(d):
        out = []
        for c in cells(d):
            l, r = c.startswith(":"), c.endswith(":")
            out.append("center" if l and r else "right" if r else "left" if l else "")
        return out

    def tds(line, align):
        cs = cells(line)
        return "".join(
            f'<td{" align=" + a if a else ""}>{_md_inline(htmllib.escape(c))}</td>'
            for c, a in zip(cs, align))

    a = aligns(delim)
    hcs = cells(header)
    ths = "".join(
        f'<th{" align=" + al if al else ""}>{_md_inline(htmllib.escape(c))}</th>'
        for c, al in zip(hcs, a))
    body = "".join(f"<tr>{tds(r, a)}</tr>" for r in rows if r.strip())
    return (f'<table><thead><tr>{ths}</tr></thead>'
            + (f"<tbody>{body}</tbody>" if body else "") + "</table>")


def md_to_html(src: str) -> str:
    """Minimal markdown → HTML for notebook cells (no deps — the runtime
    has no markdown lib). Supports: # and setext headers, **bold**,
    *italic*, ~~strike~~, `code`, [text](url), ![alt](url), autolinks,
    ``` fenced code, - / 1. lists (nested by indent), pipe tables,
    > blockquotes, --- rules. Anything fancier degrades to readable
    plain text, never to raw markdown or broken HTML."""
    esc = htmllib.escape
    lines = src.replace("\r\n", "\n").split("\n")
    out, para, code, fence = [], [], None, None

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + "<br>".join(_md_inline(esc(p)) for p in para) + "</p>")
            para = []

    def is_table_delim(s):
        t = s.strip()
        return ("|" in t and set(t) <= set("|-: ")
                and re.search(r"-{1,}", t) is not None)

    i = 0
    while i < len(lines):
        raw = lines[i]
        if code is not None:
            if raw.lstrip().startswith(fence):
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(raw)
            i += 1
            continue
        f3, f4 = raw.lstrip().startswith("```"), raw.lstrip().startswith("~~~~")
        if f3 or f4:
            flush_para()
            code, fence = [], "```" if f3 else "~~~~"
            i += 1
            continue
        # setext headers (text line + === / --- underline) — check before hr
        lvl = None
        if i > 0 and para and not lines[i - 1].strip().startswith(("```", "~~~")):
            if re.match(r"^\s{0,3}=+\s*$", raw):
                lvl = 1
            elif (re.match(r"^\s{0,3}-+\s*$", raw)
                    and not re.match(r"^\s*[-*+]\s", lines[i - 1])):
                lvl = 2
        if lvl:
            text, para = para[-1], para[:-1]
            out.append(f"<h{lvl}>{_md_inline(esc(text.strip()))}</h{lvl}>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if m:
            flush_para()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_md_inline(esc(m.group(2).strip()))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$", raw):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        if raw.lstrip().startswith(">"):
            flush_para()
            out.append("<blockquote>" + _md_inline(esc(raw.lstrip()[1:].lstrip()))
                       + "</blockquote>")
            i += 1
            continue
        # pipe table: a | line followed by a |---| delimiter
        if ("|" in raw and raw.strip() and i + 1 < len(lines)
                and is_table_delim(lines[i + 1])):
            flush_para()
            rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(lines[j])
                j += 1
            out.append(_md_table_html(raw, lines[i + 1], rows))
            i = j
            continue
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", raw)
        if m:
            flush_para()
            # collect the nested list block (indent-based)
            block = []
            j = i
            while j < len(lines):
                lm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[j])
                if lm:
                    ind, ordered = len(lm.group(1)), lm.group(2)[0].isdigit()
                    if block and ind == block[-1][0] and ordered != block[-1][1]:
                        break  # marker type flip at same indent = new list
                    block.append((ind, ordered,
                                  _md_inline(esc(lm.group(3)))))
                    j += 1
                elif lines[j].strip():
                    break  # non-list text ends the block
                else:
                    # blank: keep block alive only if a list item follows
                    k = j
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[k]):
                        j = k
                    else:
                        break
            # build a tree: node = {"ind", "ordered", "items": [(text, [children])]}.
            # A deeper indent nests under the PREVIOUS item of the parent list.
            tops, stack = [], []

            def new_node(ind, ordered):
                return {"ind": ind, "ordered": ordered, "items": []}

            for ind, ordered, item in block:
                while stack and stack[-1][0] > ind:
                    stack.pop()
                if not stack:
                    node = tops[-1] if (tops and ind == tops[-1]["ind"]) \
                        else new_node(ind, ordered)
                    if node not in tops:
                        tops.append(node)
                    stack.append((ind, node))
                elif stack[-1][0] == ind:
                    node = stack[-1][1]
                else:  # deeper indent → child list under the last item
                    child = new_node(ind, ordered)
                    stack[-1][1]["items"][-1][1].append(child)
                    stack.append((ind, child))
                    node = child
                node["items"].append((item, []))

            def render_node(n):
                tag = "ol" if n["ordered"] else "ul"
                lis = "".join(
                    f"<li>{t}" + "".join(render_node(c) for c in ch) + "</li>"
                    for t, ch in n["items"])
                return f"<{tag}>{lis}</{tag}>"

            out.append("".join(render_node(t) for t in tops))
            i = j
            continue
        if not raw.strip():
            flush_para()
            i += 1
            continue
        para.append(raw.strip())
        i += 1
    if code is not None:  # unclosed fence — render what we have
        out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush_para()
    return "".join(out)


def _code_lines_html(text: str) -> str:
    """One flex row per source line: number gutter + code that wraps under itself.
    Spans (not divs) — the rows live inside <pre>, which only allows phrasing content."""
    return "".join(
        f'<span class="cl"><span class="ln">{i + 1}</span>'
        f'<span class="ct">{htmllib.escape(line) if line else "&nbsp;"}</span></span>'
        for i, line in enumerate(text.split("\n")))


def notebook_to_html(text: str) -> str:
    """Render a .ipynb file as a static HTML document (no JS, no kernel).

    `text` must be the COMPLETE file — truncating a notebook mid-JSON makes
    even valid files fail to parse (big embedded images push them past any
    byte cap), so the caller must not slice it.
    """
    try:
        nb = json.loads(text)
    except Exception as e:
        return ("<pre>Not a valid notebook (JSON parse failed): "
                + htmllib.escape(str(e))
                + "<br><br>The file may be corrupt or an incomplete sync "
                  "copy. Open it in VS Code / Jupyter to confirm.</pre>")
    md = nb.get("metadata") or {}
    kernel = (md.get("kernelspec") or {}).get("display_name") or "Python"
    parts = [f'<div class="nb-doc"><div class="nb-kernel">Kernel: {htmllib.escape(kernel)}</div>']
    for cell in nb.get("cells") or []:
        parts.append(_nb_cell_html(cell))
    parts.append("</div>")
    return "".join(parts)


def file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".ipynb":
        return "ipynb"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"):
        return "image"
    if ext in (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".css", ".scss",
               ".sh", ".ps1", ".bat", ".cmd", ".yaml", ".yml", ".toml", ".ini",
               ".cfg", ".conf", ".xml", ".sql", ".c", ".h", ".cpp", ".hpp", ".rs",
               ".go", ".java", ".rb", ".php", ".lua", ".env", ".txt", ".csv",
               ".tsv", ".log", ".svelte", ".vue"):
        return "code"
    return "text"


def register_generated(ctx: ToolCtx, path: Path) -> dict:
    kind = file_kind(path.name)
    entry = {
        "name": path.name,
        "path": str(path),
        "kind": kind,
        "size": path.stat().st_size if path.exists() else 0,
        "preview_url": "/preview/" + preview_token(path),
        "url": None,
    }
    if settings.uploads_dir in path.parents:
        entry["url"] = "/uploads/" + path.name
    ctx.generated = [g for g in ctx.generated if g["path"] != entry["path"]]
    ctx.generated.append(entry)
    return entry


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# ── filesystem tools ────────────────────────────────────────────────

def t_list_dir(ctx: ToolCtx, path: str = ".") -> str:
    p = resolve_path(ctx, path)
    if not p.is_dir():
        raise ToolError(f"not a directory: {path}")
    entries = []
    try:
        children = sorted(p.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
    except OSError as e:
        raise ToolError(f"cannot list {p}: {e}")
    for c in children[:400]:
        try:
            if c.is_dir():
                entries.append(f"{c.name}/")
            else:
                entries.append(f"{c.name}  ({fmt_size(c.stat().st_size)})")
        except OSError:
            entries.append(f"{c.name}  (?)")
    if not entries:
        return f"(empty directory: {p})"
    more = "" if len(children) <= 400 else f"\n… and {len(children) - 400} more"
    return f"Contents of {p}:\n" + "\n".join(entries) + more


def t_read_file(ctx: ToolCtx, path: str, start_line: int | None = None,
                end_line: int | None = None) -> str:
    p = resolve_path(ctx, path)
    if not p.is_file():
        raise ToolError(f"no such file: {path}")
    data = p.read_bytes()
    if len(data) > MAX_READ and not (start_line or end_line):
        raise ToolError(
            f"file is {len(data)} bytes (max {MAX_READ} at once) — use "
            f"start_line/end_line or search_files to find the part you need")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    s = max(1, int(start_line or 1))
    e = min(len(lines), int(end_line or len(lines)))
    sel = lines[s - 1:e]
    head = f"Lines {s}–{e} of {len(lines)} in {p}:"
    if s > 1:
        head = f"[… {s - 1} lines before]\n" + head
    tail = f"\n[… {len(lines) - e} more lines after line {e}]" if e < len(lines) else ""
    return head + "\n" + "\n".join(sel) + tail


def t_write_file(ctx: ToolCtx, path: str, content: str) -> str:
    p = resolve_path(ctx, path)
    if len(content.encode("utf-8")) > MAX_WRITE:
        raise ToolError("content too large to write in one call")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")
    register_generated(ctx, p)
    return (f"Wrote {len(content.splitlines())} lines "
            f"({len(content.encode('utf-8'))} bytes) to {p}")


def t_edit_file(ctx: ToolCtx, path: str, old_text: str, new_text: str) -> str:
    p = resolve_path(ctx, path)
    if not p.is_file():
        raise ToolError(f"no such file: {path}")
    text = p.read_text("utf-8", errors="replace")
    n = text.count(old_text)
    if n == 0:
        raise ToolError("old_text not found — it must match the file exactly "
                        "(including whitespace)")
    if n > 1:
        raise ToolError(f"old_text matches {n} places; include more context around it")
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8", newline="\n")
    register_generated(ctx, p)
    return f"Edited {p} (1 replacement)"


# ── search ───────────────────────────────────────────────────────────

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea",
             ".vscode", "dist", "build", ".next", "target", ".cache"}


def t_search_files(ctx: ToolCtx, pattern: str, path: str = ".", glob: str = "*",
                   max_results: int = MAX_SEARCH) -> str:
    base = resolve_path(ctx, path)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"bad regex: {e}")
    hits: list[str] = []
    scanned = 0
    stop = False

    def walk(d: Path) -> None:
        nonlocal scanned, stop
        if stop:
            return
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for c in children:
            if stop:
                return
            if c.is_dir():
                if c.name in SKIP_DIRS:
                    continue
                walk(c)
            else:
                if not fnmatch.fnmatch(c.name.lower(), glob.lower()):
                    continue
                try:
                    if c.stat().st_size > 2_000_000:
                        continue
                    text = c.read_text("utf-8", errors="replace")
                except OSError:
                    continue
                scanned += 1
                rel = c.relative_to(settings.root_dir) if _under_root(c) else c.name
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            stop = True
                            return

    if base.is_dir():
        walk(base)
    elif base.is_file():
        try:
            text = base.read_text("utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"cannot read {base}: {e}")
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{base.name}:{i}: {line.strip()[:200]}")
    if not hits:
        return f"no matches for /{pattern}/ ({scanned} files scanned)"
    out = "\n".join(hits)
    return (f"{len(hits)} matches (in {scanned} files):\n{out}"
            + ("  [truncated]" if stop else ""))


# ── shell: approval policy + execution ─────────────────────────────

SAFE_FIRST = {
    "dir", "ls", "gci", "get-childitem", "cat", "type", "gc", "get-content",
    "where", "whoami", "hostname", "get-date", "get-item", "gi", "test-path",
    "pwd", "get-location", "gl", "echo", "write-output", "write-host",
    "get-alias", "get-command", "get-variable", "get-environmentvariable",
    "get-process", "gps", "get-service", "get-culture", "get-history",
    "get-eventlog", "get-counter", "get-wmiobject", "diff",
}
SAFE_PIPE = {
    "select-object", "select", "where-object", "where", "sort-object", "sort",
    "measure-object", "measure", "format-table", "format-list", "format-wide",
    "select-string", "findstr", "out-string", "compare-object", "diff",
}
SAFE_PREFIXES = (
    "python --version", "python -v", "python -m pip list", "python -m pip show",
    "python -m pip freeze", "node --version", "node -v", "git status",
    "git log", "git diff", "git branch", "git remote", "git show",
    "git rev-parse", "git ls-files", "git tag", "git shortlog", "git config --get",
)
DANGEROUS = re.compile(
    r"(?i)(\brm\b|\bdel\b|\bdelm\b|\berase\b|remove-item\b|\bri\b|rmdir\b|\brd\b|"
    r"move-item\b|\bmi\b|copy-item\b|\bci\b|\bnew-item\b|\bni\b|rename-item\b|\bren\b|"
    r"set-content\b|add-content\b|clear-content\b|set-itemproperty\b|"
    r"remove-itemproperty\b|set-item\b|out-file\b|\bof\b|start-process\b|\bstart\b|"
    r"invoke-expression\b|\biex\b|invoke-webrequest\b|\biwr\b|invoke-restmethod\b|\birm\b|"
    r"taskkill\b|stop-process\b|stop-service\b|stop-computer\b|\bshutdown\b|"
    r"restart-computer\b|format-volume\b|diskpart\b|set-executionpolicy\b|"
    r"install-module\b|install-package\b|setx\b|"
    r"pip\s+install|pip\s+uninstall|pip\s+download|"
    r"npm\s+install|npm\s+ci\b|npm\s+publish|npm\s+deprecate|"
    r"git\s+push|git\s+reset|git\s+clean|git\s+checkout|git\s+rebase|git\s+merge|"
    r"git\s+commit|git\s+restore|git\s+switch|git\s+stash|"
    r"\bcurl\b|\bwget\b|python\s+-c\b|python\s+-m\s+(?!pip\s+(list|show|freeze)\b)|"
    r"node\s+-e\b|powershell\b|pwsh\b|cmd\.exe|cmd\s+/c|\bcall\b|\bexec\b|"
    r">\s|\|>\s|>>\s|\|\s*sh\b|\|\s*bash\b)")


def decide_command(command: str) -> tuple[bool, str]:
    """Return (auto_ok, reason). Read-only PowerShell runs without approval."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "empty command"
    if DANGEROUS.search(cmd):
        return False, "not read-only (matches a mutating pattern)"
    if re.search(r"\|\||&&|;", cmd):
        for seg in re.split(r"\|\||&&|;", cmd):
            ok, why = _segment_ok(seg)
            if not ok:
                return False, f"segment not read-only: {seg.strip()[:80]}"
        return True, "all segments read-only"
    return _segment_ok(cmd)


def _segment_ok(seg: str) -> tuple[bool, str]:
    seg = seg.strip()
    if not seg:
        return True, ""
    low = seg.lower()
    for pre in SAFE_PREFIXES:
        if low.startswith(pre):
            return True, "allowlisted read-only command"
    parts = [p.strip() for p in seg.split("|") if p.strip()]
    for i, part in enumerate(parts):
        first = re.split(r"\s+", part, 1)[0].strip('"')
        if i == 0:
            if first not in SAFE_FIRST:
                return False, f"'{first}' is not an allowlisted read-only command"
        elif first not in SAFE_PIPE:
            return False, f"pipe target '{first}' is not read-only"
    return True, "read-only command"


#: Shell operations that destroy or relocate data — the ONLY run_command
#: actions that prompt for approval. Read, edit, create, copy and
#: non-destructive commands run without asking. Whole-string match, so nested
#: forms (e.g. `powershell -c "Remove-Item …"`) are caught too.
DESTRUCTIVE = re.compile(
    r"(?i)(?:"
    # delete / remove (files, folders, or their contents)
    r"\bremove-item\b|\brm(?=\s|$)|\bdel(?=\s|$)|\bdelm(?=\s|$)|"
    r"\berase(?=\s|$)|\brd(?=\s|$)|\brmdir(?=\s|$)|\bclear-content\b|"
    r"\bremove-itemproperty\b|\bclear-recyclebin\b|\bshred(?=\s|$)|\bsdelete(?=\s|$)|"
    # rename / move (changes a file's name or location)
    r"\brename-item\b|\bren(?=\s|$)|\bmove-item\b|\bmv(?=\s|$)|"
    # similar irreversible operations
    r"\btruncate(?=\s|$)|\bformat-volume\b|\bdiskpart\b|"
    r"git\s+clean\b|git\s+reset\s+--hard\b|git\s+push\s+(?:-f\b|--force\b)|"
    r"shutil\.rmtree|"
    # credential-store writes/deletes (2026-11-08: the agent overwrote the
    # git:https://github.com entry — the one EVERY git push uses — with
    # placeholder text via `cmdkey /generic`, no approval asked)
    r"\bcmdkey\b[^\n]*?/(?:add|generic|delete|pass)\b|"
    r"git\s+credential(?:-manager)?\s+(?:approve|reject|store|erase)\b|"
    r"\b(?:remove|new)-storedcredential\b"
    r")"
)


def destructive_command(command: str) -> tuple[bool, str]:
    """Return (is_destructive, reason). A run_command is destructive — and so
    prompts for approval — only if it deletes, removes, renames, or moves data,
    or does a similar irreversible thing. Everything else runs automatically."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "empty command"
    m = DESTRUCTIVE.search(cmd)
    if not m:
        return False, ""
    return True, (f"destructive operation: {m.group(0).strip()!r} "
                  "(delete / remove / rename / move / similar)")


def restart_command(command: str) -> tuple[bool, str]:
    """Return (touches_server_lifecycle, reason). A run_command starts,
    stops, or restarts muji's own server. Hard rule (narrowed, ratified
    2026-09-24): the clean /api/restart handoff may run WITHOUT approval
    only when no other session has in-flight work (the agent checks both);
    every other server-lifecycle command (raw kill of :8321,
    restart-muji.ps1, start.bat, `python server.py`, or /api/restart while
    other runs are live) ALWAYS asks — separate brake from DESTRUCTIVE, the
    auto-approve panel cannot waive it. Whole-string match, so nested forms
    (e.g. `powershell -c "Invoke-RestMethod ... /api/restart"`) are caught.
    Diagnostic reads of port 8321 without a kill/start verb (e.g.
    `Get-NetTCPConnection ... | Select OwningProcess`) pass freely."""
    cmd = (command or "").strip()
    if not cmd:
        return False, "empty command"
    low = cmd.lower()
    if "restart-muji" in low or "restart_muji" in low:
        return True, "server restart: runs restart-muji.ps1 (kills :8321, starts the server)"
    if re.search(r"\bstart\.bat\b", low):
        return True, "server start: runs start.bat"
    if "api/restart" in low:
        return True, "server restart: calls the /api/restart endpoint"
    if re.search(r"\bserver\.py\b", low):
        return True, "server lifecycle: runs/stops server.py"
    if "8321" in low and re.search(r"\bstop-process\b|\btaskkill\b|\bkill\b", low):
        return True, "server stop: kills the process listening on :8321"
    return False, ""


#: Secret shapes scrubbed from every command output before it reaches the
#: model, the DB, the UI or the terminal log (2026-11-08: `git credential
#: fill` printed the GitHub token in clear; it was stored, rendered, sent to
#: the provider and pasted into ~30 commands).
_SECRET_RES = [
    re.compile(r"\b(gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(gsk_[A-Za-z0-9]{20,})"),                    # Groq
    re.compile(r"\b(sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,})"),    # OpenAI / Anthropic
    re.compile(r"\b(tvly-[A-Za-z0-9-]{16,})"),                  # Tavily
    re.compile(r"\b(AKIA[0-9A-Z]{16})"),                        # AWS key id
    re.compile(r"(?im)^(\s*password\s*=\s*)(\S+)"),             # git credential fill
    re.compile(r"(https?://[^/\s:@]+:)([^@\s/]{8,})(@)"),       # https://user:token@host
]
_KNOWN_SECRETS: set[str] = set()


def redact_secrets(text: str) -> str:
    """Replace anything secret-shaped (and any secret muji itself injected)
    with «redacted». Never raises."""
    try:
        for s in _KNOWN_SECRETS:
            if s and s in text:
                text = text.replace(s, "«redacted»")
        for rx in _SECRET_RES:
            if rx.groups >= 2:
                text = rx.sub(lambda m: m.group(1) + "«redacted»" + (m.group(3) if rx.groups >= 3 else ""), text)
            else:
                text = rx.sub("«redacted»", text)
    except Exception:  # noqa: BLE001
        pass
    return text


_GH_TOKEN_CACHE: dict[str, str] = {}


def _github_token() -> str:
    """The GitHub token from Windows Credential Manager (via git's credential
    helper) — fetched only when a command asks for $env:GITHUB_TOKEN, cached
    for the process, registered for redaction. Never printed."""
    if "t" in _GH_TOKEN_CACHE:
        return _GH_TOKEN_CACHE["t"]
    tok = ""
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        out = subprocess.run(["git", "credential", "fill"],
                             input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, timeout=20, env=env).stdout
        for line in out.splitlines():
            if line.startswith("password="):
                tok = line.split("=", 1)[1].strip()
    except Exception:  # noqa: BLE001
        tok = ""
    if tok:
        _KNOWN_SECRETS.add(tok)
        _GH_TOKEN_CACHE["t"] = tok
    return tok


_NESTED_PS_RE = re.compile(
    r"^\s*(?:powershell|pwsh)(?:\.exe)?\s+"
    r"(?:-(?:NoProfile|NonInteractive|NoLogo)\s+|-ExecutionPolicy\s+\S+\s+)*"
    r"-(?:Command|c)\s+(\"(?P<dq>.*)\"|'(?P<sq>.*)')\s*$", re.I | re.S)


def unwrap_nested_powershell(command: str) -> str:
    """run_command ALREADY runs in PowerShell. A model that wraps its command
    in another `powershell -Command "…"` gets the OUTER shell expanding
    `$vars` inside the double quotes to nothing before the inner one runs —
    the 2026-11-08 session failed 25× on exactly that (`'=gho_…' is not
    recognized`). Strip one redundant wrapper and run the inner script."""
    m = _NESTED_PS_RE.match(command or "")
    if not m:
        return command
    if m.group("dq") is not None:
        return m.group("dq").replace('\\"', '"')
    return m.group("sq").replace("''", "'")


async def exec_command(command: str, cwd: Path | None = None) -> str:
    """Run a command via PowerShell (only after policy/approval allowed it).
    Output is secret-scrubbed; $env:GITHUB_TOKEN / $env:GH_TOKEN are filled
    from Credential Manager when the command references them."""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = unwrap_nested_powershell(command)
    if re.search(r"\b(?:GITHUB_TOKEN|GH_TOKEN)\b", command):
        tok = await asyncio.to_thread(_github_token)
        if tok:
            env["GITHUB_TOKEN"] = env["GH_TOKEN"] = tok
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", command,
            cwd=str(cwd or settings.root_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError:
        raise ToolError("powershell not found on this machine")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(),
                                        timeout=settings.command_timeout)
    except asyncio.TimeoutError:
        raise ToolError(f"command timed out after {settings.command_timeout}s")
    finally:
        # kill the child whenever this exits with it still alive: timeout
        # and hard stop (task cancellation) — a stop must not outlive the
        # click, and the PowerShell process must not outlive its command
        if proc.returncode is None:
            proc.kill()
    text = out.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n[truncated — {len(text)} chars total]"
    text = redact_secrets(text.strip()) or "(no output)"
    if proc.returncode != 0:
        text += f"\n[exit code {proc.returncode}]"
    return text


# ── web ─────────────────────────────────────────────────────────────

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def _strip_tags(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _ddg_url(href: str) -> str | None:
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        return q[0] if q else None
    if href.startswith("http") and "duckduckgo.com" not in href:
        return href
    return None


def _bing_url(href: str) -> str | None:
    """Bing wraps result URLs in /ck/a redirect links; the real URL is the
    base64 payload after u=a1. Returns the decoded URL, or None if not one."""
    href = htmllib.unescape(href)
    m = re.search(r"[?&]u=a1([A-Za-z0-9+/=_-]+)", href)
    if not m:
        return href if href.startswith("http") and "bing.com" not in href else None
    pad = m.group(1).replace("-", "+").replace("_", "/")
    pad += "=" * (-len(pad) % 4)
    try:
        url = base64.b64decode(pad).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    return url if url.startswith("http") else None


def _bing_search(page: str, max_results: int) -> list[dict]:
    results = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        a = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = _bing_url(a.group(1))
        if not url:
            continue
        s = re.search(r'<p[^>]*class="[^"]*(?:b_lineclamp|b_paractl)[^"]*"[^>]*>(.*?)</p>',
                      block, re.S)
        # Bing titles carry a breadcrumb ("domain.com https://... › crumb")
        title = re.sub(r"\s*(?:https?://[^\s›»]+)?\s*[›»]\s*", " ", _strip_tags(a.group(2)))
        results.append({
            "title": re.sub(r"\s+", " ", title).strip()[:200],
            "url": url,
            "snippet": _strip_tags(s.group(1) if s else "")[:300],
        })
        if len(results) >= max_results:
            break
    return results


def _tavily_search(query: str, max_results: int) -> list[dict]:
    """Tavily Search (API, 1 credit/call at basic depth). Returns the same
    {title, url, snippet} shape as the scrape chain so downstream is
    format-agnostic. Raises on any failure — caller falls back to DDG/Bing."""
    with httpx.Client(timeout=15) as c:
        r = c.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={"query": query, "max_results": max_results,
                  "search_depth": "basic", "include_answer": False},
        )
        r.raise_for_status()
    data = r.json()
    out = []
    for x in data.get("results", []):
        out.append({
            "title": (x.get("title") or "")[:200],
            "url": x.get("url") or "",
            "snippet": (x.get("content") or "")[:300],
        })
    return out


def t_web_search(ctx: ToolCtx, query: str, max_results: int = 6) -> str:
    max_results = max(1, min(int(max_results or 6), 10))
    results: list[dict] = []
    tavily_err = ""
    if settings.tavily_api_key:
        try:
            results = _tavily_search(query, max_results)
        except Exception as e:  # noqa: BLE001 — fall through to the scrape chain
            tavily_err = str(e)
    ddg_err = ""
    if not results:
        try:
            with httpx.Client(headers=UA, timeout=15, follow_redirects=True) as c:
                r = c.post("https://html.duckduckgo.com/html/", data={"q": query})
                r.raise_for_status()
                page = r.text
                for m in re.finditer(
                    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
                    r'.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
                    page, re.S):
                    url = _ddg_url(m.group(1))
                    if not url:
                        continue
                    results.append({
                        "title": _strip_tags(m.group(2))[:200],
                        "url": url,
                        "snippet": _strip_tags(m.group(3) or "")[:300],
                    })
                    if len(results) >= max_results:
                        break
        except Exception as e:  # noqa: BLE001
            ddg_err = str(e)
    bing_err = ""
    if not results:  # DDG rate-limits silently — fall back to Bing
        try:
            with httpx.Client(headers=UA, timeout=15, follow_redirects=True) as c:
                r = c.get("https://www.bing.com/search", params={"q": query})
                r.raise_for_status()
            results = _bing_search(r.text, max_results)
        except Exception as e:  # noqa: BLE001
            bing_err = str(e)
    if not results:
        parts = []
        if tavily_err:
            parts.append(f"tavily: {tavily_err}")
        if ddg_err:
            parts.append(f"duckduckgo: {ddg_err}")
        if bing_err:
            parts.append(f"bing: {bing_err}")
        detail = "; ".join(parts) or "all engines returned nothing"
        raise ToolError(f"web search failed: {detail} — try different words")
    ctx.sources.append("web search: " + query + "\n" + "\n".join(
        f"{x['title']} — {x['url']} — {x['snippet']}" for x in results))
    return json.dumps(results, ensure_ascii=False, indent=1)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br",
             "section", "article", "header", "footer", "table", "ul", "ol",
             "pre", "blockquote", "dd", "dt", "main", "figure"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)

    @property
    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r" ?\n ?", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def t_fetch_url(ctx: ToolCtx, url: str, max_chars: int = 30_000) -> str:
    if not re.match(r"^https?://", url or ""):
        raise ToolError("only http(s) URLs are supported")
    max_chars = max(1000, min(int(max_chars or 30_000), 100_000))
    try:
        with httpx.Client(headers=UA, timeout=20, follow_redirects=True) as c:
            r = c.get(url)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"fetch failed: {e}")
    ctype = (r.headers.get("content-type") or "").lower()
    head = (r.text[:200].lstrip()[:1] or "").lower()
    if "html" in ctype or head == "<":
        ex = _TextExtractor()
        try:
            ex.feed(r.text[:400_000])
            text = ex.text
        except Exception:  # noqa: BLE001
            text = r.text
    else:
        text = r.text
    text = text.strip()
    truncated = len(text) > max_chars
    ctx.sources.append(f"fetched: {url}\n{text[:8000]}")
    return (f"[HTTP {r.status_code}] {url}\n{text[:max_chars]}"
            + ("\n[truncated]" if truncated else ""))


# ── browser (Playwright, headless Chromium — JS-rendered pages) ─────

async def t_browser(ctx: ToolCtx, action: str, url: str,
                    selector: str | None = None) -> str:
    """Render a page in a real headless browser. Use this when fetch_url
    returns an empty shell (SPA / JS-rendered content) or when you need
    to SEE the page (screenshot → returned inline, you get the pixels).

    action: open (title + text) | screenshot (PNG, inline) | text (element)

    `url` may be a local file path (e.g. report.html) — it is served through
    the server's own /preview/ endpoint and opened as http://127.0.0.1. Note:
    /preview/ applies a sandbox CSP (no external scripts/fonts/CDN), so pages
    that load external assets render stripped.
    """
    local_file: Path | None = None
    if not re.match(r"^https?://", url or ""):
        # local file → serve it through our own /preview/ endpoint (same
        # machine, no separate http.server process needed)
        raw = (url or "").strip()
        if raw.startswith("file://"):
            raw = raw[len("file://"):]
        if not raw or "://" in raw:
            raise ToolError(
                "browser opens http(s) web pages or local file paths. "
                "If this is an uploaded image, it is already in your context "
                "as an image (you can see it directly); no screenshot is needed.")
        p = Path(raw)
        if not p.is_absolute():
            p = (ctx.cwd / p).resolve()
        else:
            p = p.resolve()
        if not p.is_file():
            raise ToolError(f"no such file: {p}")
        url = f"http://127.0.0.1:{settings.port}/preview/" + preview_token(p)
        local_file = p
    if action not in ("open", "screenshot", "text"):
        raise ToolError("action must be open | screenshot | text")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ToolError("playwright not installed — pip install playwright && playwright install chromium")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=UA["User-Agent"],
                                              viewport={"width": 1280, "height": 900})
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:  # noqa: BLE001 — networkidle is best-effort
                    pass
                title = await page.title()
                label = f"{url}  (local file: {local_file})" if local_file else url
                if action == "open":
                    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    text = (text or "").strip()
                    truncated = len(text) > MAX_OUTPUT
                    ctx.sources.append(f"browser: {url}\n{text[:8000]}")
                    return (f"[browser] {title}\n{label}\n{text[:MAX_OUTPUT]}"
                            + ("\n[truncated]" if truncated else ""))
                if action == "screenshot":
                    png = await page.screenshot(full_page=False)
                    shot_dir = APP_ROOT / "data"
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    path = shot_dir / f"shot_{uuid.uuid4().hex[:8]}.png"
                    path.write_bytes(png)
                    b64 = base64.b64encode(png).decode()
                    ctx.sources.append(f"browser screenshot: {url} (title: {title})")
                    return (f"[[BROWSER_IMAGE:{b64}]]\n"
                            f"[browser screenshot] {title} — {url}\n"
                            f"saved: {path}")
                # action == "text"
                if not selector:
                    raise ToolError("action=text needs a selector (CSS)")
                try:
                    text = await page.eval_on_selector(selector, "el => el.innerText")
                except Exception:  # noqa: BLE001
                    raise ToolError(f"selector matched nothing: {selector}")
                text = (text or "").strip()
                ctx.sources.append(f"browser ({selector}): {url}\n{text[:8000]}")
                return f"[browser] {title}\n{selector}:\n{text[:MAX_OUTPUT]}"
            finally:
                await browser.close()
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"browser failed: {type(e).__name__}: {e}")


# ── pass-by-reference (big results spill to disk) ───────────────────

def t_local_search(ctx: ToolCtx, query: str, top_k: int = 6,
                   method: str = "hybrid", path: str | None = None) -> str:
    from . import rag
    if method not in ("bm25", "dense", "hybrid"):
        raise ToolError(f"method must be bm25 | dense | hybrid (got {method!r})")
    res = rag.search(query, top_k=max(1, min(int(top_k or 6), 20)),
                     method=method, path_filter=path)
    if not res["ok"]:
        return res.get("note", "search failed")
    out = [f"local_search: {len(res['results'])} result(s) [{res['method']}]"]
    for i, r in enumerate(res["results"], 1):
        out.append(f"\n--- [{i}] {r['source']} (score {r['score']}) ---\n{r['text'][:1500]}")
    if not res["results"]:
        out.append("\n(no matches)")
    return "\n".join(out)


def t_search_transcript(ctx: ToolCtx, query: str = "", chat: str = "",
                        limit: int = 8) -> str:
    """Search the boss's chat transcripts (the DB, NOT the 16-message
    context window) — the recall path for anything older than the window.
    `query` = words to find (empty = recent messages); `chat` = session
    title or id prefix to restrict to one conversation (empty = all)."""
    from . import db
    lim = max(1, min(int(limit or 8), 20))
    q = (query or "").strip()
    # resolve `chat` → session id
    sid = ""
    if chat.strip():
        c = chat.strip()
        if len(c) >= 8 and c.lower() in {s["id"].lower() for s in db.list_sessions()}:
            sid = next(s["id"] for s in db.list_sessions()
                       if s["id"].lower() == c.lower())
        else:
            cands = [s for s in db.list_sessions()
                     if c.lower() in (s.get("title") or "").lower()
                     or (s["id"] or "").lower().startswith(c.lower())]
            if len(cands) == 1:
                sid = cands[0]["id"]
            elif len(cands) > 1:
                names = ", ".join(f"{s.get('title') or s['id'][:8]}" for s in cands[:5])
                raise ToolError(f"chat {chat!r} is ambiguous — matches: {names}")
            else:
                raise ToolError(f"no chat matches {chat!r}")
    if q:
        rows = db.search_messages(q, sid or ctx.session_id or None, limit=lim)
    else:
        if sid or ctx.session_id:
            rows = db.list_messages(sid or ctx.session_id, limit=lim)
        else:  # all chats: newest `lim` across every session
            pool = [m for s in db.list_sessions()
                    for m in db.list_messages(s["id"], limit=lim)]
            rows = sorted(pool, key=lambda m: m["id"])[-lim:]
    if not rows:
        scope = "this chat" if (sid or ctx.session_id) else "all chats"
        what = f" for {q!r}" if q else ""
        return (f"search_transcript: no messages{what} in {scope} "
                f"(widen with limit, different words, or chat=…)")
    out = [f"search_transcript: {len(rows)} message(s) (newest last)"]
    for m in rows:
        who = "You" if m["role"] == "user" else "muji"
        body = re.sub(r"\s+", " ", (m.get("content") or "")).strip()
        body = body[:1200] + ("…" if len(body) > 1200 else "")
        out.append(f"\n[{m['id']} · {who}] {body}")
    return "\n".join(out)


def t_index_documents(ctx: ToolCtx, paths: str | None = None,
                      rebuild: bool = False, status_only: bool = False) -> str:
    from . import rag
    plist = [s.strip() for s in (paths or "").split(",") if s.strip()] or None
    res = rag.index_documents(paths=plist, rebuild=bool(rebuild),
                              status_only=bool(status_only))
    return json.dumps(res, ensure_ascii=False, indent=1)


def spill_result(text: str, tool: str = "?") -> str:
    """Big tool output → file on disk; the model gets a preview + a handle.

    Below the threshold the text is returned unchanged (inline is cheaper
    than a handle round-trip for small results)."""
    threshold = settings.result_spill_threshold
    if text is None:
        text = ""
    if len(text) <= threshold:
        return text
    rid = "res_" + uuid.uuid4().hex[:12]
    p = settings.results_dir / (rid + ".txt")
    try:
        p.write_text(text, encoding="utf-8", errors="replace")
    except OSError as e:
        # spill failed — fall back to the plain cap rather than losing output
        return text[:MAX_OUTPUT] + f"\n[truncated — {len(text)} chars total]"
    lines = text.count("\n") + 1
    return (
        f"[big result — {len(text):,} chars, {lines:,} lines from {tool}; "
        f"full text saved to {p}]\n"
        f"{text[:RESULT_PREVIEW]}\n"
        f"[… {len(text) - RESULT_PREVIEW:,} more chars — read the rest with "
        f"read_file(path, start_line, end_line) or search_files(pattern, path)]"
    )


# result_read / result_grep were removed: a spilled result is a plain file on
# disk — recover it with read_file(path, start_line, end_line) or
# search_files(pattern, path) on the path the spill preview shows.



# ── gmail tools (Gmail API, readonly — retires the old IMAP pipe) ────

def t_gmail_search(ctx: ToolCtx, query: str = "", max_results: int = 10) -> str:
    """Search Gmail. Empty query = most recent. Examples: 'from:boss',
    'subject:report is:unread', 'label:UP after:2026-09-01', 'invoice'."""
    emails = gmail_mod.search_messages(query, max_results)
    return json.dumps(emails, ensure_ascii=False, indent=1) if emails else "(no emails)"


def t_gmail_read(ctx: ToolCtx, id: str = "") -> str:
    """Read a full email by ID (get ID from gmail_search)."""
    if not id:
        raise ToolError("need 'id' — get it from gmail_search")
    result = gmail_mod.read_message(id)
    return json.dumps(result, ensure_ascii=False, indent=1)


# ── math verification (sympy — the "I predict, I don't compute" rule) ─
# Every load-bearing number goes through this (or run_command + python),
# never through the model's head. Modes:
#   eval      — numeric evaluation of an expression
#   ode       — verify a proposed solution against a differential equation
#               + initial conditions (the TA board-check flow)
#   limit     — limit of an expression at a point ("0", "0+", "oo")
#   derivative— d/dx of an expression

_MATH_ALLOWED = {
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "exp", "log", "ln", "sqrt", "Abs", "abs", "sign", "floor", "ceiling",
    "pi", "E", "I", "oo", "Rational",
}


def _mparse(s: str, var: str = "t", func: str | None = None, implicit: bool = False):
    """Parse a math string with a strict whitelist (no arbitrary python).

    func: the dependent variable (ODE mode) — bound as sp.Function so that
    'diff(v, t)' is a real derivative instead of diff(symbol, symbol) = 0.
    implicit multiplication is OFF (sympy 1.14 bug: it mangles '25*v' into
    Integer*(25)*v) — callers must write explicit '*'.
    """
    import sympy as sp
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
    local = {}
    for n in _MATH_ALLOWED:
        v = getattr(sp, n, None)
        if v is not None:
            local[n] = v
    local.setdefault("ln", sp.log)
    local["Derivative"] = sp.Derivative
    local[var] = sp.Symbol(var)
    if func and func != var:
        # 'v' stays a Symbol (so '25*v' works); 'diff(v, t)' is built by a
        # wrapper that returns Derivative(v(t), t, n) — real derivative.
        local[func] = sp.Symbol(func)

        def _diff(f, t, n=1):
            return sp.Derivative(sp.Function(f)(t), t, n)

        local["diff"] = _diff
    else:
        local["diff"] = sp.diff
    tf = standard_transformations + ((implicit_multiplication_application,) if implicit else ())
    return parse_expr(s, transformations=tf, local_dict=local)


def _with_timeout(fn, seconds: float = 10.0):
    """Run fn in a daemon thread; bail if it hangs (simplify can)."""
    import threading
    box: dict = {}

    def _run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise ToolError(f"computation timed out after {seconds:g}s (expression too heavy?)")
    if "e" in box:
        raise box["e"]
    return box["v"]


def t_verify_math(ctx: ToolCtx, expression: str = "", equation: str = "",
                  proposed: str = "", conditions: str = "", var: str = "t",
                  at: str = "0", mode: str = "eval", precision: int = 12) -> str:
    """Verify math with sympy — tool-verified, never head-computed."""
    if mode not in ("eval", "ode", "limit", "derivative"):
        raise ToolError(f"mode must be eval | ode | limit | derivative (got {mode!r})")
    rep = [f"verify_math [{mode}]"]
    if mode == "eval":
        if not expression:
            raise ToolError("eval mode needs 'expression'")
        rep.append(f"expr: {expression}")
        e = _with_timeout(lambda: _mparse(expression))
        val = _with_timeout(lambda: e.evalf(max(precision, 6)))
        f = complex(val)
        if abs(f.imag) > 1e-12:
            rep.append(f"value: {f.real:.{min(precision,12)}g} + {f.imag:.{min(precision,12)}g}i (complex)")
        else:
            rep.append(f"value: {f.real:.{min(precision,12)}g}")
        return "\n".join(rep)
    if mode in ("limit", "derivative"):
        if not expression:
            raise ToolError(f"{mode} mode needs 'expression'")
        v = __import__("sympy").Symbol(var)
        e = _with_timeout(lambda: _mparse(expression, var))
        rep.append(f"expr: {expression}   var: {var}")
        if mode == "limit":
            a = at.strip()
            pt = __import__("sympy").oo if a in ("oo", "inf", "+oo") else (
                -__import__("sympy").oo if a in ("-oo", "-inf") else __import__("sympy").Rational(a.rstrip("+-")))
            side = "+" if a.endswith("+") else ("-" if a.endswith("-") else "+-")
            L = _with_timeout(lambda: __import__("sympy").limit(e, v, pt, side))
            rep.append(f"limit as {var} → {pt} ({side}): {L}")
        else:
            d = _with_timeout(lambda: __import__("sympy").diff(e, v))
            rep.append(f"d/d{var}: {d}")
        return "\n".join(rep)
    # ── ode mode ──
    if not (equation and proposed):
        raise ToolError("ode mode needs 'equation' (with '=') and 'proposed' (the student's answer)")
    sp = __import__("sympy")
    v = sp.Symbol(var)
    if "=" not in equation:
        raise ToolError("equation must contain '=' (e.g. 'diff(v, t) + 200*v = 0')")
    # dependent variable = first arg of diff(...) (or any f(...) on lhs)
    dep_name = None
    dm = re.search(r"diff\s*\(\s*(\w+)", equation)
    if dm:
        dep_name = dm.group(1)
    else:
        for fm in re.finditer(r"(\w+)\s*\(", equation):
            if fm.group(1) not in _MATH_ALLOWED and fm.group(1) != var:
                dep_name = fm.group(1)
                break
    if not dep_name or dep_name == var:
        raise ToolError("could not find the dependent variable in the equation (e.g. 'v' in diff(v, t))")
    eq_s, eq_r = equation.split("=", 1)
    lhs = _with_timeout(lambda: _mparse(eq_s.strip(), var, func=dep_name, implicit=False))
    rhs = _with_timeout(lambda: _mparse(eq_r.strip(), var, func=dep_name, implicit=False))
    dep = None
    for d in lhs.atoms(sp.Derivative):
        for a in d.args:
            if isinstance(a, sp.Function):
                dep = a
                break
        if dep is not None:
            break
    if dep is None:
        for f in lhs.atoms(sp.Function):
            dep = f
            break
    if dep is None:
        raise ToolError("dependent variable not found in the parsed equation")
    # proposed: "5*exp(-200*t)" or "v(t) = 5*exp(-200*t)"
    f0 = _with_timeout(lambda: _mparse(proposed, var, implicit=False))
    rep.append(f"eq:     {equation}")
    rep.append(f"soln:   {proposed}")
    # substitute v(t) → f0. Bare 'v' symbols (from '25*v') AND v(t) AND
    # Derivative(v(t), t, k) terms all map onto f0 and its k-th derivative.
    repl = {sp.Symbol(dep_name): f0, dep: f0}
    repl.update({sp.Derivative(dep, v, k): sp.diff(f0, v, k) for k in (1, 2, 3)})
    sub = lambda e: e.xreplace(repl)
    residual = _with_timeout(lambda: sp.simplify(sub(lhs) - sub(rhs)))
    exactly_zero = bool(residual == 0)
    rep.append(f"residual (eq - soln): {residual if not exactly_zero else '0 (identically)'}")
    num_ok = exactly_zero
    if not exactly_zero:
        try:
            samples = [sp.Rational(1, 100), sp.Rational(1, 10), sp.Integer(1), sp.Integer(5)]
            vals = [_with_timeout(lambda s=s: float(residual.subs(v, s).evalf(30))) for s in samples]
            num_ok = all(abs(x) < 1e-6 for x in vals)
            rep.append(f"residual at t = {samples}: {[f'{x:.3e}' for x in vals]}")
        except Exception as e:  # noqa: BLE001
            rep.append(f"(numeric residual check failed: {e})")
    # initial conditions: "t0: 0, v(0)=90, v'(0)=3"
    ic_ok = True
    ic_lines = []
    if conditions:
        t0 = 0.0
        t0m = re.search(r"t0\s*:\s*([\d.]+)", conditions)
        if t0m:
            t0 = float(t0m.group(1))
        cond_s = re.sub(r"t0\s*:\s*[\d.]+\s*,?\s*", "", conditions)
        parts = []
        for piece in cond_s.split(","):
            piece = piece.strip()
            if not piece:
                continue
            cm = re.match(r"(\w+)(')*\s*(?:\(([^)]*)\))?\s*=\s*(.+)$", piece)
            if not cm:
                ic_lines.append(f"IC {piece!r}: (unparsed — skipped)")
                continue
            parts.append((cm.group(1), (cm.group(3) or "").strip(), cm.group(4).strip(), len(cm.group(2) or "")))
        for name, tt, rhs_s, dcount in parts:
            piece = name + "'" * dcount + (f"({tt})" if tt else "") + f" = {rhs_s}"
            tval = sp.Rational(tt) if tt.strip() else sp.Rational(t0)
            if name == var:  # e.g. "t=0" — time anchor, not a condition
                continue
            eL = _with_timeout(lambda: sp.diff(f0, v, max(dcount, 1)).subs(v, tval)) if dcount else _with_timeout(lambda: f0.subs(v, tval))
            eR = _with_timeout(lambda: _mparse(rhs_s, var).subs(v, tval))
            ok = abs(complex(eL.evalf(30)) - complex(eR.evalf(30))) < 1e-6
            ic_ok = ic_ok and ok
            ic_lines.append(f"IC {piece}: {float(eL.evalf(12)):.6g} vs {float(eR.evalf(12)):.6g} → {'OK' if ok else 'MISMATCH'}")
    for l in ic_lines:
        rep.append(l)
    verdict = "VERIFIED: solution satisfies the ODE" + (" and all initial conditions" if conditions else "") if (num_ok and ic_ok) else "NOT VERIFIED — see lines above"
    rep.append(f"VERDICT: {verdict}")
    return "\n".join(rep)


# ── local task + calendar tools (option A — alarm-clock layer) ───────
# Stored in muji.db (src/taskstore.py). The reminder poller fires a Windows
# toast at the exact due moment — the thing Google Tasks never could.

def t_tasks_list(ctx: ToolCtx, max_results: int = 50, include_done: bool = False) -> str:
    """List open tasks in the local store (done ones hidden unless include_done)."""
    tasks = taskstore.list_tasks(include_done, max_results)
    return json.dumps(tasks, ensure_ascii=False, indent=1) if tasks else "(no open tasks)"


def t_tasks_add(ctx: ToolCtx, title: str, due: str = "", note: str = "") -> str:
    """Add a task to the local store. due: ISO 8601 with offset (e.g.
    '2026-09-25T09:00:00+08:00') or omit for no due date. A due time makes it
    fire a Windows toast reminder at that moment. Always verify with tasks_list."""
    task = taskstore.add_task(title, due, note)
    return json.dumps(task, ensure_ascii=False, indent=1)


def t_tasks_done(ctx: ToolCtx, id: int, done: bool = True) -> str:
    """Mark a local task done (or reopen it with done=false). id: from tasks_list."""
    task = taskstore.toggle_task(id, done)
    if task is None:
        raise ToolError(f"no such task id={id} — check tasks_list")
    return json.dumps(task, ensure_ascii=False, indent=1)


def t_events_add(ctx: ToolCtx, title: str, start: str, end: str = "", note: str = "") -> str:
    """Add a calendar event to the local store. start: ISO 8601 with offset
    (e.g. '2026-09-25T10:00:00+08:00'); end optional, must be after start.
    It fires a Windows toast when the event starts. Always verify with events_list."""
    ev = taskstore.add_event(title, start, end, note)
    return json.dumps(ev, ensure_ascii=False, indent=1)


def t_events_list(ctx: ToolCtx, days: int = 14, limit: int = 50) -> str:
    """List upcoming local calendar events (next `days` days, default 14)."""
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    evs = taskstore.list_events(now.timestamp(),
                                (now + timedelta(days=days)).timestamp(), limit)
    return json.dumps(evs, ensure_ascii=False, indent=1) if evs else "(no upcoming events)"


def t_events_done(ctx: ToolCtx, id: int, done: bool = True) -> str:
    """Mark a calendar event done (or reopen with done=false). id from
    events_list. Done events stay in the store as history — they are NOT
    deleted (no delete-event tool exists on purpose)."""
    ev = taskstore.toggle_event(id, done)
    if ev is None:
        raise ToolError(f"no such event id={id} — check events_list")
    return json.dumps(ev, ensure_ascii=False, indent=1)


# ── Telegram (the boss's own account, via the live bridge) ──────────
#: lazy wrappers — src.telegram imports src.agent at module level, so
#: importing it up front here would be a cycle; the bridge is only
#: reachable from a running server anyway.

def _tg_search(ctx: ToolCtx, query: str = "", chat: str = "", limit: int = 20) -> str:
    from .telegram import t_tg_search
    return t_tg_search(ctx, query=query, chat=chat, limit=limit)


def _tg_read(ctx: ToolCtx, chat: str, limit: int = 20) -> str:
    from .telegram import t_tg_read
    return t_tg_read(ctx, chat=chat, limit=limit)


def t_set_cwd(ctx: ToolCtx, path: str) -> str:
    """Move THIS chat's working directory (the boss asked in chat)."""
    raw = (path or "").strip()
    if not raw:
        raise ToolError("empty path")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(os.path.expanduser(raw)) if raw.startswith("~") else ctx.cwd / raw
    rp = p.resolve()
    if not rp.is_dir():
        raise ToolError(f"not a directory: {rp}")
    if ctx.session_id:
        db.set_session_cwd(ctx.session_id, str(rp))
    ctx.cwd = rp
    return (f"Working directory for this chat is now: {rp}\n"
            "The Files tab opens there, the terminal's pwd reflects it, and all "
            "relative paths (file creation, tools, commands) resolve there from "
            "now on — including after a page reload.")


def t_note_fact(ctx: ToolCtx, fact: str) -> str:
    """Append a durable fact to THIS chat's task notes (session notes)."""
    from datetime import datetime
    line = (fact or "").strip()
    if not line:
        raise ToolError("empty fact")
    if not ctx.session_id:
        raise ToolError("no session id — cannot locate the session notes")
    from .config import settings
    notes = settings.data_dir / "sessions" / ctx.session_id / "task_notes.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    if notes.exists():
        text = notes.read_text("utf-8", errors="replace")
    else:
        text = ("# task notes — this chat's durable memory\n"
                "Re-injected every turn (last 4KB) — "
                "survives compaction, reload, restart, resume.\n\n"
                "## Goal\n\n## Facts\n")
    if "## Facts" not in text:
        text = text.rstrip("\n") + "\n\n## Facts\n"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    notes.write_text(text + f"- {stamp} — {line}\n", encoding="utf-8")
    return (f"Saved to this chat's session notes ({notes}):\n{line}\n"
            "It is re-injected every turn — it survives "
            "compaction, reload, restart, and resume. Never re-ask the boss for "
            "a fact that is already in the notes.")


# ── registry ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_dir", "fn": t_list_dir,
        "description": "List files and folders of a directory.",
        "params": {"path": {"type": "string", "description": "Directory (default '.')"}},
    },
    {
        "name": "set_cwd", "fn": t_set_cwd,
        "description": ("Move THIS chat's working directory (call it when the boss "
                        "asks to move/set the working directory in chat). Persists "
                        "across reloads: the Files tab opens there, the terminal's "
                        "pwd reflects it, and all relative paths — file creation "
                        "included — resolve there by default from then on."),
        "params": {"path": {"type": "string", "description": "Target directory (absolute, or relative to the current working folder)"}},
        "required": ["path"],
    },
    {
        "name": "note_fact", "fn": t_note_fact,
        "description": ("Save a durable fact to THIS chat's session notes "
                        "(task_notes.md). Call it the moment the boss hands "
                        "you an identifier you must not lose — an SSH address "
                        "or host, an IP/port, a credential, token, path, "
                        "connection string, key decision, or task state. The "
                        "note is re-injected every "
                        "turn, so it survives compaction, reload, restart, "
                        "and resume."),
        "params": {"fact": {"type": "string", "description": "The fact to save (one line, self-contained)"}},
        "required": ["fact"],
    },
    {
        "name": "read_file", "fn": t_read_file,
        "description": "Read a text file (start_line/end_line for big files).",
        "params": {
            "path": {"type": "string", "description": "File path"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        "required": ["path"],
    },
    {
        "name": "write_file", "fn": t_write_file,
        "description": "Create or overwrite a file with the given full content.",
        "params": {
            "path": {"type": "string", "description": "File path"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
    },
    {
        "name": "edit_file", "fn": t_edit_file,
        "description": "Replace one exact occurrence of old_text with new_text.",
        "params": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "description": "Exact text to find"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    },
    {
        "name": "search_files", "fn": t_search_files,
        "description": "Regex-search file contents under a folder (grep).",
        "params": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "Folder (default '.')"},
            "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'"},
            "max_results": {"type": "integer"},
        },
        "required": ["pattern"],
    },
    {
        "name": "local_search", "fn": t_local_search,
        "description": ("Semantic+keyword search over your indexed local documents "
                        "(BantAI paper, DLX, notes, code). Returns top chunks with "
                        "source files. Index first with index_documents if empty."),
        "params": {
            "query": {"type": "string", "description": "What to look for"},
            "top_k": {"type": "integer", "description": "Results (default 6)"},
            "method": {"type": "string", "description": "bm25 | dense | hybrid (default hybrid)"},
            "path": {"type": "string", "description": "Optional: restrict to sources containing this path"},
        },
        "required": ["query"],
    },
    {
        "name": "search_transcript", "fn": t_search_transcript,
        "description": ("Search chat transcripts in the DB — the recall path "
                        "for anything OLDER than the ~16-message context "
                        "window (or after compaction). `query` = words to find "
                        "(empty = recent messages); `chat` = chat title or id "
                        "to restrict to one conversation (default: this chat; "
                        "empty query with no chat = most recent across chats). "
                        "Use it whenever the boss references an earlier "
                        "discussion you don't have in context — don't guess."),
        "params": {
            "query": {"type": "string", "description": "Words to find (empty = recent messages)"},
            "chat": {"type": "string", "description": "Chat title or id prefix (default: this chat)"},
            "limit": {"type": "integer", "description": "Max messages (default 8, cap 20)"},
        },
    },
    {
        "name": "index_documents", "fn": t_index_documents,
        "description": ("Build/refresh the local RAG index over your documents "
                        "(md, txt, py, js, ipynb, tex, …). Incremental by default; "
                        "rebuild=true forces a full re-scan; status_only=true just "
                        "reports index stats."),
        "params": {
            "paths": {"type": "string", "description": "Comma-separated dirs to index (default: home + Documents + Muji)"},
            "rebuild": {"type": "boolean"},
            "status_only": {"type": "boolean"},
        },
    },
    {
        "name": "run_command", "fn": None,  # agent handles (approval-gated)
        "description": ("Run a shell command via PowerShell. Commands run "
                        "automatically, except destructive operations (delete, "
                        "remove, rename, move, and similar irreversible actions) "
                        "and any command that starts/stops/restarts the muji "
                        "server — those need user approval, EXCEPT a clean "
                        "POST /api/restart to activate a change you verified "
                        "this session (imports clean + tests green) while no "
                        "other session has in-flight work. Never use a raw "
                        "kill, restart-muji.ps1, start.bat, or `python "
                        "server.py` — those always ask. If you can't "
                        "self-restart, say so in your final summary and stop. "
                        "SHELL FACTS (Windows PowerShell 5.1): the command "
                        "already runs IN PowerShell — never wrap it in "
                        "`powershell -Command \"…\"`; `&&` is invalid (use `;`); "
                        "`curl` is an alias of Invoke-WebRequest — use `curl.exe` "
                        "or Invoke-RestMethod; for anything with nested "
                        "quotes/JSON, write a .ps1 (or a JSON body file) with "
                        "write_file and run that. SECRETS: the GitHub token is "
                        "available as $env:GITHUB_TOKEN (filled from Credential "
                        "Manager, never shown to you) — e.g. Invoke-RestMethod "
                        "-Headers @{Authorization=\"Bearer $env:GITHUB_TOKEN\"} …; "
                        "never print it, never put a token in a git remote URL "
                        "(git authenticates by itself)."),
        "params": {
            "command": {"type": "string", "description": "The command line"},
            "cwd": {"type": "string", "description": "Working directory"},
        },
        "required": ["command"],
    },
    {
        "name": "web_search", "fn": t_web_search,
        "description": ("Search the web (Tavily API when TAVILY_API_KEY is "
                        "set, else DuckDuckGo→Bing scrape fallback); returns "
                        "titles, URLs, snippets."),
        "params": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    },
    {
        "name": "fetch_url", "fn": t_fetch_url,
        "description": "Fetch a URL and return its text (HTML converted to text).",
        "params": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["url"],
    },
    {
        "name": "browser", "fn": t_browser,
        "description": ("Open a page in a real headless browser (JS-rendered "
                        "content that fetch_url can't see). action: 'open' "
                        "(title + page text), 'screenshot' (PNG — returned "
                        "inline, you can see the pixels), 'text' (extract "
                        "innerText of a CSS selector). 'url' can also be a "
                        "local file path — it is served through the "
                        "/preview/ endpoint automatically."),
        "params": {
            "action": {"type": "string", "description": "open | screenshot | text"},
            "url": {"type": "string", "description": "http(s) URL or local file path"},
            "selector": {"type": "string", "description": "CSS selector (action=text only)"},
        },
        "required": ["action", "url"],
    },
    {
        "name": "gmail_search", "fn": t_gmail_search,
        "description": ("Search Gmail (newest first). Empty query lists recent mail. "
                        "Query examples: 'from:boss', 'subject:report is:unread', "
                        "'label:UP after:2026-09-01', plain words also work. Read-only."),
        "params": {
            "query": {"type": "string", "description": "Gmail search query (empty = recent mail)"},
            "max_results": {"type": "integer", "description": "Max results (default 10)"},
        },
    },
    {
        "name": "gmail_read", "fn": t_gmail_read,
        "description": "Read a full email by ID (get ID from gmail_search). Read-only.",
        "params": {
            "id": {"type": "string", "description": "Message ID (from gmail_search)"},
        },
        "required": ["id"],
    },
    {
        "name": "verify_math", "fn": t_verify_math,
        "description": ("Verify math with sympy (tool-verified, never head-computed). "
                        "mode='eval': numeric value of an expression. "
                        "mode='ode': check a proposed solution against a differential "
                        "equation + initial conditions (TA board-check flow). "
                        "mode='limit'/'derivative': limit or d/dt. "
                        "Examples: equation='diff(v, t) + 200*v = 0', "
                        "proposed='150 - 60*exp(-200*t)', conditions=\"v(0)=90\"."),
        "params": {
            "mode": {"type": "string", "description": "eval (default) | ode | limit | derivative"},
            "expression": {"type": "string", "description": "Math expression (eval/limit/derivative)"},
            "equation": {"type": "string", "description": "ODE with '=' (ode mode), e.g. 'diff(v, t) + 200*v = 0'"},
            "proposed": {"type": "string", "description": "Proposed solution (ode mode), e.g. '150 - 60*exp(-200*t)'"},
            "conditions": {"type": "string", "description": "Initial conditions, e.g. \"v(0)=90, v'(0)=3\" (ode mode)"},
            "var": {"type": "string", "description": "Independent variable (default 't')"},
            "at": {"type": "string", "description": "Point for limit/eval-at (default '0')"},
            "precision": {"type": "integer", "description": "Digits for eval (default 12)"},
        },
    },
    {
        "name": "tasks_list", "fn": t_tasks_list,
        "description": ("List open tasks in the local store (read-only; done ones "
                        "hidden unless include_done)."),
        "params": {
            "max_results": {"type": "integer", "description": "Max tasks (default 50)"},
            "include_done": {"type": "boolean", "description": "Include done tasks"},
        },
    },
    {
        "name": "tasks_add", "fn": t_tasks_add,
        "description": ("Add a task to the local store. due: ISO 8601 with offset "
                        "(e.g. '2026-09-25T09:00:00+08:00') or omit for no due date — a "
                        "due time fires a Windows toast at that moment. Always verify "
                        "with tasks_list after adding."),
        "params": {
            "title": {"type": "string", "description": "Task title"},
            "due": {"type": "string", "description": "Due date/time, ISO 8601 (optional)"},
            "note": {"type": "string", "description": "Optional note"},
        },
        "required": ["title"],
    },
    {
        "name": "tasks_done", "fn": t_tasks_done,
        "description": "Mark a local task done (or reopen with done=false). id from tasks_list.",
        "params": {
            "id": {"type": "integer", "description": "Task id (from tasks_list)"},
            "done": {"type": "boolean", "description": "true=complete (default), false=reopen"},
        },
        "required": ["id"],
    },
    {
        "name": "events_add", "fn": t_events_add,
        "description": ("Add a calendar event to the local store. start: ISO 8601 with "
                        "offset; end optional, must be after start. Fires a Windows "
                        "toast when the event starts. Always verify with events_list."),
        "params": {
            "title": {"type": "string", "description": "Event title"},
            "start": {"type": "string", "description": "Start, ISO 8601 with offset"},
            "end": {"type": "string", "description": "End, ISO 8601 (optional)"},
            "note": {"type": "string", "description": "Optional note"},
        },
        "required": ["title", "start"],
    },
    {
        "name": "events_list", "fn": t_events_list,
        "description": "List upcoming local calendar events (next `days` days, default 14).",
        "params": {
            "days": {"type": "integer", "description": "How many days ahead (default 14)"},
            "limit": {"type": "integer", "description": "Max events (default 50)"},
        },
    },
    {
        "name": "events_done", "fn": t_events_done,
        "description": ("Mark a calendar event done (or reopen with done=false). "
                        "id from events_list. Done events stay as history — "
                        "they are NOT deleted."),
        "params": {
            "id": {"type": "integer", "description": "Event id (from events_list)"},
            "done": {"type": "boolean", "description": "true=complete (default), false=reopen"},
        },
        "required": ["id"],
    },
    {
        "name": "tg_search", "fn": _tg_search,
        "description": ("Search the boss's Telegram conversations (his own "
                        "account). `query` = text to find (empty = recent "
                        "messages); `chat` = chat name or id to restrict to "
                        "one conversation (empty = all dialogs); `limit` = "
                        "max messages (default 20). Read-only."),
        "params": {
            "query": {"type": "string", "description": "Text to search for (empty = recent)"},
            "chat": {"type": "string", "description": "Chat name or id (empty = all dialogs)"},
            "limit": {"type": "integer", "description": "Max messages (default 20)"},
        },
    },
    {
        "name": "tg_read", "fn": _tg_read,
        "description": ("Read recent messages from ONE Telegram conversation "
                        "(the boss's own account). `chat` = chat name or id "
                        "(required); `limit` = max messages (default 20, "
                        "newest first). Read-only."),
        "params": {
            "chat": {"type": "string", "description": "Chat name or id"},
            "limit": {"type": "integer", "description": "Max messages (default 20)"},
        },
        "required": ["chat"],
    },
    {
        "name": "load_tools", "fn": None,  # agent handles (on-demand tool groups)
        "description": ("Load an extra tool group for this chat; its tools are "
                        "callable from your next step. Groups: telegram (search/"
                        "read the user's Telegram), gmail (search/read Gmail), "
                        "planner (shared task list + calendar events: tasks_*/"
                        "events_*), math (verify_math), documents (local RAG: "
                        "local_search, index_documents). Load only what the task "
                        "needs; several groups at once is fine."),
        "params": {
            "groups": {"type": "array", "items": {"type": "string",
                       "enum": ["telegram", "gmail", "planner", "math", "documents"]},
                       "description": "Group names to load"},
        },
        "required": ["groups"],
    },
    {
        "name": "ask_user", "fn": None,  # agent handles (question flow)
        "description": ("Ask the boss a multiple-choice question when part of the "
                        "instruction is genuinely vague AND the choice materially changes "
                        "the outcome. Give 2-5 concrete options and set 'recommended' to "
                        "the index (0-based) of your best guess — if he's silent for ~3 "
                        "minutes that option auto-runs. The card ALWAYS gets a final "
                        "'Something else — I'll describe it' option appended "
                        "automatically: never add it yourself, never recommend it; if he "
                        "picks it, ask him to describe his answer in one short question. "
                        "Use it ONLY for material ambiguity; for trivial ambiguity just "
                        "pick and state it."),
        "params": {
            "question": {"type": "string", "description": "The question, one line"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2-5 concrete options"},
            "recommended": {"type": "integer", "description": "Index (0-based) of your best guess"},
        },
        "required": ["question", "options", "recommended"],
    },

]


# ── auto-approve categories (Cline-style panel) ─────────────────────
#: category → tools it covers; label shown on approval cards / UI.
APPROVAL_CATEGORIES: dict[str, set[str]] = {
    "read": {"list_dir", "read_file", "search_files",
             "local_search", "search_transcript", "index_documents",
             "gmail_search", "gmail_read",
             "verify_math",
             "tasks_list",
             "tg_search", "tg_read",
             "set_cwd"},
    "edit": {"write_file", "edit_file", "note_fact"},
    "commands": {"run_command"},
    "web": {"web_search", "fetch_url"},
    "browser": {"browser"},
}
APPROVAL_CATEGORY_LABELS = {
    "read": "Read files",
    "edit": "Edit files",
    "commands": "Execute commands",
    "web": "Fetch web content",
    "browser": "Open browser",
    "selfedit": "Self-edit",
    "system": "Work outside root",
}


def category_of(tool_name: str) -> str | None:
    for cat, tools in APPROVAL_CATEGORIES.items():
        if tool_name in tools:
            return cat
    return None


#: Tools loaded ON DEMAND (token diet 2026-11-08). Each group was used in
#: <0.2% of all historical tool calls yet its schemas rode on EVERY request
#: (~1.9k tokens total). They stay out of the request until the model calls
#: load_tools(group); a loaded group then stays on for the rest of the chat.
OPTIONAL_TOOL_GROUPS: dict[str, tuple[list[str], str]] = {
    "telegram": (["tg_search", "tg_read", "tg_topics"],
                 "search/read the user's Telegram chats and forum topics"),
    "gmail": (["gmail_search", "gmail_read"], "search/read the user's Gmail"),
    "planner": (["tasks_list", "tasks_add", "tasks_done",
                 "events_list", "events_add", "events_done"],
                "the shared task list (\"our tasks\") and calendar events"),
    "math": (["verify_math"], "symbolic/numeric math verification (SymPy)"),
    "documents": (["local_search", "index_documents"],
                  "local RAG: index folders and semantic-search documents"),
}


def _filter_optional_groups() -> None:
    """Public build: keep only tools that exist in this registry AND aren't
    switched off by TOOLS_DISABLED (personal integrations are off by
    default), drop empty groups, and rewrite load_tools' description/enum
    to match — so the model is never offered a group with nothing in it."""
    global OPTIONAL_TOOL_GROUPS
    active = {t["name"] for t in TOOLS if t["name"] not in settings.tools_disabled}
    OPTIONAL_TOOL_GROUPS = {
        g: ([n for n in names if n in active], desc)
        for g, (names, desc) in OPTIONAL_TOOL_GROUPS.items()
        if any(n in active for n in names)}
    spec = next((t for t in TOOLS if t["name"] == "load_tools"), None)
    if spec is None:
        return
    if not OPTIONAL_TOOL_GROUPS:
        TOOLS.remove(spec)  # nothing optional is enabled → no meta-tool
        return
    spec["description"] = (
        "Load an extra tool group for this chat; its tools are callable from "
        "your next step. Groups: " + "; ".join(
            f"{g} ({desc}: {', '.join(names)})"
            for g, (names, desc) in OPTIONAL_TOOL_GROUPS.items())
        + ". Load only what the task needs; several groups at once is fine.")
    spec["params"]["groups"]["items"]["enum"] = list(OPTIONAL_TOOL_GROUPS)


_filter_optional_groups()


def optional_tool_names() -> set[str]:
    return {n for names, _ in OPTIONAL_TOOL_GROUPS.values() for n in names}


def schema_exclude(loaded_groups: set[str] | None = None,
                   extra: set[str] | None = None) -> set[str]:
    """Tool names to leave out of the request: every optional tool whose
    group isn't loaded, plus `extra` (e.g. MUTATING_TOOLS in Plan mode)."""
    loaded = loaded_groups or set()
    out = {n for g, (names, _) in OPTIONAL_TOOL_GROUPS.items()
           if g not in loaded for n in names}
    return out | (extra or set())


def openai_schemas(exclude: set[str] | None = None) -> list[dict]:
    out = []
    for t in TOOLS:
        if t["name"] in settings.tools_disabled:
            continue
        if exclude and t["name"] in exclude:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": {k: dict(v) for k, v in t["params"].items()},
                    "required": t.get("required", []),
                },
            },
        })
    return out


def tool_summary(exclude: set[str] | None = None) -> str:
    """One line per tool — used by the text-protocol system prompt."""
    lines = []
    for t in TOOLS:
        if t["name"] in settings.tools_disabled:
            continue
        if exclude and t["name"] in exclude:
            continue
        args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in t["params"].items())
        lines.append(f"- {t['name']}({args}) — {t['description']}")
    return "\n".join(lines)


def dispatch(ctx: ToolCtx, name: str, args: dict) -> str:
    """Run a tool (sync, or async via asyncio.run — dispatch itself is
    called from a worker thread by the agent). run_command and ask_user
    go through the agent."""
    if name == "run_command":
        raise ToolError("run_command must go through the agent (approval policy)")
    if name == "ask_user":
        raise ToolError("ask_user must go through the agent (question flow)")
    if name in settings.tools_disabled:
        raise ToolError(f"{name} is disabled for this install (TOOLS_DISABLED)")
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None or tool["fn"] is None:
        raise ToolError(f"unknown tool: {name}")
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        result = tool["fn"](ctx, **clean)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except ToolError:
        raise
    except TypeError as e:
        raise ToolError(f"bad arguments for {name}: {e}")
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"{name} failed: {type(e).__name__}: {e}")
