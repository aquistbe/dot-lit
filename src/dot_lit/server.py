"""FastMCP server exposing the local ROSA-P index.

Run with ``dot-lit-mcp`` (stdio).  All tools are read-only against the local SQLite
store except ``get_fulltext``, which may fetch one PDF from ROSA-P on a cache miss.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # mcp >= 2: FastMCP was renamed MCPServer (same decorator API)
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import config
from .fulltext import get_fulltext as _get_fulltext
from .harvest import status as _status
from .store import Store, normalize_id

log = logging.getLogger(__name__)

mcp = FastMCP(
    "dot-lit",
    instructions=(
        "Searchable local index of transportation grey literature. Sources (id prefix): "
        "ROSA-P / U.S. DOT National Transportation Library (dot:), VTI Sweden (vti:), BASt Germany (bast:), "
        "World Bank OKR (wbokr:), IPEA Brazil (ipea:), CEPAL (cepal:), plus TRID exports imported by the "
        "user (trid:). Use the `collection` filter to restrict to one source (e.g. 'VTI', 'BASt', 'World Bank', 'TRID'). "
        "Use search_reports first; use get_report for full metadata; get_fulltext extracts "
        "PDF text. Search is FTS5 over title/abstract/subjects/authors/report numbers: all "
        "terms must match first (match_mode=all_terms), then any-term matches fill the "
        "remaining slots (any_terms). Quote phrases; a trailing * is a prefix. "
        "Call harvest_status if results look thin — the index is only as complete as the "
        "last harvest."
    ),
)

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(config.DB_PATH)
    return _store


def _hit(r: dict[str, Any]) -> dict[str, Any]:
    abstract = r.get("abstract") or ""
    return {
        "id": r["id"],
        "title": r.get("title"),
        "authors": r.get("authors") or [],
        "corporate_authors": r.get("corporate_authors") or [],
        "year": r.get("year"),
        "year_source": r.get("year_source"),
        "doc_type": r.get("doc_type"),
        "report_numbers": r.get("report_numbers") or [],
        "doi": r.get("doi") or None,
        "landing_url": r.get("landing_url"),
        "collections": r.get("collections") or [],
        "abstract_snippet": r.get("snippet") or abstract[:400],
        "match_mode": r.get("match_mode"),
        "score": round(float(r.get("score") or 0), 3),
    }


@mcp.tool()
def search_reports(
    query: str,
    year_min: int | None = None,
    year_max: int | None = None,
    collection: str | None = None,
    doc_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Full-text search of the harvested ROSA-P index.

    Args:
        query: Keywords; quote phrases ("driver improvement"); trailing * = prefix.
        year_min / year_max: Inclusive publication-year filter.
        collection: Substring filter on ROSA-P collection name (see list_collections),
            e.g. "NHTSA", "Federal Highway Administration", "University Transportation Centers".
        doc_type: Substring filter on document type, e.g. "Tech Report", "Dataset".
        limit: Max hits (1-100).
    Returns ranked hits with id, title, authors, year, report numbers, DOI, landing URL and
    an abstract snippet. ``match_mode`` tells whether all query terms matched.
    """
    limit = max(1, min(int(limit or 20), 100))
    hits = store().search(query, year_min=year_min, year_max=year_max, collection=collection,
                          doc_type=doc_type, limit=limit)
    return {
        "query": query,
        "filters": {k: v for k, v in dict(year_min=year_min, year_max=year_max,
                                            collection=collection, doc_type=doc_type).items() if v},
        "n": len(hits),
        "index_size": store().count(),
        "hits": [_hit(h) for h in hits],
    }


@mcp.tool()
def get_report(id: str) -> dict[str, Any]:
    """Full metadata record for one report. Accepts 'dot:93144', '93144', the OAI
    identifier, or the ROSA-P landing URL. Includes every raw Dublin Core field."""
    rec = store().get_record(id)
    if not rec:
        return {"error": f"no record {normalize_id(id)} in the local index", "id": normalize_id(id)}
    rec["pdf_url_hint"] = f"{rec['landing_url']}/dot_{rec['id'].split(':')[-1]}_DS1.pdf"
    ft = store().get_fulltext(rec["id"])
    rec["fulltext_cached"] = bool(ft and ft.get("status") == "ok")
    return rec


@mcp.tool()
def get_fulltext(id: str, max_chars: int = 40000, offset: int = 0, refresh: bool = False) -> dict[str, Any]:
    """Resolve the report's PDF on ROSA-P, extract its text and return it (cached
    locally after the first call). Page through long documents with ``offset``.
    Text is marked with [[page N]] separators. Scanned PDFs return status 'no_text'."""
    rid = normalize_id(id)
    rec = store().get_record(rid)
    ft = _get_fulltext(store(), rid, refresh=refresh)
    text = ft.get("text") or ""
    max_chars = max(1000, min(int(max_chars), 200000))
    chunk = text[offset: offset + max_chars]
    return {
        "id": rid,
        "title": rec.get("title") if rec else None,
        "status": ft.get("status"),
        "pdf_url": ft.get("pdf_url"),
        "n_pages": ft.get("n_pages"),
        "n_chars": ft.get("n_chars"),
        "offset": offset,
        "returned_chars": len(chunk),
        "has_more": offset + len(chunk) < len(text),
        "error": ft.get("error"),
        "text": chunk,
    }


@mcp.tool()
def list_collections() -> dict[str, Any]:
    """Collections (ROSA-P ``relation.isPartOf`` values) with record counts, plus document
    types. ROSA-P's OAI-PMH endpoint exposes no OAI sets, so these are derived from the
    harvested metadata and are what the ``collection`` filter matches against."""
    s = store()
    return {"oai_sets": [], "note": "ListSets is empty on ROSA-P; collections come from dc:relation.isPartOf",
            "collections": s.collections(), "doc_types": s.doc_types()}


@mcp.tool()
def harvest_status() -> dict[str, Any]:
    """Record count, last harvest time/status, resumptions/notes, and coverage by year.
    If the last run is not 'complete' the index may be partial."""
    return _status(store())


def main() -> None:
    # stdout is the MCP transport; keep stderr quiet so Claude Desktop logs stay readable.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
