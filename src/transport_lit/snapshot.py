"""Build and install index snapshots (SQLite + active vectors) so a new user can start
searching in minutes instead of harvesting nine sources for an hour."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
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
        shutil.copytree(config.DATA_DIR / "vectors" / slug, work / "vectors" / slug)
        manifest["vectors"] = slug
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
    say(f"writing {out}…")
    with tarfile.open(out, "w:gz", compresslevel=6) as tar:
        for p in sorted(work.rglob("*")):
            tar.add(p, arcname=str(p.relative_to(work)))
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
    say("extracting…")
    with tarfile.open(local, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                raise RuntimeError(f"unsafe path in archive: {m.name}")
        tar.extractall(config.DATA_DIR, filter="data")
    for suffix in ("-wal", "-shm"):
        p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        if p.exists():
            p.unlink()
    manifest = json.loads((config.DATA_DIR / "manifest.json").read_text())
    s = Store(config.DB_PATH)
    if manifest.get("vectors"):
        s.set_meta("embeddings.active", manifest["vectors"])
    s.set_meta("snapshot.installed_at", utcnow())
    s.set_meta("snapshot.built_at", manifest.get("built_at", ""))
    n = s.count()
    s.close()
    say(f"installed snapshot built {manifest.get('built_at')}: {n} records" + (f", vectors {manifest['vectors']}" if manifest.get("vectors") else ""))
    return {**manifest, "records_now": n}
