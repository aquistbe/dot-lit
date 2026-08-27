"""SQLite + FTS5 store for harvested records, harvest bookkeeping and full-text cache."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS records (
    id              TEXT PRIMARY KEY,
    oai_identifier  TEXT NOT NULL,
    datestamp       TEXT,
    title           TEXT,
    alt_title       TEXT,
    authors         TEXT,   -- JSON list
    corporate_authors TEXT, -- JSON list
    contributors    TEXT,   -- JSON list
    year            INTEGER,
    year_source     TEXT,   -- date | title | description | NULL
    date_raw        TEXT,
    abstract        TEXT,
    table_of_contents TEXT,
    notes           TEXT,
    publisher       TEXT,
    doc_type        TEXT,
    format          TEXT,
    language        TEXT,
    subjects        TEXT,   -- JSON list
    collections     TEXT,   -- JSON list
    doi             TEXT,
    report_numbers  TEXT,   -- JSON list
    other_urls      TEXT,   -- JSON list
    spatial         TEXT,
    source          TEXT,
    rights          TEXT,
    landing_url     TEXT,
    raw             TEXT,   -- JSON: every DC field as harvested
    harvested_at    TEXT,
    first_seen_at   TEXT    -- set on first insert only; basis for whats_new / digests
);
CREATE INDEX IF NOT EXISTS idx_records_year ON records(year);
CREATE INDEX IF NOT EXISTS idx_records_datestamp ON records(datestamp);

CREATE TABLE IF NOT EXISTS record_collections (
    record_id  TEXT NOT NULL,
    collection TEXT NOT NULL,
    PRIMARY KEY (record_id, collection)
);
CREATE INDEX IF NOT EXISTS idx_rc_collection ON record_collections(collection);

-- External-content FTS5 index over the searchable columns of `records`.
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    title, abstract, subjects, authors, report_numbers, collections, publisher,
    corporate_authors, alt_title,
    content='records', content_rowid='rowid',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid, title, abstract, subjects, authors, report_numbers,
                          collections, publisher, corporate_authors, alt_title)
  VALUES (new.rowid, new.title, new.abstract, new.subjects, new.authors, new.report_numbers,
          new.collections, new.publisher, new.corporate_authors, new.alt_title);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, abstract, subjects, authors, report_numbers,
                          collections, publisher, corporate_authors, alt_title)
  VALUES ('delete', old.rowid, old.title, old.abstract, old.subjects, old.authors, old.report_numbers,
          old.collections, old.publisher, old.corporate_authors, old.alt_title);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, abstract, subjects, authors, report_numbers,
                          collections, publisher, corporate_authors, alt_title)
  VALUES ('delete', old.rowid, old.title, old.abstract, old.subjects, old.authors, old.report_numbers,
          old.collections, old.publisher, old.corporate_authors, old.alt_title);
  INSERT INTO records_fts(rowid, title, abstract, subjects, authors, report_numbers,
                          collections, publisher, corporate_authors, alt_title)
  VALUES (new.rowid, new.title, new.abstract, new.subjects, new.authors, new.report_numbers,
          new.collections, new.publisher, new.corporate_authors, new.alt_title);
END;

CREATE TABLE IF NOT EXISTS harvest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- full | incremental
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,           -- running | complete | failed
    from_ts       TEXT,
    until_ts      TEXT,
    pages         INTEGER DEFAULT 0,
    records_seen  INTEGER DEFAULT 0,
    last_cursor   INTEGER,
    min_datestamp TEXT,
    resumptions   INTEGER DEFAULT 0,
    notes         TEXT                     -- JSON list of strings
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS fulltext (
    record_id  TEXT PRIMARY KEY,
    pdf_url    TEXT,
    status     TEXT,      -- ok | no_pdf | too_large | no_text | error
    n_pages    INTEGER,
    n_chars    INTEGER,
    text       TEXT,
    error      TEXT,
    fetched_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS fulltext_fts USING fts5(
    text, content='fulltext', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS fulltext_ai AFTER INSERT ON fulltext BEGIN
  INSERT INTO fulltext_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS fulltext_ad AFTER DELETE ON fulltext BEGIN
  INSERT INTO fulltext_fts(fulltext_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS fulltext_au AFTER UPDATE ON fulltext BEGIN
  INSERT INTO fulltext_fts(fulltext_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO fulltext_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""

RECORD_COLUMNS = [
    "id", "oai_identifier", "datestamp", "title", "alt_title", "authors", "corporate_authors",
    "contributors", "year", "year_source", "date_raw", "abstract", "table_of_contents", "notes", "publisher", "doc_type",
    "format", "language", "subjects", "collections", "doi", "report_numbers", "other_urls",
    "spatial", "source", "rights", "landing_url", "raw", "harvested_at", "first_seen_at",
]
JSON_COLUMNS = {"authors", "corporate_authors", "contributors", "subjects", "collections",
                "report_numbers", "other_urls", "raw"}

# bm25 weights, in FTS column order:
# title, abstract, subjects, authors, report_numbers, collections, publisher, corporate_authors, alt_title
BM25_WEIGHTS = "10.0, 1.0, 3.0, 4.0, 6.0, 0.5, 0.5, 1.0, 5.0"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(records)")}
        for col, decl in (("year_source", "TEXT"), ("notes", "TEXT"), ("first_seen_at", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE records ADD COLUMN {col} {decl}")
        self.conn.execute("UPDATE records SET first_seen_at = harvested_at WHERE first_seen_at IS NULL")
        # populate the full-text FTS index for rows that predate it
        if self.conn.execute("SELECT (SELECT COUNT(*) FROM fulltext) > (SELECT COUNT(*) FROM fulltext_fts)").fetchone()[0]:
            self.conn.execute("INSERT INTO fulltext_fts(fulltext_fts) VALUES ('rebuild')")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass  # another connection holds the database; the WAL stays valid and will be replayed
        self.conn.close()

    def doctor(self) -> dict[str, Any]:
        """Consistency report: SQLite integrity, FTS indexes, stale runs, impossible timestamps, WAL."""
        c = self.conn
        rep: dict[str, Any] = {"integrity": c.execute("PRAGMA integrity_check").fetchone()[0]}
        for fts in ("records_fts", "fulltext_fts"):
            try:
                c.execute(f"INSERT INTO {fts}({fts}) VALUES('integrity-check')")
                rep[fts] = "ok"
            except sqlite3.DatabaseError as exc:
                rep[fts] = f"inconsistent: {exc}"
        rep["runs_still_running"] = [dict(r) for r in c.execute(
            "SELECT id, source, started_at FROM harvest_runs WHERE status='running' AND started_at < datetime('now', '-6 hours')")]
        rep["runs_with_impossible_times"] = [dict(r) for r in c.execute(
            "SELECT id, source, started_at, finished_at FROM harvest_runs WHERE finished_at IS NOT NULL AND finished_at < started_at")]
        wal = self.path.with_name(self.path.name + "-wal")
        rep["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        rep["records"] = self.count()
        return rep

    def repair(self) -> list[str]:
        """Fix what doctor() can fix safely: rebuild inconsistent FTS indexes, fail runs stuck
        in 'running', clear impossible finish times, checkpoint the WAL."""
        done: list[str] = []
        rep = self.doctor()
        for fts in ("records_fts", "fulltext_fts"):
            if rep[fts] != "ok":
                self.conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
                done.append(f"rebuilt {fts}")
        for r in rep["runs_still_running"]:
            self.conn.execute("UPDATE harvest_runs SET status='failed', finished_at=?, notes=json_insert(COALESCE(notes,'[]'), '$[#]', 'marked failed by doctor: still running after 6 h') WHERE id=?",
                              (utcnow(), r["id"]))
            done.append(f"run {r['id']} marked failed")
        for r in rep["runs_with_impossible_times"]:
            self.conn.execute("UPDATE harvest_runs SET finished_at=NULL WHERE id=?", (r["id"],))
            done.append(f"run {r['id']} finish time cleared")
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            done.append("wal checkpointed")
        except sqlite3.OperationalError:
            done.append("wal checkpoint skipped (database busy)")
        return done

    # -- meta ---------------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- records ------------------------------------------------------------------------
    def upsert_records(self, recs: Iterable[dict[str, Any]]) -> int:
        now = utcnow()
        n = 0
        rows = []
        coll_rows = []
        ids = []
        for r in recs:
            if r.get("deleted"):
                ids.append(r["id"])
                self.conn.execute("DELETE FROM records WHERE id = ?", (r["id"],))
                continue
            row = dict(r)
            row["harvested_at"] = now
            row["first_seen_at"] = now
            for c in JSON_COLUMNS:
                row[c] = json.dumps(row.get(c) or ([] if c != "raw" else {}), ensure_ascii=False)
            rows.append(tuple(row.get(c) for c in RECORD_COLUMNS))
            ids.append(r["id"])
            for coll in r.get("collections") or []:
                coll_rows.append((r["id"], coll))
            n += 1
        cols = ", ".join(RECORD_COLUMNS)
        placeholders = ", ".join("?" for _ in RECORD_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in RECORD_COLUMNS if c not in ("id", "first_seen_at"))
        with self.conn:
            self.conn.executemany(
                f"INSERT INTO records({cols}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
                rows,
            )
            self.conn.executemany("DELETE FROM record_collections WHERE record_id = ?", [(i,) for i in ids])
            self.conn.executemany(
                "INSERT OR IGNORE INTO record_collections(record_id, collection) VALUES (?, ?)", coll_rows
            )
        return n

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        rid = normalize_id(record_id)
        row = self.conn.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for c in JSON_COLUMNS:
            if c in d and isinstance(d[c], str):
                try:
                    d[c] = json.loads(d[c])
                except json.JSONDecodeError:
                    pass
        return d

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # -- search -------------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        year_min: int | None = None,
        year_max: int | None = None,
        collection: str | None = None,
        doc_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
        sources: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        terms = tokenize_query(query)
        if not terms:
            return []
        filters, params = self._filters(year_min, year_max, collection, doc_type, sources)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        if _CJK.search(query):
            # FTS5's unicode61 tokenizer does not segment Chinese/Japanese/Korean, so a
            # compound like 交通事故 only matches when it stands alone.  Fall back to substring
            # matching over title/abstract/subjects for CJK queries (index scan; fine at 1e5 rows).
            words = [w for w in re.split(r"\s+", query.strip()) if w]
            like = " AND ".join("(r.title LIKE ? OR r.abstract LIKE ? OR r.subjects LIKE ?)" for _ in words)
            lp = [f"%{w}%" for w in words for _ in range(3)]
            sql = f"SELECT r.*, '' AS snippet, 0.0 AS score FROM records r WHERE {like} {filters} ORDER BY r.year DESC LIMIT ? OFFSET ?"
            for row in self.conn.execute(sql, [*lp, *params, limit, offset]):
                d = self._row_to_record(row)
                d["match_mode"] = "cjk_substring"
                d["snippet"] = (d.get("abstract") or "")[:300]
                results.append(d)
            return results

        def run(match: str, mode: str, remaining: int) -> None:
            sql = f"""
                SELECT r.*, 
                       snippet(records_fts, 1, '', '', ' … ', 40) AS snippet,
                       bm25(records_fts, {BM25_WEIGHTS}) AS score
                FROM records_fts
                JOIN records r ON r.rowid = records_fts.rowid
                WHERE records_fts MATCH ? {filters}
                ORDER BY score
                LIMIT ?
            """
            for row in self.conn.execute(sql, [match, *params, remaining + len(seen) + offset]):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                if len(seen) <= offset:
                    continue
                d = self._row_to_record(row)
                d["match_mode"] = mode
                results.append(d)
                if len(results) >= limit:
                    break

        run(" AND ".join(terms), "all_terms", limit)
        if len(results) < limit and len(terms) > 1:
            run(" OR ".join(terms), "any_terms", limit - len(results))
        return results

    @staticmethod
    def _filters(year_min, year_max, collection, doc_type, sources=None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if sources:
            prefixes = ["dot" if s_ in ("rosap", "dot") else s_ for s_ in sources]
            clauses.append("AND (" + " OR ".join("r.id LIKE ?" for _ in prefixes) + ")")
            params.extend(f"{pfx}:%" for pfx in prefixes)
        if year_min is not None:
            clauses.append("AND r.year >= ?")
            params.append(int(year_min))
        if year_max is not None:
            clauses.append("AND r.year <= ?")
            params.append(int(year_max))
        if collection:
            clauses.append(
                "AND r.id IN (SELECT record_id FROM record_collections WHERE collection LIKE ?)"
            )
            params.append(f"%{collection}%")
        if doc_type:
            clauses.append("AND r.doc_type LIKE ?")
            params.append(f"%{doc_type}%")
        return " ".join(clauses), params

    def filter_ids(self, ids: list[str], *, year_min=None, year_max=None, collection=None, doc_type=None,
                   sources=None) -> set[str]:
        """Subset of ids that pass the same filters search() applies (and still exist)."""
        if not ids:
            return set()
        filters, params = self._filters(year_min, year_max, collection, doc_type, sources)
        out: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i: i + 500]
            q = f"SELECT r.id FROM records r WHERE r.id IN ({','.join('?' for _ in chunk)}) {filters}"
            out.update(r[0] for r in self.conn.execute(q, [*chunk, *params]))
        return out

    def lookup(self, identifier: str) -> list[dict[str, Any]]:
        """Find records by DOI, PMID, report number, TRID/ROSA-P id or landing URL."""
        ident = identifier.strip()
        m = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", ident)
        rows: list = []
        if m:
            rows = self.conn.execute("SELECT * FROM records WHERE lower(doi) = lower(?)", (m.group(1).rstrip(".,;"),)).fetchall()
        if not rows and re.fullmatch(r"\d{5,9}", ident):
            rows = self.conn.execute("SELECT * FROM records WHERE id IN (?, ?)", (f"pubmed:{ident}", f"dot:{ident}")).fetchall()
        if not rows:
            rec = self.get_record(ident)
            if rec:
                return [rec]
        if not rows:
            rows = self.conn.execute("SELECT * FROM records WHERE report_numbers LIKE ? LIMIT 20", (f"%{ident}%",)).fetchall()
        out = [self._row_to_record(r) for r in rows]
        if not out and len(ident) >= 5:
            # report numbers often live only in the abstract/title of legacy records
            for h in self.search(f'"{ident}"', limit=10):
                if h.get("match_mode") == "all_terms":
                    h["lookup_match"] = "text"
                    out.append(h)
        return out

    def similar(self, record_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rec = self.get_record(record_id)
        if not rec:
            return []
        words = re.findall(r"[A-Za-zÀ-ÿ]{4,}", " ".join([rec.get("title") or "", " ".join(rec.get("subjects") or [])]))
        stop = {"with", "from", "that", "this", "study", "report", "analysis", "final", "evaluation", "using", "their",
                "united", "states", "transportation", "research", "based", "toward", "towards"}
        terms = []
        for w in words:
            lw = w.lower()
            if lw not in stop and lw not in terms:
                terms.append(lw)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms[:12])
        rows = self.conn.execute(f"""
            SELECT r.*, bm25(records_fts, {BM25_WEIGHTS}) AS score FROM records_fts
            JOIN records r ON r.rowid = records_fts.rowid
            WHERE records_fts MATCH ? AND r.id != ? ORDER BY score LIMIT ?""", (match, rec["id"], limit)).fetchall()
        return [self._row_to_record(r) for r in rows]

    def whats_new(self, days: int = 7, *, sources: list[str] | None = None, limit: int = 200) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters, params = self._filters(None, None, None, None, sources)
        rows = self.conn.execute(f"SELECT * FROM records r WHERE first_seen_at >= ? {filters} ORDER BY first_seen_at DESC, year DESC LIMIT ?",
                                 [cutoff, *params, limit]).fetchall()
        counts = self.conn.execute(f"SELECT substr(id,1,instr(id,':')-1) AS src, COUNT(*) AS n FROM records r WHERE first_seen_at >= ? {filters} GROUP BY src ORDER BY n DESC",
                                   [cutoff, *params]).fetchall()
        return {"since": cutoff, "counts_by_source": {r["src"]: r["n"] for r in counts},
                "records": [self._row_to_record(r) for r in rows]}

    def search_fulltext(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        terms = tokenize_query(query)
        if not terms:
            return []
        rows = self.conn.execute("""
            SELECT f.record_id, r.title, r.year, r.landing_url, f.pdf_url, f.n_pages,
                   snippet(fulltext_fts, 0, '[', ']', ' … ', 30) AS snippet, bm25(fulltext_fts) AS score
            FROM fulltext_fts JOIN fulltext f ON f.rowid = fulltext_fts.rowid
            LEFT JOIN records r ON r.id = f.record_id
            WHERE fulltext_fts MATCH ? ORDER BY score LIMIT ?""", (" AND ".join(terms), limit)).fetchall()
        return [dict(r) for r in rows]

    def fulltext_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM fulltext WHERE status='ok'").fetchone()[0]

    # -- stats --------------------------------------------------------------------------
    def collections(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT collection, COUNT(*) AS n FROM record_collections GROUP BY collection ORDER BY n DESC"
        ).fetchall()
        return [{"collection": r["collection"], "records": r["n"]} for r in rows]

    def doc_types(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT doc_type, COUNT(*) AS n FROM records GROUP BY doc_type ORDER BY n DESC"
        ).fetchall()
        return [{"doc_type": r["doc_type"] or "(none)", "records": r["n"]} for r in rows]

    def year_distribution(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT COALESCE(CAST(year AS TEXT), 'unknown') AS y, COUNT(*) AS n FROM records GROUP BY y ORDER BY y"
        ).fetchall()
        return {r["y"]: r["n"] for r in rows}

    def source_distribution(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT substr(id, 1, instr(id, ':') - 1) AS src, COUNT(*) AS n FROM records GROUP BY src ORDER BY n DESC"
        ).fetchall()
        return {r["src"]: r["n"] for r in rows}

    def replace_source(self, prefix: str, other: "Store") -> int:
        """Atomically replace every record with id prefix `prefix:` by the rows in `other`
        (a freshly harvested store).  Used by `harvest --fresh` for a true rebuild that
        drops records no longer served, without ever leaving the index empty."""
        like = f"{prefix}:%"
        self.conn.execute("ATTACH DATABASE ? AS fresh", (str(other.path),))
        try:
            with self.conn:
                self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS old_seen(id TEXT PRIMARY KEY, first_seen_at TEXT)")
                self.conn.execute("DELETE FROM old_seen")
                self.conn.execute("INSERT INTO old_seen SELECT id, first_seen_at FROM records WHERE id LIKE ?", (like,))
                self.conn.execute("DELETE FROM record_collections WHERE record_id LIKE ?", (like,))
                self.conn.execute("DELETE FROM records WHERE id LIKE ?", (like,))
                cols = ", ".join(RECORD_COLUMNS)
                self.conn.execute(f"INSERT INTO records({cols}) SELECT {cols} FROM fresh.records WHERE id LIKE ?", (like,))
                self.conn.execute("UPDATE records SET first_seen_at = (SELECT first_seen_at FROM old_seen WHERE old_seen.id = records.id) "
                                  "WHERE id LIKE ? AND id IN (SELECT id FROM old_seen)", (like,))
                self.conn.execute("INSERT INTO record_collections SELECT * FROM fresh.record_collections WHERE record_id LIKE ?", (like,))
                self.conn.execute("INSERT OR IGNORE INTO harvest_runs(source, kind, started_at, finished_at, status, from_ts, until_ts, pages, records_seen, last_cursor, min_datestamp, resumptions, notes) "
                                  "SELECT source, kind, started_at, finished_at, status, from_ts, until_ts, pages, records_seen, last_cursor, min_datestamp, resumptions, notes FROM fresh.harvest_runs WHERE status='complete'")
                n = self.conn.execute("SELECT COUNT(*) FROM records WHERE id LIKE ?", (like,)).fetchone()[0]
        finally:
            self.conn.execute("DETACH DATABASE fresh")
        return n

    def prune_source(self, prefix: str, keep_ids: set[str]) -> int:
        """Delete records with id prefix `prefix:` whose id is not in keep_ids."""
        like = f"{prefix}:%"
        existing = [r[0] for r in self.conn.execute("SELECT id FROM records WHERE id LIKE ?", (like,))]
        doomed = [(i,) for i in existing if i not in keep_ids]
        with self.conn:
            self.conn.executemany("DELETE FROM record_collections WHERE record_id = ?", doomed)
            self.conn.executemany("DELETE FROM records WHERE id = ?", doomed)
        return len(doomed)

    def year_source_distribution(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT COALESCE(year_source, 'none') AS s, COUNT(*) AS n FROM records GROUP BY s ORDER BY n DESC"
        ).fetchall()
        return {r["s"]: r["n"] for r in rows}

    def decade_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for y, n in self.year_distribution().items():
            key = f"{y[:3]}0s" if y != "unknown" else "unknown"
            out[key] = out.get(key, 0) + n
        return out

    # -- harvest runs -------------------------------------------------------------------
    def start_run(self, source: str, kind: str, from_ts: str | None, until_ts: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO harvest_runs(source, kind, started_at, status, from_ts, until_ts, notes) "
            "VALUES (?, ?, ?, 'running', ?, ?, '[]')",
            (source, kind, utcnow(), from_ts, until_ts),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if "notes" in fields and not isinstance(fields["notes"], str):
            fields["notes"] = json.dumps(fields["notes"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE harvest_runs SET {sets} WHERE id = ?", [*fields.values(), run_id])
        self.conn.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM harvest_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def last_runs(self, n: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [self._run_row(r) for r in rows]

    def last_complete_run(self, source: str, kind: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM harvest_runs WHERE source = ? AND status = 'complete'"
        params: list[Any] = [source]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return self._run_row(row) if row else None

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["notes"] = json.loads(d.get("notes") or "[]")
        except json.JSONDecodeError:
            d["notes"] = [d.get("notes")]
        return d

    # -- fulltext cache -----------------------------------------------------------------
    def get_fulltext(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM fulltext WHERE record_id = ?", (normalize_id(record_id),)).fetchone()
        return dict(row) if row else None

    def put_fulltext(self, record_id: str, **fields: Any) -> None:
        fields.setdefault("fetched_at", utcnow())
        cols = ["record_id", *fields.keys()]
        vals = [normalize_id(record_id), *fields.values()]
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO fulltext({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                f"ON CONFLICT(record_id) DO UPDATE SET {updates}",
                vals,
            )


# --- helpers -------------------------------------------------------------------------------

_ID_NUM = re.compile(r"^\d+$")


def normalize_id(record_id: str) -> str:
    """Accept 'dot:93144', '93144', 'oai:dot.stacks:dot:93144' or a landing URL."""
    s = str(record_id).strip()
    m = re.search(r"/view/dot/(\d+)", s)
    if m:
        return f"dot:{m.group(1)}"
    if s.startswith("oai:"):
        return ":".join(s.split(":")[-2:])
    if _ID_NUM.match(s):
        return f"dot:{s}"
    return s


_PHRASE_RE = re.compile(r'"([^"]+)"|(\S+)')
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def tokenize_query(q: str) -> list[str]:
    """Build safe FTS5 terms: quoted phrases stay phrases, bare words are quoted so that
    FTS5 operators/punctuation in user input can't cause syntax errors. A trailing '*'
    on a bare word is kept as a prefix query."""
    terms: list[str] = []
    for phrase, word in _PHRASE_RE.findall(q or ""):
        if phrase:
            p = phrase.replace('"', "").strip()
            if p:
                terms.append(f'"{p}"')
        elif word:
            w = word.strip()
            if w.upper() in {"AND", "OR", "NOT"}:
                continue
            prefix = w.endswith("*")
            w = w.rstrip("*").replace('"', "")
            if not re.search(r"\w", w):
                continue
            terms.append(f'"{w}"' + ("*" if prefix else ""))
    return terms
