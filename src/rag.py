"""Local RAG: chunked index over your own docs + BM25 / dense / hybrid search.

Design (adapted from OnIt's index_documents, deliberately lightweight):
- Single JSON index on disk (settings.data_dir / "rag_index.json") — no vector DB.
- BM25 works with zero config. Dense/hybrid light up automatically when
  RAG_EMBED_MODEL is set (any OpenAI-compatible /v1/embeddings endpoint —
  the same base_url/api_key the chat model uses).
- Chunks are ~RAG_CHUNK_SIZE chars with RAG_CHUNK_OVERLAP overlap, each
  attributed to its source file so search results are traceable.
- Re-indexing is incremental: unchanged files (mtime+size) are skipped.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

import httpx

from .config import settings

# ── constants ─────────────────────────────────────────────────────────

TEXT_EXTS = {".md", ".markdown", ".txt", ".py", ".js", ".ts", ".html",
             ".css", ".json", ".csv", ".tex", ".ipynb", ".log", ".ini",
             ".cfg", ".toml", ".yaml", ".yml", ".pdf"}
MAX_FILE_BYTES = 2_000_000          # skip files bigger than this
MAX_PDF_BYTES = 50_000_000          # PDFs: limit on raw bytes (text is extracted,
                                    # so a 10 MB paper is a few hundred KB of text)
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
             "venv", ".idea", ".vs", "AppData", "OneDrive", "$Recycle.Bin",
             ".cache", ".claude", ".cline", ".copilot", ".vscode",
             ".vscode-shared", ".jupyter", ".ipython", ".matplotlib",
             "Zotero", ".ssh", ".conda", "scikit_learn_data"}
INDEX_VERSION = 1


def index_path() -> Path:
    return settings.data_dir / "rag_index.json"


def _default_paths() -> list[Path]:
    """Corpus roots used when the tool is called without explicit paths."""
    home = settings.root_dir
    return [home, home / "Documents", home / "Muji"]


# ── global index store ────────────────────────────────────────────────

_idx: dict | None = None


def _load() -> dict:
    global _idx
    if _idx is None:
        p = index_path()
        if p.exists():
            try:
                _idx = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                _idx = None
        if _idx is None:
            _idx = {"version": INDEX_VERSION, "files": {}, "chunks": []}
    return _idx


def _save() -> None:
    p = index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_idx, ensure_ascii=False), encoding="utf-8")


# ── text extraction ───────────────────────────────────────────────────

def _extract_text(path: Path) -> str | None:
    """Return text content, or None if the file has no extractable text."""
    try:
        if path.suffix == ".ipynb":
            nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            parts = []
            for cell in nb.get("cells", []):
                src = cell.get("source", [])
                if isinstance(src, list):
                    parts.append("".join(src))
                elif isinstance(src, str):
                    parts.append(src)
                for out in cell.get("outputs", []):
                    txt = out.get("text") or out.get("data", {}).get("text/plain")
                    if isinstance(txt, list):
                        parts.append("".join(txt))
                    elif isinstance(txt, str):
                        parts.append(txt)
            return "\n".join(parts)
        if path.suffix == ".pdf":
            return _extract_pdf(path)
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _extract_pdf(path: Path) -> str | None:
    """PDF text via pymupdf (pymupdf is a muji dep; pypdf as fallback)."""
    try:
        import pymupdf  # type: ignore
        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text("text") for page in doc)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception:
        return None


# ── chunking ──────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[str]:
    """Split into ~chunk_size char blocks with overlap, preferring
    paragraph boundaries."""
    size = settings.rag_chunk_size
    overlap = settings.rag_chunk_overlap
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        # oversized paragraph: hard-split it
        while len(para) > size:
            if cur:
                chunks.append(cur)
                cur = cur[-overlap:] if overlap < len(cur) else ""
            chunks.append(para[:size])
            para = para[size - overlap:] if overlap else para[size:]
        if len(cur) + len(para) + 2 > size and cur:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap < len(cur) else ""
        cur = (cur + "\n\n" + para).strip()
    if cur:
        chunks.append(cur)
    return chunks or ([text[:size]] if text.strip() else [])


# ── embeddings (optional) ─────────────────────────────────────────────

def _embed_model() -> str:
    return settings.rag_embed_model


def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """Call the OpenAI-compatible embeddings endpoint; None if unavailable."""
    model = _embed_model()
    if not model or not settings.base_url:
        return None
    # pinned to the "default" profile — the chat model may be switched to a
    # provider with no embeddings endpoint for RAG_EMBED_MODEL
    embed_url, embed_key = settings.embed_endpoint()
    url = embed_url.rstrip("/") + "/embeddings"
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {embed_key}"},
            json={"model": model, "input": texts},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    except Exception:
        return None


def _embed_pending() -> int:
    """Embed chunks that don't have vectors yet. Returns count embedded."""
    model = _embed_model()
    if not model:
        return 0
    idx = _load()
    pending = [c for c in idx["chunks"] if c.get("vec") is None]
    if not pending:
        return 0
    done = 0
    for i in range(0, len(pending), 32):
        batch = pending[i:i + 32]
        vecs = _embed_batch([c["text"] for c in batch])
        if vecs is None:
            return done
        for c, v in zip(batch, vecs):
            c["vec"] = v
            done += 1
    idx["embedded_at"] = time.time()
    return done


# ── BM25 ──────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall(s.lower())


def _bm25_scores(query: str, chunks: list[dict], k1: float = 1.5,
                 b: float = 0.75) -> list[float]:
    if not chunks:
        return []
    toks = _tokens(query)
    if not toks:
        return [0.0] * len(chunks)
    n = len(chunks)
    doc_tokens = [_tokens(c["text"]) for c in chunks]
    avgdl = sum(len(d) for d in doc_tokens) / n
    df: dict[str, int] = {}
    for d in doc_tokens:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    scores = []
    for d in doc_tokens:
        if not d:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in toks:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            f = tf[t]
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / avgdl))
        scores.append(s)
    return scores


# ── dense ─────────────────────────────────────────────────────────────

def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _dense_scores(query: str, chunks: list[dict]) -> list[float] | None:
    if any(c.get("vec") is None for c in chunks):
        return None
    vecs = _embed_batch([query])
    if not vecs:
        return None
    q = vecs[0]
    return [_cos(q, c["vec"]) for c in chunks]


# ── public API ────────────────────────────────────────────────────────

def search(query: str, top_k: int = 6, method: str = "hybrid",
           path_filter: str | None = None) -> dict:
    idx = _load()
    chunks = idx["chunks"]
    if path_filter:
        pf = str(path_filter).lower()
        chunks = [c for c in chunks if pf in c["source"].lower()]
    if not chunks:
        return {"ok": True, "results": [], "note": "index is empty — run index_documents first"}

    bm = _bm25_scores(query, chunks)
    dm = _dense_scores(query, chunks) if method in ("dense", "hybrid") else None
    if method == "dense" and dm is None:
        return {"ok": False, "results": [],
                "note": "dense search needs embeddings: set RAG_EMBED_MODEL and re-run index_documents"}

    if method == "bm25":
        scores = bm
    elif method == "dense":
        scores = dm
    else:  # hybrid: rank-normalize both, average
        def norm(v):
            lo, hi = min(v), max(v)
            return [0.5 if hi == lo else (x - lo) / (hi - lo) for x in v]
        nb, nd = norm(bm), norm(dm or [0.0] * len(chunks))
        scores = [(a + b) / 2 for a, b in zip(nb, nd)]

    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    results = []
    for i in order[:top_k]:
        if scores[i] <= 0:
            break
        c = chunks[i]
        results.append({
            "score": round(scores[i], 4),
            "source": c["source"],
            "text": c["text"],
        })
    return {"ok": True, "results": results,
            "method": method if dm is not None or method == "bm25" else "bm25(fallback)"}


def index_documents(paths: list[str] | None = None, rebuild: bool = False,
                    status_only: bool = False) -> dict:
    idx = _load()
    if status_only:
        return {
            "ok": True,
            "status": {
                "files": len(idx["files"]),
                "chunks": len(idx["chunks"]),
                "embedded": sum(1 for c in idx["chunks"] if c.get("vec") is not None),
                "embed_model": _embed_model() or "(none — BM25 only)",
                "index_file": str(index_path()),
            },
        }

    if rebuild:
        idx["files"], idx["chunks"] = {}, []

    roots = [Path(p) for p in paths] if paths else _default_paths()
    seen: set[str] = set()
    added = changed = skipped = 0

    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in p.parts):
                continue
            if p.suffix.lower() not in TEXT_EXTS:
                continue
            limit = MAX_PDF_BYTES if p.suffix.lower() == ".pdf" else MAX_FILE_BYTES
            if p.stat().st_size > limit:
                continue
            key = str(p).lower()
            seen.add(key)
            st = p.stat()
            sig = (int(st.st_mtime), st.st_size)
            prev = idx["files"].get(key)
            if prev and prev["sig"] == sig and not rebuild:
                skipped += 1
                continue
            text = _extract_text(p)
            if text is None or not text.strip():
                continue
            chunks = _chunk_text(text)
            if not chunks:
                continue
            # drop old chunks for this file
            idx["chunks"] = [c for c in idx["chunks"] if c["source"] != key]
            for j, ct in enumerate(chunks):
                idx["chunks"].append({"source": key, "text": ct, "i": j, "vec": None})
            idx["files"][key] = {"sig": list(sig), "chunks": len(chunks),
                                 "hash": hashlib.md5(text.encode("utf-8", "replace")).hexdigest()[:8]}
            if prev:
                changed += 1
            else:
                added += 1

    # drop files no longer on disk — but ONLY within the roots scanned this
    # run: an explicit-paths call must not trash the rest of the index.
    root_prefixes = [str(r).lower().rstrip("\\/") for r in roots]
    stale = [k for k in idx["files"]
             if k not in seen and any(k.startswith(rp) for rp in root_prefixes)]
    for k in stale:
        del idx["files"][k]
        idx["chunks"] = [c for c in idx["chunks"] if c["source"] != k]

    embedded = _embed_pending()
    _save()
    return {
        "ok": True,
        "added": added, "changed": changed, "skipped": skipped,
        "removed": len(stale), "embedded_now": embedded,
        "total_files": len(idx["files"]), "total_chunks": len(idx["chunks"]),
        "embed_model": _embed_model() or "(none — BM25 only)",
    }
