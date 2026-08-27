"""Command-line interface: harvest, status, search, probe, install-claude-desktop."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import __version__, config
from .harvest import harvest, harvest_fresh, make_client, reindex, status
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


def cmd_harvest(a: argparse.Namespace) -> int:
    s = _store()
    try:
        say = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
        if a.fresh:
            res = harvest_fresh(s, progress=say)
        else:
            res = harvest(s, mode=a.mode, from_ts=a.__dict__.get("from"), until_ts=a.until,
                          max_pages=a.max_pages, progress=say)
    finally:
        s.close()
    print(json.dumps(res.__dict__, indent=2))
    return 0 if res.status == "complete" else 1


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


def cmd_reindex(_: argparse.Namespace) -> int:
    s = _store()
    try:
        res = reindex(s, progress=lambda m: print(m, file=sys.stderr, flush=True))
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
    s = _store()
    try:
        hits = s.search(" ".join(a.query), year_min=a.year_min, year_max=a.year_max,
                        collection=a.collection, limit=a.limit)
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
        print(f"    {h.get('landing_url')}  mode={h.get('match_mode')} score={h.get('score'):.2f}")
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
              "  0 6 * * 1   dot-lit harvest --mode incremental >> ~/.local/share/dot-lit/logs/weekly.log 2>&1\n"
              "  0 5 1 * *   dot-lit harvest --fresh            >> ~/.local/share/dot-lit/logs/monthly.log 2>&1",
              file=sys.stderr)
        return 1
    exe = shutil.which("dot-lit") or str(Path(sys.argv[0]).resolve())
    logs = config.DATA_DIR / "logs"
    env = {"DOT_LIT_DATA_DIR": str(config.DATA_DIR), "PATH": "/usr/local/bin:/usr/bin:/bin:" + str(Path(exe).parent)}
    if config.CONTACT_EMAIL:
        env["DOT_LIT_CONTACT"] = config.CONTACT_EMAIL
    jobs = {
        "org.dot-lit.harvest-weekly": (["harvest", "--mode", "incremental"], {"Weekday": 1, "Hour": 6, "Minute": 0}),
        "org.dot-lit.harvest-monthly": (["harvest", "--fresh"], {"Day": 1, "Hour": 5, "Minute": 0}),
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

    h = sub.add_parser("harvest", help="harvest ROSA-P into the local index")
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
    sub.add_parser("reindex", help="re-parse cached raw OAI pages into the index (no network)").set_defaults(fn=cmd_reindex)
    sub.add_parser("status", help="record counts, last harvest, coverage by year").set_defaults(fn=cmd_status)

    q = sub.add_parser("search", help="search the local index")
    q.add_argument("query", nargs="+")
    q.add_argument("--year-min", type=int)
    q.add_argument("--year-max", type=int)
    q.add_argument("--collection")
    q.add_argument("--limit", type=int, default=10)
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
