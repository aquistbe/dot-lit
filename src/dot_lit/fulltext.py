"""Resolve a record's PDF and extract its text (cached in the store and on disk).

ROSA-P does not put the file URL in the OAI metadata.  Two routes, tried in order:

1. The landing page carries ``<meta name="citation_pdf_url" content="...">`` (Google
   Scholar tag) — authoritative when present.
2. The conventional datastream URL ``/view/dot/{n}/dot_{n}_DS1.pdf`` — verified by HEAD.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx

from . import config
from .oai import RateLimiter
from .store import Store, normalize_id

log = logging.getLogger(__name__)

_META_PDF = re.compile(r'<meta\s+name="citation_pdf_url"\s+content="([^"]+)"', re.I)
_HREF_PDF = re.compile(r'href="(https?://rosap\.ntl\.bts\.gov/view/dot/\d+/[^"]+\.pdf)"', re.I)

_limiter = RateLimiter(config.MIN_REQUEST_INTERVAL)


def _http() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT,
                        follow_redirects=True)


def resolve_pdf_url(record_id: str, client: httpx.Client | None = None) -> dict[str, Any]:
    rid = normalize_id(record_id)
    n = rid.split(":")[-1]
    landing = f"{config.ROSAP_VIEW_BASE}{n}"
    guess = f"{landing}/dot_{n}_DS1.pdf"
    own = client is None
    client = client or _http()
    try:
        _limiter.wait()
        candidates: list[str] = []
        try:
            r = client.get(landing)
            if r.status_code == 200:
                m = _META_PDF.search(r.text)
                if m:
                    candidates.append(m.group(1))
                candidates += _HREF_PDF.findall(r.text)
        except httpx.HTTPError as exc:
            log.warning("landing page fetch failed for %s: %s", rid, exc)
        candidates.append(guess)
        seen: set[str] = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            _limiter.wait()
            try:
                h = client.head(url)
            except httpx.HTTPError:
                continue
            if h.status_code == 200 and "pdf" in h.headers.get("content-type", "").lower():
                size = int(h.headers.get("content-length") or 0)
                return {"pdf_url": url, "content_length": size, "landing_url": landing,
                        "all_candidates": [c for c in candidates if c != url and c in seen] + [url]}
        return {"pdf_url": None, "landing_url": landing, "all_candidates": list(seen)}
    finally:
        if own:
            client.close()


def _download(url: str, dest: Path, client: httpx.Client) -> int:
    total = 0
    _limiter.wait()
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 16):
                total += len(chunk)
                if total > config.MAX_PDF_BYTES:
                    raise ValueError(f"PDF exceeds MAX_PDF_BYTES ({config.MAX_PDF_BYTES})")
                fh.write(chunk)
    return total


def _extract_text(path: Path) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            pass
    n_pages = len(reader.pages)
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        if i >= config.MAX_PDF_PAGES:
            parts.append(f"\n[truncated: {n_pages - i} more pages beyond MAX_PDF_PAGES={config.MAX_PDF_PAGES}]")
            break
        try:
            t = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            t = f"[page {i + 1}: extraction error {exc}]"
        parts.append(f"\n\n[[page {i + 1}]]\n{t}")
    text = "".join(parts).strip()
    # collapse excessive whitespace but keep paragraph breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, n_pages


def get_fulltext(store: Store, record_id: str, *, refresh: bool = False) -> dict[str, Any]:
    rid = normalize_id(record_id)
    cached = store.get_fulltext(rid)
    if cached and not refresh and cached.get("status") in {"ok", "no_pdf", "no_text", "too_large"}:
        return cached
    config.ensure_dirs()
    with _http() as client:
        resolved = resolve_pdf_url(rid, client)
        url = resolved.get("pdf_url")
        if not url:
            store.put_fulltext(rid, pdf_url=None, status="no_pdf", n_pages=0, n_chars=0, text=None,
                               error="no PDF linked from landing page or datastream URL")
            return store.get_fulltext(rid) or {}
        if resolved.get("content_length", 0) > config.MAX_PDF_BYTES:
            store.put_fulltext(rid, pdf_url=url, status="too_large", n_pages=0, n_chars=0, text=None,
                               error=f"content-length {resolved['content_length']} > {config.MAX_PDF_BYTES}")
            return store.get_fulltext(rid) or {}
        dest = config.PDF_DIR / f"{rid.replace(':', '_')}.pdf"
        try:
            if not dest.exists() or refresh:
                _download(url, dest, client)
            text, n_pages = _extract_text(dest)
        except Exception as exc:  # noqa: BLE001
            store.put_fulltext(rid, pdf_url=url, status="error", n_pages=0, n_chars=0, text=None, error=repr(exc))
            return store.get_fulltext(rid) or {}
    if len(re.sub(r"\[\[page \d+\]\]|\s", "", text)) < 200:
        store.put_fulltext(rid, pdf_url=url, status="no_text", n_pages=n_pages, n_chars=len(text), text=text,
                           error="PDF has little/no extractable text (probably scanned; OCR not attempted)")
    else:
        store.put_fulltext(rid, pdf_url=url, status="ok", n_pages=n_pages, n_chars=len(text), text=text, error=None)
    return store.get_fulltext(rid) or {}
