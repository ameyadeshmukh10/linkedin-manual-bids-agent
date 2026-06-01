"""
Shared LinkedIn API helpers:
  - get_with_retry: GET with 429 backoff, 401 hard-halt, auto-version fallback,
    network-error retry.
  - parse_amount / parse_currency / parse_schedule_date: tolerant of all
    response shapes LinkedIn has used across API versions.
"""
import sys
import time
from datetime import datetime, timezone

import requests

import config


class AuthFailure(Exception):
    """Raised on 401. Callers should halt the pipeline."""


def _is_nonexistent_version_error(response):
    if response.status_code != 426:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return body.get("code") == "NONEXISTENT_VERSION"


def get_with_retry(
    url,
    params=None,
    max_retries=3,
    retry_wait_s=60,
    connection_retries=3,
):
    """GET with retries.
    Handles:
      - 401 → AuthFailure (halt)
      - 429 → sleep retry_wait_s, retry up to max_retries
      - 426 NONEXISTENT_VERSION → step back one month in candidate list
      - transient ConnectionError/Timeout → retry up to connection_retries
      - 4xx/5xx non-429 → RuntimeError with LinkedIn's message
    """
    # API version fallback: try the current candidate, on 426 step back
    version_candidates = config._candidate_versions()
    current_version = config.get_api_version()
    # Reorder so the cached/default version is tried first
    if current_version in version_candidates:
        idx = version_candidates.index(current_version)
        version_candidates = version_candidates[idx:] + version_candidates[:idx]

    last_exc = None
    for version in version_candidates:
        # Temporarily set for this attempt (only cache if successful)
        if config._VERSION_OVERRIDE is None:
            config.set_api_version(version)
        headers = config.get_headers()

        for attempt in range(max_retries):
            # Wrap network I/O in its own retry so transient timeouts don't
            # eat a full version+rate-limit attempt.
            for net_attempt in range(connection_retries):
                try:
                    response = requests.get(
                        url, headers=headers, params=params, timeout=60
                    )
                    break
                except (requests.ConnectionError, requests.Timeout) as exc:
                    last_exc = exc
                    if net_attempt == connection_retries - 1:
                        raise RuntimeError(
                            f"Network error after {connection_retries} "
                            f"attempts: {exc}"
                        )
                    wait = 2 ** net_attempt
                    print(
                        f"  Network error ({exc}), retrying in {wait}s "
                        f"(attempt {net_attempt + 1}/{connection_retries}) ..."
                    )
                    time.sleep(wait)
            else:
                continue  # unreachable but explicit

            if response.status_code == 401:
                raise AuthFailure(
                    "AUTH_FAILURE: 401 from LinkedIn. Token expired or "
                    "invalid. Run `python3 auth.py` to refresh."
                )

            if _is_nonexistent_version_error(response):
                print(
                    f"  API version {version} has been sunset. Trying older."
                )
                break  # try next version in outer loop

            if response.status_code == 429:
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"Rate limited after {max_retries} attempts: {url}"
                    )
                print(
                    f"  429 rate limited. Waiting {retry_wait_s}s "
                    f"(attempt {attempt + 1}/{max_retries}) ..."
                )
                time.sleep(retry_wait_s)
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"LinkedIn API error {response.status_code}: "
                    f"{response.text[:500]}"
                )

            # Success — cache this version for subsequent calls
            config.set_api_version(version)
            return response.json()
        else:
            # Exhausted max_retries for this version (probably endless 429)
            raise RuntimeError(f"Exhausted retries: {url}")

    # If we fell out of the version loop, every version was rejected.
    raise RuntimeError(
        f"LinkedIn rejected every API version we tried "
        f"({version_candidates[0]} back to {version_candidates[-1]}). "
        f"Check https://learn.microsoft.com/en-us/linkedin/marketing/versioning "
        f"for currently supported versions."
    )


def parse_amount(money_obj):
    """Parse money fields across all shapes LinkedIn has used:
      - None / "" → None
      - {amount: "12.50", currencyCode: "USD"} → 12.50
      - "12.50" → 12.50
      - 12.50 → 12.50
      - {amount: 12.50} → 12.50
      - garbage → None (never throws)
    """
    if money_obj is None or money_obj == "":
        return None
    if isinstance(money_obj, (int, float)):
        try:
            return float(money_obj)
        except (TypeError, ValueError):
            return None
    if isinstance(money_obj, str):
        try:
            return float(money_obj)
        except ValueError:
            return None
    if isinstance(money_obj, dict):
        amount = money_obj.get("amount")
        if amount is None:
            return None
        try:
            return float(amount)
        except (TypeError, ValueError):
            return None
    return None


def parse_currency(money_obj):
    """Currency only present on old {amount, currencyCode} shape."""
    if isinstance(money_obj, dict):
        return money_obj.get("currencyCode")
    return None


def parse_schedule_date(side):
    """runSchedule.start / .end comes in multiple shapes:
      - None → None
      - {day, month, year} dict (old API) → "YYYY-MM-DD"
      - int/float Unix milliseconds (new API) → "YYYY-MM-DD"
      - garbage → None (never throws)
    """
    if side is None:
        return None
    if isinstance(side, (int, float)):
        try:
            dt = datetime.fromtimestamp(side / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(side, dict):
        day = side.get("day")
        month = side.get("month")
        year = side.get("year")
        if day is None or month is None or year is None:
            return None
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except (TypeError, ValueError):
            return None
    return None


def die(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)
