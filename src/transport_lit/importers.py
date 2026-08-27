"""Import bibliographic exports (RIS) into the store as an additional source.

Why this exists: TRID (trid.trb.org) has no API, its FAQ refuses backend/bulk access and
its robots.txt disallows AI crawlers, but every user may search TRID and export results
(RIS / CSV / XML) from the web interface.  This module ingests those hand-exported RIS
files so TRID hits sit in the same index as ROSA-P.  It is generic RIS, so exports from
Zotero, EndNote, Scopus, etc. work too.

Record ids: ``trid:<n>`` when a TRID view URL is present, otherwise ``<source>:<AN/ID>``,
otherwise ``<source>:<sha1(title|year)>``.  Re-importing the same file is idempotent.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .store import Store, utcnow

_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")
_TRID_URL = re.compile(r"trid\.trb\.org/[Vv]iew/(\d+)")
_DOI = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")

RIS_TYPES = {
    "RPRT": "Tech Report", "JOUR": "Journal Article", "CONF": "Conference Paper", "CPAPER": "Conference Paper",
    "THES": "Thesis", "BOOK": "Book", "CHAP": "Book Chapter", "GEN": "Generic", "ELEC": "Web Page",
    "DATA": "Dataset", "STD": "Standard", "MGZN": "Magazine Article", "NEWS": "News",
}
_MULTI = {"AU", "A1", "A2", "A3", "A4", "KW", "UR", "L1", "L2", "N1", "ID", "AN", "DO", "PB"}


def parse_ris(text: str) -> Iterable[dict[str, list[str]]]:
    rec: dict[str, list[str]] = {}
    last_tag: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = _RIS_LINE.match(line)
        if not m:
            # continuation line of the previous tag (common for AB)
            if last_tag and rec.get(last_tag) and line.strip():
                rec[last_tag][-1] += " " + line.strip()
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "ER":
            if rec:
                yield rec
            rec, last_tag = {}, None
            continue
        rec.setdefault(tag, []).append(val)
        last_tag = tag
    if rec:
        yield rec


def _first(rec: dict[str, list[str]], *tags: str) -> str:
    for t in tags:
        if rec.get(t):
            return rec[t][0].strip()
    return ""


def ris_to_record(rec: dict[str, list[str]], *, source: str, collection: str) -> dict[str, Any]:
    title = _first(rec, "TI", "T1")
    authors = [a.strip() for t in ("AU", "A1", "A2") for a in rec.get(t, []) if a.strip()]
    urls = [u.strip() for t in ("UR", "L1", "L2") for u in rec.get(t, []) if u.strip()]
    doi = ""
    for cand in rec.get("DO", []) + urls:
        m = _DOI.search(cand)
        if m:
            doi = m.group(1).rstrip(".,;")
            break
    year = None
    ym = _YEAR.search(_first(rec, "PY", "Y1", "DA"))
    if ym:
        year = int(ym.group(1))
    trid_n = next((m.group(1) for u in urls if (m := _TRID_URL.search(u))), None)
    if trid_n:
        rid, src = f"trid:{trid_n}", "trid"
        landing = f"https://trid.trb.org/View/{trid_n}"
    else:
        src = source
        acc = _first(rec, "AN", "ID")
        key = acc or hashlib.sha1(f"{title}|{year}".encode()).hexdigest()[:12]
        rid = f"{src}:{key}"
        landing = f"https://doi.org/{doi}" if doi else (urls[0] if urls else "")
    other_urls = [u for u in urls if trid_n is None or trid_n not in u]
    container = _first(rec, "T2", "JO", "JF", "J2")
    report_numbers = [v for t in ("M3", "M1") for v in rec.get(t, []) if v.strip()]
    notes = " ; ".join(rec.get("N1", []))
    colls = [collection] if collection else []
    if src == "trid" and "TRID" not in colls:
        colls.insert(0, "TRID")
    return {
        "id": rid,
        "oai_identifier": f"import:{src}:{rid}",
        "datestamp": utcnow(),
        "deleted": False,
        "title": title,
        "alt_title": "",
        "authors": authors,
        "corporate_authors": [],
        "contributors": [],
        "year": year,
        "year_source": "date" if year else None,
        "date_raw": _first(rec, "PY", "Y1", "DA"),
        "abstract": _first(rec, "AB", "N2"),
        "table_of_contents": "",
        "notes": notes,
        "publisher": _first(rec, "PB"),
        "doc_type": RIS_TYPES.get(_first(rec, "TY"), _first(rec, "TY")),
        "format": "",
        "language": _first(rec, "LA"),
        "subjects": [k.strip() for k in rec.get("KW", []) if k.strip()],
        "collections": colls,
        "doi": doi,
        "report_numbers": report_numbers,
        "other_urls": other_urls,
        "spatial": _first(rec, "CY"),
        "source": container,
        "rights": "",
        "landing_url": landing,
        "raw": dict(rec),
    }


def import_ris(store: Store, path: Path, *, source: str = "import", collection: str | None = None) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    coll = collection if collection is not None else Path(path).stem
    records = [ris_to_record(r, source=source, collection=coll) for r in parse_ris(text)]
    records = [r for r in records if r["title"]]
    run_id = store.start_run(source, "import", None, None)
    n = store.upsert_records(records)
    store.update_run(run_id, status="complete", finished_at=utcnow(), pages=1, records_seen=n,
                     notes=[f"file={Path(path).name}", f"collection={coll}"])
    by_prefix: dict[str, int] = {}
    for r in records:
        p = r["id"].split(":")[0]
        by_prefix[p] = by_prefix.get(p, 0) + 1
    return {"file": str(path), "records": n, "by_source": by_prefix, "collection": coll, "run_id": run_id,
            "total_in_store": store.count()}
