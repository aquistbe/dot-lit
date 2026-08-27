"""Turn a ROSA-P ``oai_dc`` record into a flat, typed dictionary.

ROSA-P's "oai_dc" is qualified-Dublin-Core-in-disguise: element names such as
``dc:contributor.author``, ``dc:description.abstract``, ``dc:relation.isPartOf`` and
``dc:identifier.uri`` live in the plain DC namespace.  All raw fields are preserved in
``raw`` (name -> list of values) so nothing is lost; the derived fields are what the
index and the MCP tools use.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .config import ROSAP_VIEW_BASE

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
DC_NS = "http://purl.org/dc/elements/1.1/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"

_YEAR_RE = re.compile(r"\b(1[6-9]\d\d|20\d\d)\b")
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)
_MULTI_SPLIT = re.compile(r"\s*;\s*")


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def local_id_from_oai(oai_identifier: str) -> str:
    """'oai:dot.stacks:dot:93144' -> 'dot:93144'."""
    parts = oai_identifier.split(":")
    if len(parts) >= 2:
        return ":".join(parts[-2:])
    return oai_identifier


def parse_record(rec: ET.Element) -> dict[str, Any] | None:
    header = rec.find(f"{{{OAI_NS}}}header")
    if header is None:
        return None
    oai_id = _clean(header.findtext(f"{{{OAI_NS}}}identifier"))
    datestamp = _clean(header.findtext(f"{{{OAI_NS}}}datestamp"))
    deleted = header.get("status") == "deleted"

    raw: dict[str, list[str]] = {}
    dc = rec.find(f"{{{OAI_NS}}}metadata/{{{OAI_DC_NS}}}dc")
    if dc is not None:
        for child in dc:
            name = _local(child.tag)
            val = _clean(child.text)
            if val:
                raw.setdefault(name, []).append(val)

    local_id = local_id_from_oai(oai_id)
    numeric = local_id.split(":")[-1]

    def get(name: str) -> list[str]:
        return raw.get(name, [])

    # Title
    title = get("title")[0] if get("title") else ""

    # Authors: personal authors first, then plain dc:creator (often corporate).
    authors = _uniq(get("contributor.author") + get("creator"))
    corporate = _uniq(get("contributor.creator"))
    contributors = _uniq(get("contributor.collaborator") + get("contributor") + get("contributor.consultant"))

    # Year
    date_raw = get("date")[0] if get("date") else ""
    year: int | None = None
    if date_raw:
        m = _YEAR_RE.search(date_raw)
        if m:
            year = int(m.group(1))

    abstract = " ".join(get("description.abstract")) or " ".join(get("description"))
    toc = " ".join(get("description.tableOfContents"))

    # Identifiers: DOI vs. report numbers vs. URLs
    doi = ""
    report_numbers: list[str] = []
    other_urls: list[str] = []
    for v in get("identifier.uri") + get("identifier") + get("relation.isVersionOf"):
        m = _DOI_RE.search(v)
        if m and ("doi.org" in v.lower() or v.lower().startswith("10.")):
            if not doi:
                doi = m.group(1).rstrip(".,;")
            continue
        if v.lower().startswith(("http://", "https://")):
            if "rosap.ntl.bts.gov/view" not in v:
                other_urls.append(v)
            continue
        if v == local_id or v.startswith("dot:"):
            continue
        report_numbers.append(v)
    report_numbers = _uniq(report_numbers)

    landing_url = f"{ROSAP_VIEW_BASE}{numeric}"

    collections: list[str] = []
    for v in get("relation.isPartOf"):
        collections.extend(p for p in _MULTI_SPLIT.split(v) if p)
    collections = _uniq(collections)

    subjects = _uniq(get("subject"))

    return {
        "id": local_id,
        "oai_identifier": oai_id,
        "datestamp": datestamp,
        "deleted": deleted,
        "title": title,
        "alt_title": " ".join(get("title.alternative")),
        "authors": authors,
        "corporate_authors": corporate,
        "contributors": contributors,
        "year": year,
        "date_raw": date_raw,
        "abstract": abstract,
        "table_of_contents": toc,
        "publisher": " ; ".join(get("publisher")),
        "doc_type": " ; ".join(get("type")),
        "format": " ; ".join(get("format")),
        "language": " ; ".join(get("language")),
        "subjects": subjects,
        "collections": collections,
        "doi": doi,
        "report_numbers": report_numbers,
        "other_urls": _uniq(other_urls),
        "spatial": " ; ".join(get("coverage.spatial") + get("coverage")),
        "source": " ; ".join(get("source")),
        "rights": " ; ".join(get("rights.accessRights")),
        "landing_url": landing_url,
        "raw": raw,
    }
