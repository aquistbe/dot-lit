"""RIS and BibTeX export for records."""

from __future__ import annotations

import re
from typing import Any

_RIS_TYPE = {"Tech Report": "RPRT", "report": "RPRT", "Journal Article": "JOUR", "article": "JOUR",
             "Conference Paper": "CONF", "Thesis": "THES", "Book": "BOOK", "Dataset": "DATA", "Manual": "RPRT", "Brief": "RPRT"}


def _ris_type(rec: dict[str, Any]) -> str:
    dt = (rec.get("doc_type") or "").lower()
    for k, v in _RIS_TYPE.items():
        if k.lower() in dt:
            return v
    if rec["id"].startswith("pubmed:"):
        return "JOUR"
    return "RPRT"


def to_ris(recs: list[dict[str, Any]]) -> str:
    out = []
    for r in recs:
        lines = [f"TY  - {_ris_type(r)}", f"TI  - {r.get('title') or ''}"]
        lines += [f"AU  - {a}" for a in (r.get("authors") or [])]
        lines += [f"A2  - {a}" for a in (r.get("corporate_authors") or [])]
        if r.get("year"):
            lines.append(f"PY  - {r['year']}")
        if r.get("abstract"):
            lines.append(f"AB  - {r['abstract']}")
        lines += [f"KW  - {k}" for k in (r.get("subjects") or [])[:30]]
        if r.get("publisher"):
            lines.append(f"PB  - {r['publisher']}")
        if r.get("source"):
            lines.append(f"T2  - {r['source']}")
        if r.get("doi"):
            lines.append(f"DO  - {r['doi']}")
        if r.get("landing_url"):
            lines.append(f"UR  - {r['landing_url']}")
        lines += [f"M3  - {n}" for n in (r.get("report_numbers") or [])]
        if r.get("language"):
            lines.append(f"LA  - {r['language']}")
        lines.append(f"N1  - dot-lit id: {r['id']}")
        lines.append("ER  - ")
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def _bib_key(rec: dict[str, Any]) -> str:
    first = (rec.get("authors") or rec.get("corporate_authors") or ["anon"])[0]
    surname = re.sub(r"[^A-Za-z]", "", first.split(",")[0].split()[-1] if first else "anon") or "anon"
    return f"{surname.lower()}{rec.get('year') or 'nd'}{rec['id'].split(':')[0]}{re.sub(r'[^A-Za-z0-9]', '', rec['id'].split(':', 1)[1])[:8]}"


def _esc(s: str) -> str:
    return s.replace("{", "\\{").replace("}", "\\}").replace("&", "\\&")


def to_bibtex(recs: list[dict[str, Any]]) -> str:
    out = []
    for r in recs:
        t = _ris_type(r)
        entry = {"JOUR": "article", "CONF": "inproceedings", "THES": "phdthesis", "BOOK": "book"}.get(t, "techreport")
        f = {"title": r.get("title") or "", "author": " and ".join(r.get("authors") or r.get("corporate_authors") or []),
             "year": str(r.get("year") or ""), "institution" if entry == "techreport" else "publisher": r.get("publisher") or "",
             "journal" if entry == "article" else "series": r.get("source") or "", "number": "; ".join(r.get("report_numbers") or []),
             "doi": r.get("doi") or "", "url": r.get("landing_url") or "", "abstract": r.get("abstract") or "",
             "keywords": ", ".join((r.get("subjects") or [])[:20]), "note": f"dot-lit id: {r['id']}"}
        body = ",\n".join(f"  {k} = {{{_esc(v)}}}" for k, v in f.items() if v)
        out.append(f"@{entry}{{{_bib_key(r)},\n{body}\n}}")
    return "\n\n".join(out) + "\n"
