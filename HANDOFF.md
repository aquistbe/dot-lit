# HANDOFF — transport-lit: current state and where to pick up

Last updated 2026-08-27 (session with Claude Code). Read this before touching anything.
The README is the user-facing reference; this file is the operator's view.

## What it is

`transport-lit` is an MCP server + CLI giving AI assistants search over transportation
grey and journal literature via a **local** SQLite/FTS5 index (optional embeddings, citation
graph). Repo: https://github.com/aquistbe/transport-lit (renamed from `dot-lit` on
2026-08-27; old env names `DOT_LIT_*` still honoured). PyPI: https://pypi.org/project/transport-lit/
(0.3.0, 0.4.0, 0.5.0, 0.5.1 published). Owner/maintainer: D. Alex Quistberg (Drexel),
GitHub `aquistbe`. Commits are SSH-signed via 1Password — signing prompts him; if it
fails ("agent returned an error"), ask him to unlock 1Password and retry.

## Where things live (his machine)

- Code: `/Users/daq26/code/mcp-transport-lit` (uv project; `uv run pytest` = 28 tests).
- Installed tool: `uv tool install ".[semantic]"` → `~/.local/bin/transport-lit`, `transport-lit-mcp`.
- Data: `~/.local/share/transport-lit/` — `transport-lit.sqlite` (~1.6 GB), `raw/` (cached
  harvest pages, lets `reindex` work offline), `pdf/`, `vectors/<slug>/`, `models/`, `logs/`.
- Settings: `~/.config/transport-lit/env` (KEY=VALUE; holds `TRANSPORT_LIT_CONTACT` and the
  CiNii app id `TRANSPORT_LIT_CINII_APPID`; never commit it).
- Claude Desktop: server entry `transport-lit` (config written by `install-claude-desktop --write`).
- launchd: `org.transport-lit.harvest-weekly` (Mon 06:00, `harvest --source all --mode incremental`),
  `org.transport-lit.harvest-monthly` (1st 05:00, `harvest --source all --fresh`). Logs in data `logs/`.
- Cloud routine (claude.ai): "transport-lit monthly issue triage", 1st of month 13:00 UTC,
  id `trig_01BNRGg5BWjuiiCAH3cwjbtw` — triages issues, opens PRs, never pushes main.

## Index state (2026-08-27)

342,727 records: ROSA-P (dot) 90,599 · CiNii 118,609+ · PubMed 105,028 · VTI 11,460 ·
OpenAlex reports 11,448 · BASt 2,970 · CEPAL 1,165 · World Bank 976 · IPEA 207.
Vectors: 342,462 (fastembed `paraphrase-multilingual-MiniLM-L12-v2`, 384-d); the active
set is recorded in `meta.embeddings.active`. Citation graph: `cite prefetch` in progress
(last log line: `doi:26050/117262queried,23977resolvedsofar`); a second pass is needed for batches skipped on 429s.
Public snapshot (v0.4.0 release asset, 989 MB): 223,853 records + vectors, excludes CiNii
and TRID. Rebuild with `transport-lit snapshot build <file>` then `gh release upload`.

## Architecture (src/transport_lit)

config.py (env + env file) · oai.py (OAI-PMH client, ROSA-P quirks) · dc.py (record parser,
two ROSA-P metadata profiles, year fallback) · sources.py (OAI source registry + multilingual
transport filter) · apis.py (OpenAlex/CiNii/PubMed query harvesters) · harvest.py (full /
incremental / --fresh rebuild / reindex; per-source granularity) · store.py (schema, FTS5,
search, lookup, doctor/repair) · embeddings.py (fastembed + Ollama backends, hybrid RRF,
source diversification) · graph.py (OpenAlex citation graph, on-demand + prefetch) ·
importers.py (RIS, for TRID exports) · fulltext.py (PDF → text) · citations.py (RIS/BibTeX
export) · snapshot.py · server.py (MCP tools) · cli.py.

Verified facts worth not re-deriving: ROSA-P offers only oai_dc, no sets, tokens expire in
~60 s, returns an empty envelope instead of noRecordsMatch, unstable page order; BASt OPUS
needs day-granularity dates; DSpace puts accession timestamps in dc:date; CiNii Research
requires an appid (503 without a valid one; new ids take time to activate); OpenAlex resolves
NTL 10.21949 DOIs; PyPI trusted publishing rejects reusable-workflow tokens (hence
release.yml *dispatches* publish.yml); a release created by GITHUB_TOKEN does not trigger
`release:` events.

## Release procedure

Bump version in pyproject.toml, src/transport_lit/__init__.py, server.json → `uv lock` →
tests → commit → `git tag -a vX.Y.Z` → push tag. release.yml builds, creates the GitHub
release, dispatches publish.yml; **Alex must approve** the `pypi` environment run
(https://github.com/aquistbe/transport-lit/actions). Then verify with
`uvx --refresh --from "transport-lit==X.Y.Z" transport-lit --version`.

## Open items / where to pick up

1. Finish citation prefetch second pass (`transport-lit cite prefetch`, skips resolved records)
   and report `harvest_status().citation_graph`.
2. Refresh the public snapshot after the first monthly rebuild (v0.4.0 asset is from 2026-08-26).
3. First launchd runs: weekly Mon 2026-08-31 06:00, monthly 2026-09-01 05:00 — check
   `~/.local/share/transport-lit/logs/` and `transport-lit doctor` afterwards.
4. Alex's threads: TRIS/TRB email about a TRID data agreement (long shot; draft was given);
   SAVIR colleagues about reviving SafetyLit as a weekly bulletin (`transport-lit digest`
   is the raw material).
5. Possible next features: OpenCitations/Semantic Scholar as graph fallbacks; separate
   title/abstract vectors (measured benefit small); more sources (IRDB direct, HAL, gov.uk).

## Known lessons (do not repeat)

- Never delete SQLite `-wal`/`-shm` while any process (e.g. Claude Desktop's server) has the
  DB open; use `transport-lit doctor --repair`.
- Run pytest in a way that fails the chain (`if uv run pytest ...; then ... else exit 1`),
  not piped through `tail`.
- Alex reviews by exercising the tools in Claude Desktop and sends findings as bullets;
  answer each with a measurement, not a guess.
