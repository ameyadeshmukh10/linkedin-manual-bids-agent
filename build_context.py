"""
Script 4 — Stitch Campaigns (LinkedIn groups) + Ad Sets (LinkedIn campaigns) +
analytics into a hierarchical context payload.

Terminology:
  "Campaign" (user-facing)  = LinkedIn campaign group
  "Ad Set"   (user-facing)  = LinkedIn campaign

For each Campaign (group):
  - group metadata (objective, daily/lifetime budget, schedule)
  - list of its active ad sets, each with:
      - bid config (strategy, amount/cap, type)
      - raw metrics (over the analytics window)
      - derived metrics tailored to the Campaign's objective
      - pacing assessment (uses ad-set budget if set, else historical spend
        share of the group's daily budget)

Output: data/context_payload.json
  { windowDays, dateRange, campaigns: [ {group fields..., adSets: [...]}, ...] }
"""
import argparse
import json
import os
from datetime import datetime, timedelta

import config


def _num(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_div(n, d):
    try:
        if not d:
            return None
        return float(n) / float(d)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _derived_metrics(objective, m):
    """Per-objective derived metrics, following the reporting agent's spec."""
    impressions = _num(m.get("impressions"))
    clicks = _num(m.get("clicks"))
    spend = _num(m.get("costInLocalCurrency"))

    ctr = _safe_div(clicks, impressions)
    ctr_pct = ctr * 100 if ctr is not None else None
    cpc = _safe_div(spend, clicks)
    cpm_raw = _safe_div(spend, impressions)
    cpm = cpm_raw * 1000 if cpm_raw is not None else None

    derived = {
        "ctrPct": ctr_pct,
        "cpc": cpc,
        "cpm": cpm,
    }

    if objective == "LEAD_GENERATION":
        leads = _num(m.get("oneClickLeads"))
        form_opens = _num(m.get("oneClickLeadFormOpens"))
        ext_conv = _num(m.get("externalWebsiteConversions"))
        derived["cpl"] = _safe_div(spend, leads)
        derived["costPerExtConv"] = _safe_div(spend, ext_conv)
        form_open_rate = _safe_div(form_opens, impressions)
        derived["formOpenRatePct"] = form_open_rate * 100 if form_open_rate else None
        lead_conv = _safe_div(leads, form_opens)
        derived["leadConvRatePct"] = lead_conv * 100 if lead_conv else None

    elif objective in ("WEBSITE_VISIT", "WEBSITE_CONVERSION", "JOB_APPLICANTS"):
        ext_conv = _num(m.get("externalWebsiteConversions"))
        derived["costPerConversion"] = _safe_div(spend, ext_conv)
        # Landing page CTR (LP clicks / impressions) is useful for WEBSITE_VISIT
        lpc = _num(m.get("landingPageClicks"))
        lpc_rate = _safe_div(lpc, impressions)
        derived["lpcRatePct"] = lpc_rate * 100 if lpc_rate else None

    elif objective == "ENGAGEMENT":
        engagements = _num(m.get("totalEngagements"))
        eng_rate = _safe_div(engagements, impressions)
        derived["engagementRatePct"] = eng_rate * 100 if eng_rate else None
        derived["costPerEngagement"] = _safe_div(spend, engagements)

    elif objective in ("VIDEO_VIEW", "VIDEO_VIEWS"):
        views = _num(m.get("videoViews"))
        starts = _num(m.get("videoStarts"))
        completions = _num(m.get("videoCompletions"))
        view_rate = _safe_div(views, starts)
        derived["viewRatePct"] = view_rate * 100 if view_rate else None
        comp_rate = _safe_div(completions, views)
        derived["completionRatePct"] = comp_rate * 100 if comp_rate else None
        derived["cpv"] = _safe_div(spend, views)

    return derived


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _ad_set_context(ad_set, raw_metrics, window_days):
    """Build the enriched ad-set dict (no group-dependent fields yet)."""
    objective = ad_set.get("objectiveType")
    impressions = int(_num(raw_metrics.get("impressions")))
    clicks = int(_num(raw_metrics.get("clicks")))
    spend = round(_num(raw_metrics.get("costInLocalCurrency")), 2)
    avg_daily_spend = round(spend / window_days, 2) if window_days else 0

    daily_budget = ad_set.get("dailyBudget")
    daily_budget_utilization = (
        _safe_div(avg_daily_spend, daily_budget) if daily_budget else None
    )

    derived = _derived_metrics(objective, raw_metrics)

    return {
        "id": ad_set.get("id"),
        "urn": ad_set.get("urn"),
        "name": ad_set.get("name"),
        "objectiveType": objective,
        "bidStrategy": ad_set.get("bidStrategy"),
        "bidType": ad_set.get("bidType"),
        "bidAmount": ad_set.get("bidAmount"),
        "bidCap": ad_set.get("bidCap"),
        "optimizationTargetType": ad_set.get("optimizationTargetType"),
        "costType": ad_set.get("costType"),
        "budgetType": ad_set.get("budgetType"),
        "dailyBudget": daily_budget,
        "lifetimeBudget": ad_set.get("lifetimeBudget"),
        "budgetCurrency": ad_set.get("budgetCurrency"),
        "scheduleStart": ad_set.get("scheduleStart"),
        "scheduleEnd": ad_set.get("scheduleEnd"),
        "leadGenFormEnabled": ad_set.get("leadGenFormEnabled"),
        "metrics": {
            "impressions": impressions,
            "clicks": clicks,
            "spend": spend,
            "avgDailySpend": avg_daily_spend,
            **{
                k: int(_num(raw_metrics.get(k)))
                for k in (
                    "oneClickLeads", "oneClickLeadFormOpens", "follows",
                    "landingPageClicks", "externalWebsiteConversions",
                    "externalWebsitePostClickConversions",
                    "externalWebsitePostViewConversions",
                    "totalEngagements", "reactions", "likes",
                    "shares", "comments", "companyPageClicks",
                    "otherEngagements",
                    "videoViews", "videoStarts", "videoCompletions",
                    "videoFirstQuartileCompletions",
                    "videoMidpointCompletions",
                    "videoThirdQuartileCompletions",
                    "fullScreenPlays",
                ) if k in raw_metrics
            },
        },
        "derived": derived,
        "dailyBudgetUtilization": (
            round(daily_budget_utilization, 3)
            if daily_budget_utilization is not None else None
        ),
    }


def _allocate_group_budget_share(group, group_ad_sets):
    """For each ad set missing its own dailyBudget, compute an implied
    expected-daily based on its historical share of total group spend over
    the window. Falls back to even-split if the group had zero spend.

    Returns dict: ad_set_urn -> impliedExpectedDaily
    """
    total_spend = sum(a_ctx["metrics"]["spend"] for a_ctx in group_ad_sets)
    group_daily_budget = group.get("dailyBudget")

    shares = {}
    for a in group_ad_sets:
        spend = a["metrics"]["spend"]
        if total_spend > 0:
            shares[a["urn"]] = spend / total_spend
        else:
            # No spend data — fall back to even split across ad sets
            shares[a["urn"]] = 1.0 / max(1, len(group_ad_sets))

    implied = {}
    for a in group_ad_sets:
        if a.get("dailyBudget"):
            implied[a["urn"]] = None  # uses its own dailyBudget, no implied needed
        elif group_daily_budget:
            implied[a["urn"]] = round(group_daily_budget * shares[a["urn"]], 2)
        else:
            implied[a["urn"]] = None

    return implied, shares


def _pacing_assessment(ad_set, implied_daily, share, window_days):
    """Return a pacing dict with the signals Rule engines need:
       - effectiveDailyBudget: per-ad-set budget, or implied from group
       - budgetSource: 'AD_SET' | 'GROUP_SHARE' | 'NONE'
       - spendVsBudgetPct: avg daily spend / effective daily budget
       - underDelivering: bool — <50% utilization
       - pacingFine: bool — 70-110% utilization
       - overDelivering: bool — >110% utilization
    """
    avg_daily = ad_set["metrics"]["avgDailySpend"]

    if ad_set.get("dailyBudget"):
        effective = ad_set["dailyBudget"]
        source = "AD_SET"
    elif implied_daily:
        effective = implied_daily
        source = "GROUP_SHARE"
    else:
        return {
            "effectiveDailyBudget": None,
            "budgetSource": "NONE",
            "spendShareOfGroup": share,
            "spendVsBudgetPct": None,
            "underDelivering": False,
            "pacingFine": False,
            "overDelivering": False,
        }

    utilization = _safe_div(avg_daily, effective)
    return {
        "effectiveDailyBudget": effective,
        "budgetSource": source,
        "spendShareOfGroup": round(share, 3) if share is not None else None,
        "spendVsBudgetPct": (
            round(utilization * 100, 1) if utilization is not None else None
        ),
        "underDelivering": utilization is not None and utilization < 0.5,
        "pacingFine": utilization is not None and 0.7 <= utilization <= 1.1,
        "overDelivering": utilization is not None and utilization > 1.1,
    }


def build_context(start_date, end_date):
    config.ensure_dirs()
    window_days = max(1, (end_date - start_date).days + 1)

    with open(os.path.join(config.DATA_DIR, "campaign_groups.json")) as f:
        groups = json.load(f)
    with open(os.path.join(config.DATA_DIR, "campaigns.json")) as f:
        ad_sets = json.load(f)
    with open(os.path.join(config.DATA_DIR, "analytics_raw.json")) as f:
        analytics_raw = json.load(f)

    # Group ad sets by parent group
    ad_sets_by_group = {}
    for a in ad_sets:
        ad_sets_by_group.setdefault(a["groupId"], []).append(a)

    enriched_groups = []
    for g in groups:
        ad_set_ctx_list = []
        for a in ad_sets_by_group.get(g["id"], []):
            raw = analytics_raw.get(a["urn"], {}) or {}
            ad_set_ctx_list.append(_ad_set_context(a, raw, window_days))

        implied, shares = _allocate_group_budget_share(g, ad_set_ctx_list)

        for a_ctx in ad_set_ctx_list:
            a_ctx["pacing"] = _pacing_assessment(
                a_ctx,
                implied.get(a_ctx["urn"]),
                shares.get(a_ctx["urn"]),
                window_days,
            )

        group_spend = sum(a["metrics"]["spend"] for a in ad_set_ctx_list)
        group_impressions = sum(a["metrics"]["impressions"] for a in ad_set_ctx_list)
        group_clicks = sum(a["metrics"]["clicks"] for a in ad_set_ctx_list)
        group_avg_daily = round(group_spend / window_days, 2)

        group_pacing = None
        if g.get("dailyBudget"):
            group_util = _safe_div(group_avg_daily, g["dailyBudget"])
            group_pacing = {
                "dailyBudget": g["dailyBudget"],
                "avgDailySpend": group_avg_daily,
                "utilizationPct": (
                    round(group_util * 100, 1) if group_util is not None else None
                ),
                "underDelivering": group_util is not None and group_util < 0.5,
                "pacingFine": group_util is not None and 0.7 <= group_util <= 1.1,
                "overDelivering": group_util is not None and group_util > 1.1,
            }

        enriched_groups.append({
            "id": g["id"],
            "urn": g["urn"],
            "name": g["name"],
            "objectiveType": _dominant_objective(ad_set_ctx_list),
            "budgetType": g["budgetType"],
            "dailyBudget": g.get("dailyBudget"),
            "lifetimeBudget": g.get("lifetimeBudget"),
            "budgetCurrency": g.get("budgetCurrency"),
            "scheduleStart": g.get("scheduleStart"),
            "scheduleEnd": g.get("scheduleEnd"),
            "totals": {
                "spend": round(group_spend, 2),
                "impressions": group_impressions,
                "clicks": group_clicks,
                "adSetCount": len(ad_set_ctx_list),
                "adSetsWithData": sum(
                    1 for a in ad_set_ctx_list if a["metrics"]["impressions"] > 0
                ),
            },
            "groupPacing": group_pacing,
            "adSets": ad_set_ctx_list,
        })

    # Drop groups with no ad sets
    enriched_groups = [g for g in enriched_groups if g["adSets"]]

    payload = {
        "dateRange": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "windowDays": window_days,
        "campaigns": enriched_groups,
    }

    out_path = os.path.join(config.DATA_DIR, "context_payload.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    total_ad_sets = sum(len(g["adSets"]) for g in enriched_groups)
    ad_sets_with_data = sum(
        sum(1 for a in g["adSets"] if a["metrics"]["impressions"] > 0)
        for g in enriched_groups
    )
    print(
        f"Built context: {len(enriched_groups)} campaigns, "
        f"{total_ad_sets} ad sets ({ad_sets_with_data} with data) → {out_path}"
    )
    return payload


def _dominant_objective(ad_set_ctx_list):
    """A campaign's objective is whatever most of its ad sets have. Usually
    all ad sets in a group share the objective so this is straightforward."""
    from collections import Counter
    counts = Counter(a["objectiveType"] for a in ad_set_ctx_list if a.get("objectiveType"))
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    build_context(_parse_date(args.start), _parse_date(args.end))
