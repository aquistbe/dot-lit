"""Runtime configuration. Everything is overridable through environment variables so the
same package works in Claude Desktop (no shell profile) and from a terminal.

Nothing here is a secret: the only "identity" sent to remote hosts is a descriptive
User-Agent with a contact address, which is what OAI-PMH good-citizen practice asks for.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import __version__

# --- Identity / etiquette -----------------------------------------------------------
# Set DOT_LIT_CONTACT to your own address: OAI-PMH etiquette is that a harvester is
# identifiable and contactable.  Without it the User-Agent still names the project.
CONTACT_EMAIL = os.environ.get("DOT_LIT_CONTACT", "").strip()
USER_AGENT = (
    f"dot-lit/{__version__} (OAI-PMH harvester for research use; "
    f"https://github.com/aquistbe/dot-lit"
    + (f"; mailto:{CONTACT_EMAIL}" if CONTACT_EMAIL else "; DOT_LIT_CONTACT not set")
    + ")"
)

# Minimum seconds between outbound HTTP requests to the same host. ROSA-P resumption
# tokens expire ~60 s after issue, so the interval must stay well under that.
MIN_REQUEST_INTERVAL = float(os.environ.get("DOT_LIT_MIN_INTERVAL", "1.0"))
HTTP_TIMEOUT = float(os.environ.get("DOT_LIT_HTTP_TIMEOUT", "90"))

# --- Sources ----------------------------------------------------------------------------
ROSAP_OAI_BASE = "https://rosap.ntl.bts.gov/fedora/oai"
ROSAP_VIEW_BASE = "https://rosap.ntl.bts.gov/view/dot/"
ROSAP_METADATA_PREFIX = "oai_dc"  # the only format ROSA-P offers (verified 2026-08-26)

# --- Local storage ----------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DOT_LIT_DATA_DIR", "~/.local/share/dot-lit")).expanduser()
DB_PATH = DATA_DIR / "dot-lit.sqlite"
RAW_DIR = DATA_DIR / "raw"          # gzipped OAI-PMH responses, one file per page
PDF_DIR = DATA_DIR / "pdf"          # downloaded PDFs (full-text cache)

# Full-text limits
MAX_PDF_BYTES = int(os.environ.get("DOT_LIT_MAX_PDF_BYTES", str(80 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.environ.get("DOT_LIT_MAX_PDF_PAGES", "600"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, PDF_DIR):
        d.mkdir(parents=True, exist_ok=True)
