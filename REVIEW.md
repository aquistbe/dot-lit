# Transportation literature MCP review — 2026-09-05

The local index works for literature discovery. It contains 342,881 records across nine
sources. Keyword queries for pedestrian safety, traffic calming, and older adults returned
records. The scheduled updates need the patched installation: both launchd harvest logs
ended with `sqlite3.OperationalError: database is locked`. The most recent recorded
harvests finished on September 2, except OpenAlex, whose incremental attempt failed.

## Current connections

| Source | Local records | Live check |
|---|---:|---|
| ROSA-P | 90,674 | Identify and one 100-record page parsed |
| VTI | 11,467 | Identify and one 14-record page parsed |
| BASt | 2,970 | Identify and one 100-record page parsed |
| World Bank OKR | 976 | Identify and one 100-record page parsed, including two records without titles |
| IPEA | 207 | Identify and one 11-record page parsed |
| CEPAL | 1,165 | Identify and one 37-record page parsed |
| CiNii | 118,903 | Authenticated query and one result parsed |
| PubMed | 105,071 | Strategy returned 105,142 matches; one article fetched and parsed |
| OpenAlex | 11,448 | HTTP 429; live harvesting and citation retrieval could not be verified |

These are connection and parser checks, not full harvests or a completeness audit.
The citation cache contains 1,029,466 works and 2,258,220 edges. Record counts include
cross-source duplicates. TRID is supported through RIS imports; no TRID records are
currently in this local database.

## Issues fixed in this checkout

1. **Scheduled harvests blocked by database writes.** Preserved the existing staged
   migration fix. Citation prefetching and hydration now commit before the next network
   request so retries cannot hold the SQLite writer for minutes. The installed copy
   examined at the start of this review did not contain the staged fixes.
2. **Fresh rebuilds could overwrite other harvests' raw pages.** Temporary databases
   restarted run numbering at 1 while sharing the live raw cache. Fresh rebuilds now
   reserve a unique live run ID, use a unique temporary directory, retain first-seen
   dates, and record unsuccessful attempts. A drop exceeding 5% blocks replacement.
3. **Reindexing could prune valid records after cache loss.** It now checks the expected
   page sequence and compressed/XML/JSON readability before changing the index.
4. **OpenAlex incremental updates assumed paid access.** Free-plan updates now re-read
   the configured subset, preserving retrieval of newly indexed older reports. Both
   harvesting and citations support API keys, and paging uses the documented 100-record
   maximum. Error messages omit credential-bearing query strings.
5. **Field searches did not implement the advertised syntax.** Title, author, and report
   number qualifiers now work. Lookup recognizes exact landing URLs and OAI identifiers;
   empty lookup input returns no records.
6. **Semantic filtering could hide an entire small source.** Source and date restrictions
   now apply before nearest-neighbor selection. Semantic mode falls back to keyword
   search when its backend is unavailable. Loaded embedding models are reused across calls.
7. **Partial PDF files were reused after failed downloads.** Downloads now use temporary
   files and replace the cache only after success. PDF links with query parameters work;
   unknown record IDs return a clear result; cache filenames cannot traverse directories.
   Non-ROSA-P reports no longer receive invented ROSA-P-style PDF hints.
8. **PubMed metadata could take a DOI from a cited reference.** DOI and PMC identifiers
   now come from the article's own identifier list. Incremental queries use modification
   date. Unexpected XML/error envelopes and empty retrievals fail the harvest; a single
   over-limit date slice fails explicitly.
9. **Health checks missed some failures.** FTS checks now compare against external content,
   same-day stale runs are compared as dates, and source status exposes the latest failed
   attempt alongside the last successful run.
10. **Snapshot installation overwrote the live file and deleted WAL/SHM files.** It now
    stages and validates the archive, then installs through SQLite's backup API. Snapshot
    creation avoids duplicate archive members and removes excluded IDs from vectors and
    linked citation metadata.
11. **MCP 1.x compatibility was incomplete.** Tool annotation names now serialize correctly
    on both SDK generations; HTTP startup uses the appropriate settings API. The minimum
    dependency is now the tested 1.29.1 release.
12. **Citation edge handling had avoidable errors.** Unicode title matching retains
    Japanese text, missing works do not become permanently cached empty reference lists,
    and explicit reference refresh replaces stale edges.

## Validation

The original 29 tests passed before changes. The patched suite has 49 passing tests. Regression tests cover the failures above,
including fresh OAI/API harvest → cache → reindex, refused truncation, missing cache
pages, filtering before nearest-neighbor selection, snapshot installation with another
connection open, excluded snapshot vectors, and MCP stdio initialization/tool calls.
All 49 tests passed with MCP 1.29.1 and again with MCP 2.1.1; wheel and source-distribution builds succeeded. Live parser checks fetched only
small samples and did not modify the research index or launch a full harvest.

## Installed verification

With approval, the patched local wheel was installed into the tool used by Claude
Desktop and launchd. A new installed MCP process exposed all 12 tools and searched the
existing 342,881-record index successfully. The first keyword request took 8.04 seconds;
the first hybrid request took 13.01 seconds, including model startup; a second hybrid
request took 0.88 seconds. These are individual observations, not a benchmark. Status
reported 342,462 vectors (419 fewer than the record count) and 384 embedding dimensions.
A live ROSA-P full-text check (`dot:86012`) downloaded and extracted a 40-page PDF with
78,112 characters into a temporary test directory. A backend pooling-change warning also appeared; future vector metadata should record
library versions and pooling settings so compatibility can be checked explicitly.

Restart Claude Desktop to replace its already-running MCP process. The next scheduled
harvest has not yet been observed. OpenAlex remains subject to the rate limit. Changes
are local and have not been pushed to GitHub or published to PyPI; the installed local
patch retains version 0.5.1.

## Improvements to prioritize next

- **Add an OpenAlex key locally.** No key was configured during the review. Current
  unauthenticated requests returned 429. A free key increases the budget but does not
  unlock paid sync filters. The live OpenAlex fix still needs verification after the
  rate-limit restriction clears. See the official [authentication guide](https://help.openalex.org/api/authentication/)
  and [filter guide](https://help.openalex.org/api/filtering/).
- **Measure retrieval against a known set of relevant papers.** Use 30–50 sentinel papers
  spanning pedestrian safety, older adults, Latin America, and intervention evaluations.
  Report recall by source and language. Current topic filters, journal selection, and
  CiNii's per-query cap prevent this index from representing all transportation literature.
  CiNii incremental queries restrict publication year, so newly added older records depend
  on full refreshes. PubMed's MeSH component can miss new, not-yet-indexed papers outside
  the selected journals; consider a reviewed title/abstract component.
- **Separate reproducible review searches from ranked discovery.** The existing query
  parser ignores Boolean operators; hybrid ranking, source diversification, and result
  limits affect what appears. Add a strict Boolean/all-term mode, complete result export,
  saved query/source/date provenance, and stable pagination before using this as the sole
  search tool for a systematic review.
- **Track changed content in the vector index.** Existing vectors are keyed by ID, so
  changed titles or abstracts are skipped by incremental embedding. Store a content hash,
  refresh changed vectors, and publish vector generations atomically. The scheduler
  currently harvests metadata without updating embeddings.
- **Represent many local records per citation work.** `works.record_id` stores only one
  local match. The same DOI in PubMed, ROSA-P, and CiNii needs a separate mapping table
  if citation counts and in-index links are to appear consistently on every duplicate.
  Title-only matches should be reviewed before interpreting citation counts; choosing
  the most-cited candidate may favor a different edition.
- **Improve full-text coverage.** PubMed records usually link to PMC landing pages, not
  direct PDFs. Add a supported PMC retrieval route, broader repository file resolution,
  and optional OCR. A failed full-text fetch does not mean that a paper has no full text.
- **Monitor actual scheduled execution.** Add a lightweight daily report of last successful
  harvest by source, failed runs, and vector coverage. Health-check warnings about long
  runs are heuristic: `doctor --repair` can mark a legitimately long harvest failed.

The review targets local Claude Desktop use. The HTTP listener has no application-level
user authentication; exposing it beyond localhost requires a separate deployment review.
