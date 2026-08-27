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
_BARE_YEAR_RE = re.compile(r"^(19\d\d|20\d\d)$")
FALLBACK_YEAR_MIN, FALLBACK_YEAR_MAX = 1900, 2027  # inferred years outside this are rejected
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.I)
_MULTI_SPLIT = re.compile(r"\s*;\s*")
# Short line that looks like a report/contract number: starts with letters, has a digit,
# few words, no sentence punctuation.  e.g. "SPR 634", "FHWA-OR-RD-08-06", "DOT HS 811 727".
_REPORT_NO_RE = re.compile(r"^[A-Za-z][A-Za-z&./ -]{0,24}[-\s/]?\d[\w./ -]*$")
_CORPORATE_HINTS = re.compile(
    r"\b(universit|institute|dept\.?|department|administration|council|center|centre|commission|"
    r"office|bureau|agency|board|company|corp\.?|corporation|inc\.?|associates|laboratory|"
    r"division|research unit|program|services|authority|society|association|consultants?|group|"
    r"united states|u\.s\.)\b", re.I)


def looks_like_report_number(v: str) -> bool:
    return len(v) <= 40 and len(v.split()) <= 6 and not v.endswith((".", ",", ";", ":")) and bool(
        _REPORT_NO_RE.match(v))


def looks_corporate(name: str) -> bool:
    return bool(_CORPORATE_HINTS.search(name))


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

    # Two metadata profiles coexist in ROSA-P (observed 2026-08-26):
    #  * "qualified": contributor.author / contributor.creator / description.abstract /
    #    identifier.uri / date  (~54 % of records)
    #  * "legacy":    creator (people and organisations mixed) / description (report
    #    number lines followed by the abstract split into lines) / coverage, and NO date
    #    (~46 % of records).
    authors = list(get("contributor.author"))
    corporate = list(get("contributor.creator"))
    for c in get("creator"):
        (corporate if looks_corporate(c) else authors).append(c)
    authors = _uniq(authors)
    corporate = _uniq(corporate)
    contributors = _uniq(get("contributor.collaborator") + get("contributor") + get("contributor.consultant"))

    # Description / abstract / report numbers embedded in description
    legacy_report_numbers: list[str] = []
    if get("description.abstract"):
        abstract = " ".join(get("description.abstract"))
        notes = list(get("description"))
    else:
        text_lines: list[str] = []
        for v in get("description"):
            (legacy_report_numbers if looks_like_report_number(v) else text_lines).append(v)
        abstract = " ".join(text_lines)
        notes = []
    toc = " ".join(get("description.tableOfContents"))

    # Year: dc:date first.  Legacy records have no dc:date, so fall back in order to
    #   (1) a description line that is a bare year ("2018"),
    #   (2) a year in the title ("..., Sixth Edition, 2011"),
    #   (3) a year in a short description line that is not a report number
    #       ("Final report; June 2007" yes, "RC-1600" no).
    # Inferred years must fall in FALLBACK_YEAR_MIN..MAX; `year_source` records the route.
    date_raw = get("date")[0] if get("date") else ""
    year: int | None = None
    year_source: str | None = None
    if date_raw:
        m = _YEAR_RE.search(date_raw)
        if m:
            year, year_source = int(m.group(1)), "date"

    def _plausible(y: int) -> bool:
        return FALLBACK_YEAR_MIN <= y <= FALLBACK_YEAR_MAX

    desc_lines = get("description") + notes
    if year is None:
        for v in desc_lines:
            m = _BARE_YEAR_RE.match(v)
            if m and _plausible(int(m.group(1))):
                year, year_source = int(m.group(1)), "description"
                break
    if year is None:
        for m in _YEAR_RE.finditer(title):
            if _plausible(int(m.group(1))):
                year, year_source = int(m.group(1)), "title"
                break
    if year is None:
        for v in desc_lines:
            if len(v) <= 60 and not looks_like_report_number(v):
                for m in _YEAR_RE.finditer(v):
                    if _plausible(int(m.group(1))):
                        year, year_source = int(m.group(1)), "description"
                        break
            if year is not None:
                break

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
    report_numbers = _uniq(report_numbers + legacy_report_numbers)

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
        "year_source": year_source,
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
        "notes": " ; ".join(notes),
        "spatial": " ; ".join(get("coverage.spatial") + get("coverage")),
        "source": " ; ".join(get("source")),
        "rights": " ; ".join(get("rights.accessRights")),
        "landing_url": landing_url,
        "raw": raw,
    }
