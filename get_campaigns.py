"""
Script 2 — For each campaign group, fetch all ACTIVE campaigns and resolve
budget type + bid strategy + lead gen flag. Carries parent group context
onto each campaign record. Saves a flat array to data/campaigns.json.
"""
import json
import os
from collections import Counter

import config
from api_utils import (
    AuthFailure,
    die,
    get_with_retry,
    parse_amount,
    parse_currency,
    parse_schedule_date,
)


def _resolve_campaign_budget(campaign):
    daily = parse_amount(campaign.get("dailyBudget"))
    lifetime = parse_amount(campaign.get("totalBudget"))
    currency = (
        parse_currency(campaign.get("dailyBudget"))
        or parse_currency(campaign.get("totalBudget"))
    )

    if daily is not None and lifetime is not None:
        return {
            "budgetType": "DAILY_AND_LIFETIME",
            "dailyBudget": daily,
            "lifetimeBudget": lifetime,
            "budgetCurrency": currency,
        }
    if daily is not None:
        return {
            "budgetType": "DAILY",
            "dailyBudget": daily,
            "lifetimeBudget": None,
            "budgetCurrency": currency,
        }
    if lifetime is not None:
        return {
            "budgetType": "LIFETIME",
            "dailyBudget": None,
            "lifetimeBudget": lifetime,
            "budgetCurrency": currency,
        }
    return {
        "budgetType": "INHERITED",
        "dailyBudget": None,
        "lifetimeBudget": None,
        "budgetCurrency": None,
    }


def _resolve_bid_strategy(campaign):
    # Newer API field name is optimizationTargetType; old docs used
    # bidOptimizationTarget. Accept either.
    target = (
        campaign.get("optimizationTargetType")
        or campaign.get("bidOptimizationTarget")
    )
    cost_type = campaign.get("costType")
    unit_cost_amount = parse_amount(campaign.get("unitCost"))
    has_bid = unit_cost_amount is not None

    # No unitCost set → LinkedIn fully auto-bids (Max Delivery)
    if not has_bid:
        return {
            "bidStrategy": "MAX_DELIVERY",
            "bidType": cost_type or "CPM",
            "bidAmount": None,
            "bidCap": None,
        }

    # Manual bid targets — advertiser sets the bid, LinkedIn maximizes the goal
    # Target naming: MAX_X means "maximize X at this bid amount"
    manual_bid_cpc = {"MAX_CLICK", "MAX_LEAD", "MAX_QUALIFIED_LEAD", "MAX_CONVERSION"}
    manual_bid_cpm = {"MAX_IMPRESSION", "MAX_REACH"}
    manual_bid_cpv = {"MAX_VIDEO_VIEW"}

    if target in manual_bid_cpc:
        return {
            "bidStrategy": "MANUAL_BID",
            "bidType": "CPC",
            "bidAmount": unit_cost_amount,
            "bidCap": None,
        }
    if target in manual_bid_cpm:
        return {
            "bidStrategy": "MANUAL_BID",
            "bidType": "CPM",
            "bidAmount": unit_cost_amount,
            "bidCap": None,
        }
    if target in manual_bid_cpv:
        return {
            "bidStrategy": "MANUAL_BID",
            "bidType": "CPV",
            "bidAmount": unit_cost_amount,
            "bidCap": None,
        }

    # Cost cap — CAP_COST_AND_MAXIMIZE_X means "stay under this cost, maximize X"
    cost_cap_cpc = {
        "CAP_COST_AND_MAXIMIZE_CLICKS",
        "CAP_COST_AND_MAXIMIZE_LEADS",
        "CAP_COST_AND_MAXIMIZE_QUALIFIED_LEADS",
        "CAP_COST_AND_MAXIMIZE_CONVERSIONS",
        "TARGET_COST_PER_CLICK",  # legacy names
    }
    cost_cap_cpm = {
        "CAP_COST_AND_MAXIMIZE_IMPRESSIONS",
        "CAP_COST_AND_MAXIMIZE_REACH",
        "TARGET_COST_PER_IMPRESSION",  # legacy
    }
    cost_cap_cpv = {"CAP_COST_AND_MAXIMIZE_VIDEO_VIEWS"}

    if target in cost_cap_cpc:
        return {
            "bidStrategy": "COST_CAP",
            "bidType": "CPC",
            "bidAmount": None,
            "bidCap": unit_cost_amount,
        }
    if target in cost_cap_cpm:
        return {
            "bidStrategy": "COST_CAP",
            "bidType": "CPM",
            "bidAmount": None,
            "bidCap": unit_cost_amount,
        }
    if target in cost_cap_cpv:
        return {
            "bidStrategy": "COST_CAP",
            "bidType": "CPV",
            "bidAmount": None,
            "bidCap": unit_cost_amount,
        }

    # Target cost — LinkedIn aims for an average cost
    if target == "ENHANCED_CONVERSION":
        return {
            "bidStrategy": "TARGET_COST",
            "bidType": "CPC",
            "bidAmount": None,
            "bidCap": unit_cost_amount,
        }

    # Enhanced CPC — manual CPC with LinkedIn micro-adjustments
    if target == "ENHANCED_CPC":
        return {
            "bidStrategy": "ENHANCED_CPC",
            "bidType": "CPC",
            "bidAmount": unit_cost_amount,
            "bidCap": None,
        }

    # Unknown target with bid present → treat as MANUAL_BID on the cost type
    # This is safer than falling back to MAX_DELIVERY (which would nullify
    # the bid entirely).
    return {
        "bidStrategy": "MANUAL_BID",
        "bidType": cost_type or "CPC",
        "bidAmount": unit_cost_amount,
        "bidCap": None,
    }


def _resolve_lead_gen(campaign):
    explicit = campaign.get("leadGenerationFormEnabled")
    if isinstance(explicit, bool):
        return explicit
    return campaign.get("objectiveType") == "LEAD_GENERATION"


def _normalize_campaign(raw, group):
    campaign_id = str(raw.get("id"))
    budget = _resolve_campaign_budget(raw)
    bid = _resolve_bid_strategy(raw)
    run_schedule = raw.get("runSchedule") or {}

    return {
        "id": campaign_id,
        "urn": f"urn:li:sponsoredCampaign:{campaign_id}",
        "name": raw.get("name"),
        "status": raw.get("status"),
        "objectiveType": raw.get("objectiveType"),
        "type": raw.get("type"),
        "locale": raw.get("locale"),
        "pacingStrategy": raw.get("pacingStrategy"),
        "costType": raw.get("costType"),
        "optimizationTargetType": (
            raw.get("optimizationTargetType")
            or raw.get("bidOptimizationTarget")
        ),
        # budget
        "budgetType": budget["budgetType"],
        "dailyBudget": budget["dailyBudget"],
        "lifetimeBudget": budget["lifetimeBudget"],
        "budgetCurrency": budget["budgetCurrency"],
        # bid
        "bidStrategy": bid["bidStrategy"],
        "bidType": bid["bidType"],
        "bidAmount": bid["bidAmount"],
        "bidCap": bid["bidCap"],
        # schedule
        "scheduleStart": parse_schedule_date(run_schedule.get("start")),
        "scheduleEnd": parse_schedule_date(run_schedule.get("end")),
        # lead gen
        "leadGenFormEnabled": _resolve_lead_gen(raw),
        # parent group context
        "groupId": group["id"],
        "groupName": group["name"],
        "groupBudgetType": group["budgetType"],
        "groupDailyBudget": group["dailyBudget"],
        "groupLifetimeBudget": group["lifetimeBudget"],
        "groupBudgetCurrency": group["budgetCurrency"],
        "groupScheduleEnd": group["scheduleEnd"],
    }


def _fetch_all_ad_sets_raw():
    """Fetch every Ad Set in the account in one paginated pass.

    LinkedIn's search filter for campaignGroup is inconsistent across API
    versions, and each per-group fetch returns the whole account anyway
    (pagination serves one page of 100 regardless). So we fetch once and
    bucket client-side — ~1 API call vs N groups × 2 calls previously.
    """
    url = f"{config.API_BASE}/adAccounts/{config.ACCOUNT_ID}/adCampaigns"
    page_size = 100
    all_raw = []
    page_token = None
    page_num = 0
    while True:
        params = {"q": "search", "count": page_size}
        if page_token:
            params["pageToken"] = page_token
        print(f"Fetching Ad Sets (page {page_num}) ...")
        payload = get_with_retry(url, params=params)
        elements = payload.get("elements", []) or []
        all_raw.extend(elements)
        metadata = payload.get("metadata") or {}
        next_token = metadata.get("nextPageToken")
        if not next_token or not elements:
            break
        page_token = next_token
        page_num += 1
        if page_num > 200:
            print("  hit 200-page safety cap")
            break
    return all_raw


def _ad_sets_for_group(all_raw, group):
    """Filter to active ad sets belonging to this Campaign (group)."""
    filtered = [
        c for c in all_raw
        if c.get("campaignGroup") == group["urn"]
        and c.get("status") == "ACTIVE"
    ]
    return [_normalize_campaign(c, group) for c in filtered]


def fetch_campaigns():
    config.ensure_dirs()
    config.preflight_check()
    groups_path = os.path.join(config.DATA_DIR, "campaign_groups.json")
    if not os.path.exists(groups_path):
        raise FileNotFoundError(
            f"{groups_path} missing. Run `python3 get_campaign_groups.py` first."
        )
    with open(groups_path) as f:
        groups = json.load(f)

    # Single fetch of all ad sets (the API ignores filter params anyway — see
    # _fetch_all_ad_sets_raw docstring).
    try:
        all_raw = _fetch_all_ad_sets_raw()
    except AuthFailure:
        raise

    all_campaigns = []
    failed_groups = []
    for group in groups:
        try:
            campaigns = _ad_sets_for_group(all_raw, group)
            all_campaigns.extend(campaigns)
            print(
                f"  Campaign {group['id']} ({group['name']}): "
                f"{len(campaigns)} ad sets"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  Campaign {group['id']} failed: {exc}")
            failed_groups.append({"groupId": group["id"], "error": str(exc)})

    out_path = os.path.join(config.DATA_DIR, "campaigns.json")
    with open(out_path, "w") as f:
        json.dump(all_campaigns, f, indent=2)

    if failed_groups:
        err_path = os.path.join(config.DATA_DIR, "failed_groups.json")
        with open(err_path, "w") as f:
            json.dump(failed_groups, f, indent=2)
        print(f"Logged {len(failed_groups)} failed groups to {err_path}")

    print(f"Saved {len(all_campaigns)} campaigns to {out_path}")
    objective_counts = Counter(c["objectiveType"] for c in all_campaigns)
    bid_counts = Counter(c["bidStrategy"] for c in all_campaigns)
    print(f"By objectiveType: {dict(objective_counts)}")
    print(f"By bidStrategy:   {dict(bid_counts)}")

    return all_campaigns


if __name__ == "__main__":
    try:
        fetch_campaigns()
    except AuthFailure as exc:
        die(str(exc), code=2)
    except Exception as exc:  # noqa: BLE001
        die(f"Failed to fetch campaigns: {exc}")
