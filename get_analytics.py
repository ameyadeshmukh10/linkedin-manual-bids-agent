"""
Script 3 — Pull ad set analytics grouped by objective.

Terminology note: an "ad set" in user parlance is a LinkedIn API "campaign".
A "campaign" in user parlance is a LinkedIn API "campaign group".

For each objective bucket, we request a tailored field set. Batch size is 10
ad sets per API call. If a field set exceeds LinkedIn's 20-field cap we split
into chunks, each including pivotValues so we can map rows back to ad sets.

Aggregate period is the user-supplied --start / --end window (inclusive), using
timeGranularity=ALL so one row per ad set covers the whole window.

Output: data/analytics_raw.json — dict keyed by campaignUrn (= ad set URN)
    with merged metric fields.
"""
import argparse
import json
import os
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime

import requests

import config
from api_utils import AuthFailure, die


BATCH_SIZE = 10    # ad sets per API call (proven safe)
FIELD_LIMIT = 20   # LinkedIn's hard cap per adAnalytics call


# Objective → analytics field set. pivotValues is required in every chunk
# to identify the ad set URN on each response row.
FIELDS_BY_OBJECTIVE = {
    "LEAD_GENERATION": [
        "impressions", "clicks", "costInLocalCurrency",
        "oneClickLeads", "oneClickLeadFormOpens", "landingPageClicks",
        "follows", "pivotValues",
    ],
    "WEBSITE_VISIT": [
        "impressions", "clicks", "costInLocalCurrency",
        "landingPageClicks", "externalWebsiteConversions",
        "externalWebsitePostClickConversions",
        "externalWebsitePostViewConversions",
        "conversionValueInLocalCurrency", "pivotValues",
    ],
    "WEBSITE_CONVERSION": [
        "impressions", "clicks", "costInLocalCurrency",
        "landingPageClicks", "externalWebsiteConversions",
        "externalWebsitePostClickConversions",
        "externalWebsitePostViewConversions",
        "conversionValueInLocalCurrency", "pivotValues",
    ],
    "ENGAGEMENT": [
        "impressions", "clicks", "costInLocalCurrency",
        "totalEngagements", "reactions", "likes", "shares", "comments",
        "follows", "companyPageClicks", "landingPageClicks",
        "otherEngagements", "pivotValues",
    ],
    "VIDEO_VIEW": [
        "impressions", "videoViews", "videoStarts",
        "videoFirstQuartileCompletions", "videoMidpointCompletions",
        "videoThirdQuartileCompletions", "videoCompletions",
        "fullScreenPlays", "costInLocalCurrency", "pivotValues",
    ],
    "VIDEO_VIEWS": [
        "impressions", "videoViews", "videoStarts",
        "videoFirstQuartileCompletions", "videoMidpointCompletions",
        "videoThirdQuartileCompletions", "videoCompletions",
        "fullScreenPlays", "costInLocalCurrency", "pivotValues",
    ],
    "BRAND_AWARENESS": [
        "impressions", "clicks", "costInLocalCurrency",
        "follows", "likes", "comments", "pivotValues",
    ],
    "JOB_APPLICANTS": [
        "impressions", "clicks", "costInLocalCurrency",
        "landingPageClicks", "externalWebsiteConversions",
        "pivotValues",
    ],
}


def _rest_li_date_range(start: date, end: date) -> str:
    return (
        f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
        f"end:(year:{end.year},month:{end.month},day:{end.day}))"
    )


def _rest_li_campaigns_list(ad_set_ids):
    urns = [f"urn%3Ali%3AsponsoredCampaign%3A{cid}" for cid in ad_set_ids]
    return "List(" + ",".join(urns) + ")"


def _build_url(params_ordered):
    """Build URL preserving Rest.li tuple syntax (parens/colons/commas)."""
    safe = ":,()-%"
    pairs = [
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe=safe)}"
        for k, v in params_ordered
    ]
    return f"{config.API_BASE}/adAnalytics?" + "&".join(pairs)


def _request_with_retry(url):
    headers = config.get_headers()
    for attempt in range(3):
        resp = requests.get(url, headers=headers, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            raise AuthFailure(
                "AUTH_FAILURE: 401 from LinkedIn. Token expired. Run auth.py."
            )
        if resp.status_code == 429:
            if attempt == 2:
                raise RuntimeError(f"Rate limited after 3 attempts: {url}")
            print(f"  429 rate limited. Waiting 60s (attempt {attempt + 1}/3).")
            time.sleep(60)
            continue
        raise RuntimeError(
            f"LinkedIn API error {resp.status_code}: {resp.text[:500]}"
        )
    raise RuntimeError(f"Exhausted retries: {url}")


def _split_fields(fields):
    """Split >20 fields into chunks, preserving pivotValues in every chunk."""
    if len(fields) <= FIELD_LIMIT:
        return [fields]
    other = [f for f in fields if f != "pivotValues"]
    step = FIELD_LIMIT - 1
    return [["pivotValues"] + other[i:i + step] for i in range(0, len(other), step)]


def _extract_urn(row):
    for pv in row.get("pivotValues") or []:
        if isinstance(pv, str) and pv.startswith("urn:li:sponsoredCampaign:"):
            return pv
    return None


def _fetch_bucket(ad_sets, objective, start_date, end_date):
    """Fetch a homogeneous bucket of ad sets with that objective's fields.
    Returns dict urn -> merged metric row."""
    fields = FIELDS_BY_OBJECTIVE.get(objective)
    if not fields:
        # Unknown objective — use the WEBSITE_VISIT default
        fields = FIELDS_BY_OBJECTIVE["WEBSITE_VISIT"]

    field_chunks = _split_fields(fields)
    ids = [str(c["id"]) for c in ad_sets]
    by_urn = {}
    date_range = _rest_li_date_range(start_date, end_date)

    for batch_start in range(0, len(ids), BATCH_SIZE):
        batch = ids[batch_start:batch_start + BATCH_SIZE]
        campaigns_param = _rest_li_campaigns_list(batch)
        for chunk_idx, chunk in enumerate(field_chunks):
            params = [
                ("q", "analytics"),
                ("pivot", "CAMPAIGN"),
                ("timeGranularity", "ALL"),
                ("dateRange", date_range),
                ("campaigns", campaigns_param),
                ("fields", ",".join(chunk)),
            ]
            url = _build_url(params)
            suffix = (
                f" chunk {chunk_idx + 1}/{len(field_chunks)}"
                if len(field_chunks) > 1 else ""
            )
            print(
                f"  batch {batch_start // BATCH_SIZE + 1} "
                f"({len(batch)} ad sets){suffix}"
            )
            try:
                payload = _request_with_retry(url)
            except AuthFailure:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"    FAILED: {exc}")
                continue
            for row in payload.get("elements") or []:
                urn = _extract_urn(row)
                if not urn:
                    continue
                if urn in by_urn:
                    by_urn[urn].update(row)
                else:
                    by_urn[urn] = dict(row)

    return by_urn


def fetch_analytics(start_date, end_date):
    config.ensure_dirs()
    config.preflight_check()
    # data/campaigns.json holds LinkedIn-API "campaigns" = user-facing "Ad Sets"
    campaigns_path = os.path.join(config.DATA_DIR, "campaigns.json")
    if not os.path.exists(campaigns_path):
        raise FileNotFoundError(
            f"{campaigns_path} missing. Run `python3 get_campaigns.py` first."
        )
    with open(campaigns_path) as f:
        ad_sets = json.load(f)

    by_obj = defaultdict(list)
    unknown = []
    for c in ad_sets:
        obj = c.get("objectiveType")
        if obj in FIELDS_BY_OBJECTIVE:
            by_obj[obj].append(c)
        else:
            unknown.append(c)

    if unknown:
        print(
            f"WARN: {len(unknown)} ad sets have unsupported objectiveType; "
            f"using WEBSITE_VISIT fields as fallback"
        )
        by_obj["WEBSITE_VISIT"].extend(unknown)

    print(f"Date range: {start_date} → {end_date}")
    all_by_urn = {}
    for obj, group in by_obj.items():
        print(f"\n=== {obj}: {len(group)} ad sets ===")
        result = _fetch_bucket(group, obj, start_date, end_date)
        all_by_urn.update(result)

    # Write a row for every ad set (empty dict if no metrics returned)
    out = {}
    for c in ad_sets:
        urn = c["urn"]
        out[urn] = all_by_urn.get(urn, {})

    out_path = os.path.join(config.DATA_DIR, "analytics_raw.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    with_data = sum(1 for v in out.values() if v)
    print(f"\nSaved analytics for {with_data}/{len(out)} ad sets → {out_path}")
    return out


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    args = parser.parse_args()

    try:
        fetch_analytics(_parse_date(args.start), _parse_date(args.end))
    except AuthFailure as exc:
        die(str(exc), code=2)
    except Exception as exc:  # noqa: BLE001
        die(f"Failed to fetch analytics: {exc}")
