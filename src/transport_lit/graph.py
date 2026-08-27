"""Citation graph, backed by OpenAlex and cached locally.

Any local record is resolved to an OpenAlex work: ``openalex:W…`` directly, otherwise by
DOI, then PMID, then an exact normalised title + year match (flagged ``match="title"``).
References come from the work's ``referenced_works``; citing works from
``works?filter=cites:W…``.  Edges are stored in ``citations`` and work identities in
``works``; repeat calls are local until the cache is older than ``STALE_DAYS``.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config
from .oai import RateLimiter
from .store import Store, normalize_id, utcnow

log = logging.getLogger(__name__)

OPENALEX = "https://api.openalex.org"
STALE_DAYS = 90
MAX_CITING = 2000
_limiter = RateLimiter(0.2)
_client: httpx.Client | None = None

FetchFn = Callable[[str, dict[str, Any] | None], dict[str, Any] | None]


def _http_fetch(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    global _client
    if _client is None:
        _client = httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT)
    p = dict(params or {})
    p["mailto"] = config.CONTACT_EMAIL or "transport-lit@example.invalid"
    for attempt in range(4):
        _limiter.wait()
        r = _client.get(OPENALEX + path, params=p)
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503):
            import time
            time.sleep(2.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"OpenAlex {path}: gave up after retries ({r.status_code})")


fetch: FetchFn = _http_fetch  # tests replace this


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _titles_match(a: str, b: str) -> bool:
    """Exact normalised match, or one title a prefix of the other (edition/subtitle tails),
    or Jaccard token similarity >= 0.8 for titles of at least four words."""
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    if min(len(ta), len(tb)) < 4:
        return False
    if a.startswith(b) or b.startswith(a):
        return True
    return len(ta & tb) / len(ta | tb) >= 0.8


def _wid(url_or_id: str) -> str:
    return (url_or_id or "").rsplit("/", 1)[-1]


def _work_row(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "openalex_id": _wid(w["id"]),
        "doi": (w.get("doi") or "").replace("https://doi.org/", "").lower() or None,
        "pmid": _wid((w.get("ids") or {}).get("pmid", "")) or None,
        "title": w.get("display_name") or w.get("title") or "",
        "year": w.get("publication_year"),
        "cited_by_count": w.get("cited_by_count"),
        "type": w.get("type"),
        "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
    }


class Graph:
    def __init__(self, store: Store):
        self.s = store
        self.s.conn.executescript("""
        CREATE TABLE IF NOT EXISTS works (
            openalex_id TEXT PRIMARY KEY, record_id TEXT, doi TEXT, pmid TEXT, title TEXT, year INTEGER,
            cited_by_count INTEGER, type TEXT, venue TEXT, match TEXT,
            refs_fetched_at TEXT, cites_fetched_at TEXT, cites_truncated INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_works_record ON works(record_id);
        CREATE INDEX IF NOT EXISTS idx_works_doi ON works(doi);
        CREATE INDEX IF NOT EXISTS idx_works_pmid ON works(pmid);
        CREATE TABLE IF NOT EXISTS citations (
            citing TEXT NOT NULL, cited TEXT NOT NULL, PRIMARY KEY (citing, cited)
        );
        CREATE INDEX IF NOT EXISTS idx_citations_cited ON citations(cited);
        CREATE TABLE IF NOT EXISTS unresolved (record_id TEXT PRIMARY KEY, tried_at TEXT);
        """)
        self.s.conn.commit()

    # -- identity -------------------------------------------------------------------------
    def _upsert_work(self, row: dict[str, Any], record_id: str | None = None, match: str | None = None) -> None:
        if record_id is None:
            record_id, match = self._local_for(row)
        cols = ["openalex_id", "doi", "pmid", "title", "year", "cited_by_count", "type", "venue"]
        self.s.conn.execute(
            f"INSERT INTO works({', '.join(cols)}, record_id, match) VALUES ({', '.join('?' for _ in cols)}, ?, ?) "
            "ON CONFLICT(openalex_id) DO UPDATE SET doi=COALESCE(excluded.doi, works.doi), pmid=COALESCE(excluded.pmid, works.pmid), "
            "title=excluded.title, year=COALESCE(excluded.year, works.year), cited_by_count=COALESCE(excluded.cited_by_count, works.cited_by_count), "
            "type=COALESCE(excluded.type, works.type), venue=COALESCE(excluded.venue, works.venue), "
            "record_id=COALESCE(works.record_id, excluded.record_id), match=COALESCE(works.match, excluded.match)",
            [row.get(c) for c in cols] + [record_id, match])

    def _local_for(self, row: dict[str, Any]) -> tuple[str | None, str | None]:
        """Map an OpenAlex work to a record already in the index: (record_id, match)."""
        c = self.s.conn
        r = c.execute("SELECT id FROM records WHERE id = ?", (f"openalex:{row['openalex_id']}",)).fetchone()
        if r:
            return r[0], "openalex"
        if row.get("doi"):
            r = c.execute("SELECT id FROM records WHERE lower(doi) = ? LIMIT 1", (row["doi"],)).fetchone()
            if r:
                return r[0], "doi"
        if row.get("pmid"):
            r = c.execute("SELECT id FROM records WHERE id = ?", (f"pubmed:{row['pmid']}",)).fetchone()
            if r:
                return r[0], "pmid"
        return None, None

    def resolve(self, record_id: str, *, refresh: bool = False) -> dict[str, Any] | None:
        """OpenAlex work for a local record (cached).  Returns the works row or None."""
        rid = normalize_id(record_id)
        c = self.s.conn
        row = c.execute("SELECT * FROM works WHERE record_id = ?", (rid,)).fetchone()
        if row:
            return dict(row)
        if not refresh and c.execute("SELECT 1 FROM unresolved WHERE record_id = ? AND tried_at > ?",
                     (rid, _ts(datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)))).fetchone():
            return None
        rec = self.s.get_record(rid)
        if not rec:
            return None
        w = None
        match = None
        if rid.startswith("openalex:"):
            w, match = fetch(f"/works/{rid.split(':', 1)[1]}"), "openalex"
        if w is None and rec.get("doi"):
            w, match = fetch(f"/works/https://doi.org/{rec['doi']}"), "doi"
        if w is None and rid.startswith("pubmed:"):
            w, match = fetch(f"/works/pmid:{rid.split(':', 1)[1]}"), "pmid"
        if w is None and rec.get("title"):
            res = fetch("/works", {"filter": f"title.search:{_search_safe(rec['title'])}", "per-page": 5})
            want = _norm_title(rec["title"])
            for cand in (res or {}).get("results", []):
                have = _norm_title(cand.get("display_name") or "")
                year_ok = (not rec.get("year") or not cand.get("publication_year")
                           or abs(cand["publication_year"] - rec["year"]) <= 1)
                if year_ok and _titles_match(want, have):
                    w, match = cand, "title"
                    break
        if w is None:
            c.execute("INSERT INTO unresolved(record_id, tried_at) VALUES (?, ?) ON CONFLICT(record_id) DO UPDATE SET tried_at=excluded.tried_at",
                      (rid, utcnow()))
            c.commit()
            return None
        row = _work_row(w)
        self._upsert_work(row, record_id=rid, match=match)
        c.commit()
        return dict(c.execute("SELECT * FROM works WHERE openalex_id = ?", (row["openalex_id"],)).fetchone())

    # -- edges ------------------------------------------------------------------------------
    def references(self, record_id: str, *, refresh: bool = False) -> dict[str, Any]:
        w = self.resolve(record_id, refresh=refresh)
        if not w:
            return {"id": normalize_id(record_id), "resolved": False, "references": []}
        c = self.s.conn
        if refresh or not w.get("refs_fetched_at"):
            full = fetch(f"/works/{w['openalex_id']}") or {}
            refs = [_wid(x) for x in full.get("referenced_works") or []]
            c.executemany("INSERT OR IGNORE INTO citations(citing, cited) VALUES (?, ?)", [(w["openalex_id"], r) for r in refs])
            self._hydrate(refs)
            c.execute("UPDATE works SET refs_fetched_at = ?, cited_by_count = COALESCE(?, cited_by_count) WHERE openalex_id = ?",
                      (utcnow(), full.get("cited_by_count"), w["openalex_id"]))
            c.commit()
        rows = c.execute("""SELECT k.* FROM citations e JOIN works k ON k.openalex_id = e.cited
                            WHERE e.citing = ? ORDER BY k.year DESC""", (w["openalex_id"],)).fetchall()
        return {"id": normalize_id(record_id), "resolved": True, "openalex_id": w["openalex_id"], "match": w.get("match"),
                "n": len(rows), "in_index": sum(1 for r in rows if r["record_id"]), "references": [dict(r) for r in rows]}

    def citations(self, record_id: str, *, refresh: bool = False, only_in_index: bool = False) -> dict[str, Any]:
        w = self.resolve(record_id, refresh=refresh)
        if not w:
            return {"id": normalize_id(record_id), "resolved": False, "citations": []}
        c = self.s.conn
        stale = not w.get("cites_fetched_at") or w["cites_fetched_at"] < _ts(datetime.now(timezone.utc) - timedelta(days=STALE_DAYS))
        if refresh or stale:
            cursor, got, truncated = "*", 0, 0
            while cursor:
                page = fetch("/works", {"filter": f"cites:{w['openalex_id']}", "per-page": 200, "cursor": cursor,
                                        "select": "id,doi,ids,display_name,publication_year,cited_by_count,type,primary_location"}) or {}
                for cw in page.get("results", []):
                    row = _work_row(cw)
                    self._upsert_work(row)
                    c.execute("INSERT OR IGNORE INTO citations(citing, cited) VALUES (?, ?)", (row["openalex_id"], w["openalex_id"]))
                    got += 1
                cursor = (page.get("meta") or {}).get("next_cursor")
                if not page.get("results") or got >= MAX_CITING:
                    truncated = int(bool(cursor) and got >= MAX_CITING)
                    break
            c.execute("UPDATE works SET cites_fetched_at = ?, cites_truncated = ? WHERE openalex_id = ?", (utcnow(), truncated, w["openalex_id"]))
            c.commit()
        q = """SELECT k.* FROM citations e JOIN works k ON k.openalex_id = e.citing WHERE e.cited = ?"""
        if only_in_index:
            q += " AND k.record_id IS NOT NULL"
        rows = c.execute(q + " ORDER BY k.year DESC", (w["openalex_id"],)).fetchall()
        w2 = dict(c.execute("SELECT * FROM works WHERE openalex_id = ?", (w["openalex_id"],)).fetchone())
        return {"id": normalize_id(record_id), "resolved": True, "openalex_id": w["openalex_id"], "match": w.get("match"),
                "cited_by_count_openalex": w2.get("cited_by_count"), "n": len(rows),
                "in_index": sum(1 for r in rows if r["record_id"]), "truncated": bool(w2.get("cites_truncated")),
                "citations": [dict(r) for r in rows]}

    def _hydrate(self, wids: list[str]) -> None:
        """Fetch identity rows for OpenAlex ids we only know as edges (50 per request)."""
        c = self.s.conn
        missing = [w for w in wids if not c.execute("SELECT 1 FROM works WHERE openalex_id = ? AND title != ''", (w,)).fetchone()]
        for i in range(0, len(missing), 50):
            chunk = missing[i: i + 50]
            page = fetch("/works", {"filter": "openalex:" + "|".join(chunk), "per-page": 50,
                                    "select": "id,doi,ids,display_name,publication_year,cited_by_count,type,primary_location"}) or {}
            seen = set()
            for cw in page.get("results", []):
                row = _work_row(cw)
                seen.add(row["openalex_id"])
                self._upsert_work(row)
            for w in chunk:
                if w not in seen:
                    c.execute("INSERT OR IGNORE INTO works(openalex_id, title) VALUES (?, '')", (w,))

    def cited_by_counts(self, record_ids: list[str]) -> dict[str, int]:
        if not record_ids:
            return {}
        out: dict[str, int] = {}
        for i in range(0, len(record_ids), 500):
            chunk = record_ids[i: i + 500]
            for r in self.s.conn.execute(f"SELECT record_id, cited_by_count FROM works WHERE record_id IN ({','.join('?' for _ in chunk)}) AND cited_by_count IS NOT NULL", chunk):
                out[r[0]] = r[1]
        return out

    def stats(self) -> dict[str, Any]:
        c = self.s.conn
        return {"works": c.execute("SELECT COUNT(*) FROM works").fetchone()[0],
                "works_in_index": c.execute("SELECT COUNT(*) FROM works WHERE record_id IS NOT NULL").fetchone()[0],
                "edges": c.execute("SELECT COUNT(*) FROM citations").fetchone()[0],
                "unresolved": c.execute("SELECT COUNT(*) FROM unresolved").fetchone()[0]}


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _search_safe(title: str) -> str:
    return re.sub(r"[,:;|&()\"']", " ", title)[:200]
