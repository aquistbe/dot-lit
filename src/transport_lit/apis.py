"""Query-API sources: OpenAlex (reports by transport topic), CiNii Research (Japan),
PubMed E-utilities (transport/injury subset).

Unlike OAI-PMH these have no "list everything" verb, so each source defines
  pages(client, since)  -> iterator of (label, raw_bytes)      (network)
  parse(raw_bytes)      -> list[record dict]                   (pure)
Raw pages are cached under raw/ exactly like OAI pages, so `transport-lit reindex` works here
too.  Records use the same shape `dc.parse_record` produces; ids are `openalex:W…`,
`cinii:<crid>`, `pubmed:<pmid>`.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from . import config
from .oai import RateLimiter
from .store import utcnow

Client = httpx.Client


@dataclass(frozen=True)
class ApiSource:
    key: str
    name: str
    collection: str
    pages: Callable[[Client, str | None, Callable[[str], None]], Iterator[tuple[str, bytes]]]
    parse: Callable[[bytes], list[dict[str, Any]]]
    min_interval: float = 1.0
    country: str = ""
    notes: str = ""
    raw_ext: str = "json"


def _client(min_interval: float) -> tuple[Client, RateLimiter]:
    return (httpx.Client(headers={"User-Agent": config.USER_AGENT, "Accept": "application/json, text/xml"},
                         timeout=config.HTTP_TIMEOUT, follow_redirects=True), RateLimiter(min_interval))


def _get(client: Client, limiter: RateLimiter, url: str, params: dict | None = None, retries: int = 4) -> bytes:
    import time
    last: Exception | None = None
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            r = client.get(url, params=params)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            time.sleep(min(3.0 * (attempt + 1), 20.0))
    raise RuntimeError(f"GET {url} failed after retries: {last}")


def _base(rid: str, title: str, *, year: int | None, authors: list[str], abstract: str, doi: str,
          landing: str, other_urls: list[str], publisher: str, doc_type: str, language: str,
          subjects: list[str], collections: list[str], raw: dict, source: str = "",
          report_numbers: list[str] | None = None, corporate: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": rid, "oai_identifier": f"api:{rid}", "datestamp": utcnow(), "deleted": False,
        "title": title, "alt_title": "", "authors": authors, "corporate_authors": corporate or [],
        "contributors": [], "year": year, "year_source": "date" if year else None, "date_raw": str(year or ""),
        "abstract": abstract, "table_of_contents": "", "notes": "", "publisher": publisher,
        "doc_type": doc_type, "format": "", "language": language, "subjects": subjects,
        "collections": collections, "doi": doi, "report_numbers": report_numbers or [],
        "other_urls": other_urls, "spatial": "", "source": source, "rights": "", "landing_url": landing, "raw": raw,
    }


# ======================================================================= OpenAlex
OPENALEX_TOPICS = {  # verified 2026-08-26 via /topics?search=…
    "T10370": "Traffic and Road Safety",
    "T10524": "Traffic control and management",
    "T10698": "Transportation Planning and Optimization",
    "T10298": "Urban Transport and Accessibility",
    "T11344": "Traffic Prediction and Management Techniques",
    "T11099": "Autonomous Vehicle Technology and Safety",
    "T12095": "Vehicle emissions and performance",
    "T12644": "Wildlife-Road Interactions and Conservation",
    "T11622": "Maritime Navigation and Safety",
    "T11489": "Air Traffic Management and Optimization",
}
OPENALEX_TYPES = os.environ.get("TRANSPORT_LIT_OPENALEX_TYPES", "report")  # e.g. "report|dissertation"
_OA_SELECT = ("id,doi,title,display_name,publication_year,type,language,authorships,primary_location,"
              "best_oa_location,abstract_inverted_index,topics,keywords,updated_date")


def openalex_pages(client: Client, since: str | None, say: Callable[[str], None]) -> Iterator[tuple[str, bytes]]:
    limiter = RateLimiter(0.2)  # OpenAlex polite pool allows ~10 r/s; we use 5
    flt = f"type:{OPENALEX_TYPES},primary_topic.id:{'|'.join(OPENALEX_TOPICS)}"
    if since:
        flt += f",from_updated_date:{since[:10]}"
    cursor = "*"
    n = 0
    while cursor:
        params = {"filter": flt, "per-page": 200, "cursor": cursor, "select": _OA_SELECT,
                  "mailto": config.CONTACT_EMAIL or "transport-lit@example.invalid"}
        raw = _get(client, limiter, "https://api.openalex.org/works", params)
        d = json.loads(raw)
        if n == 0:
            say(f"openalex: {d['meta']['count']} works match (types={OPENALEX_TYPES}, {len(OPENALEX_TOPICS)} topics)")
        yield f"p{n:05d}", raw
        n += 1
        cursor = d["meta"].get("next_cursor")
        if not d.get("results"):
            break


def _oa_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos: list[tuple[int, str]] = [(p, w) for w, ps in inv.items() for p in ps]
    return " ".join(w for _, w in sorted(pos))


def openalex_parse(raw: bytes) -> list[dict[str, Any]]:
    out = []
    for w in json.loads(raw).get("results", []):
        wid = w["id"].rsplit("/", 1)[-1]
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        loc = w.get("primary_location") or {}
        oa = w.get("best_oa_location") or {}
        landing = loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else w["id"])
        pdfs = [u for u in (oa.get("pdf_url"), loc.get("pdf_url")) if u]
        topics = [t["display_name"] for t in (w.get("topics") or [])]
        kws = [k["display_name"] for k in (w.get("keywords") or [])]
        primary = topics[0] if topics else ""
        w2 = {k: v for k, v in w.items() if k != "abstract_inverted_index"}
        out.append(_base(
            f"openalex:{wid}", w.get("display_name") or w.get("title") or "",
            year=w.get("publication_year"),
            authors=[a["author"]["display_name"] for a in (w.get("authorships") or []) if a.get("author")],
            abstract=_oa_abstract(w.get("abstract_inverted_index")), doi=doi, landing=landing, other_urls=pdfs,
            publisher=((loc.get("source") or {}).get("display_name") or ""), doc_type=w.get("type") or "",
            language=w.get("language") or "", subjects=kws + topics,
            collections=["OpenAlex reports"] + ([f"OpenAlex: {primary}"] if primary else []), raw=w2,
            corporate=[i["display_name"] for a in (w.get("authorships") or []) for i in (a.get("institutions") or [])][:3],
        ))
    return out


# ======================================================================= CiNii Research (Japan)
CINII_QUERIES = [
    "交通安全", "交通事故", "歩行者", "自転車 安全", "道路交通", "公共交通", "交通計画", "交通工学", "自動車 安全",
    "高齢者 運転", "運転者", "都市交通", "交通政策", "鉄道 安全", "交通 負傷", "traffic safety", "road safety",
    "pedestrian", "transportation planning", "public transport Japan",
]
CINII_MAX_PER_QUERY = int(os.environ.get("TRANSPORT_LIT_CINII_MAX", "10000"))


CINII_REGISTER_URL = "https://support.nii.ac.jp/en/cinii/api/developer"


def cinii_pages(client: Client, since: str | None, say: Callable[[str], None]) -> Iterator[tuple[str, bytes]]:
    appid = os.environ.get("TRANSPORT_LIT_CINII_APPID", "").strip()
    if not appid:
        raise RuntimeError("CiNii's API terms require an application ID: register (free) at "
                           f"{CINII_REGISTER_URL} and set TRANSPORT_LIT_CINII_APPID")
    limiter = RateLimiter(1.0)
    page = 0
    for q in CINII_QUERIES:
        start = 1
        total = None
        while True:
            params = {"q": q, "format": "json", "count": 200, "start": start, "appid": appid}
            if since:
                params["from"] = since[:4]
            raw = _get(client, limiter, "https://cir.nii.ac.jp/opensearch/all", params)
            d = json.loads(raw)
            items = d.get("items") or []
            if total is None:
                total = int(d.get("opensearch:totalResults") or 0)
                say(f"cinii: '{q}' -> {total} results" + (f" (capped at {CINII_MAX_PER_QUERY})" if total > CINII_MAX_PER_QUERY else ""))
            yield f"p{page:05d}", raw
            page += 1
            if not items or start + 200 > min(total, CINII_MAX_PER_QUERY):
                break
            start += 200


def cinii_parse(raw: bytes) -> list[dict[str, Any]]:
    d = json.loads(raw)
    out = []
    for it in d.get("items") or []:
        crid = it.get("@id", "").rsplit("/", 1)[-1]
        if not crid:
            continue
        creators = it.get("dc:creator") or []
        if isinstance(creators, str):
            creators = [creators]
        ids = it.get("dc:identifier") or []
        if isinstance(ids, dict):
            ids = [ids]
        doi = next((i.get("@value", "") for i in ids if i.get("@type") == "cir:DOI"), "")
        date = it.get("prism:publicationDate") or it.get("dc:date") or ""
        m = re.search(r"(19|20)\d\d", str(date))
        out.append(_base(
            f"cinii:{crid}", it.get("title") or "", year=int(m.group(0)) if m else None,
            authors=[c for c in creators if isinstance(c, str)], abstract=it.get("description") or "",
            doi=doi, landing=it.get("@id", ""), other_urls=[], publisher=it.get("dc:publisher") or "",
            doc_type=it.get("dc:type") or "", language="ja", subjects=[],
            collections=["CiNii Research (Japan)"], raw=it, source=it.get("prism:publicationName") or "",
        ))
    return out


# ======================================================================= PubMed (E-utilities)
PUBMED_MESH = ['"Accidents, Traffic"[MeSH]', '"Pedestrians"[MeSH]', '"Bicycling"[MeSH]', '"Automobile Driving"[MeSH]',
               '"Motorcycles"[MeSH]', '"Motor Vehicles"[MeSH]', '"Transportation"[MeSH] AND "Safety"[MeSH]',
               '"Built Environment"[MeSH] AND "Wounds and Injuries"[MeSH]']
PUBMED_JOURNALS = ["Accid Anal Prev", "Traffic Inj Prev", "J Safety Res", "Inj Prev", "J Transp Health",
                   "Inj Epidemiol", "Transp Res Part F Traffic Psychol Behav", "Saf Sci", "IATSS Res",
                   "Int J Inj Contr Saf Promot", "J Transp Geogr", "Transp Policy (Oxf)"]


def pubmed_term() -> str:
    override = os.environ.get("TRANSPORT_LIT_PUBMED_TERM")
    if override:
        return override
    mesh = " OR ".join(f"({m})" for m in PUBMED_MESH)
    journals = " OR ".join(f'"{j}"[Journal]' for j in PUBMED_JOURNALS)
    return f"(({mesh}) OR ({journals}))"


PUBMED_SLICE_MAX = 9500  # E-utilities refuses retstart >= 10,000, so every date slice must stay below it


def pubmed_pages(client: Client, since: str | None, say: Callable[[str], None]) -> Iterator[tuple[str, bytes]]:
    from datetime import date, timedelta

    key = os.environ.get("NCBI_API_KEY", "")
    limiter = RateLimiter(0.11 if key else 0.35)
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    term = pubmed_term()
    if since:
        term = f"({term}) AND (\"{since[:10].replace('-', '/')}\"[edat] : \"3000\"[edat])"

    def esearch(t: str, history: bool) -> dict:
        params = {"db": "pubmed", "term": t, "retmax": 0, "retmode": "json"}
        if history:
            params["usehistory"] = "y"
        if key:
            params["api_key"] = key
        return json.loads(_get(client, limiter, base + "esearch.fcgi", params))["esearchresult"]

    total = int(esearch(term, False)["count"])
    say(f"pubmed: {total} articles match strategy; slicing by publication date (<= {PUBMED_SLICE_MAX} each)")

    # Recursive date slicing on [dp] so no slice exceeds the retstart cap.
    slices: list[tuple[date, date, int]] = []

    def add(lo: date, hi: date) -> None:
        t = f"({term}) AND (\"{lo:%Y/%m/%d}\"[dp] : \"{hi:%Y/%m/%d}\"[dp])"
        n = int(esearch(t, False)["count"])
        if n == 0:
            return
        if n <= PUBMED_SLICE_MAX or lo == hi:
            slices.append((lo, hi, n))
            return
        mid = lo + (hi - lo) // 2
        add(lo, mid)
        add(mid + timedelta(days=1), hi)

    add(date(1800, 1, 1), date.today() + timedelta(days=366))
    say(f"pubmed: {len(slices)} date slices, {sum(n for *_, n in slices)} articles with a publication date")
    page = 0
    for lo, hi, n in slices:
        t = f"({term}) AND (\"{lo:%Y/%m/%d}\"[dp] : \"{hi:%Y/%m/%d}\"[dp])"
        h = esearch(t, True)
        for start in range(0, n, 500):
            p = {"db": "pubmed", "query_key": h["querykey"], "WebEnv": h["webenv"], "retstart": start, "retmax": 500,
                 "retmode": "xml"}
            if key:
                p["api_key"] = key
            yield f"p{page:05d}", _get(client, limiter, base + "efetch.fcgi", p)
            page += 1


def _txt(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def pubmed_parse(raw: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = _txt(art.find(".//PMID"))
        a = art.find(".//Article")
        if a is None:
            continue
        title = _txt(a.find("ArticleTitle"))
        abstract = " ".join(_txt(x) for x in a.findall(".//Abstract/AbstractText"))
        authors = []
        for au in a.findall(".//AuthorList/Author"):
            ln, fn, coll = _txt(au.find("LastName")), _txt(au.find("ForeName")), _txt(au.find("CollectiveName"))
            if ln:
                authors.append(f"{ln}, {fn}".strip(", "))
            elif coll:
                authors.append(coll)
        journal = _txt(a.find(".//Journal/Title"))
        yr = _txt(a.find(".//Journal/JournalIssue/PubDate/Year")) or _txt(a.find(".//Journal/JournalIssue/PubDate/MedlineDate"))[:4]
        doi = next((_txt(e) for e in art.findall(".//ArticleId") if e.get("IdType") == "doi"), "") or \
              next((_txt(e) for e in a.findall("ELocationID") if e.get("EIdType") == "doi"), "")
        pmc = next((_txt(e) for e in art.findall(".//ArticleId") if e.get("IdType") == "pmc"), "")
        mesh = [_txt(m.find("DescriptorName")) for m in art.findall(".//MeshHeading")]
        ptypes = [_txt(p) for p in a.findall(".//PublicationTypeList/PublicationType")]
        lang = _txt(a.find("Language"))
        out.append(_base(
            f"pubmed:{pmid}", title, year=int(yr) if yr.isdigit() else None, authors=authors, abstract=abstract,
            doi=doi, landing=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            other_urls=[f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/"] if pmc else [],
            publisher=journal, doc_type=" ; ".join(ptypes), language=lang, subjects=mesh,
            collections=["PubMed transport subset"] + ([f"PubMed: {journal}"] if journal else []),
            raw={"pmid": pmid, "pmc": pmc, "journal": journal, "publication_types": ptypes}, source=journal,
        ))
    return out


API_SOURCES: dict[str, ApiSource] = {
    "openalex": ApiSource("openalex", "OpenAlex — reports in transport topics (global)", "OpenAlex reports",
                          openalex_pages, openalex_parse, country="INT",
                          notes=f"type={OPENALEX_TYPES}; topics: " + ", ".join(list(OPENALEX_TOPICS.values())[:3]) + ", …"),
    "cinii": ApiSource("cinii", "CiNii Research — Japanese articles, theses, IRDB repository items", "CiNii Research (Japan)",
                       cinii_pages, cinii_parse, country="JP",
                       notes=f"{len(CINII_QUERIES)} ja/en queries, {CINII_MAX_PER_QUERY} max each; needs TRANSPORT_LIT_CINII_APPID (register at {CINII_REGISTER_URL})"),
    "pubmed": ApiSource("pubmed", "PubMed — transport/injury subset (MeSH strategy + journal list)", "PubMed transport subset",
                        pubmed_pages, pubmed_parse, country="INT", notes="E-utilities; NCBI_API_KEY optional; TRANSPORT_LIT_PUBMED_TERM overrides",
                        raw_ext="xml"),
}
