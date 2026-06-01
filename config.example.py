"""
LinkedIn Ads Bid Optimization — configuration, auth headers, preflight checks.

Copy this file to `config.py` and fill in your own LinkedIn app credentials
and ad account ID. `config.py` is gitignored; do not commit it.
"""
import json
import os
import sys
import time
from datetime import date, datetime, timezone

# ---------------------------------------------------------------------------
# LinkedIn app credentials — fill these in
# ---------------------------------------------------------------------------
CLIENT_ID = "your-linkedin-client-id"
CLIENT_SECRET = "your-linkedin-client-secret"

# OAuth endpoints
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
REDIRECT_URI = "http://localhost:8000/callback"
OAUTH_SCOPES = "r_ads r_ads_reporting rw_ads"

# ---------------------------------------------------------------------------
# LinkedIn API
# ---------------------------------------------------------------------------
API_BASE = "https://api.linkedin.com/rest"

# LinkedIn deprecates API versions on a ~12-month rolling basis. Rather than
# hardcode a version that will eventually sunset, we try the current month
# first and fall back to progressively older versions if the API returns
# "NONEXISTENT_VERSION". The first version that works is cached for the run.
_VERSION_OVERRIDE = os.environ.get("LINKEDIN_API_VERSION")  # manual override
_CACHED_VERSION = None


def _candidate_versions():
    """Generate version candidates: current month, then going back up to 14
    months. LinkedIn usually keeps ~12 months of versions live."""
    today = date.today()
    candidates = []
    y, m = today.year, today.month
    for _ in range(14):
        candidates.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return candidates


def get_api_version():
    """Return the API version to use. Respects LINKEDIN_API_VERSION env var.
    First successful-use version is cached via set_api_version()."""
    global _CACHED_VERSION
    if _VERSION_OVERRIDE:
        return _VERSION_OVERRIDE
    if _CACHED_VERSION:
        return _CACHED_VERSION
    return _candidate_versions()[0]


def set_api_version(version):
    """Cache a known-good API version for the rest of this process."""
    global _CACHED_VERSION
    _CACHED_VERSION = version


# ---------------------------------------------------------------------------
# Account + token — fill ACCOUNT_ID with your numeric LinkedIn ad account ID
# ---------------------------------------------------------------------------
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
ACCESS_TOKEN = ""  # prefer token.json; leave blank here

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_DIR, "data")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
TOKEN_FILE = os.path.join(PROJECT_DIR, "token.json")


# ---------------------------------------------------------------------------
# Token handling with expiry awareness
# ---------------------------------------------------------------------------


def _load_token_blob():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_access_token():
    blob = _load_token_blob()
    if blob and blob.get("access_token"):
        return blob["access_token"]
    if ACCESS_TOKEN:
        return ACCESS_TOKEN
    return None


def token_seconds_until_expiry():
    """Return seconds until token expires, or None if unknown.
    `expires_in` is at issue-time; we derive `expires_at_epoch` if absent
    by trusting the file mtime as a proxy for issue time."""
    blob = _load_token_blob()
    if not blob:
        return None
    expires_in = blob.get("expires_in")
    if not expires_in:
        return None
    expires_at = blob.get("expires_at_epoch")
    if not expires_at:
        try:
            mtime = os.path.getmtime(TOKEN_FILE)
        except OSError:
            return None
        expires_at = mtime + expires_in
    return expires_at - time.time()


def preflight_check(require_token=True):
    """Validate config + token before expensive work. Exit with a clear
    message if anything is off."""
    if not ACCOUNT_ID or ACCOUNT_ID == "YOUR_ACCOUNT_ID":
        print(
            "ERROR: ACCOUNT_ID is not set in config.py. Fill in your "
            "LinkedIn ad account ID (numeric) and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not CLIENT_ID or not CLIENT_SECRET:
        print(
            "ERROR: CLIENT_ID or CLIENT_SECRET missing in config.py.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not require_token:
        return
    token = get_access_token()
    if not token:
        print(
            "ERROR: No access token found. Run:\n\n"
            "    python3 auth.py\n\n"
            "then rerun this command.",
            file=sys.stderr,
        )
        sys.exit(2)
    ttl = token_seconds_until_expiry()
    if ttl is not None and ttl < 0:
        print(
            f"ERROR: Access token in {TOKEN_FILE} appears expired "
            f"({int(-ttl / 86400)} days ago). Run:\n\n"
            "    python3 auth.py\n\n"
            "then rerun this command.",
            file=sys.stderr,
        )
        sys.exit(2)
    if ttl is not None and ttl < 3 * 86400:
        print(
            f"WARNING: Token expires in {int(ttl / 86400)} days. "
            f"Consider refreshing soon with `python3 auth.py`.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Headers for LinkedIn REST calls
# ---------------------------------------------------------------------------


def get_headers():
    token = get_access_token() or ""
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": get_api_version(),
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


if __name__ == "__main__":
    ensure_dirs()
    print(f"PROJECT_DIR:    {PROJECT_DIR}")
    print(f"DATA_DIR:       {DATA_DIR}")
    print(f"OUTPUT_DIR:     {OUTPUT_DIR}")
    print(f"TOKEN_FILE:     {TOKEN_FILE}")
    print(f"ACCOUNT_ID:     {ACCOUNT_ID}")
    print(f"API VERSION:    {get_api_version()} (override={_VERSION_OVERRIDE})")
    ttl = token_seconds_until_expiry()
    if ttl is not None:
        if ttl < 0:
            print(f"TOKEN:          EXPIRED ({int(-ttl / 86400)} days ago)")
        else:
            print(f"TOKEN:          valid for {int(ttl / 86400)} more days")
    else:
        print("TOKEN:          unknown expiry (or no token.json)")
