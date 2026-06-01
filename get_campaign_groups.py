"""
Script 1 — Fetch all ACTIVE + DRAFT campaign groups for the account.
Resolves group-level budget type and saves to data/campaign_groups.json.

Halts with exit code 1 if zero groups are returned.
"""
import json
import os
import sys

import config
from api_utils import (
    AuthFailure,
    die,
    get_with_retry,
    parse_amount,
    parse_currency,
    parse_schedule_date,
)


def _resolve_group_budget_type(group):
    if parse_amount(group.get("dailyBudget")) is not None:
        return "DAILY"
    if parse_amount(group.get("totalBudget")) is not None:
        return "LIFETIME"
    return "UNCAPPED"


def _normalize_group(group):
    group_id = str(group.get("id"))
    run_schedule = group.get("runSchedule") or {}
    return {
        "id": group_id,
        "urn": f"urn:li:sponsoredCampaignGroup:{group_id}",
        "name": group.get("name"),
        "status": group.get("status"),
        "budgetType": _resolve_group_budget_type(group),
        "dailyBudget": parse_amount(group.get("dailyBudget")),
        "lifetimeBudget": parse_amount(group.get("totalBudget")),
        "budgetCurrency": (
            parse_currency(group.get("dailyBudget"))
            or parse_currency(group.get("totalBudget"))
        ),
        "scheduleStart": parse_schedule_date(run_schedule.get("start")),
        "scheduleEnd": parse_schedule_date(run_schedule.get("end")),
        "allowedCampaignTypes": group.get("allowedCampaignTypes"),
    }


def fetch_campaign_groups():
    """Fetch all ACTIVE Campaigns (LinkedIn campaign groups) for the account.

    LinkedIn's Rest.li 2.0 search filter syntax is inconsistent across
    versions, so we fetch unfiltered and filter client-side. Pagination uses
    metadata.nextPageToken (start/count is silently ignored by newer versions).
    """
    config.ensure_dirs()
    config.preflight_check()

    url = f"{config.API_BASE}/adAccounts/{config.ACCOUNT_ID}/adCampaignGroups"
    page_size = 100

    all_groups = []
    page_token = None
    page_num = 0
    while True:
        params = {"q": "search", "count": page_size}
        if page_token:
            params["pageToken"] = page_token
        print(f"Fetching Campaigns (page {page_num}) ...")
        payload = get_with_retry(url, params=params)

        elements = payload.get("elements", []) or []
        all_groups.extend(elements)

        metadata = payload.get("metadata") or {}
        next_token = metadata.get("nextPageToken")
        if not next_token or not elements:
            break
        page_token = next_token
        page_num += 1
        if page_num > 200:  # safety cap against runaway pagination
            print("  hit 200-page safety cap")
            break

    # Client-side status filter: ACTIVE only
    all_groups = [g for g in all_groups if g.get("status") == "ACTIVE"]

    if not all_groups:
        print("NO_ACTIVE_GROUPS")
        sys.exit(1)

    normalized = [_normalize_group(g) for g in all_groups]

    out_path = os.path.join(config.DATA_DIR, "campaign_groups.json")
    with open(out_path, "w") as f:
        json.dump(normalized, f, indent=2)

    print(f"Saved {len(normalized)} campaign groups to {out_path}")

    counts = {}
    for g in normalized:
        counts[g["budgetType"]] = counts.get(g["budgetType"], 0) + 1
    print("Budget type breakdown:", counts)

    return normalized


if __name__ == "__main__":
    try:
        fetch_campaign_groups()
    except AuthFailure as exc:
        die(str(exc), code=2)
    except Exception as exc:  # noqa: BLE001
        die(f"Failed to fetch campaign groups: {exc}")
