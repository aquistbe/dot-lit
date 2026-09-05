"""FastMCP server exposing the local ROSA-P index.

Run with ``transport-lit-mcp`` (stdio).  All tools are read-only against the local SQLite
store except ``get_fulltext``, which may fetch one PDF from ROSA-P on a cache miss.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from mcp.types import ToolAnnotations

try:  # mcp >= 2: FastMCP was renamed MCPServer (same decorator API)
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import config
from .citations import to_bibtex, to_ris
from .embeddings import active_index, hybrid_search
from .fulltext import direct_pdf_urls, get_fulltext as _get_fulltext
from .graph import Graph
from .harvest import status as _status
from .store import Store, normalize_id

log = logging.getLogger(__name__)

mcp = FastMCP(
    "transport-lit",
    instructions=(
        "Searchable local index of transportation grey literature. Sources (id prefix): "
        "ROSA-P / U.S. DOT National Transportation Library (dot:), VTI Sweden (vti:), BASt Germany (bast:), "
        "World Bank OKR (wbokr:), IPEA Brazil (ipea:), CEPAL (cepal:), plus TRID exports imported by the "
        "user (trid:), OpenAlex reports (openalex:), CiNii Research (cinii:), and the PubMed transport subset (pubmed:). "
        "Use the `collection` filter to restrict to one source (e.g. 'VTI', 'BASt', 'World Bank', 'TRID'). "
        "Use search_reports first; use get_report for full metadata; get_fulltext extracts "
        "PDF text. Search is FTS5 over title/abstract/subjects/authors/report numbers: all "
        "terms must match first (match_mode=all_terms), then any-term matches fill the "
        "remaining slots (any_terms). Quote phrases; a trailing * is a prefix. "
        "Call harvest_status if results look thin — the index is only as complete as the "
        "last harvest."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
RO_NET = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(config.DB_PATH)
    return _store


_graph: Graph | None = None


def graph() -> Graph:
    global _graph
    if _graph is None:
        _graph = Graph(store())
    return _graph


def _work(w: dict[str, Any]) -> dict[str, Any]:
    return {k: w.get(k) for k in ("openalex_id", "record_id", "doi", "pmid", "title", "year", "cited_by_count", "type", "venue")}


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


@mcp.tool(annotations=RO)
def search_reports(
    query: str,
    year_min: int | None = None,
    year_max: int | None = None,
    collection: str | None = None,
    doc_type: str | None = None,
    source: str | None = None,
    limit: int = 20,
    offset: int = 0,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Search titles, abstracts, subjects, authors and report numbers across all sources.

    Args:
        query: Keywords. Quote phrases ("driver improvement"); trailing * = prefix;
            column prefixes work: title:pedestrian, authors:lynn, report_numbers:"813 097".
        year_min / year_max: Inclusive publication-year filter.
        collection: Substring filter on collection name (see list_collections), e.g. "NHTSA",
            "VTI", "BASt", "World Bank", "CEPAL", "TRID", "PubMed".
        doc_type: Substring filter on document type, e.g. "Tech Report", "Dataset".
        source: Comma-separated id prefixes to restrict to, e.g. "dot,vti" (dot = ROSA-P,
            vti, bast, wbokr, ipea, cepal, openalex, cinii, pubmed, trid).
        limit: Max hits (1-100). offset: for paging.
        mode: "hybrid" (default: keyword BM25 fused with semantic vectors when an embedding
            index exists), "keyword", or "semantic" (meaning-based, cross-language). Semantic
            results are capped at 50% from any one source unless `source`/`collection` is
            set; use `source` to search one corpus by meaning. ``mode_used`` says what ran.
    Returns ranked hits; ``match_mode`` says whether all query terms matched (all_terms),
    the hit came from the any-term fallback, or from the semantic index.
    """
    limit = max(1, min(int(limit or 20), 100))
    srcs = [x.strip() for x in source.split(",") if x.strip()] if source else None
    mode = (mode or "hybrid").lower()
    if mode not in ("hybrid", "keyword", "semantic"):
        mode = "hybrid"
    hits, used = hybrid_search(store(), query, mode=mode, limit=limit, offset=max(0, int(offset or 0)),
                               year_min=year_min, year_max=year_max, collection=collection, doc_type=doc_type,
                               sources=srcs)
    counts = graph().cited_by_counts([h["id"] for h in hits])
    for h in hits:
        if h["id"] in counts:
            h["cited_by_count"] = counts[h["id"]]
    return {
        "query": query,
        "mode_used": used,
        "filters": {k: v for k, v in dict(year_min=year_min, year_max=year_max, collection=collection,
                                            doc_type=doc_type, source=source, offset=offset or None).items() if v},
        "n": len(hits),
        "index_size": store().count(),
        "hits": [_hit(h) | {k: h.get(k) for k in ("keyword_rank", "semantic_rank", "semantic_score", "cited_by_count") if h.get(k) is not None} for h in hits],
    }


@mcp.tool(annotations=RO)
def get_report(id: str) -> dict[str, Any]:
    """Full metadata record for one report. Accepts 'dot:93144', '93144', the OAI
    identifier, or the ROSA-P landing URL. Includes every raw Dublin Core field."""
    rec = store().get_record(id)
    if not rec:
        return {"error": f"no record {normalize_id(id)} in the local index", "id": normalize_id(id)}
    pdfs = direct_pdf_urls(rec)
    rec["pdf_url_hint"] = (f"{config.ROSAP_VIEW_BASE}{rec['id'].split(':')[-1]}/dot_{rec['id'].split(':')[-1]}_DS1.pdf"
                           if rec["id"].startswith("dot:") else next(iter(pdfs), None))
    ft = store().get_fulltext(rec["id"])
    rec["fulltext_cached"] = bool(ft and ft.get("status") == "ok")
    return rec


@mcp.tool(annotations=RO_NET)
def get_fulltext(id: str, max_chars: int = 40000, offset: int = 0, refresh: bool = False) -> dict[str, Any]:
    """Resolve the report's PDF on ROSA-P, extract its text and return it (cached
    locally after the first call). Page through long documents with ``offset``.
    Text is marked with [[page N]] separators. Scanned PDFs return status 'no_text'."""
    rid = normalize_id(id)
    rec = store().get_record(rid)
    ft = _get_fulltext(store(), rid, refresh=refresh)
    text = ft.get("text") or ""
    offset = max(0, int(offset))
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


@mcp.tool(annotations=RO)
def list_collections() -> dict[str, Any]:
    """Collections (ROSA-P ``relation.isPartOf`` values) with record counts, plus document
    types. ROSA-P's OAI-PMH endpoint exposes no OAI sets, so these are derived from the
    harvested metadata and are what the ``collection`` filter matches against."""
    s = store()
    return {"oai_sets": [], "note": "ListSets is empty on ROSA-P; collections come from dc:relation.isPartOf",
            "collections": s.collections(), "doc_types": s.doc_types()}


@mcp.tool(annotations=RO)
def harvest_status() -> dict[str, Any]:
    """Record counts per source, last harvest time/status and notes, coverage by year.
    If a source's last run is not 'complete' its part of the index may be partial."""
    st = _status(store())
    idx = active_index(store())
    st["citation_graph"] = graph().stats()
    st["embeddings"] = ({"backend": idx.meta.get("backend"), "model": idx.meta.get("model"), "dim": idx.meta.get("dim"),
                         "vectors": len(idx), "coverage": round(len(idx) / max(store().count(), 1), 3),
                         "updated_at": idx.meta.get("updated_at")} if idx else None)
    return st


@mcp.tool(annotations=RO)
def lookup(identifier: str) -> dict[str, Any]:
    """Exact lookup by DOI, PMID, report number (e.g. "DOT HS 813 097"), transport-lit id, or
    landing URL. Use this instead of search when you already have an identifier."""
    recs = store().lookup(identifier)
    return {"identifier": identifier, "n": len(recs), "records": [_hit(r) for r in recs]}


@mcp.tool(annotations=RO)
def find_similar(id: str, limit: int = 10) -> dict[str, Any]:
    """Records similar to a given one (by title and subject terms), across all sources."""
    recs = store().similar(id, limit=max(1, min(int(limit or 10), 50)))
    return {"id": id, "n": len(recs), "hits": [_hit(r) for r in recs]}


@mcp.tool(annotations=RO)
def export_citations(ids: list[str], format: str = "ris") -> dict[str, Any]:
    """Export records as RIS (Zotero/EndNote/Mendeley) or BibTeX. Pass transport-lit ids."""
    recs = [r for r in (store().get_record(i) for i in ids[:200]) if r]
    fmt = (format or "ris").lower()
    text = to_bibtex(recs) if fmt in ("bib", "bibtex") else to_ris(recs)
    return {"format": "bibtex" if fmt in ("bib", "bibtex") else "ris", "n": len(recs),
            "missing": [i for i in ids if not store().get_record(i)], "text": text}


@mcp.tool(annotations=RO)
def search_fulltext(query: str, limit: int = 20) -> dict[str, Any]:
    """Search inside the PDF text that get_fulltext has already extracted and cached
    (only documents someone fetched before). Returns snippets with [term] markers."""
    hits = store().search_fulltext(query, limit=max(1, min(int(limit or 20), 100)))
    return {"query": query, "documents_indexed": store().fulltext_count(), "n": len(hits), "hits": hits}


@mcp.tool(annotations=RO)
def whats_new(days: int = 7, source: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Records that entered the index in the last N days (from the weekly incremental
    harvests), newest first, with counts by source. The raw material for a digest."""
    srcs = [x.strip() for x in source.split(",") if x.strip()] if source else None
    d = store().whats_new(max(1, int(days or 7)), sources=srcs, limit=max(1, min(int(limit or 100), 500)))
    return {"since": d["since"], "counts_by_source": d["counts_by_source"], "n": len(d["records"]),
            "records": [_hit(r) for r in d["records"]]}


@mcp.tool(annotations=RO_NET)
def get_references(id: str, limit: int = 100, refresh: bool = False) -> dict[str, Any]:
    """Works a record cites (its reference list), via OpenAlex, cached locally. Each entry
    carries ``record_id`` when the cited work is itself in this index. ``match`` says how
    the record was matched to OpenAlex (openalex, doi, pmid, or title)."""
    d = graph().references(id, refresh=refresh)
    d["references"] = [_work(w) for w in d["references"][: max(1, min(int(limit or 100), 500))]]
    return d


@mcp.tool(annotations=RO_NET)
def get_citations(id: str, limit: int = 100, only_in_index: bool = False, refresh: bool = False) -> dict[str, Any]:
    """Works that cite a record, via OpenAlex, cached locally (refreshed after 90 days).
    ``only_in_index=True`` returns just citing works that are in this index — 'what has
    built on this report'. ``cited_by_count_openalex`` is OpenAlex's total."""
    d = graph().citations(id, refresh=refresh, only_in_index=only_in_index)
    d["citations"] = [_work(w) for w in d["citations"][: max(1, min(int(limit or 100), 500))]]
    return d


@mcp.prompt()
def literature_scan(topic: str) -> str:
    """Scan the grey literature on a topic and summarise what exists, by source and decade."""
    return (f"Use search_reports to find reports on: {topic}. Run 3-5 varied queries (synonyms, "
            f"quoted phrases), then filter by source (dot, vti, bast, wbokr, cepal, ipea, pubmed, "
            f"openalex, cinii) to see coverage. For the 5 most relevant, call get_report and, if a PDF "
            f"is linked, get_fulltext. Summarise: what exists, by agency/country and decade; key "
            f"findings with effect sizes when reported; gaps. Cite each item as title (year) [id] "
            f"with its landing_url. Finish with export_citations for the items you cite.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="transport-lit-mcp", description="transport-lit MCP server")
    ap.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    # stdout is the MCP transport in stdio mode; keep stderr quiet so client logs stay readable.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # MCP 1.x configures the listener through settings; 2.x takes run options.
        if hasattr(mcp, "settings"):
            mcp.settings.host, mcp.settings.port = a.host, a.port
            mcp.run(transport=a.transport)
        else:
            mcp.run(transport=a.transport, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
