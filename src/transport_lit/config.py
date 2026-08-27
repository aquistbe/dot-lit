"""Runtime configuration. Everything is overridable through environment variables so the
same package works in Claude Desktop (no shell profile) and from a terminal.

Nothing here is a secret: the only "identity" sent to remote hosts is a descriptive
User-Agent with a contact address, which is what OAI-PMH good-citizen practice asks for.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__


def _env(name: str, default: str = "") -> str:
    """TRANSPORT_LIT_* wins; DOT_LIT_* (the project's original name) is still honoured."""
    return os.environ.get(f"TRANSPORT_LIT_{name}", os.environ.get(f"DOT_LIT_{name}", default))

# --- Identity / etiquette -----------------------------------------------------------
# Set TRANSPORT_LIT_CONTACT to your own address: OAI-PMH etiquette is that a harvester is
# identifiable and contactable.  Without it the User-Agent still names the project.
CONTACT_EMAIL = _env("CONTACT").strip()
USER_AGENT = (
    f"transport-lit/{__version__} (OAI-PMH harvester for research use; "
    f"https://github.com/aquistbe/transport-lit"
    + (f"; mailto:{CONTACT_EMAIL}" if CONTACT_EMAIL else "; TRANSPORT_LIT_CONTACT not set")
    + ")"
)

# Minimum seconds between outbound HTTP requests to the same host. ROSA-P resumption
# tokens expire ~60 s after issue, so the interval must stay well under that.
MIN_REQUEST_INTERVAL = float(_env("MIN_INTERVAL", "1.0"))
HTTP_TIMEOUT = float(_env("HTTP_TIMEOUT", "90"))

# --- Sources ----------------------------------------------------------------------------
ROSAP_OAI_BASE = "https://rosap.ntl.bts.gov/fedora/oai"
ROSAP_VIEW_BASE = "https://rosap.ntl.bts.gov/view/dot/"
ROSAP_METADATA_PREFIX = "oai_dc"  # the only format ROSA-P offers (verified 2026-08-26)

# --- Local storage ----------------------------------------------------------------------
_default_dir = Path("~/.local/share/transport-lit").expanduser()
_legacy_dir = Path("~/.local/share/dot-lit").expanduser()
DATA_DIR = Path(_env("DATA_DIR") or (str(_legacy_dir) if _legacy_dir.exists() and not _default_dir.exists() else str(_default_dir))).expanduser()
# the database file keeps whichever name already exists (installs made under the old name)
DB_PATH = DATA_DIR / ("dot-lit.sqlite" if (DATA_DIR / "dot-lit.sqlite").exists() and not (DATA_DIR / "transport-lit.sqlite").exists()
                      else "transport-lit.sqlite")
RAW_DIR = DATA_DIR / "raw"          # gzipped OAI-PMH responses, one file per page
PDF_DIR = DATA_DIR / "pdf"          # downloaded PDFs (full-text cache)

# Full-text limits
MAX_PDF_BYTES = int(_env("MAX_PDF_BYTES", str(80 * 1024 * 1024)))
MAX_PDF_PAGES = int(_env("MAX_PDF_PAGES", "600"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, PDF_DIR):
        d.mkdir(parents=True, exist_ok=True)
