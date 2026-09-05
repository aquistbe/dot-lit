"""Build and install index snapshots (SQLite + active vectors) so a new user can start
searching in minutes instead of harvesting nine sources for an hour."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx

from . import config
from .store import Store, utcnow


# Sources whose terms do not clearly allow redistribution of harvested metadata are left out
# of snapshots unless the builder overrides it; users harvest those themselves.
DEFAULT_EXCLUDE = ["cinii", "trid"]


def build(store: Store, out: Path, *, include_vectors: bool = True, exclude_sources: list[str] | None = None,
          progress: Callable[[str], None] | None = None) -> dict:
    say = progress or print
    exclude_sources = DEFAULT_EXCLUDE if exclude_sources is None else exclude_sources
    out = Path(out)
    work = out.with_suffix(".tmpdir")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    db_copy = work / "transport-lit.sqlite"
    say("copying database (online backup API)…")
    dst = sqlite3.connect(db_copy)
    store.conn.backup(dst)
    dst.execute("PRAGMA journal_mode = DELETE")
    dst.commit()
    dst.close()
    if exclude_sources:
        c = sqlite3.connect(db_copy)
        # external-content FTS tables must be consistent before rows are deleted through them
        c.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
        c.execute("INSERT INTO fulltext_fts(fulltext_fts) VALUES ('rebuild')")
        for src in exclude_sources:
            pfx = "dot" if src == "rosap" else src
            c.execute("DELETE FROM record_collections WHERE record_id LIKE ?", (f"{pfx}:%",))
            c.execute("DELETE FROM records WHERE id LIKE ?", (f"{pfx}:%",))
            c.execute("DELETE FROM harvest_runs WHERE source = ?", (src,))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='works'").fetchone():
                c.execute("DELETE FROM citations WHERE citing IN (SELECT openalex_id FROM works WHERE record_id LIKE ?) OR cited IN (SELECT openalex_id FROM works WHERE record_id LIKE ?)", (f"{pfx}:%", f"{pfx}:%"))
                c.execute("DELETE FROM works WHERE record_id LIKE ?", (f"{pfx}:%",))
                c.execute("DELETE FROM unresolved WHERE record_id LIKE ?", (f"{pfx}:%",))
        c.execute("DELETE FROM fulltext")
        c.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
        c.execute("INSERT INTO fulltext_fts(fulltext_fts) VALUES ('rebuild')")
        c.commit()
        c.execute("VACUUM")
        c.close()
        say(f"removed sources {exclude_sources} from the copy")
    manifest = {"built_at": utcnow(), "records": sqlite3.connect(db_copy).execute("SELECT COUNT(*) FROM records").fetchone()[0],
                "excluded_sources": exclude_sources or [], "vectors": None}
    slug = store.get_meta("embeddings.active")
    if include_vectors and slug and (config.DATA_DIR / "vectors" / slug / "meta.json").exists():
        say(f"including vectors {slug}…")
        from .embeddings import VectorIndex
        import numpy as np
        idx = VectorIndex(config.DATA_DIR / "vectors" / slug)
        with sqlite3.connect(db_copy) as c:
            kept = {r[0] for r in c.execute("SELECT id FROM records")}
        positions = [i for i, rid in enumerate(idx.ids) if rid in kept]
        if positions:
            target = VectorIndex(work / "vectors" / slug)
            target.add([idx.ids[i] for i in positions], np.asarray(idx.vecs)[positions], idx.meta)
            manifest["vectors"] = slug
    with sqlite3.connect(db_copy) as c:
        if not manifest["vectors"]:
            c.execute("DELETE FROM meta WHERE key='embeddings.active'")
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
    say(f"writing {out}…")
    with tarfile.open(out, "w:gz", compresslevel=6) as tar:
        for p in sorted(work.rglob("*")):
            tar.add(p, arcname=str(p.relative_to(work)), recursive=False)
    shutil.rmtree(work)
    manifest["size_bytes"] = out.stat().st_size
    return manifest


def install(src: str, *, force: bool = False, progress: Callable[[str], None] | None = None) -> dict:
    say = progress or print
    config.ensure_dirs()
    if config.DB_PATH.exists() and not force:
        raise RuntimeError(f"{config.DB_PATH} already exists; pass --force to replace it (harvest runs and imports in it are lost)")
    local = Path(src)
    if src.startswith(("http://", "https://")):
        local = config.DATA_DIR / "snapshot.tar.gz"
        say(f"downloading {src} → {local}")
        with httpx.stream("GET", src, follow_redirects=True, timeout=None,
                          headers={"User-Agent": config.USER_AGENT}) as r, local.open("wb") as fh:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got = 0
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
                got += len(chunk)
                if total and got % (200 << 20) < (1 << 20):
                    say(f"  {got / 1e9:.2f} / {total / 1e9:.2f} GB")
    say("validating snapshot before installation…")
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=config.DATA_DIR) as staging:
        stage = Path(staging)
        with tarfile.open(local, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                    raise RuntimeError(f"unsafe path or member in archive: {m.name}")
            tar.extractall(stage, filter="data")
        manifest = json.loads((stage / "manifest.json").read_text())
        staged_db = stage / "transport-lit.sqlite"
        if not staged_db.is_file():
            raise RuntimeError("snapshot has no transport-lit.sqlite")
        with sqlite3.connect(staged_db.as_uri() + "?mode=ro", uri=True) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("snapshot database failed integrity_check")
            n = check.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if n != manifest.get("records"):
                raise RuntimeError("snapshot record count does not match its manifest")
        slug = manifest.get("vectors")
        if slug:
            if not isinstance(slug, str) or Path(slug).name != slug or slug in {".", ".."}:
                raise RuntimeError("invalid vector index name in snapshot")
            from .embeddings import VectorIndex
            idx = VectorIndex(stage / "vectors" / slug)
            if idx.vecs is None or len(idx.ids) != idx.vecs.shape[0]:
                raise RuntimeError("snapshot vector index is missing or inconsistent")
            # A new generation avoids changing files mapped by an active MCP process.
            import uuid
            new_slug = slug + "-snapshot-" + uuid.uuid4().hex[:12]
            shutil.copytree(stage / "vectors" / slug, config.DATA_DIR / "vectors" / new_slug)
            manifest["vectors"] = new_slug
        with sqlite3.connect(staged_db) as staged:
            staged.execute("DELETE FROM meta WHERE key='embeddings.active'")
            if manifest.get("vectors"):
                staged.execute("INSERT INTO meta(key,value) VALUES ('embeddings.active',?)", (manifest["vectors"],))
        # SQLite coordinates existing connections and WAL state. Never overwrite an
        # open database file or unlink its WAL/SHM sidecars.
        source_conn = sqlite3.connect(staged_db)
        target_conn = sqlite3.connect(config.DB_PATH, timeout=60)
        try:
            import time
            deadline = time.monotonic() + 60
            def check_timeout(status, remaining, total):
                if time.monotonic() > deadline:
                    raise RuntimeError("snapshot installation timed out waiting for the database")
            source_conn.backup(target_conn, pages=1024, progress=check_timeout)
        finally:
            source_conn.close()
            target_conn.close()
        (config.DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if src.startswith(("http://", "https://")) and local.exists():
        local.unlink()  # the downloaded archive is ~1 GB; nothing needs it after extraction
    s = Store(config.DB_PATH)
    if manifest.get("vectors"):
        s.set_meta("embeddings.active", manifest["vectors"])
    s.set_meta("snapshot.installed_at", utcnow())
    s.set_meta("snapshot.built_at", manifest.get("built_at", ""))
    n = s.count()
    s.close()
    say(f"installed snapshot built {manifest.get('built_at')}: {n} records" + (f", vectors {manifest['vectors']}" if manifest.get("vectors") else ""))
    return {**manifest, "records_now": n}
