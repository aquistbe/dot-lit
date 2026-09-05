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
import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .dc import OAI_NS, parse_record
from .oai import BadResumptionToken, NoRecordsMatch, OAIClient, TransportError, TruncatedList
from .apis import API_SOURCES, ApiSource, _client as _api_client
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
        say("warning: TRANSPORT_LIT_CONTACT is not set; set it to your e-mail so the repository can reach you (good OAI-PMH citizenship)")
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
            # Cursor semantics differ: ROSA-P/OPUS = record offset of the page start, DiVA =
            # offset after the page, DSpace = page index.  Note the first inconsistency only;
            # completeness is established by the token-less last page, not by the cursor.
            if (page.cursor is not None and resumptions == 0 and pages > 0
                    and page.cursor not in (seen, seen + len(page.records), pages)
                    and not any(n.startswith("cursor mismatch") for n in notes)):
                notes.append(f"cursor mismatch on page {pages}: server cursor={page.cursor}, local count={seen} (informational)")

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
                    # An updated record may no longer pass the source filter.
                    parsed.append({"id": d["id"], "deleted": True})
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
                return finish("failed")
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


def harvest_api(store: Store, source: ApiSource, *, mode: str = "auto",
                progress: Callable[[str], None] | None = None) -> HarvestResult:
    """Harvest a query-API source.  Raw pages are cached like OAI pages."""
    import gzip as _gzip

    say = progress or (lambda m: log.info(m))
    config.ensure_dirs()
    last_any = store.last_complete_run(source.key)
    if mode == "auto":
        mode = "incremental" if last_any else "full"
    since = None
    if mode == "incremental":
        if not last_any:
            raise RuntimeError(f"no complete harvest of {source.key} to be incremental from")
        since = _ts(_parse_ts(last_any["started_at"]) - timedelta(days=2))
    run_id = store.start_run(source.key, mode, since, utcnow())
    notes: list[str] = []
    pages = seen = 0
    client, _ = _api_client(source.min_interval)
    say(f"run {run_id}: {source.key} {mode} harvest" + (f" since {since}" if since else ""))
    try:
        for label, raw in source.pages(client, since, say):
            (config.RAW_DIR / f"run{run_id}-{label}.{source.raw_ext}.gz").write_bytes(_gzip.compress(raw))
            recs = source.parse(raw)
            store.upsert_records(recs)
            pages += 1
            seen += len(recs)
            if pages % 10 == 0:
                store.update_run(run_id, pages=pages, records_seen=seen)
                say(f"page {pages}: +{len(recs)} (total {seen})")
        fin = utcnow()
        store.update_run(run_id, status="complete", finished_at=fin, pages=pages, records_seen=seen, notes=notes)
        store.set_meta(f"last_harvest_finished_at:{source.key}", fin)
        say(f"complete: {pages} pages, {seen} records, store now {store.count()}")
        return HarvestResult(run_id, mode, "complete", seen, pages, 0, notes, "", fin, store.count())
    except KeyboardInterrupt:
        notes.append("interrupted by user")
        store.update_run(run_id, status="failed", finished_at=utcnow(), pages=pages, records_seen=seen, notes=notes)
        return HarvestResult(run_id, mode, "failed", seen, pages, 0, notes, "", utcnow(), store.count())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"error: {exc!r}")
        say(f"FAILED: {exc}")
        store.update_run(run_id, status="failed", finished_at=utcnow(), pages=pages, records_seen=seen, notes=notes)
        return HarvestResult(run_id, mode, "failed", seen, pages, 0, notes, "", utcnow(), store.count())
    finally:
        client.close()


def reindex_api(store: Store, source: ApiSource, *, progress: Callable[[str], None] | None = None) -> dict:
    import gzip as _gzip

    say = progress or (lambda m: log.info(m))
    last_full = store.last_complete_run(source.key, "full")
    if not last_full:
        raise RuntimeError(f"no complete full harvest of {source.key} to reindex from")
    runs = [r for r in reversed(store.last_runs(10_000))
            if r["status"] == "complete" and r["id"] >= last_full["id"] and r["source"] == source.key]
    cached = _cached_pages(runs, source.raw_ext)
    kept: set[str] = set()
    total = files = 0
    for run in runs:
        for f in cached[run["id"]]:
            recs = source.parse(_gzip.decompress(f.read_bytes()))
            store.upsert_records(recs)
            kept.update(r["id"] for r in recs)
            total += len(recs)
            files += 1
    pruned = store.prune_source(source.key, kept) if files else 0
    say(f"reindex complete: {files} pages, {total} records re-parsed, {pruned} pruned, store has {store.count()} records")
    return {"runs": [r["id"] for r in runs], "pages": files, "records": total, "pruned": pruned, "total_in_store": store.count()}


def _fresh(store: Store, source: Source | ApiSource, say: Callable[[str], None]) -> HarvestResult:
    """Reserve a live run ID before staging, so raw cache names never collide."""
    import tempfile
    from pathlib import Path

    run_id = store.start_run(source.key, "full", None, utcnow())
    prefix = "dot" if source.key == "rosap" else source.key
    with tempfile.TemporaryDirectory(prefix=f"rebuild-{source.key}-", dir=store.path.parent) as work:
        tmp = Store(Path(work) / "index.sqlite")
        try:
            tmp.conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('harvest_runs', ?)", (run_id - 1,))
            tmp.conn.commit()
            if isinstance(source, ApiSource):
                res = harvest_api(tmp, source, mode="full", progress=say)
            else:
                res = harvest(tmp, source=source, mode="full", progress=say)
            staged = tmp.get_run(res.run_id)
            if res.status == "complete":
                old_count = store.conn.execute("SELECT COUNT(*) FROM records WHERE id LIKE ?", (prefix + ":%",)).fetchone()[0]
                new_count = tmp.count()
                if old_count and new_count < old_count * 0.95:
                    res.status = "failed"
                    res.notes.append(f"fresh rebuild refused: record count dropped from {old_count} to {new_count} (>5%); live index unchanged")
                else:
                    store.replace_source(prefix, tmp, import_runs=False)
            fields = {k: staged[k] for k in ("from_ts", "until_ts", "pages", "records_seen", "last_cursor", "min_datestamp", "resumptions")}
            store.update_run(run_id, **fields, status=res.status, finished_at=res.finished_at, notes=res.notes)
            if res.status == "complete":
                store.set_meta(f"last_harvest_finished_at:{source.key}", res.finished_at)
                store.set_meta(f"last_full_harvest_finished_at:{source.key}", res.finished_at)
                if source.key == "rosap":
                    store.set_meta("last_harvest_finished_at", res.finished_at)
                    store.set_meta("last_full_harvest_finished_at", res.finished_at)
            res.total_in_store = store.count()
            say(f"fresh rebuild {res.status}: {source.key}; store has {res.total_in_store} records")
            return res
        except BaseException:
            store.update_run(run_id, status="failed", finished_at=utcnow(), notes=["fresh rebuild interrupted or failed during staging/swap"])
            raise
        finally:
            tmp.close()


def harvest_fresh_api(store: Store, source: ApiSource, *, progress: Callable[[str], None] | None = None) -> HarvestResult:
    return _fresh(store, source, progress or (lambda m: log.info(m)))


def harvest_fresh(store: Store, *, source: Source | None = None, progress: Callable[[str], None] | None = None) -> HarvestResult:
    return _fresh(store, source or SOURCES["rosap"], progress or (lambda m: log.info(m)))


def _cached_pages(runs: list[dict], ext: str) -> dict[int, list]:
    """Preflight the entire cache before any upserts or pruning."""
    out = {}
    for run in runs:
        files = sorted(config.RAW_DIR.glob(f"run{run['id']}-p*.{ext}.gz"))
        expected = [config.RAW_DIR / f"run{run['id']}-p{i:05d}.{ext}.gz" for i in range(run["pages"])]
        if ext == "xml" and run["pages"] == 0 and len(files) == 1:
            # OAI saves the response before interpreting noRecordsMatch. A valid
            # no-change attempt can therefore have one envelope and zero data pages.
            root = ET.fromstring(gzip.decompress(files[0].read_bytes()))
            error = root.find(f"{{{OAI_NS}}}error")
            if (root.tag == f"{{{OAI_NS}}}OAI-PMH" and not root.findall(f".//{{{OAI_NS}}}record")
                    and root.find(f"{{{OAI_NS}}}ListRecords") is None
                    and (error is None or error.get("code") == "noRecordsMatch")):
                files = []
        if files != expected:
            raise RuntimeError(f"cannot reindex run {run['id']}: expected {run['pages']} cached pages, found {len(files)} (missing or unexpected pages); index unchanged")
        # Reject unreadable/corrupt pages before records can be pruned.
        for path in files:
            raw = gzip.decompress(path.read_bytes())
            if ext == "xml":
                ET.fromstring(raw)
            else:
                json.loads(raw)
        out[run["id"]] = files
    return out


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
    cached = _cached_pages(runs, "xml")
    total = 0
    files_seen = 0
    kept_ids: set[str] = set()
    for run in runs:
        files = cached[run["id"]]
        for f in files:
            root = ET.fromstring(gzip.decompress(f.read_bytes()))
            recs = [d for d in (parse_record(r, source.key, source.collection) for r in root.findall(f".//{{{OAI_NS}}}record"))
                    if d and (d.get("deleted") or matches_filter(source, d))]
            store.upsert_records(recs)
            kept_ids.update(r["id"] for r in recs if not r.get("deleted"))
            total += len(recs)
            files_seen += 1
            if files_seen % 100 == 0:
                say(f"reindexed {files_seen} pages, {total} records")
    # Prune records of this source that the (possibly tightened) parser/filter no longer keeps.
    prefix = "dot" if source.key == "rosap" else source.key
    pruned = store.prune_source(prefix, kept_ids) if files_seen else 0
    say(f"reindex complete: {files_seen} pages, {total} records re-parsed, {pruned} pruned, store has {store.count()} records")
    return {"runs": [r["id"] for r in runs], "pages": files_seen, "records": total, "pruned": pruned,
            "total_in_store": store.count()}


def status(store: Store) -> dict:
    total = store.count()
    last_runs = store.last_runs(5)
    last_complete = store.last_complete_run(SOURCE)
    last_full = store.last_complete_run(SOURCE, "full")
    years = store.year_distribution()
    known = {k: v for k, v in years.items() if k != "unknown"}
    per_source = {}
    dist = store.source_distribution()
    for key, src in list(SOURCES.items()) + list(API_SOURCES.items()):
        lc = store.last_complete_run(key)
        latest_row = store.conn.execute("SELECT * FROM harvest_runs WHERE source=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        latest = store._run_row(latest_row) if latest_row else None
        per_source[key] = {
            "name": src.name, "base_url": getattr(src, "base_url", "api"),
            "records": dist.get("dot" if key == "rosap" else key, 0),
            "latest_run": {k: latest[k] for k in ("id", "kind", "status", "started_at", "finished_at", "records_seen", "pages", "notes")} if latest else None,
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
