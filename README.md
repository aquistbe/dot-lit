# dot-lit — U.S. DOT grey literature over MCP

`dot-lit` gives an AI assistant (Claude Desktop, Claude Code, any MCP client) keyword
search over the transportation research reports that PubMed does not index and Semantic
Scholar covers poorly. It started with **ROSA-P**, the U.S. National Transportation
Library's repository (NHTSA *DOT HS* reports, FHWA/FRA/FTA/FAA, UTC and state DOT
research; <https://rosap.ntl.bts.gov>), and now harvests six OAI-PMH sources on three
continents plus whatever you export from TRID:

| key | source | records | notes |
|-----|--------|--------:|-------|
| `dot` | ROSA-P — U.S. DOT National Transportation Library | 90,599 | full repository |
| `vti` | VTI — Swedish National Road and Transport Research Institute (DiVA) | 11,460 | reports, conference papers, articles; en/sv |
| `bast` | BASt — German Federal Highway Research Institute (OPUS) | 2,970 | 1,901 with direct PDF links; de/en |
| `wbokr` | World Bank Open Knowledge Repository | 976 | title-filtered subset of 40,332; measured precision 18/20 |
| `ipea` | IPEA (Brazil) | 207 | filtered subset of 14,400; pt; precision ~16/20 |
| `cepal` | CEPAL/ECLAC (Latin America) | 1,165 | filtered subset of 52,199; es/en; precision ~15/20 |
| `openalex` | OpenAlex — works typed *report* in 10 transport topics (global) | 11,448 | topics: Traffic and Road Safety, Urban Transport and Accessibility, Transportation Planning, … |
| `cinii` | CiNii Research (Japan) — articles, theses, IRDB repository items | 118,609 | 20 ja/en queries, 10k cap each; 59k with abstracts; CJK queries use substring matching |
| `pubmed` | PubMed — transport/injury subset (MeSH strategy + 12 journals) | 105,028 | date-sliced E-utilities harvest; `DOT_LIT_PUBMED_TERM` overrides the strategy |
| `trid` | TRID exports you import (`dot-lit import`) | yours | see below |

`dot-lit sources` lists them; `dot-lit harvest --source <key>|all` harvests them; the
`collection` filter in `search_reports` selects one (e.g. `"VTI"`, `"BASt"`, `"World Bank"`,
`"CEPAL"`, `"TRID"`). Adding another OAI-PMH repository is one entry in
`src/dot_lit/sources.py`.

> I created this for my own personal academic and research use and am happy to share it
> with anyone else who finds it useful. I welcome feedback on errors, integration needs,
> improvements and other commentaries. I will check those regularly and will integrate them
> as much as possible and document that. If you are interested in helping to support this or
> have other ideas for it, I welcome them! — Alex Quistberg
> ([open an issue](https://github.com/aquistbe/dot-lit/issues))

It does this the only way that works for an OAI-PMH source: it **harvests the whole
repository's metadata into a local SQLite database**, builds an **FTS5 full-text index**
over it, and serves search from that index. Nothing is queried live except an optional PDF
fetch for full text. Re-harvests are incremental (`from=` on the OAI request) and cheap.

## Tool surface

| Tool | What it returns |
|------|-----------------|
| `search_reports(query, year_min?, year_max?, collection?, doc_type?, source?, limit?, offset?)` | Ranked hits: id, title, authors, year, report numbers, DOI, landing URL, abstract snippet, `match_mode`. Column prefixes work (`title:pedestrian`, `authors:lynn`) |
| `lookup(identifier)` | Exact match by DOI, PMID, report number ("DOT HS 813 097"), id or landing URL |
| `get_report(id)` | Full metadata record, including every raw field as harvested |
| `get_fulltext(id, max_chars?, offset?, refresh?)` | Resolves the PDF (ROSA-P landing page, BASt/OpenAlex direct links), extracts and caches the text; page with `offset` |
| `search_fulltext(query, limit?)` | Searches inside all PDF text already extracted, with snippets |
| `find_similar(id, limit?)` | Related records across sources, by title and subject terms |
| `export_citations(ids, format?)` | RIS (Zotero/EndNote/Mendeley) or BibTeX for a list of ids |
| `whats_new(days?, source?, limit?)` | Records that entered the index in the last N days, with counts by source — the raw material for a weekly digest |
| `list_collections()` | Collections and document types with counts |
| `harvest_status()` | Record counts per source, last run and its status/notes, coverage by year |

Plus one prompt, `literature_scan(topic)`, that walks a model through a multi-query scan with
citations. Every tool carries MCP annotations (`readOnlyHint`, `idempotentHint`; only
`get_fulltext` is `openWorldHint` because it may fetch one PDF).

`id` accepts `dot:93144`, `93144`, `oai:dot.stacks:dot:93144`, or the landing URL. Imported
records use other prefixes (`trid:813520`, `import:…`).

Query syntax: bare words are ANDed first; if fewer than `limit` hits match every term the
remaining slots are filled with any-term matches (`match_mode` = `all_terms` /
`any_terms`). Quote phrases (`"driver improvement"`), use a trailing `*` for a prefix.
Ranking is BM25 with title, report number and author weighted above abstract.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/aquistbe/dot-lit && cd dot-lit
uv tool install .            # installs `dot-lit` (CLI) and `dot-lit-mcp` (server) on PATH
export DOT_LIT_CONTACT=you@example.org   # identifies your harvester to ROSA-P (put it in your shell profile)
dot-lit probe                # live check: Identify / ListMetadataFormats / ListSets
dot-lit harvest              # full harvest the first time (~15 min), incremental afterwards
dot-lit status               # counts, last run, coverage by year
dot-lit search driver improvement program evaluation
```

For development use `uv sync` and prefix commands with `uv run` (e.g. `uv run pytest`).

### Any MCP client, any model

The server speaks standard MCP over **stdio** (default) and **Streamable HTTP** / SSE
(`dot-lit-mcp --transport streamable-http --port 8765`, endpoint `/mcp`). `dot-lit
mcp-config [client]` prints a ready-to-paste snippet for: Claude Desktop, Claude Code,
Cursor, VS Code (Copilot agent mode), Zed, Continue, LM Studio, Goose, Open WebUI and
LibreChat (the last two over HTTP). A `Dockerfile` builds an HTTP server image with the
index on a volume.

**Open models.** Tested end to end on 2026-08-26 with Ollama `qwen2.5:3b` (3 B parameters)
via `tests/ollama_smoke.py`: given "find reports about driver improvement programs; list 3
titles with years and ids", the model called `search_reports({"query": "driver improvement",
"limit": 3})` once and answered with correct titles, years, ids and landing URLs from three
sources. Design choices that make small models work: ten tools with one-line-first
descriptions, flat JSON arguments with defaults, compact hit objects (no raw metadata in
search results), and a server `instructions` string that names the sources and filters. Run
the smoke test with any tool-capable model: `OLLAMA_MODEL=llama3.1 uv run python
tests/ollama_smoke.py "…"`.

### Register in Claude Desktop

```bash
dot-lit install-claude-desktop          # prints the JSON to add
dot-lit install-claude-desktop --write  # merges it into claude_desktop_config.json (keeps a .bak)
```

The entry it writes is simply:

```json
{ "mcpServers": { "dot-lit": { "command": "/Users/you/.local/bin/dot-lit-mcp", "args": [],
                               "env": { "DOT_LIT_DATA_DIR": "/Users/you/.local/share/dot-lit",
                                        "DOT_LIT_CONTACT": "you@example.org" } } } }
```

Restart Claude Desktop afterwards. For Claude Code: `claude mcp add dot-lit -- dot-lit-mcp`.

### Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOT_LIT_DATA_DIR` | `~/.local/share/dot-lit` | SQLite DB, raw OAI pages (`raw/`), PDF cache (`pdf/`) |
| `DOT_LIT_CONTACT` | *(unset)* | Your e-mail, placed in the User-Agent so the repository can contact you. Set it. |
| `DOT_LIT_MIN_INTERVAL` | `1.0` | Minimum seconds between outbound requests |
| `DOT_LIT_HTTP_TIMEOUT` | `90` | Per-request timeout (s) |
| `DOT_LIT_MAX_PDF_BYTES` | 80 MB | Refuse larger PDFs in `get_fulltext` |
| `DOT_LIT_MAX_PDF_PAGES` | 600 | Stop extraction after this many pages |

No credentials are used or stored anywhere; every request goes to public endpoints.

### Install without cloning (after the first PyPI release)

`uvx --from dot-lit dot-lit-mcp` will run the server straight from PyPI once the
`publish.yml` workflow is armed: on pypi.org create the `dot-lit` project and add a *trusted
publisher* (owner `aquistbe`, repo `dot-lit`, workflow `publish.yml`, environment `pypi`);
every GitHub Release then publishes automatically. `server.json` is the manifest for the
MCP Registry (`registry.modelcontextprotocol.io`), to submit after the PyPI package exists.

### Pinned versions

Releases are git tags `vMAJOR.MINOR.PATCH` (semantic versioning: patch = fixes, minor = new
tools/sources, major = a breaking change to the tool surface or database schema). Each tag
triggers the `release` workflow, which runs the tests, builds a wheel + sdist and attaches
them to a GitHub Release. Python dependencies are pinned by the committed `uv.lock`; CI
installs with `uv sync --frozen`, so a release always runs against the exact versions it was
tested with. To install a specific version:

```bash
uv tool install git+https://github.com/aquistbe/dot-lit@v0.1.0
uv tool upgrade dot-lit          # later, to move to whatever main points at
```

## Harvesting

```bash
dot-lit harvest                     # ROSA-P; auto: incremental if a complete full harvest exists, else full
dot-lit harvest --source all        # every configured source (vti, bast, wbokr, ipea, cepal, rosap)
dot-lit harvest --mode full         # walk the whole repository again
dot-lit harvest --mode incremental  # from = start of last complete run − 1 h, until = now
dot-lit harvest --from 2026-08-01T00:00:00Z   # explicit window (full timestamp required)
dot-lit harvest --max-pages 3       # testing only; the run is recorded as failed/partial
dot-lit reindex                     # re-parse the cached raw pages (no network) after a parser change
```

What the harvester does and why (all behaviour verified against ROSA-P on 2026-08-26):

* `ListRecords&metadataPrefix=oai_dc`, 100 records per page, following `resumptionToken`
  until a page arrives **without** one. Only then is the run marked `complete`; any error
  leaves it `failed` and does not advance the "last harvest" pointer, so `harvest_status`
  never claims a partial index is complete.
* **Pacing:** one request per `DOT_LIT_MIN_INTERVAL` seconds (default 1 s). Tokens expire
  about 60 s after issue, so retries use short backoff (2/4/6 s).
* **`badResumptionToken`, transport errors, truncated XML, or an empty envelope while a
  token is live** → the list is re-issued. ROSA-P does *not* return records in a stable
  datestamp order (checked on every page), so recovery restarts the list from the top; upserts
  make that idempotent. If ordering had been monotone the harvester would instead resume from
  the smallest datestamp seen via `until=`. Up to 8 recoveries per run, then `failed`.
* **`noRecordsMatch`:** ROSA-P does not send the error code; an empty selective harvest comes
  back as an OAI-PMH envelope with no `<ListRecords>` element. That is mapped to "nothing to
  do" only when no token was in play; mid-list it is treated as truncation.
* **Silent truncation checks:** the token's `cursor` is compared with the local count on every
  page; a full harvest that returns >5 % fewer records than the previous full harvest is
  flagged in the run notes. Both appear in `harvest_status().last_harvest.notes`.
* **Deletions:** the repository reports `deletedRecord=no`, so nothing is ever removed
  locally; a record that vanishes from ROSA-P stays in the index until a full re-harvest
  into a fresh `DOT_LIT_DATA_DIR`.
* **Caching:** every OAI page is stored gzipped under `raw/run<N>-p<page>.xml.gz`, so the
  parser can be changed and the index rebuilt without touching the network; PDFs and their
  extracted text are cached under `pdf/` and in the `fulltext` table.
* `from`/`until` are formatted to each repository's declared `granularity` (read from
  `Identify`): ROSA-P, DiVA and DSpace take full `YYYY-MM-DDThh:mm:ssZ` timestamps, OPUS
  (BASt) takes only `YYYY-MM-DD` and the window is widened a day each side.
* Broad repositories (World Bank, IPEA, CEPAL) are filtered at harvest time by a
  multilingual transport vocabulary (`sources.TRANSPORT_RE`, en/es/pt/de/fr/sv): a record is
  kept if a term appears in the **title**, or (IPEA, CEPAL) if two distinct terms appear
  among the **subject headings**. Abstracts are ignored — development literature mentions
  roads and ports in passing — and World Bank subjects are ignored too (100+ headings per
  record). This was tuned on 2026-08-26 against random 20-title samples: the loose
  title+subjects+abstract rule kept 15,271 World Bank records at roughly 35–50 % precision;
  the final rule keeps 976 at 18/20, IPEA 207 at ~16/20, CEPAL 1,165 at ~15/20. Recall is
  the price; loosen `min_subject_hits` in `sources.py` and run `dot-lit reindex --source
  <key>` (no network) if you want the other trade. The run notes record kept vs skipped.
* `dot-lit reindex --source <key>` re-parses the cached pages **and prunes** records the
  current parser/filter no longer keeps, so filter changes never need a re-harvest.

### Monthly rebuild and weekly updates (maintenance schedule)

The corpus changes slowly, so the cadence is: **weekly incremental** harvest and a **monthly
fresh rebuild**. `harvest --fresh` full-harvests into a temporary store and then atomically
replaces the `dot:` records in the live index — the only way records ROSA-P stops serving
ever disappear (its OAI-PMH endpoint does not track deletions). Imported sources (TRID
exports) are untouched, and a failed rebuild changes nothing.

```bash
dot-lit install-schedule          # shows the two launchd agents
dot-lit install-schedule --write  # installs them: Mon 06:00 `--source all` incremental, 1st 05:00 `--source all --fresh`
```

Logs land in `$DOT_LIT_DATA_DIR/logs/`. On Linux use the cron lines the command prints.

Monthly maintenance checklist (done with the rebuild): read new GitHub issues; `uv lock
--upgrade && uv run pytest`; note fixes in the changelog section of the release; bump
`version` in `pyproject.toml` and `src/dot_lit/__init__.py`; `git tag vX.Y.Z && git push
--tags`.

### TRID: import what you export

TRID (<https://trid.trb.org>) is the most complete transportation bibliography and the
natural complement to ROSA-P, but it has no API, its FAQ says TRB "does not grant access to
TRID backend systems or lift export/download restrictions", and its `robots.txt` disallows
AI crawlers. What every user *may* do is search and export. So:

1. Run your search in TRID, choose **Export → RIS** (CSV and XML are also offered).
2. `dot-lit import ~/Downloads/trid-driver-improvement.ris --collection "TRID: driver improvement"`

Records get ids `trid:<accession>` from the TRID view URL, land in the `TRID` collection
(`search_reports(..., collection="TRID")`), and re-importing the same file is idempotent.
The importer is generic RIS, so Zotero/EndNote/Scopus exports work the same way with
`--source <prefix>`. `get_fulltext` on an imported record only follows a direct `.pdf`
link; otherwise use `landing_url`.

### What ROSA-P's OAI-PMH endpoint offers

`https://rosap.ntl.bts.gov/fedora/oai` — repository "DOT Stacks" (the CDC Stacks platform),
protocol 2.0, earliest datestamp 2008-07-02, no deletion tracking, no OAI sets
(`ListSets` is empty), and **`oai_dc` is the only metadata format**. It is a qualified
Dublin Core in disguise, though: elements such as `dc:contributor.author`,
`dc:description.abstract`, `dc:relation.isPartOf`, `dc:identifier.uri` (DOI *and* report
numbers, e.g. `DOT HS 813 827`), `dc:coverage.spatial`, `dc:title.alternative` and
`dc:description.tableOfContents` are all present. The parser (`dc.py`) keeps every raw
field and derives the typed columns from them. `dc:relation.isPartOf` (semicolon-separated)
is what `list_collections` / the `collection` filter use.

PDF links are not in the metadata; `get_fulltext` reads `citation_pdf_url` from the landing
page and falls back to the datastream convention `/view/dot/{n}/dot_{n}_DS1.pdf`.

## Verification (2026-08-26)

**v0.3.0 API sources.** OpenAlex: 58 pages, 11,448 reports (10 topics, `type:report`),
1,428 with PDF links. CiNii: 730 pages, 144,348 hits over 20 queries, 118,609 unique. PubMed:
105,028 articles in 17 date slices (E-utilities caps `retstart` at 10,000, so slices are
found recursively). Open-model check: Ollama `qwen2.5:3b` answered a driver-improvement
question with one correct `search_reports` call.

**v0.2.0 multi-source harvest.** VTI: 120 pages, 11,944 seen, 11,460 unique (DiVA serves
some records in several sets), 0 resumptions. BASt: 30 pages, 2,987 seen, 2,970 unique;
1,901 with direct PDF links; day-granularity incremental path exercised (24 records).
World Bank: 404 pages / 40,332 seen; IPEA: 144 / 14,400; CEPAL: 522 / 52,199 — all ended on
a token-less page with 0 resumptions; filtered counts above. Spot searches: `Fußgänger
Unfall` (BASt) → crash reconstruction and rural-road crash statistics; `acidentes de
trânsito mortalidade` (IPEA) → "Mortalidade por acidentes de transporte terrestre e
desigualdades interestaduais no Brasil"; `seguridad vial peatones` (CEPAL) → road-safety
governance and campaign evaluations; `pedestrian safety` (VTI) → 1990s child-pedestrian
training studies.

### v0.1.0 (first ROSA-P harvest)

**Harvest completeness.** Run 1 (`full`) walked 908 pages / 90,706 records in 15 min
(00:03:59–00:19:11 UTC) with 0 resumptions, 0 cursor mismatches, and ended on a page of 6
records with no resumption token — the OAI-PMH definition of a complete list. 90,603
unique records are in the store; the 103-record gap is the same record appearing on two
pages, which happens because ROSA-P does not return records in a stable order (the
harvester logs this: "datestamp ordering violated on page 2"). A second independent full
pass, 30 minutes later into a separate directory, returned exactly the same numbers —
908 pages, 90,706 seen, 90,603 unique — and the two ID sets are identical (0 records
unique to either pass). The 103 repeats are the repository serving the same record on
two pages, not records being skipped.

**Coverage by decade** (year present for 74,448 = 82 %; the remaining 16,155 have no date
in any metadata field; `year_source` says whether a year came from `dc:date` (48,658), a
bare-year description line (22,205) or the title (3,585)):

| decade | records | decade | records |
|-------:|--------:|-------:|--------:|
| 1900s–1930s | 3,243 | 1980s | 5,408 |
| 1940s | 2,618 | 1990s | 8,936 |
| 1950s | 2,627 | 2000s | 11,466 |
| 1960s | 2,947 | 2010s | 18,690 |
| 1970s | 5,057 | 2020s | 13,456 |

**Known-item retrieval** (`dot-lit search …`, rank 1 unless noted):

| Target | Query | Result |
|--------|-------|--------|
| NHTSA *Countermeasures That Work* | `"countermeasures that work" guide highway safety offices` | dot:1789 (2005), dot:1827 (3rd ed. 2008), dot:40255 (1st ed. 2006), dot:1778 (2nd ed. 2007); 11th ed. 2023 is dot:72947 (DOT HS 813 490), 10th ed. dot:57466. The bare phrase alone ranks the one-page *Traffic Tech* summaries of CTW first (short documents win on BM25), then the guides. |
| Oregon DMV Driver Improvement Program evaluation (Strathman et al., 2007) | `oregon driver improvement program strathman` | dot:21848 "Evaluation of the Oregon DMV driver improvement program", Strathman, Kimpel, Leistner; report no. SPR 634. Undated in ROSA-P metadata. |
| Virginia driver improvement reports (Lynn, 1982) | `virginia driver improvement lynn` | dot:18959 (12-month report), dot:18905 (short-term effects), dot:18969 (24-month final report), all Cheryl Lynn, Virginia Highway & Transportation Research Council. Undated in ROSA-P metadata. |

**Real query** `driver improvement program evaluation negligent operator` (top 6 of 10):

1. dot:18905 — An evaluation of the short-term effects of the Virginia driver improvement program (Lynn) — *all_terms*
2. dot:29326 — Review of NJ point system (Carnegie, Ozbay, Mudigonda, 2013; FHWA NJ-2013-004) — *all_terms*
3. dot:18959 — …Virginia driver improvement program on negligent driving: 12-month report (Lynn)
4. dot:18969 — …Virginia driver improvement program on negligent driving: 24-month report (Lynn)
5. dot:17678 — Study of recidivism rates among drivers administratively sanctioned by the New Jersey MVC (Carnegie et al., 2009)
6. dot:17677 — Study of the effects of plea bargaining motor vehicle offenses (Carnegie et al., 2009)

Full-text extraction was checked on dot:93144 (DOT HS 813 827, 3.7 MB PDF, resolved via
`citation_pdf_url`). Unit tests: `uv run pytest` (parser for both metadata profiles, year
fallback, FTS search/filters, upsert idempotence, query tokenizer, id normalisation).

## Layout

```
src/dot_lit/
  config.py    paths, User-Agent, pacing, limits (env-overridable)
  oai.py       rate-limited OAI-PMH client; typed errors; raw-page cache
  dc.py        oai_dc record -> typed dict (authors, year, DOI, report numbers, collections …)
  store.py     SQLite schema, FTS5 index + triggers, search, stats, harvest-run bookkeeping
  harvest.py   full / incremental harvest with completeness + truncation handling
  fulltext.py  PDF resolution, download (size-capped), pypdf extraction, cache
  server.py    MCP tools (FastMCP / MCPServer)
  importers.py RIS import (TRID exports and any other reference-manager export)
  cli.py       dot-lit probe | harvest [--fresh] | import | reindex | status | search | get | fulltext
               | install-claude-desktop | install-schedule
.github/workflows/  ci.yml (tests on push/PR), release.yml (wheel + GitHub Release on tag)
tests/         unit tests (parser, store, query tokenizer)
```

## Adding a second source later (e.g. NHTSA crashstats)

The store is source-agnostic: `records.id` is a prefixed string (`dot:93144` today),
`harvest_runs.source` records which harvester wrote a run, and the FTS index does not care
where a row came from. To add a source:

1. Write `src/dot_lit/sources/<name>.py` exposing `harvest(store, *, mode, progress)` that
   yields dicts in the same shape `dc.parse_record` produces (`id`, `title`, `authors`,
   `year`, `abstract`, `report_numbers`, `doi`, `landing_url`, `collections`, `raw`, …) and
   calls `store.upsert_records()`. Use a new id prefix (`nhtsa:812115`) and pass your own
   `source` name to `store.start_run()` so `harvest_status` can report it separately.
2. Reuse `oai.RateLimiter` and `config.USER_AGENT` for etiquette; store raw responses under
   `raw/<source>/` for reproducibility.
3. Give `harvest.status()` a per-source block (count by `id` prefix).
4. Add a `--source` option to `dot-lit harvest` and, if the source has its own facet, a
   corresponding filter on `search_reports`.
5. Dedupe against ROSA-P by DOI / report number (`records.doi`, `records.report_numbers`)
   rather than by title — NHTSA reports are often present in both places.

Verified facts for the NHTSA crashstats source, so nobody re-derives them:
`https://crashstats.nhtsa.dot.gov/Api/Public/Publication/{id}` returns the PDF directly
(`812115` → NMVCCS critical-reasons report, `application/pdf`, ~0.5 MB). It is a
document-retrieval endpoint, not a search or listing API, so a connector will need an
enumeration strategy (e.g. the `DOT HS` numbers already present in ROSA-P
`report_numbers`) rather than a crawl.

### Beyond the U.S.: candidate sources assessed (2026-08-26)

Probed live for (a) whether the holdings are *literature* rather than datasets and (b)
whether there is machine access that fits this harvester. Counts are what the endpoints
reported that day.

| Source | Holdings | Machine access | Verdict |
|--------|----------|----------------|---------|
| **VTI (Sweden) via DiVA** `vti.diva-portal.org/dice/oai` | 7,474 records, set `all-vti`; road-safety research institute | OAI-PMH, `completeListSize`, `oai_dc` + `swepub_mods` + MARC21 | **Harvest — drop-in** |
| **BASt (Germany) OPUS** `bast.opus.hbz-nrw.de/oai` | 2,987 records; federal highway research institute reports | OAI-PMH, `completeListSize`, `oai_dc` + `xMetaDissPlus` | **Harvest — drop-in** |
| **World Bank Open Knowledge Repository** `openknowledge.worldbank.org/server/oai/request` | 40,332 records; 1,787 hits for "transport safety"; OAI set `transport` holds only 100 | OAI-PMH (DSpace 7) + DSpace REST `discover/search` | Harvest all, keep by subject; or REST query |
| **WHO IRIS** `iris.who.int/oai/request` | 276,681 records; 3,334 hits for "road traffic"; no sets | OAI-PMH + DSpace REST search | REST query by subject (full OAI walk is 2,800 pages) |
| **CEPAL repository** (Latin America) `repositorio.cepal.org/server/oai/request` | 52,199 records; no subject sets | OAI-PMH + DSpace REST | Harvest and filter by subject |
| **MTT Chile Biblioteca Digital de Transportes** `biblioteca.mtt.gob.cl` | 5,820 `program_report` rows with name, description, category, files | Open Hasura GraphQL at `api.biblioteca.mtt.gob.cl/v1/graphql` (introspection enabled, unauthenticated read) | Harvestable via GraphQL; confirm terms of use with MTT first |
| **OpenAlex** `api.openalex.org` | 2,604 works typed *report* matching "road safety"; 16,639 works of any type for "pedestrian safety" | Free REST API, cursor pagination | Best global *aggregator*; use as a source for non-U.S. grey lit and DOIs |
| **GOV.UK (DfT)** `gov.uk/api/search.json` | 4,998 DfT items for "road safety research" | Free content API | Harvestable; needs document-type filtering |
| **Spain, Centro de Documentación del Transporte** | 66,000 bibliographic records (45,000 monographs) in AbsysNet | OPAC only; site blocks non-browser clients (HTTP 403) | Out of scope unless the ministry exposes OAI/Z39.50 |
| **TRIMIS (EU)** `trimis.ec.europa.eu` | EU-funded transport projects and results | Site up; no documented API (bulk open-data dumps exist) | Evaluate the open-data dump, not the site |
| **IDB Publications, CAF Scioteca** | Development-bank transport reports | DSpace, but bot-blocked (403 / challenge page) | Out of scope unless access is granted |
| **SWOV (Netherlands)** | Road-safety institute library | Bot-detection page on every path | Out of scope |
| **ITF/OECD** | International Transport Forum reports | HTTP 403 to non-browser clients; no API | Out of scope (OECD iLibrary API is licensed) |
| Transport Data Commons `portal.transport-data.org` | **Datasets** (32 institutions, 120+ countries), PortalJS | No API found (`/api/3` is 404) | Not literature |
| ITDP Rapid Transit Database | **Dataset** (BRT/LRT/metro km per city); Google Sheet download | Download only | Not literature |
| AASHTO TERI database | **Research-needs statements**, not completed reports | None | Not literature |
| nismod/Africa-transport-database (GitHub) | **GIS dataset** of African transport infrastructure | Git clone | Not literature |
| TRID | 1.5 M bibliographic records, international | None; export/backend access refused by policy | Out of scope |

**By region** (same-day probes; "open" means unauthenticated machine access confirmed):

| Region | What exists | Access | Notes |
|--------|-------------|--------|-------|
| Europe | VTI (SE), BASt (DE) — above; **HAL** (FR): 74,952 items in the Université Gustave Eiffel/IFSTTAR collection, 117 `REPORT`-typed hits for "sécurité routière"; **OpenAIRE**: 82,053 publications for "road safety" (all types, Europe-wide aggregator); **EU Publications Office Cellar** SPARQL and **CORDIS** search JSON both answer | HAL REST (open), OpenAIRE REST (open), Cellar SPARQL (open), CORDIS JSON (open); DTU Orbit OAI 500, TU Delft OAI not found, TØI 403 | ITF/OECD's ITRD merged into TRID, so ITF content is reachable only through TRID |
| Australia / NZ | **Figshare** OAI-PMH + REST (Monash/MUARC and other AU universities publish reports there); NZTA research report pages (HTML, 200); Austroads (403 to non-browsers); **APO** grey-literature observatory (403 to non-browsers); Trove API (needs key) | Figshare open; Trove key-gated; APO/Austroads bot-blocked | Figshare search for "road safety" reports returns mostly datasets/code — needs item-type + institution filtering to be useful |
| Japan | **IRDB** (`irdb.nii.ac.jp/oai`, national aggregator of institutional repositories; JPCOAR 2.0 + oai_dc, 9 sets); **CiNii Research** OpenSearch: 16,547 hits for 交通安全; **J-STAGE** WebAPI: 9,786 for "traffic safety" (journals, incl. IATSS Research) | All open, no key | IRDB is the grey-lit route (theses, technical reports from universities); NILIM/PWRI ministry reports are web-only |
| India | Shodhganga OAI not found at DSpace paths; CSIR-CRRI site is static HTML; IRC/MoRTH web-only | None found | Best coverage is OpenAlex/OpenAIRE for Indian journal output; no harvestable grey-lit source identified |
| China | No open repository of MOT/RIOH reports; RIOH site is static; CNKI is licensed | None found | OpenAlex returns 15,416 works from CN institutions for "traffic safety" (journal literature) — that is the realistic route |
| Latin America | **IPEA** (BR) `repositorio.ipea.gov.br/server/oai/request`: 14,400 records, 8,021 REST hits for "transporte"; **CEPAL** — above; **MTT Chile** GraphQL — above; IMT Mexico technical publications are HTML/PDF lists | IPEA/CEPAL OAI open; MTT GraphQL open; IDB/CAF bot-blocked; LA Referencia OAI not found at guessed URLs | SciELO OAI endpoints not found at legacy paths (journals anyway) |

The three OAI-PMH repositories with `completeListSize` (VTI, BASt, World Bank OKR) fit the
existing harvester with a source prefix and a per-source `metadataPrefix`; DSpace 7 sites
also tolerate `from`/`until` and return proper `noRecordsMatch`, so the ROSA-P quirks in
`oai.py` are already the harder case.

### TRID is out of scope

TRID (<https://trid.trb.org>) has no public API, no OAI-PMH endpoint and no bulk export.
Its FAQ states that "TRB does not grant access to TRID backend systems or lift
export/download restrictions for individuals or organizations" and that the database may
not be used to train LLMs. It is deliberately not scraped here.

### v2 order (agreed 2026-08-26)

1. ~~VTI + BASt~~ (done, v0.2.0) — 2. ~~World Bank OKR, IPEA, CEPAL~~ (done, v0.2.0) —
3. IRDB Japan — 4. OpenAlex `type:report` as global backstop — 5. a **PubMed transport
subset** (see below). VTI note: DiVA's `oai_dc` carries no full-text link; switching that
source to `swepub_mods`/`mets_kb` would give `get_fulltext` the `FULLTEXT01.pdf` URL.

### PubMed: a transport/injury subset, not all of PubMed

PubMed's E-utilities (`esearch`/`efetch`, free, 3 req/s without a key) can maintain a
local subset from a fixed strategy, refreshed with `mindate`/`maxdate` on the same
weekly/monthly cadence. Two complementary filters, OR-ed together:

* **MeSH strategy** — `"Accidents, Traffic"[MeSH] OR "Pedestrians"[MeSH] OR "Bicycling"[MeSH]
  OR "Automobile Driving"[MeSH] OR "Motorcycles"[MeSH] OR "Wounds and Injuries"[MeSH] AND
  ("Transportation"[MeSH] OR "Built Environment"[MeSH] OR "City Planning"[MeSH])` — catches
  transport papers in general and clinical journals.
* **Journal list** — Accident Analysis & Prevention, Traffic Injury Prevention, Journal of
  Safety Research, Injury Prevention, Injury Epidemiology, Journal of Transport & Health,
  Safety Science, Transportation Research Part F, Transport Reviews, BMC Public Health
  (transport-tagged only), etc. — catches transport papers indexed without the MeSH terms.

SafetyLit (safetylit.org, the WHO-affiliated weekly injury-literature bulletin) maintains
exactly such a journal list and hand-classifies articles by topic, which would make it the
best seed for the journal filter; its site was unreachable (connection refused on every
host name) when checked on 2026-08-26, so its current status is unconfirmed.

### Weekly digest (a SafetyLit-style bulletin)

`dot-lit digest --days 7 [--abstracts]` prints a Markdown bulletin of everything that entered
the index in the last week, grouped by source, with counts. It is driven by `first_seen_at`,
which is set the first time a record is seen and preserved across fresh rebuilds, so a
monthly rebuild does not make the whole index look new. The `whats_new` tool exposes the
same data to a model, which can then write the summaries — the editorial step SafetyLit did
by hand.

### Compared with other literature MCPs

PubMed, Semantic Scholar, OpenAlex and arXiv MCP servers proxy live queries to one API.
`dot-lit` differs in three ways: it indexes **grey literature the aggregators lack** (agency
reports, state DOT evaluations, ITRD-contributing institutes), it **runs offline** on a local
index after harvesting (no rate limits at query time, no key), and it is **multi-source**
with one id scheme, so a model can search everything at once and export citations. What
those servers have that this one still lacks: citation graphs (who cites whom), author
disambiguation, and semantic (embedding) search — see below.

## Embeddings (not in v1)

Search is lexical (FTS5/BM25). The schema leaves room for a `record_embeddings` table keyed
by `records.id`; a hybrid ranker could fuse BM25 with cosine scores. Keep it optional so the
package installs without a model download; an Ollama embedding model (e.g. `qwen3-embedding`)
would keep it fully local.
