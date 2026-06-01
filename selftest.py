"""
Self-test — validates preflight + deterministic logic without hitting the
LinkedIn API. Run this before a real pipeline run to catch config issues
or parse-function regressions.

Usage:
    python3 selftest.py
"""
import json
import os
import sys
import tempfile
from datetime import date

import config
from api_utils import parse_amount, parse_currency, parse_schedule_date
from build_context import build_context
from analyze import analyze
from format_report import format_report


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def test_parse_amount():
    print("\nparse_amount:")
    ok = True
    ok &= check("None → None", parse_amount(None) is None)
    ok &= check("empty string → None", parse_amount("") is None)
    ok &= check("'30' → 30.0", parse_amount("30") == 30.0)
    ok &= check("30 → 30.0", parse_amount(30) == 30.0)
    ok &= check("30.5 → 30.5", parse_amount(30.5) == 30.5)
    ok &= check(
        "{amount: '12.50'} → 12.5",
        parse_amount({"amount": "12.50", "currencyCode": "USD"}) == 12.5,
    )
    ok &= check("{amount: None} → None", parse_amount({"amount": None}) is None)
    ok &= check("garbage dict → None", parse_amount({"foo": "bar"}) is None)
    ok &= check("bogus string → None", parse_amount("thirty") is None)
    return ok


def test_parse_currency():
    print("\nparse_currency:")
    ok = True
    ok &= check(
        "dict with currencyCode",
        parse_currency({"amount": "1", "currencyCode": "USD"}) == "USD",
    )
    ok &= check("plain string → None", parse_currency("30") is None)
    ok &= check("None → None", parse_currency(None) is None)
    return ok


def test_parse_schedule_date():
    print("\nparse_schedule_date:")
    ok = True
    ok &= check("None → None", parse_schedule_date(None) is None)
    ok &= check(
        "{year:2026,month:4,day:13}",
        parse_schedule_date({"year": 2026, "month": 4, "day": 13}) == "2026-04-13",
    )
    # Unix ms — known value: 2024-01-01 UTC = 1704067200000
    ok &= check(
        "1704067200000 (Unix ms) → 2024-01-01",
        parse_schedule_date(1704067200000) == "2024-01-01",
    )
    ok &= check(
        "missing day → None",
        parse_schedule_date({"year": 2026, "month": 4}) is None,
    )
    ok &= check("garbage → None", parse_schedule_date("not-a-date") is None)
    return ok


def test_end_to_end_deterministic():
    """Feed fake campaign_groups + campaigns + analytics into the non-API
    pipeline stages and make sure they produce a report."""
    print("\nEnd-to-end (no API):")

    with tempfile.TemporaryDirectory() as tmp:
        # Hot-swap DATA_DIR / OUTPUT_DIR for this test
        orig_data = config.DATA_DIR
        orig_out = config.OUTPUT_DIR
        config.DATA_DIR = os.path.join(tmp, "data")
        config.OUTPUT_DIR = os.path.join(tmp, "output")
        os.makedirs(config.DATA_DIR, exist_ok=True)
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        try:
            # 1 campaign (group) with 2 ad sets
            groups = [{
                "id": "G1",
                "urn": "urn:li:sponsoredCampaignGroup:G1",
                "name": "Test Campaign",
                "status": "ACTIVE",
                "budgetType": "DAILY",
                "dailyBudget": 100.0,
                "lifetimeBudget": None,
                "budgetCurrency": "USD",
                "scheduleStart": "2026-04-01",
                "scheduleEnd": None,
                "allowedCampaignTypes": [],
            }]
            ad_sets = [
                {
                    "id": "A1", "urn": "urn:li:sponsoredCampaign:A1",
                    "name": "AdSet 1 (high perf)", "status": "ACTIVE",
                    "objectiveType": "LEAD_GENERATION", "type": "SPONSORED_UPDATES",
                    "locale": {"country": "US", "language": "en"},
                    "pacingStrategy": "LIFETIME", "costType": "CPC",
                    "optimizationTargetType": "ENHANCED_CONVERSION",
                    "budgetType": "INHERITED", "dailyBudget": None,
                    "lifetimeBudget": None, "budgetCurrency": None,
                    "bidStrategy": "TARGET_COST", "bidType": "CPC",
                    "bidAmount": None, "bidCap": 10.0,
                    "scheduleStart": "2026-04-01", "scheduleEnd": None,
                    "leadGenFormEnabled": True,
                    "groupId": "G1", "groupName": "Test Campaign",
                    "groupBudgetType": "DAILY", "groupDailyBudget": 100.0,
                    "groupLifetimeBudget": None, "groupBudgetCurrency": "USD",
                    "groupScheduleEnd": None,
                },
                {
                    "id": "A2", "urn": "urn:li:sponsoredCampaign:A2",
                    "name": "AdSet 2 (zero data)", "status": "ACTIVE",
                    "objectiveType": "LEAD_GENERATION", "type": "SPONSORED_UPDATES",
                    "locale": {"country": "US", "language": "en"},
                    "pacingStrategy": "LIFETIME", "costType": "CPC",
                    "optimizationTargetType": "MAX_CLICK",
                    "budgetType": "INHERITED", "dailyBudget": None,
                    "lifetimeBudget": None, "budgetCurrency": None,
                    "bidStrategy": "MANUAL_BID", "bidType": "CPC",
                    "bidAmount": 5.0, "bidCap": None,
                    "scheduleStart": "2026-04-01", "scheduleEnd": None,
                    "leadGenFormEnabled": True,
                    "groupId": "G1", "groupName": "Test Campaign",
                    "groupBudgetType": "DAILY", "groupDailyBudget": 100.0,
                    "groupLifetimeBudget": None, "groupBudgetCurrency": "USD",
                    "groupScheduleEnd": None,
                },
            ]
            analytics = {
                "urn:li:sponsoredCampaign:A1": {
                    "pivotValues": ["urn:li:sponsoredCampaign:A1"],
                    "impressions": 10000, "clicks": 80,
                    "costInLocalCurrency": "200",
                    "oneClickLeads": 10, "oneClickLeadFormOpens": 30,
                    "landingPageClicks": 70, "follows": 0,
                },
                "urn:li:sponsoredCampaign:A2": {},
            }

            with open(os.path.join(config.DATA_DIR, "campaign_groups.json"), "w") as f:
                json.dump(groups, f)
            with open(os.path.join(config.DATA_DIR, "campaigns.json"), "w") as f:
                json.dump(ad_sets, f)
            with open(os.path.join(config.DATA_DIR, "analytics_raw.json"), "w") as f:
                json.dump(analytics, f)

            payload = build_context(date(2026, 4, 6), date(2026, 4, 19))
            check("build_context produces 1 campaign", len(payload["campaigns"]) == 1)
            check(
                "build_context produces 2 ad sets",
                len(payload["campaigns"][0]["adSets"]) == 2,
            )

            recs = analyze()
            check(
                "analyze produced direction per ad set",
                all("direction" in a.get("assessment", {})
                    for g in recs["campaigns"] for a in g["adSets"])
            )
            counts = recs["portfolio"]["directionCounts"]
            # A1 has strong signal (10 leads, CTR 0.8%) + under-pacing (shared group budget)
            # A2 has no data → HOLD NO_DATA
            check(
                f"A1 should be INCREASE or HOLD, got directions {counts}",
                ("INCREASE" in counts or "HOLD" in counts) and counts.get("ESCALATE", 0) == 0,
            )

            format_report("markdown", "Self-Test")
            md_path = os.path.join(config.OUTPUT_DIR, "report.md")
            check(
                "format_report produced report.md",
                os.path.exists(md_path) and os.path.getsize(md_path) > 500,
            )
            return True
        finally:
            config.DATA_DIR = orig_data
            config.OUTPUT_DIR = orig_out


def main():
    print("Self-test — deterministic pipeline validation")
    print("=" * 60)
    results = [
        test_parse_amount(),
        test_parse_currency(),
        test_parse_schedule_date(),
        test_end_to_end_deterministic(),
    ]
    print()
    if all(results):
        print("All self-tests passed.")
        return 0
    print("Some self-tests FAILED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
