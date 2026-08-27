# dot-lit — U.S. DOT grey literature over MCP

`dot-lit` gives an AI assistant (Claude Desktop, Claude Code, any MCP client) keyword
search over the transportation research reports that PubMed does not index and Semantic
Scholar covers poorly: NHTSA's *DOT HS* technical report series, FHWA/FRA/FTA/FAA
research reports, University Transportation Center reports, and state DOT evaluations, as
held by **ROSA-P**, the National Transportation Library's repository
(<https://rosap.ntl.bts.gov>).

It does this the only way that works for an OAI-PMH source: it **harvests the whole
repository's metadata into a local SQLite database**, builds an **FTS5 full-text index**
over it, and serves search from that index. Nothing is queried live except an optional PDF
fetch for full text. Re-harvests are incremental (`from=` on the OAI request) and cheap.

## Tool surface

| Tool | What it returns |
|------|-----------------|
| `search_reports(query, year_min?, year_max?, collection?, doc_type?, limit?)` | Ranked hits: id, title, authors, year, report numbers, DOI, landing URL, abstract snippet, `match_mode` |
| `get_report(id)` | Full metadata record, including every raw Dublin Core field |
| `get_fulltext(id, max_chars?, offset?, refresh?)` | Resolves the PDF on ROSA-P, extracts and caches the text; page through with `offset` |
| `list_collections()` | Collections (`dc:relation.isPartOf`) and document types with counts |
| `harvest_status()` | Record count, last harvest run and its status/notes, coverage by year and decade |

`id` accepts `dot:93144`, `93144`, `oai:dot.stacks:dot:93144`, or the landing URL.

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

## Harvesting

```bash
dot-lit harvest                     # auto: incremental if a complete full harvest exists, else full
dot-lit harvest --mode full         # walk the whole repository again
dot-lit harvest --mode incremental  # from = start of last complete run − 1 h, until = now
dot-lit harvest --from 2026-08-01T00:00:00Z   # explicit window (full timestamp required)
dot-lit harvest --max-pages 3       # testing only; the run is recorded as failed/partial
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
* `from`/`until` must be full `YYYY-MM-DDThh:mm:ssZ` timestamps (a bare date is a
  `badArgument`).

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
  cli.py       dot-lit probe | harvest | status | search | get | fulltext | install-claude-desktop
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

### TRID is out of scope

TRID (<https://trid.trb.org>) has no public API, no OAI-PMH endpoint and no bulk export.
Its FAQ states that "TRB does not grant access to TRID backend systems or lift
export/download restrictions for individuals or organizations" and that the database may
not be used to train LLMs. It is deliberately not scraped here.

## Embeddings (not in v1)

Search is lexical (FTS5/BM25). The schema leaves room for a `record_embeddings` table keyed
by `records.id`; a hybrid ranker could fuse BM25 with cosine scores. Keep it optional so the
package installs without a model download.
