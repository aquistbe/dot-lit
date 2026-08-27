"""Command-line interface: harvest, status, search, probe, install-claude-desktop."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import __version__, config
from .apis import API_SOURCES
from .harvest import harvest, harvest_api, harvest_fresh, harvest_fresh_api, make_client, reindex, reindex_api, status
from .sources import SOURCES, get_source
from .store import Store


def _store() -> Store:
    config.ensure_dirs()
    return Store(config.DB_PATH)


def cmd_probe(_: argparse.Namespace) -> int:
    """Live check of the OAI-PMH endpoint: Identify, ListMetadataFormats, ListSets."""
    c = make_client()
    try:
        ident = c.identify()
        fmts = c.list_metadata_formats()
        sets = c.list_sets()
    finally:
        c.close()
    print(json.dumps({"identify": ident, "metadata_formats": [f.__dict__ for f in fmts],
                      "sets": [s.__dict__ for s in sets]}, indent=2))
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    for k, src in SOURCES.items():
        print(f"{k:8s} {src.name}\n         OAI-PMH {src.base_url}  set={src.set_spec or '-'}  filter={'yes' if src.include else 'no'}  {src.notes}")
    for k, src in API_SOURCES.items():
        print(f"{k:8s} {src.name}\n         API  {src.notes}")
    return 0


def cmd_harvest(a: argparse.Namespace) -> int:
    keys = list(SOURCES) + list(API_SOURCES) if a.source == "all" else [a.source]
    s = _store()
    results = []
    try:
        say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
        for k in keys:
            if k in API_SOURCES:
                api = API_SOURCES[k]
                res = harvest_fresh_api(s, api, progress=say) if a.fresh else harvest_api(s, api, mode=a.mode, progress=say)
            else:
                src = get_source(k)
                if a.fresh:
                    res = harvest_fresh(s, source=src, progress=say)
                else:
                    res = harvest(s, source=src, mode=a.mode, from_ts=a.__dict__.get("from"), until_ts=a.until,
                                  max_pages=a.max_pages, progress=say)
            results.append(res.__dict__ | {"source": k})
    finally:
        s.close()
    print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0 if all(r["status"] == "complete" for r in results) else 1


def cmd_import(a: argparse.Namespace) -> int:
    from .importers import import_ris

    s = _store()
    out = []
    try:
        for f in a.files:
            out.append(import_ris(s, Path(f), source=a.source, collection=a.collection))
            print(f"{f}: {out[-1]['records']} records ({out[-1]['by_source']})", file=sys.stderr)
    finally:
        s.close()
    print(json.dumps(out, indent=2))
    return 0


def cmd_reindex(a: argparse.Namespace) -> int:
    s = _store()
    try:
        say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
        if a.source in API_SOURCES:
            res = reindex_api(s, API_SOURCES[a.source], progress=say)
        else:
            res = reindex(s, source=get_source(a.source), progress=say)
    finally:
        s.close()
    print(json.dumps(res, indent=2))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    s = _store()
    try:
        print(json.dumps(status(s), indent=2))
    finally:
        s.close()
    return 0


def cmd_search(a: argparse.Namespace) -> int:
    from .embeddings import hybrid_search

    s = _store()
    try:
        hits, used = hybrid_search(s, " ".join(a.query), mode=a.mode, limit=a.limit, year_min=a.year_min,
                                   year_max=a.year_max, collection=a.collection)
        print(f"(mode: {used})", file=sys.stderr)
    finally:
        s.close()
    if a.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0
    for i, h in enumerate(hits, 1):
        authors = ", ".join(h.get("authors") or [])[:80]
        rn = "; ".join(h.get("report_numbers") or [])
        print(f"{i:2d}. [{h['id']}] ({h.get('year')}) {h.get('title')}")
        print(f"    {authors}")
        if rn:
            print(f"    report no.: {rn}")
        extra = "".join(f" {k}={h[k]}" for k in ("keyword_rank", "semantic_rank", "semantic_score") if h.get(k) is not None)
        print(f"    {h.get('landing_url')}  mode={h.get('match_mode')}{extra}")
        snip = (h.get("snippet") or "").replace("\n", " ")
        if snip:
            print(f"    {snip[:300]}")
    if not hits:
        print("(no hits)")
    return 0


def cmd_get(a: argparse.Namespace) -> int:
    s = _store()
    try:
        rec = s.get_record(a.id)
    finally:
        s.close()
    print(json.dumps(rec, indent=2, ensure_ascii=False) if rec else f"no record {a.id}")
    return 0 if rec else 1


def cmd_fulltext(a: argparse.Namespace) -> int:
    from .fulltext import get_fulltext

    s = _store()
    try:
        ft = get_fulltext(s, a.id, refresh=a.refresh)
    finally:
        s.close()
    meta = {k: v for k, v in ft.items() if k != "text"}
    print(json.dumps(meta, indent=2), file=sys.stderr)
    print((ft.get("text") or "")[: a.max_chars])
    return 0 if ft.get("status") == "ok" else 1


def cmd_embed(a: argparse.Namespace) -> int:
    from .embeddings import embed_records, make_backend

    backend = make_backend(a.backend, a.model, **({"dim": a.dim} if a.dim and a.backend == "ollama" else {}))
    s = _store()
    try:
        res = embed_records(s, backend, rebuild=a.rebuild, limit=a.limit, batch=a.batch,
                            sources=a.source.split(",") if a.source else None,
                            progress=lambda m: print(m, file=sys.stderr, flush=True))
    finally:
        s.close()
    print(json.dumps(res, indent=2))
    return 0


def cmd_snapshot(a: argparse.Namespace) -> int:
    from . import snapshot

    say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    if a.action == "build":
        s = _store()
        try:
            res = snapshot.build(s, Path(a.path), include_vectors=not a.no_vectors,
                                 exclude_sources=([x for x in a.exclude.split(",") if x] if a.exclude is not None else None), progress=say)
        finally:
            s.close()
    else:
        res = snapshot.install(a.path, force=a.force, progress=say)
    print(json.dumps(res, indent=2))
    return 0


def cmd_digest(a: argparse.Namespace) -> int:
    """Markdown digest of what entered the index recently — the skeleton of a weekly bulletin."""
    s = _store()
    try:
        d = s.whats_new(a.days, limit=a.limit)
    finally:
        s.close()
    from collections import defaultdict
    by = defaultdict(list)
    for r in d["records"]:
        by[r["id"].split(":")[0]].append(r)
    names = {"dot": "ROSA-P (U.S. DOT)", "vti": "VTI (Sweden)", "bast": "BASt (Germany)", "wbokr": "World Bank",
             "ipea": "IPEA (Brazil)", "cepal": "CEPAL", "openalex": "OpenAlex reports", "cinii": "CiNii (Japan)",
             "pubmed": "PubMed", "trid": "TRID imports"}
    print(f"# New transport literature, last {a.days} days (since {d['since'][:10]})\n")
    print("Counts: " + ", ".join(f"{names.get(k, k)} {v}" for k, v in d["counts_by_source"].items()) + "\n")
    for src, recs in by.items():
        print(f"## {names.get(src, src)} ({len(recs)})\n")
        for r in recs:
            au = ", ".join((r.get("authors") or [])[:3]) + (" et al." if len(r.get("authors") or []) > 3 else "")
            print(f"- **{r.get('title')}** ({r.get('year') or 'n.d.'}). {au}  \n  {r.get('landing_url')}")
            if a.abstracts and r.get("abstract"):
                print(f"  \n  {r['abstract'][:400]}…")
        print()
    return 0


def cmd_mcp_config(a: argparse.Namespace) -> int:
    """Print MCP client configuration snippets for common clients."""
    exe = shutil.which("dot-lit-mcp") or str(Path(sys.argv[0]).resolve().parent / "dot-lit-mcp")
    env = {"DOT_LIT_DATA_DIR": str(config.DATA_DIR)}
    if config.CONTACT_EMAIL:
        env["DOT_LIT_CONTACT"] = config.CONTACT_EMAIL
    stdio = {"command": exe, "args": [], "env": env}
    http = "http://127.0.0.1:8765/mcp"
    snippets = {
        "claude-desktop": ("~/Library/Application Support/Claude/claude_desktop_config.json (or `dot-lit install-claude-desktop --write`)",
                           json.dumps({"mcpServers": {"dot-lit": stdio}}, indent=2)),
        "claude-code": ("shell", f"claude mcp add dot-lit -e DOT_LIT_DATA_DIR={config.DATA_DIR} -- {exe}"),
        "cursor": ("~/.cursor/mcp.json", json.dumps({"mcpServers": {"dot-lit": stdio}}, indent=2)),
        "vscode": (".vscode/mcp.json (GitHub Copilot agent mode)", json.dumps({"servers": {"dot-lit": {"type": "stdio", **stdio}}}, indent=2)),
        "zed": ("~/.config/zed/settings.json", json.dumps({"context_servers": {"dot-lit": {"command": {"path": exe, "args": [], "env": env}}}}, indent=2)),
        "continue": ("~/.continue/config.yaml", "mcpServers:\n  - name: dot-lit\n    command: " + exe + "\n    env:\n" + "".join(f"      {k}: {v}\n" for k, v in env.items())),
        "lm-studio": ("LM Studio > Program > Install > Edit mcp.json", json.dumps({"mcpServers": {"dot-lit": stdio}}, indent=2)),
        "goose": ("~/.config/goose/config.yaml", "extensions:\n  dot-lit:\n    type: stdio\n    enabled: true\n    cmd: " + exe + "\n    args: []\n    envs:\n" + "".join(f"      {k}: {v}\n" for k, v in env.items())),
        "open-webui": ("Run `dot-lit-mcp --transport streamable-http --port 8765`, then add as a Streamable HTTP tool server", http),
        "librechat": ("librechat.yaml", "mcpServers:\n  dot-lit:\n    type: streamable-http\n    url: " + http),
        "ollama-python": ("see tests/ollama_smoke.py for a minimal tool-calling loop over stdio", ""),
    }
    keys = [a.client] if a.client else list(snippets)
    for k in keys:
        if k not in snippets:
            print(f"unknown client {k}; known: {', '.join(snippets)}", file=sys.stderr)
            return 1
        where, body = snippets[k]
        print(f"### {k}\n# {where}\n{body}\n")
    return 0


def claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if sys.platform.startswith("win"):
        import os
        return Path(os.environ.get("APPDATA", "~")).expanduser() / "Claude" / "claude_desktop_config.json"
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


def cmd_install(a: argparse.Namespace) -> int:
    exe = shutil.which("dot-lit-mcp")
    if not exe:
        # fall back to the script next to the running interpreter (uv tool / venv layout)
        cand = Path(sys.argv[0]).resolve().parent / "dot-lit-mcp"
        exe = str(cand) if cand.exists() else None
    if not exe:
        print("dot-lit-mcp not found on PATH; install with `uv tool install .` first", file=sys.stderr)
        return 1
    env = {"DOT_LIT_DATA_DIR": str(config.DATA_DIR)}
    if config.CONTACT_EMAIL:
        env["DOT_LIT_CONTACT"] = config.CONTACT_EMAIL
    entry = {"command": exe, "args": [], "env": env}
    path = claude_desktop_config_path()
    snippet = {"mcpServers": {"dot-lit": entry}}
    if not a.write:
        print(f"# Add this to {path}\n{json.dumps(snippet, indent=2)}")
        print("\n# Re-run with --write to merge it into that file (a .bak copy is made first).")
        return 0
    cfg = {}
    if path.exists():
        cfg = json.loads(path.read_text() or "{}")
        shutil.copy(path, path.with_suffix(".json.bak"))
    cfg.setdefault("mcpServers", {})["dot-lit"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    print(f"wrote {path} (backup at {path.with_suffix('.json.bak')}); restart Claude Desktop")
    return 0


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict>{env}</dict>
  <key>StartCalendarInterval</key><dict>{cal}</dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>RunAtLoad</key><false/>
</dict></plist>
"""


def cmd_install_schedule(a: argparse.Namespace) -> int:
    """macOS launchd: weekly incremental harvest (Mon 06:00) + monthly fresh rebuild (1st, 05:00)."""
    if sys.platform != "darwin":
        print("install-schedule writes launchd agents (macOS). On Linux use cron, e.g.:\n"
              "  0 6 * * 1   dot-lit harvest --source all --mode incremental >> ~/.local/share/dot-lit/logs/weekly.log 2>&1\n"
              "  0 5 1 * *   dot-lit harvest --source all --fresh            >> ~/.local/share/dot-lit/logs/monthly.log 2>&1",
              file=sys.stderr)
        return 1
    exe = shutil.which("dot-lit") or str(Path(sys.argv[0]).resolve())
    logs = config.DATA_DIR / "logs"
    env = {"DOT_LIT_DATA_DIR": str(config.DATA_DIR), "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(Path(exe).parent)}
    if config.CONTACT_EMAIL:
        env["DOT_LIT_CONTACT"] = config.CONTACT_EMAIL
    jobs = {
        "org.dot-lit.harvest-weekly": (["harvest", "--source", "all", "--mode", "incremental"], {"Weekday": 1, "Hour": 6, "Minute": 0}),
        "org.dot-lit.harvest-monthly": (["harvest", "--source", "all", "--fresh"], {"Day": 1, "Hour": 5, "Minute": 0}),
    }
    agents = Path("~/Library/LaunchAgents").expanduser()
    written = []
    for label, (args, cal) in jobs.items():
        xml = PLIST.format(
            label=label,
            args="".join(f"<string>{x}</string>" for x in [exe, *args]),
            env="".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items()),
            cal="".join(f"<key>{k}</key><integer>{v}</integer>" for k, v in cal.items()),
            log=str(logs / f"{label.split('.')[-1]}.log"),
        )
        target = agents / f"{label}.plist"
        if not a.write:
            print(f"# would write {target}\n{xml}")
            continue
        logs.mkdir(parents=True, exist_ok=True)
        agents.mkdir(parents=True, exist_ok=True)
        target.write_text(xml)
        import os
        import subprocess
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], capture_output=True, text=True)
        written.append((target, r.returncode, r.stderr.strip()))
    for target, rc, err in written:
        print(f"loaded {target}" + (f" (launchctl rc={rc}: {err})" if rc else ""))
    if not a.write:
        print("\n# Re-run with --write to install and load these agents. Logs go to", logs)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dot-lit", description="U.S. DOT grey-literature index (ROSA-P)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="query Identify/ListMetadataFormats/ListSets live").set_defaults(fn=cmd_probe)

    sub.add_parser("sources", help="list configured OAI-PMH sources").set_defaults(fn=cmd_sources)

    h = sub.add_parser("harvest", help="harvest a source (default rosap) into the local index")
    h.add_argument("--source", default="rosap", help="source key from `dot-lit sources`, or 'all'")
    h.add_argument("--mode", choices=["auto", "full", "incremental"], default="auto")
    h.add_argument("--from", dest="from", help="OAI from= (YYYY-MM-DDThh:mm:ssZ); overrides mode")
    h.add_argument("--until", help="OAI until= (YYYY-MM-DDThh:mm:ssZ)")
    h.add_argument("--max-pages", type=int, help="stop early (marks run failed/partial); for testing")
    h.add_argument("--fresh", action="store_true",
                   help="true rebuild: full harvest into a temp store, then atomically replace all ROSA-P records")
    h.set_defaults(fn=cmd_harvest)

    im = sub.add_parser("import", help="import RIS export(s) (e.g. from TRID's Export button) into the index")
    im.add_argument("files", nargs="+")
    im.add_argument("--source", default="import", help="id prefix for records without a TRID URL (default: import)")
    im.add_argument("--collection", help="collection label for the imported records (default: file name)")
    im.set_defaults(fn=cmd_import)
    ri = sub.add_parser("reindex", help="re-parse cached raw OAI pages into the index (no network)")
    ri.add_argument("--source", default="rosap")
    ri.set_defaults(fn=cmd_reindex)
    sub.add_parser("status", help="record counts, last harvest, coverage by year").set_defaults(fn=cmd_status)

    q = sub.add_parser("search", help="search the local index")
    q.add_argument("query", nargs="+")
    q.add_argument("--year-min", type=int)
    q.add_argument("--year-max", type=int)
    q.add_argument("--collection")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_search)

    g = sub.add_parser("get", help="print one record's full metadata")
    g.add_argument("id")
    g.set_defaults(fn=cmd_get)

    f = sub.add_parser("fulltext", help="fetch + extract a report's PDF text")
    f.add_argument("id")
    f.add_argument("--refresh", action="store_true")
    f.add_argument("--max-chars", type=int, default=5000)
    f.set_defaults(fn=cmd_fulltext)

    sch = sub.add_parser("install-schedule", help="print (or --write) launchd agents: weekly incremental + monthly fresh rebuild")
    sch.add_argument("--write", action="store_true")
    sch.set_defaults(fn=cmd_install_schedule)

    em = sub.add_parser("embed", help="compute embeddings for records that lack them (semantic search)")
    em.add_argument("--backend", choices=["fastembed", "ollama"], default=None, help="default: $DOT_LIT_EMBED_BACKEND or fastembed")
    em.add_argument("--model", help="fastembed model name or Ollama model tag (e.g. qwen3-embedding:8b)")
    em.add_argument("--dim", type=int, help="ollama only: truncate vectors to this many dims (default 1024)")
    em.add_argument("--rebuild", action="store_true")
    em.add_argument("--limit", type=int)
    em.add_argument("--source", help="comma-separated source keys to embed")
    em.add_argument("--batch", type=int, default=64)
    em.set_defaults(fn=cmd_embed)

    sn = sub.add_parser("snapshot", help="build or install an index snapshot (SQLite + vectors)")
    sn.add_argument("action", choices=["build", "install"])
    sn.add_argument("path", help="output .tar.gz (build) or local path / https URL (install)")
    sn.add_argument("--no-vectors", action="store_true")
    sn.add_argument("--exclude", help="build: comma-separated sources to leave out (default: cinii,trid; pass '' for none)")
    sn.add_argument("--force", action="store_true", help="install: replace an existing index")
    sn.set_defaults(fn=cmd_snapshot)

    dg = sub.add_parser("digest", help="markdown digest of records added in the last N days")
    dg.add_argument("--days", type=int, default=7)
    dg.add_argument("--limit", type=int, default=200)
    dg.add_argument("--abstracts", action="store_true")
    dg.set_defaults(fn=cmd_digest)

    mc = sub.add_parser("mcp-config", help="print MCP client config snippets (claude-desktop, cursor, vscode, zed, continue, lm-studio, goose, open-webui, librechat)")
    mc.add_argument("client", nargs="?")
    mc.set_defaults(fn=cmd_mcp_config)

    i = sub.add_parser("install-claude-desktop", help="print (or --write) the Claude Desktop MCP config entry")
    i.add_argument("--write", action="store_true")
    i.set_defaults(fn=cmd_install)

    a = p.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
