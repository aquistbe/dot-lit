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

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .dc import parse_record
from .oai import BadResumptionToken, NoRecordsMatch, OAIClient, TransportError, TruncatedList
from .store import Store, utcnow

log = logging.getLogger(__name__)

SOURCE = "rosap"
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


def make_client() -> OAIClient:
    return OAIClient(
        config.ROSAP_OAI_BASE,
        config.USER_AGENT,
        min_interval=config.MIN_REQUEST_INTERVAL,
        timeout=config.HTTP_TIMEOUT,
        raw_dir=config.RAW_DIR,
    )


def harvest(
    store: Store,
    client: OAIClient | None = None,
    *,
    mode: str = "auto",
    from_ts: str | None = None,
    until_ts: str | None = None,
    max_pages: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> HarvestResult:
    say = progress or (lambda m: log.info(m))
    own_client = client is None
    client = client or make_client()
    config.ensure_dirs()

    # ---- decide window -------------------------------------------------------------
    last_full = store.last_complete_run(SOURCE, "full")
    last_any = store.last_complete_run(SOURCE)
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

    run_id = store.start_run(SOURCE, mode, from_ts, until_ts)
    notes: list[str] = []
    seen = 0
    pages = 0
    resumptions = 0
    min_datestamp: str | None = None
    ordering_ok = True
    token: str | None = None
    effective_until = until_ts
    if not config.CONTACT_EMAIL:
        say("warning: DOT_LIT_CONTACT is not set; set it to your e-mail so the repository can reach you (good OAI-PMH citizenship)")
    say(f"run {run_id}: {mode} harvest from={from_ts or '-'} until={until_ts} (page size 100, {config.MIN_REQUEST_INTERVAL}s pacing)")

    def finish(status: str) -> HarvestResult:
        fin = utcnow()
        store.update_run(run_id, status=status, finished_at=fin, pages=pages, records_seen=seen,
                         resumptions=resumptions, min_datestamp=min_datestamp, notes=notes)
        if status == "complete":
            store.set_meta("last_harvest_finished_at", fin)
            store.set_meta("last_harvest_run_id", str(run_id))
            if mode == "full":
                store.set_meta("last_full_harvest_finished_at", fin)
        run = store.get_run(run_id) or {}
        return HarvestResult(run_id, mode, status, seen, pages, resumptions, notes,
                             run.get("started_at", ""), fin, store.count())

    try:
        while True:
            label = f"run{run_id}-p{pages:05d}"
            try:
                page = client.list_records_page(
                    config.ROSAP_METADATA_PREFIX, from_=from_ts, until=effective_until,
                    token=token, raw_label=label,
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
                    effective_until = min_datestamp
                    notes.append(f"{type(exc).__name__} at page {pages}; resumed with until={effective_until}")
                    say(f"{type(exc).__name__}: resuming from datestamp boundary {effective_until} (resumption {resumptions})")
                else:
                    effective_until = until_ts
                    notes.append(f"{type(exc).__name__} at page {pages}; restarted from top (ordering not monotone)")
                    say(f"{type(exc).__name__}: restarting list from the top (resumption {resumptions})")
                token = None
                continue

            # ---- cursor cross-check (only meaningful on a single uninterrupted list) ----
            if page.cursor is not None and resumptions == 0 and page.cursor != seen:
                notes.append(f"cursor mismatch on page {pages}: server cursor={page.cursor}, local count={seen}")

            parsed = []
            page_max: str | None = None
            page_min: str | None = None
            for rec in page.records:
                d = parse_record(rec)
                if not d:
                    continue
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
            seen += len(parsed)
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
    except Exception as exc:  # noqa: BLE001 — record and re-raise so status is accurate
        notes.append(f"unexpected error: {exc!r}")
        finish("failed")
        raise
    finally:
        if own_client:
            client.close()


def status(store: Store) -> dict:
    total = store.count()
    last_runs = store.last_runs(5)
    last_complete = store.last_complete_run(SOURCE)
    last_full = store.last_complete_run(SOURCE, "full")
    years = store.year_distribution()
    known = {k: v for k, v in years.items() if k != "unknown"}
    return {
        "source": SOURCE,
        "oai_base_url": config.ROSAP_OAI_BASE,
        "data_dir": str(config.DATA_DIR),
        "total_records": total,
        "records_with_year": sum(known.values()),
        "records_without_year": years.get("unknown", 0),
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
