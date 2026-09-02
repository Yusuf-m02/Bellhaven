"""Central configuration. Everything tunable lives here."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("BELLHAVEN_DATA", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
DB_PATH = DATA_DIR / "bellhaven.sqlite3"

SITE_BASE = os.environ.get("BELLHAVEN_SITE", "https://analyst-assessment-production.up.railway.app")
API_BASE = os.environ.get("BELLHAVEN_API", SITE_BASE + "/api/v1")

# The CRM token never lives in the repo. Environment first, then a gitignored
# token file for local convenience. Callers get a clear error rather than a 401.
def _read_token() -> str:
    env = os.environ.get("BELLHAVEN_TOKEN", "").strip()
    if env:
        return env
    local = ROOT / "token.txt"
    if local.exists():
        return local.read_text(encoding="utf-8").strip()
    return ""


API_TOKEN = _read_token()

# The parent account every website community should hang from.
BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

# ---------------------------------------------------------------- matching ---
# Score bands. "Conservative" profile: only address-level agreement is auto-confident.
PROFILE = os.environ.get("BELLHAVEN_PROFILE", "conservative")

_BANDS = {
    # (confident_at, review_at)  -- below review_at we call it "no match"
    "conservative": (90, 62),
    "balanced": (78, 55),
}
CONFIDENT_AT, REVIEW_AT = _BANDS[PROFILE]

REVIEW_HOST = os.environ.get("BELLHAVEN_REVIEW_HOST", "127.0.0.1")
REVIEW_PORT = int(os.environ.get("BELLHAVEN_REVIEW_PORT", "8787"))
