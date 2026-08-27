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

# Strong terms: unambiguous transport vocabulary.  Generic words that development
# literature uses for other things (infrastructure, mobility, vehicles, trip, ports, rail,
# cars, bus) are deliberately left out or narrowed.
_TERMS = [
    # English
    r"transport(?:ation)?", r"transport (?:sector|policy|planning|infrastructure|system|corridor)s?",
    r"traffic", r"roads?", r"roadways?", r"road (?:safety|network|sector|transport|traffic|users?|infrastructure|maintenance)",
    r"highways?", r"streets?", r"pedestrians?", r"bicycl\w*", r"cycling", r"cyclists?", r"micromobility",
    r"motorcycl\w*", r"motor ?vehicles?", r"automobiles?", r"trucking", r"bus (?:rapid transit|systems?|services?|routes?)",
    r"public transit", r"transit (?:systems?|agenc\w+|oriented)", r"urban mobility", r"mobility (?:plans?|services?)",
    r"railways?", r"railroads?", r"rail (?:transport|freight|network|sector|safety)", r"metro (?:systems?|lines?|rail)", r"subway",
    r"crash\w*", r"collisions?", r"road traffic (?:injur\w+|deaths?|fatalit\w+|crash\w*|accidents?)",
    r"driver (?:behavio\w+|licens\w+|training|education|improvement)", r"drivers? (?:and|or) (?:pedestrians|passengers)",
    r"driving", r"speed (?:limits?|management|cameras?)", r"aviation", r"airports?", r"maritime", r"shipping",
    r"seaports?", r"port (?:sector|infrastructure|operations?|authority)", r"freight", r"logistics (?:sector|performance|costs?)",
    r"parking", r"intersections?", r"commut\w+", r"travel behavio\w+", r"vehicle (?:emissions?|fleet|safety|inspection)s?",
    # Spanish
    r"tr[aá]nsito", r"tr[aá]fico (?:vehicular|rodado|urbano|vial|a[eé]reo|mar[ií]timo|terrestre|de veh[ií]culos)", r"carreteras?", r"v[ií]as?", r"vial(?:idad)?", r"seguridad vial",
    r"peat[oó]n\w*", r"bicicletas?", r"ciclistas?", r"ciclov[ií]as?", r"veh[ií]culos?", r"autom[oó]vil\w*",
    r"camiones?", r"autobus\w*", r"movilidad (?:urbana|sostenible|el[eé]ctrica|de personas|activa)", r"ferrocarril\w*", r"ferrovi\w*", r"siniestros? (?:viales?|de tr[aá]nsito)",
    r"accidentes? de tr[aá]nsito", r"conductores?", r"puertos? (?:mar[ií]timos?|de)", r"aeropuertos?", r"infraestructura (?:vial|de transporte)",
    # Portuguese
    r"tr[aâ]nsito", r"tr[aá]fego", r"tr[aá]fico (?:rodovi[aá]rio|urbano|a[eé]reo|mar[ií]timo|de ve[ií]culos)", r"rodovi\w*", r"estradas?", r"segurança vi[aá]ria", r"pedestres?",
    r"ciclov\w*", r"ve[ií]culos?", r"motoristas?", r"mobilidade (?:urbana|sustent[aá]vel|ativa)", r"ferrovia\w*", r"portos? (?:mar[ií]timos?|de)", r"aeroportos?",
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
    min_subject_hits: int | None = 2      # None = title must match; N = or N distinct terms in subjects
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
        include=TRANSPORT_RE, min_subject_hits=None, country="INT",
        notes="DSpace 7; ~40k records; title-only transport filter (100+ subject headings per record make subjects useless)",
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
    """Keep a record if a transport term appears in the title, or if the subject headings
    contain at least two *distinct* transport terms.  Abstracts are ignored: development
    literature mentions roads and ports in passing.  Development-bank records carry 100+
    subject headings (a budget review lists "ROADS" among them), so one hit is not enough.
    Measured precision on random samples (2026-08-26): see README."""
    if source.include is None:
        return True
    if source.include.search(rec.get("title") or ""):
        return True
    if source.min_subject_hits is None:
        return False
    hits = {m.group(0).lower() for m in source.include.finditer(" ; ".join(rec.get("subjects") or []))}
    return len(hits) >= source.min_subject_hits
