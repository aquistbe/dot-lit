"""Optional semantic search: embeddings stored as a memory-mapped matrix next to the index.

Backends
--------
* ``fastembed`` (default, CPU, no account): ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
  — 384 dims, ~220 MB one-time download, multilingual (en/sv/de/es/pt/ja/…).
  Installed with the ``semantic`` extra:  ``uv tool install "transport-lit[semantic]"``.
* ``ollama``: any embedding model served by a local Ollama (``qwen3-embedding:0.6b`` or
  ``:8b``).  Vectors are Matryoshka-truncated to ``TRANSPORT_LIT_EMBED_DIM`` (default 1024) and
  re-normalised.  Needs nothing but httpx.

Storage: ``$TRANSPORT_LIT_DATA_DIR/vectors/<slug>/{ids.json, vecs.npy (float16), meta.json}``;
the active set is recorded in the store's ``meta`` table.  Search is a chunked dot product
over the memory-mapped matrix (≈50 ms for 350k × 384), no extension required.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from . import config
from .store import Store, utcnow

log = logging.getLogger(__name__)

DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_OLLAMA_MODELS = ("qwen3-embedding:8b", "qwen3-embedding:0.6b", "nomic-embed-text", "mxbai-embed-large")
VEC_DIR = config.DATA_DIR / "vectors"
MODEL_DIR = config.DATA_DIR / "models"


def _norm(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def record_text(rec: dict[str, Any]) -> str:
    parts = [rec.get("title") or ""]
    if rec.get("abstract"):
        parts.append((rec["abstract"] or "")[:1500])
    subs = rec.get("subjects") or []
    if subs:
        parts.append("; ".join(subs[:10]))
    return "\n".join(p for p in parts if p)


class FastEmbedBackend:
    name = "fastembed"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("TRANSPORT_LIT_EMBED_MODEL") or DEFAULT_FASTEMBED_MODEL
        self._m = None
        self.dim = 0

    def _load(self):
        if self._m is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("fastembed is not installed; `uv tool install 'transport-lit[semantic]'` "
                                   "or use --backend ollama") from exc
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            self._m = TextEmbedding(model_name=self.model, cache_dir=str(MODEL_DIR))
            info = next((m for m in TextEmbedding.list_supported_models() if m["model"] == self.model), None)
            self.dim = int(info["dim"]) if info else 0
        return self._m

    def embed(self, texts: list[str]) -> np.ndarray:
        m = self._load()
        vecs = _norm(np.stack(list(m.embed(texts, batch_size=64))))
        self.dim = vecs.shape[1]
        return vecs

    def embed_stream(self, texts: Iterable[str], batch: int = 64) -> Iterable[np.ndarray]:
        """Lazy, data-parallel encoding for large runs (fastembed spawns worker processes
        once per call, so one long call beats many short ones)."""
        m = self._load()
        parallel = int(os.environ.get("TRANSPORT_LIT_EMBED_PARALLEL") or os.environ.get("DOT_LIT_EMBED_PARALLEL")
                       or max(1, min(8, (os.cpu_count() or 2) - 2)))
        for v in m.embed(texts, batch_size=batch, parallel=parallel if parallel > 1 else None):
            yield _norm(np.asarray(v, dtype=np.float32)[None, :])[0]

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    @property
    def slug(self) -> str:
        return "fastembed-" + re.sub(r"[^a-z0-9]+", "-", self.model.lower()).strip("-")


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str | None = None, host: str | None = None, dim: int | None = None):
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        self.model = model or os.environ.get("TRANSPORT_LIT_EMBED_MODEL") or self._pick()
        self.max_dim = int(dim or os.environ.get("TRANSPORT_LIT_EMBED_DIM") or 1024)
        self.dim = 0
        self._client = httpx.Client(timeout=600)

    def _pick(self) -> str:
        try:
            tags = {m["name"] for m in httpx.get(f"{self.host}/api/tags", timeout=5).json().get("models", [])}
        except Exception:  # noqa: BLE001
            tags = set()
        for cand in DEFAULT_OLLAMA_MODELS:
            if cand in tags or cand + ":latest" in tags:
                return cand
        return DEFAULT_OLLAMA_MODELS[1]

    def embed(self, texts: list[str]) -> np.ndarray:
        r = self._client.post(f"{self.host}/api/embed", json={"model": self.model, "input": texts})
        r.raise_for_status()
        vecs = np.asarray(r.json()["embeddings"], dtype=np.float32)
        if vecs.shape[1] > self.max_dim:   # Matryoshka truncation (qwen3-embedding supports 32..4096)
            vecs = vecs[:, : self.max_dim]
        vecs = _norm(vecs)
        self.dim = vecs.shape[1]
        return vecs

    def embed_stream(self, texts: Iterable[str], batch: int = 32) -> Iterable[np.ndarray]:
        buf: list[str] = []
        for t in texts:
            buf.append(t)
            if len(buf) >= batch:
                yield from self.embed(buf)
                buf = []
        if buf:
            yield from self.embed(buf)

    def embed_query(self, text: str) -> np.ndarray:
        if "qwen3" in self.model:
            text = f"Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: {text}"
        return self.embed([text])[0]

    @property
    def slug(self) -> str:
        return "ollama-" + re.sub(r"[^a-z0-9]+", "-", self.model.lower()).strip("-") + f"-{self.max_dim}"


def make_backend(name: str | None = None, model: str | None = None, **kw) -> FastEmbedBackend | OllamaBackend:
    name = name or os.environ.get("TRANSPORT_LIT_EMBED_BACKEND") or "fastembed"
    if name == "ollama":
        return OllamaBackend(model, **kw)
    if name == "fastembed":
        return FastEmbedBackend(model)
    raise ValueError(f"unknown embedding backend {name!r} (fastembed|ollama)")


class VectorIndex:
    """Append-only id list + float16 matrix.  Rows for records later pruned from the store
    are simply ignored at query time (their ids no longer resolve)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.ids: list[str] = []
        self._pos: dict[str, int] = {}
        self.vecs: np.ndarray | None = None
        self.meta: dict[str, Any] = {}
        if (self.path / "meta.json").exists():
            self.meta = json.loads((self.path / "meta.json").read_text())
            self.ids = json.loads((self.path / "ids.json").read_text())
            self._pos = {i: k for k, i in enumerate(self.ids)}
            self.vecs = np.load(self.path / "vecs.npy", mmap_mode="r")

    def __len__(self) -> int:
        return len(self.ids)

    def has(self, rid: str) -> bool:
        return rid in self._pos

    def add(self, ids: list[str], vecs: np.ndarray, meta: dict[str, Any]) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        new = np.asarray(vecs, dtype=np.float16)
        if self.vecs is not None and len(self.ids):
            new = np.concatenate([np.asarray(self.vecs, dtype=np.float16), new])
        tmp = self.path / "vecs.tmp.npy"
        np.save(tmp, new)
        tmp.replace(self.path / "vecs.npy")
        self.ids = self.ids + list(ids)
        self._pos = {i: k for k, i in enumerate(self.ids)}
        (self.path / "ids.json").write_text(json.dumps(self.ids))
        self.meta = {**self.meta, **meta, "n": len(self.ids), "dim": int(new.shape[1]), "updated_at": utcnow()}
        (self.path / "meta.json").write_text(json.dumps(self.meta, indent=1))
        self.vecs = np.load(self.path / "vecs.npy", mmap_mode="r")

    def search(self, q: np.ndarray, k: int = 50, chunk: int = 65536,
               allowed_ids: set[str] | None = None) -> list[tuple[str, float]]:
        if self.vecs is None or not len(self.ids):
            return []
        q = np.asarray(q, dtype=np.float32)
        n = len(self.ids)
        best_s = np.empty(0, dtype=np.float32)
        best_i = np.empty(0, dtype=np.int64)
        for start in range(0, n, chunk):
            block = np.asarray(self.vecs[start: start + chunk], dtype=np.float32)
            s = block @ q
            if allowed_ids is not None:
                allowed = np.array([rid in allowed_ids for rid in self.ids[start: start + chunk]])
                s[~allowed] = -np.inf
            kk = min(k, len(s))
            idx = np.argpartition(-s, kk - 1)[:kk]
            best_s = np.concatenate([best_s, s[idx]])
            best_i = np.concatenate([best_i, idx + start])
            if len(best_s) > 4 * k:
                keep = np.argpartition(-best_s, k - 1)[:k]
                best_s, best_i = best_s[keep], best_i[keep]
        order = np.argsort(-best_s)[:k]
        return [(self.ids[int(best_i[j])], float(best_s[j])) for j in order if np.isfinite(best_s[j])]


def active_index(store: Store) -> VectorIndex | None:
    slug = store.get_meta("embeddings.active")
    if not slug or not (VEC_DIR / slug / "meta.json").exists():
        return None
    return VectorIndex(VEC_DIR / slug)


def backend_for(index: VectorIndex) -> FastEmbedBackend | OllamaBackend:
    m = index.meta
    return _cached_backend(m.get("backend"), m.get("model"), m.get("dim"))


@lru_cache(maxsize=4)
def _cached_backend(backend: str, model: str, dim: int):
    # Loading an ONNX model on every MCP search dominates query latency.
    if backend == "ollama":
        return OllamaBackend(model, dim=dim)
    return FastEmbedBackend(model)


def embed_records(store: Store, backend: FastEmbedBackend | OllamaBackend, *, rebuild: bool = False,
                  limit: int | None = None, sources: list[str] | None = None, batch: int = 64,
                  progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda m: log.info(m))
    path = VEC_DIR / backend.slug
    if rebuild and path.exists():
        for f in path.iterdir():
            f.unlink()
    index = VectorIndex(path)
    where = ""
    params: list[Any] = []
    if sources:
        pfx = ["dot" if s_ in ("rosap", "dot") else s_ for s_ in sources]
        where = "WHERE " + " OR ".join("id LIKE ?" for _ in pfx)
        params = [f"{p_}:%" for p_ in pfx]
    rows = store.conn.execute(f"SELECT id, title, abstract, subjects FROM records {where} ORDER BY first_seen_at DESC", params)
    todo: list[tuple[str, str]] = []
    for r in rows:
        if index.has(r["id"]):
            continue
        rec = {"title": r["title"], "abstract": r["abstract"], "subjects": json.loads(r["subjects"] or "[]")}
        todo.append((r["id"], record_text(rec)))
        if limit and len(todo) >= limit:
            break
    say(f"embedding {len(todo)} records with {backend.name}:{backend.model} (index has {len(index)})")
    done = 0
    import time
    t0 = time.time()
    buf_ids: list[str] = []
    buf_vecs: list[np.ndarray] = []
    stream = getattr(backend, "embed_stream", None)
    if stream is None:  # simple backends: batch calls
        def stream(texts, batch=batch):
            texts = list(texts)
            for i in range(0, len(texts), batch):
                yield from backend.embed(texts[i: i + batch])
    for (rid, _), vec in zip(todo, stream((t for _, t in todo), batch)):
        buf_ids.append(rid)
        buf_vecs.append(vec)
        done += 1
        if len(buf_vecs) >= 5000 or done == len(todo):
            index.add(buf_ids, np.stack(buf_vecs), {"backend": backend.name, "model": backend.model})
            buf_ids, buf_vecs = [], []
            rate = done / max(time.time() - t0, 1e-6)
            say(f"{done}/{len(todo)} embedded ({rate:.0f}/s, ~{(len(todo) - done) / max(rate, 1e-6) / 60:.0f} min left)")
    if len(index):
        store.set_meta("embeddings.active", backend.slug)
    return {"backend": backend.name, "model": backend.model, "slug": backend.slug, "embedded_now": done,
            "index_size": len(index), "dim": index.meta.get("dim"), "records_in_store": store.count()}


def semantic_candidates(store: Store, query: str, k: int, **filters: Any) -> list[tuple[str, float]]:
    index = active_index(store)
    if index is None:
        return []
    backend = backend_for(index)
    clauses, params = store._filters(filters.get("year_min"), filters.get("year_max"), filters.get("collection"),
                                    filters.get("doc_type"), filters.get("sources"))
    allowed = {r[0] for r in store.conn.execute(f"SELECT r.id FROM records r WHERE 1=1 {clauses}", params)}
    # Filter before top-k selection so a small source is not hidden by a larger one.
    return index.search(backend.embed_query(query), k=k, allowed_ids=allowed)


SEMANTIC_WEIGHT = float(os.environ.get("TRANSPORT_LIT_SEMANTIC_WEIGHT", "0.7"))  # keyword list weight is 1.0
SEMANTIC_PER_SOURCE = float(os.environ.get("TRANSPORT_LIT_SEMANTIC_PER_SOURCE", "0.5"))  # max share of one source in semantic results


def diversify(ranked: list[str], limit: int, share: float = SEMANTIC_PER_SOURCE) -> list[str]:
    """Cap any one source (id prefix) at `share` of the first `limit` results; overflow is
    appended afterwards in score order.  Counters corpus-composition dominance (CiNii is a
    third of the index and full of short titles that nearly equal a short query)."""
    if share >= 1.0 or limit <= 0:
        return ranked
    cap = max(3, int(round(limit * share)))
    head: list[str] = []
    tail: list[str] = []
    seen: dict[str, int] = {}
    for rid in ranked:
        src = rid.split(":", 1)[0]
        if len(head) < limit and seen.get(src, 0) < cap:
            head.append(rid)
            seen[src] = seen.get(src, 0) + 1
        else:
            tail.append(rid)
    return head + tail
SEMANTIC_MIN = float(os.environ.get("TRANSPORT_LIT_SEMANTIC_MIN", "0.5"))        # cosine floor for semantic-only hits


def rrf(rankings: Iterable[list[str]], k: int = 60, weights: Iterable[float] | None = None) -> dict[str, float]:
    scores: dict[str, float] = {}
    ws = list(weights) if weights is not None else None
    for i, ranking in enumerate(rankings):
        w = ws[i] if ws else 1.0
        for rank, rid in enumerate(ranking):
            scores[rid] = scores.get(rid, 0.0) + w / (k + rank + 1)
    return scores


def hybrid_search(store: Store, query: str, *, mode: str = "hybrid", limit: int = 20, offset: int = 0,
                  **filters: Any) -> tuple[list[dict[str, Any]], str]:
    """Returns (hits, mode_used).  Falls back to keyword when no vectors are available."""
    want = limit + offset
    keyword = store.search(query, limit=want * 3, **filters) if mode != "semantic" else []
    sem: list[tuple[str, float]] = []
    if mode in ("hybrid", "semantic"):
        try:
            # a wide pool so diversification has other sources to draw from (dot product is
            # over the whole matrix anyway; only the top-k bookkeeping grows)
            sem = semantic_candidates(store, query, k=max(want * 10, 300), **filters)
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic search unavailable: %s", exc)
            sem = []
    if not sem:
        if mode == "semantic":
            return store.search(query, limit=limit, offset=offset, **filters), "keyword"
        return keyword[offset: offset + limit], "keyword"
    sem_ids = store.filter_ids([rid for rid, _ in sem], **filters)
    sem_scores = {rid: sc for rid, sc in sem}
    sem_rank = [rid for rid, _ in sem if rid in sem_ids]
    if not filters.get("sources") and not filters.get("collection"):
        sem_rank = diversify(sem_rank, want)
    if mode == "semantic":
        ordered = sem_rank
    else:
        kw_ids = {h["id"] for h in keyword}
        # semantic-only candidates must clear a cosine floor; keyword hits never need to
        sem_for_fusion = [rid for rid in sem_rank if rid in kw_ids or sem_scores.get(rid, 0.0) >= SEMANTIC_MIN]
        fused = rrf([[h["id"] for h in keyword], sem_for_fusion], weights=[1.0, SEMANTIC_WEIGHT])
        ordered = sorted(fused, key=lambda r: -fused[r])
    kw_by_id = {h["id"]: h for h in keyword}
    kw_rank = {h["id"]: i + 1 for i, h in enumerate(keyword)}
    sem_pos = {rid: i + 1 for i, rid in enumerate(sem_rank)}
    out = []
    for rid in ordered[offset: offset + limit]:
        h = kw_by_id.get(rid) or store.get_record(rid)
        if not h:
            continue
        h = dict(h)
        h["keyword_rank"] = kw_rank.get(rid)
        h["semantic_rank"] = sem_pos.get(rid)
        h["semantic_score"] = round(sem_scores[rid], 4) if rid in sem_scores else None
        h["match_mode"] = mode if rid in sem_pos and rid in kw_rank else ("semantic" if rid in sem_pos else h.get("match_mode"))
        if not h.get("snippet"):
            h["snippet"] = (h.get("abstract") or "")[:300]
        out.append(h)
    return out, mode
