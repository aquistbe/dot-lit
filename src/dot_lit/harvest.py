"""Harvest ROSA-P into the local store.

Modes
-----
full          Walk the entire repository (ListRecords with no ``from``).
incremental   ``from`` = start of the last complete run minus a 1-hour overlap, ``until``
              = now.  ROSA-P does not track deletions, so nothing is ever removed.
auto          incremental if a complete full harvest exists, otherwise full.

Completeness / truncation handling
----------------------------------
* A list is only marked ``complete`` when the last page carries **no** resumption token.
  Any exception mid-list leaves the run ``failed`` and the previous "last successful
  harvest" untouched, so ``harvest_status`` never over-claims.
* The ``cursor`` attribute on each token is compared with our own record count; a
  mismatch is recorded in the run notes.
* An empty envelope (no ``<ListRecords>``) while a token is live is treated as a
  truncation, not as end-of-list.
* Records arrive in descending ``datestamp`` order (observed, and checked on every page).
  When that ordering has held so far, a ``badResumptionToken`` (tokens live ~60 s) or a
  transport failure is recovered by re-issuing the list with ``until`` = the smallest
  datestamp seen, which resumes from the boundary at the cost of re-fetching one page's
  worth of overlap.  If ordering was ever violated, recovery restarts the list from the
  top instead (upserts make that idempotent, just slower).
* After a full harvest the record count is compared with the previous full harvest; a
  drop of more than 5 % is flagged.
"""

from __future__ import annotations

import gzip
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .dc import OAI_NS, parse_record
from .oai import BadResumptionToken, NoRecordsMatch, OAIClient, TransportError, TruncatedList
from .sources import SOURCES, Source, matches_filter
from .store import Store, utcnow

log = logging.getLogger(__name__)

SOURCE = "rosap"  # default source key (kept for backwards compatibility)
OVERLAP = timedelta(hours=1)
MAX_RESUMPTIONS = 8


@dataclass
class HarvestResult:
    run_id: int
    kind: str
    status: str
    records_seen: int
    pages: int
    resumptions: int
    notes: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    total_in_store: int = 0


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt_for(ts: str | None, granularity: str) -> str | None:
    """Format an ISO timestamp for a repository's declared OAI granularity.
    OPUS (BASt) accepts only YYYY-MM-DD; ROSA-P/DSpace/DiVA accept full timestamps."""
    if not ts:
        return ts
    if granularity.startswith("YYYY-MM-DDThh"):
        return ts
    return ts[:10]


def make_client(source: Source | None = None) -> OAIClient:
    source = source or SOURCES["rosap"]
    return OAIClient(
        source.base_url,
        config.USER_AGENT,
        min_interval=config.MIN_REQUEST_INTERVAL,
        timeout=config.HTTP_TIMEOUT,
        raw_dir=config.RAW_DIR,
    )


def harvest(
    store: Store,
    client: OAIClient | None = None,
    *,
    source: Source | None = None,
    mode: str = "auto",
    from_ts: str | None = None,
    until_ts: str | None = None,
    max_pages: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> HarvestResult:
    say = progress or (lambda m: log.info(m))
    source = source or SOURCES["rosap"]
    own_client = client is None
    client = client or make_client(source)
    config.ensure_dirs()

    # ---- decide window -------------------------------------------------------------
    last_full = store.last_complete_run(source.key, "full")
    last_any = store.last_complete_run(source.key)
    if mode == "auto":
        mode = "incremental" if last_full else "full"
    if mode == "incremental":
        if from_ts is None:
            if not last_any:
                raise RuntimeError("no complete harvest to be incremental from; run a full harvest first")
            from_ts = _ts(_parse_ts(last_any["started_at"]) - OVERLAP)
    elif mode == "full":
        from_ts = from_ts  # usually None
    else:
        raise ValueError(f"unknown mode {mode!r}")
    until_ts = until_ts or utcnow()
    try:
        granularity = client.identify().get("granularity", "YYYY-MM-DDThh:mm:ssZ")
    except Exception as exc:  # noqa: BLE001 — Identify failing is not fatal; assume full timestamps
        log.warning("Identify failed for %s (%s); assuming second granularity", source.key, exc)
        granularity = "YYYY-MM-DDThh:mm:ssZ"
    if not granularity.startswith("YYYY-MM-DDThh"):
        # day granularity: widen the window by a day on both ends so nothing is missed
        if from_ts:
            from_ts = _ts(_parse_ts(from_ts) - timedelta(days=1))
        until_ts = _ts(_parse_ts(until_ts) + timedelta(days=1))
    q_from, q_until = _fmt_for(from_ts, granularity), _fmt_for(until_ts, granularity)

    run_id = store.start_run(source.key, mode, from_ts, until_ts)
    notes: list[str] = []
    seen = 0
    kept = 0
    skipped = 0
    pages = 0
    resumptions = 0
    min_datestamp: str | None = None
    ordering_ok = True
    token: str | None = None
    effective_until = q_until
    if not config.CONTACT_EMAIL:
        say("warning: DOT_LIT_CONTACT is not set; set it to your e-mail so the repository can reach you (good OAI-PMH citizenship)")
    say(f"run {run_id}: {source.key} {mode} harvest from={from_ts or '-'} until={until_ts} set={source.set_spec or '-'} ({config.MIN_REQUEST_INTERVAL}s pacing)")

    def finish(status: str) -> HarvestResult:
        fin = utcnow()
        if source.include is not None:
            notes.append(f"filter kept {kept} of {seen} records ({skipped} skipped as off-topic)")
        store.update_run(run_id, status=status, finished_at=fin, pages=pages, records_seen=seen,
                         resumptions=resumptions, min_datestamp=min_datestamp, notes=notes)
        if status == "complete":
            store.set_meta(f"last_harvest_finished_at:{source.key}", fin)
            if source.key == "rosap":
                store.set_meta("last_harvest_finished_at", fin)
                store.set_meta("last_harvest_run_id", str(run_id))
            if mode == "full":
                store.set_meta(f"last_full_harvest_finished_at:{source.key}", fin)
        run = store.get_run(run_id) or {}
        return HarvestResult(run_id, mode, status, seen, pages, resumptions, notes,
                             run.get("started_at", ""), fin, store.count())

    try:
        while True:
            label = f"run{run_id}-p{pages:05d}"
            try:
                page = client.list_records_page(
                    source.metadata_prefix, from_=q_from, until=effective_until,
                    set_spec=source.set_spec, token=token, raw_label=label,
                )
            except NoRecordsMatch:
                if pages == 0:
                    notes.append("noRecordsMatch: nothing changed in window")
                    say("no records in window — nothing to do")
                    return finish("complete")
                raise TruncatedList("noRecordsMatch after pages had been returned")
            except (BadResumptionToken, TransportError, TruncatedList) as exc:
                if resumptions >= MAX_RESUMPTIONS:
                    notes.append(f"gave up after {resumptions} resumptions: {exc}")
                    say(f"FAILED: {exc}")
                    return finish("failed")
                resumptions += 1
                if ordering_ok and min_datestamp:
                    effective_until = _fmt_for(min_datestamp, granularity)
                    notes.append(f"{type(exc).__name__} at page {pages}; resumed with until={effective_until}")
                    say(f"{type(exc).__name__}: resuming from datestamp boundary {effective_until} (resumption {resumptions})")
                else:
                    effective_until = q_until
                    notes.append(f"{type(exc).__name__} at page {pages}; restarted from top (ordering not monotone)")
                    say(f"{type(exc).__name__}: restarting list from the top (resumption {resumptions})")
                token = None
                continue

            # ---- cursor cross-check (only meaningful on a single uninterrupted list) ----
            # DiVA reports the cursor *after* the page; others report the page start.
            if page.cursor is not None and resumptions == 0 and page.cursor not in (seen, seen + len(page.records)):
                notes.append(f"cursor mismatch on page {pages}: server cursor={page.cursor}, local count={seen}")

            parsed = []
            page_max: str | None = None
            page_min: str | None = None
            for rec in page.records:
                d = parse_record(rec, source.key, source.collection)
                if not d:
                    continue
                seen += 1
                if not d.get("deleted") and not matches_filter(source, d):
                    skipped += 1
                    continue
                kept += 1
                parsed.append(d)
                ds = d["datestamp"]
                page_max = ds if page_max is None or ds > page_max else page_max
                page_min = ds if page_min is None or ds < page_min else page_min
            if page_max and min_datestamp and page_max > min_datestamp and ordering_ok:
                # A datestamp larger than everything we have already passed means the
                # stream is not sorted descending; disable boundary-based resumption.
                ordering_ok = False
                notes.append(f"datestamp ordering violated on page {pages} ({page_max} > {min_datestamp})")
            if page_min and (min_datestamp is None or page_min < min_datestamp):
                min_datestamp = page_min

            n = store.upsert_records(parsed)
            pages += 1
            token = page.token
            if pages % 10 == 0 or token is None:
                store.update_run(run_id, pages=pages, records_seen=seen, last_cursor=page.cursor,
                                 min_datestamp=min_datestamp, resumptions=resumptions, notes=notes)
                say(f"page {pages}: +{n} records (total seen {seen}, min datestamp {min_datestamp}, cursor {page.cursor})")

            if token is None:
                break
            if max_pages and pages >= max_pages:
                notes.append(f"stopped early at max_pages={max_pages} (partial by request)")
                say("stopped at max_pages; run marked failed so it is not mistaken for a full harvest")
                return finish("failed")

        # ---- post-checks -------------------------------------------------------------
        if mode == "full" and last_full and last_full.get("records_seen"):
            prev = int(last_full["records_seen"])
            if seen < prev * 0.95:
                notes.append(f"record count dropped from {prev} to {seen} (>5%): possible silent truncation")
        say(f"complete: {pages} pages, {seen} records seen, {resumptions} resumptions, store now {store.count()} records")
        return finish("complete")
    except KeyboardInterrupt:
        notes.append("interrupted by user")
        return finish("failed")
    except Exception as exc:  # noqa: BLE001 — record it; the run is failed, not crashed
        notes.append(f"unexpected error: {exc!r}")
        say(f"FAILED: {exc}")
        return finish("failed")
    finally:
        if own_client:
            client.close()


def harvest_fresh(store: Store, *, source: Source | None = None, progress: Callable[[str], None] | None = None) -> HarvestResult:
    """True rebuild: full-harvest ROSA-P into a temporary store, then atomically swap the
    `dot:` records into the live store.  Records that ROSA-P no longer serves disappear;
    imported sources (e.g. trid:) are untouched; a failed harvest changes nothing."""
    say = progress or (lambda m: log.info(m))
    source = source or SOURCES["rosap"]
    tmp_path = store.path.with_name(f"{store.path.stem}.rebuild-{source.key}.sqlite")
    for suffix in ("", "-wal", "-shm"):
        p = tmp_path.with_name(tmp_path.name + suffix)
        if p.exists():
            p.unlink()
    tmp = Store(tmp_path)
    try:
        res = harvest(tmp, source=source, mode="full", progress=say)
        if res.status != "complete":
            say("fresh rebuild aborted: harvest did not complete; live index unchanged")
            return res
        tmp.close()
        tmp = None
        other = Store(tmp_path)
        try:
            n = store.replace_source("dot" if source.key == "rosap" else source.key, other)
        finally:
            other.close()
        store.set_meta(f"last_harvest_finished_at:{source.key}", res.finished_at)
        store.set_meta(f"last_full_harvest_finished_at:{source.key}", res.finished_at)
        if source.key == "rosap":
            store.set_meta("last_harvest_finished_at", res.finished_at)
            store.set_meta("last_full_harvest_finished_at", res.finished_at)
        say(f"fresh rebuild swapped in {n} {source.key} records; store now {store.count()} records")
        res.total_in_store = store.count()
        return res
    finally:
        if tmp is not None:
            tmp.close()
        for suffix in ("", "-wal", "-shm"):
            p = tmp_path.with_name(tmp_path.name + suffix)
            if p.exists():
                p.unlink()


def reindex(store: Store, *, source: Source | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Re-parse the cached raw OAI pages (last complete full harvest and every complete run
    after it) and upsert them.  Lets the parser evolve without re-harvesting."""
    say = progress or (lambda m: log.info(m))
    source = source or SOURCES["rosap"]
    last_full = store.last_complete_run(source.key, "full")
    if not last_full:
        raise RuntimeError(f"no complete full harvest of {source.key} to reindex from")
    runs = [r for r in reversed(store.last_runs(10_000))
            if r["status"] == "complete" and r["id"] >= last_full["id"] and r["source"] == source.key]
    total = 0
    files_seen = 0
    for run in runs:
        files = sorted(config.RAW_DIR.glob(f"run{run['id']}-p*.xml.gz"))
        if len(files) != run["pages"]:
            say(f"warning: run {run['id']} has {len(files)} cached pages but recorded {run['pages']}")
        for f in files:
            root = ET.fromstring(gzip.decompress(f.read_bytes()))
            recs = [d for d in (parse_record(r, source.key, source.collection) for r in root.findall(f".//{{{OAI_NS}}}record"))
                    if d and (d.get("deleted") or matches_filter(source, d))]
            store.upsert_records(recs)
            total += len(recs)
            files_seen += 1
            if files_seen % 100 == 0:
                say(f"reindexed {files_seen} pages, {total} records")
    say(f"reindex complete: {files_seen} pages, {total} records re-parsed, store has {store.count()} records")
    return {"runs": [r["id"] for r in runs], "pages": files_seen, "records": total, "total_in_store": store.count()}


def status(store: Store) -> dict:
    total = store.count()
    last_runs = store.last_runs(5)
    last_complete = store.last_complete_run(SOURCE)
    last_full = store.last_complete_run(SOURCE, "full")
    years = store.year_distribution()
    known = {k: v for k, v in years.items() if k != "unknown"}
    per_source = {}
    for key, src in SOURCES.items():
        lc = store.last_complete_run(key)
        per_source[key] = {
            "name": src.name, "base_url": src.base_url, "records": store.source_distribution().get("dot" if key == "rosap" else key, 0),
            "last_complete_run": {k: lc[k] for k in ("id", "kind", "finished_at", "records_seen", "pages", "notes")} if lc else None,
        }
    return {
        "source": SOURCE,
        "sources": per_source,
        "oai_base_url": config.ROSAP_OAI_BASE,
        "data_dir": str(config.DATA_DIR),
        "total_records": total,
        "by_source": store.source_distribution(),
        "records_with_year": sum(known.values()),
        "records_without_year": years.get("unknown", 0),
        "year_source": store.year_source_distribution(),
        "year_range": [min(known), max(known)] if known else None,
        "last_harvest": {
            "run_id": last_complete["id"], "kind": last_complete["kind"],
            "started_at": last_complete["started_at"], "finished_at": last_complete["finished_at"],
            "pages": last_complete["pages"], "records_seen": last_complete["records_seen"],
            "resumptions": last_complete["resumptions"], "notes": last_complete["notes"],
        } if last_complete else None,
        "last_full_harvest": {
            "run_id": last_full["id"], "finished_at": last_full["finished_at"],
            "records_seen": last_full["records_seen"], "pages": last_full["pages"],
        } if last_full else None,
        "recent_runs": [
            {k: r[k] for k in ("id", "kind", "status", "started_at", "finished_at", "pages", "records_seen", "resumptions", "notes")}
            for r in last_runs
        ],
        "by_decade": store.decade_distribution(),
        "by_year": years,
    }
