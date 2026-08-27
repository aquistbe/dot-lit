"""Registry of OAI-PMH sources.  Adding a repository is one entry here.

Each source gets an id prefix (`key`), an OAI base URL, an optional set, a collection
label attached to every record, and an optional `include` filter for repositories whose
scope is much broader than transport (development banks, UN commissions): a record is kept
only if the filter matches its title, subjects or abstract.  The default filter is
multilingual (en/es/pt/de/fr/sv) and deliberately loose — a false positive just sits in
the index behind its collection label; a false negative is lost until the next rebuild.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ROSAP_OAI_BASE

_TERMS = [
    # English
    r"transport\w*", r"traffic", r"roads?", r"roadways?", r"road safety", r"highways?", r"streets?",
    r"pedestrians?", r"bicycl\w*", r"cycling", r"cyclists?", r"micromobility", r"motorcycl\w*",
    r"motor ?vehicles?", r"vehicles?", r"automobiles?", r"cars?", r"trucks?", r"buses", r"bus",
    r"transit", r"mobility", r"railways?", r"railroads?", r"rail", r"metro", r"subway",
    r"crash\w*", r"collisions?", r"drivers?", r"driving", r"speed(?:ing)? ?(?:limit|management)",
    r"aviation", r"airports?", r"maritime", r"shipping", r"ports?", r"freight", r"logistics?",
    r"infrastructure", r"parking", r"intersections?", r"trip", r"commut\w*", r"travel behavio\w*",
    # Spanish
    r"tr[aá]nsito", r"tr[aá]fico", r"carreteras?", r"v[ií]as?", r"vial(?:idad)?", r"seguridad vial",
    r"peat[oó]n\w*", r"bicicletas?", r"ciclistas?", r"ciclov[ií]as?", r"veh[ií]culos?", r"autom[oó]vil\w*",
    r"camiones?", r"autobus\w*", r"movilidad", r"ferrocarril\w*", r"ferrovi\w*", r"siniestros? (?:viales?|de tr[aá]nsito)",
    r"accidentes? de tr[aá]nsito", r"conductor\w*", r"puertos?", r"aeropuertos?", r"log[ií]stica", r"infraestructura",
    # Portuguese
    r"tr[aâ]nsito", r"tr[aá]fego", r"rodovi\w*", r"estradas?", r"segurança vi[aá]ria", r"pedestres?",
    r"ciclov\w*", r"ve[ií]culos?", r"motoristas?", r"mobilidade", r"ferrovia\w*", r"portos?", r"aeroportos?",
    # German / French / Swedish
    r"verkehr\w*", r"stra(?:ß|ss)e\w*", r"fu(?:ß|ss)g[aä]nger\w*", r"radfahr\w*", r"fahrzeug\w*", r"unf[aä]ll\w*",
    r"circulation routi[eè]re", r"s[eé]curit[eé] routi[eè]re", r"pi[eé]tons?", r"v[eé]hicules?", r"routes?",
    r"trafik\w*", r"v[aä]g\w*", r"fotg[aä]ngare", r"fordon\w*", r"cyklist\w*",
]
TRANSPORT_RE = re.compile(r"\b(?:" + "|".join(_TERMS) + r")\b", re.I)


@dataclass(frozen=True)
class Source:
    key: str                      # id prefix and harvest_runs.source
    name: str
    base_url: str
    metadata_prefix: str = "oai_dc"
    set_spec: str | None = None
    collection: str = ""          # label added to every record's collections
    include: re.Pattern[str] | None = None
    country: str = ""
    notes: str = ""


SOURCES: dict[str, Source] = {
    "rosap": Source(
        "rosap", "ROSA-P — U.S. DOT National Transportation Library", ROSAP_OAI_BASE,
        country="US", notes="no sets; qualified DC in oai_dc; ~90k records",
    ),
    "vti": Source(
        "vti", "VTI — Swedish National Road and Transport Research Institute (DiVA)",
        "https://vti.diva-portal.org/dice/oai", set_spec="all-vti", collection="VTI (Sweden)", country="SE",
        notes="DiVA; setSpecs carry document type",
    ),
    "bast": Source(
        "bast", "BASt — German Federal Highway Research Institute (OPUS)",
        "https://bast.opus.hbz-nrw.de/oai", collection="BASt (Germany)", country="DE",
        notes="OPUS4; PDF URL in dc:identifier",
    ),
    "wbokr": Source(
        "wbokr", "World Bank Open Knowledge Repository",
        "https://openknowledge.worldbank.org/server/oai/request", collection="World Bank OKR",
        include=TRANSPORT_RE, country="INT", notes="DSpace 7; ~40k records, transport filter applied",
    ),
    "ipea": Source(
        "ipea", "IPEA — Instituto de Pesquisa Econômica Aplicada (Brazil)",
        "https://repositorio.ipea.gov.br/server/oai/request", collection="IPEA (Brazil)",
        include=TRANSPORT_RE, country="BR", notes="DSpace 7; ~14k records, transport filter applied",
    ),
    "cepal": Source(
        "cepal", "CEPAL/ECLAC — UN Economic Commission for Latin America and the Caribbean",
        "https://repositorio.cepal.org/server/oai/request", collection="CEPAL (Latin America)",
        include=TRANSPORT_RE, country="INT", notes="DSpace 7; ~52k records, transport filter applied",
    ),
}


def get_source(key: str) -> Source:
    try:
        return SOURCES[key]
    except KeyError:
        raise ValueError(f"unknown source {key!r}; known: {', '.join(SOURCES)}") from None


def matches_filter(source: Source, rec: dict) -> bool:
    if source.include is None:
        return True
    text = " ".join([rec.get("title") or "", " ".join(rec.get("subjects") or []), rec.get("abstract") or ""])
    return bool(source.include.search(text))
