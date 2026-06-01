"""
Orchestrator — runs the full LinkedIn Ads bid optimization pipeline end to end.

Terminology:
  "Campaign" (user-facing) = LinkedIn campaign group
  "Ad Set"   (user-facing) = LinkedIn campaign

Required args:
  --start YYYY-MM-DD
  --end   YYYY-MM-DD

Steps:
  1. get_campaign_groups    — data/campaign_groups.json (active Campaigns)
  2. get_campaigns          — data/campaigns.json (active Ad Sets per Campaign)
  3. get_analytics          — data/analytics_raw.json (by ad set URN)
  4. build_context          — data/context_payload.json (hierarchical)
  5. analyze                — data/recommendations.json
  6. format_report          — output/report.md (or slack/email)
  7. generate_html_report   — output/report.html (interactive dashboard)
"""
import argparse
import sys
import time
from datetime import datetime

import config
from api_utils import AuthFailure
from build_context import build_context
from format_report import format_report
from generate_html_report import generate_html
from get_analytics import fetch_analytics
from get_campaign_groups import fetch_campaign_groups
from get_campaigns import fetch_campaigns
from analyze import analyze


def _log_step(name):
    print(f"\n{'=' * 72}")
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {name}")
    print("=" * 72)


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def run(start_date, end_date, fmt, account_name):
    config.ensure_dirs()
    t0 = time.time()

    try:
        _log_step("Step 1 — Fetch active Campaigns (LinkedIn campaign groups)")
        groups = fetch_campaign_groups()
        if not groups:
            print("NO_ACTIVE_GROUPS — halting.")
            sys.exit(1)

        _log_step("Step 2 — Fetch active Ad Sets (LinkedIn campaigns) per Campaign")
        ad_sets = fetch_campaigns()
        if not ad_sets:
            print("No active ad sets found — halting.")
            sys.exit(1)

        _log_step(f"Step 3 — Fetch analytics ({start_date} → {end_date})")
        fetch_analytics(start_date, end_date)

        _log_step("Step 4 — Build hierarchical context")
        build_context(start_date, end_date)

        _log_step("Step 5 — Analyze and recommend")
        analyze()

        _log_step(f"Step 6 — Format report ({fmt})")
        format_report(fmt, account_name)

        _log_step("Step 7 — Generate HTML dashboard")
        generate_html(account_name)

    except AuthFailure as exc:
        print(f"\nAUTH FAILURE: {exc}", file=sys.stderr)
        print("Run auth.py to refresh the token.", file=sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"\nPIPELINE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\nPipeline complete in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--format",
        choices=["markdown", "slack", "email"],
        default="markdown",
    )
    parser.add_argument("--account-name", default="LinkedIn Ads Account")
    args = parser.parse_args()

    start_d = _parse_date(args.start)
    end_d = _parse_date(args.end)
    if end_d < start_d:
        print("ERROR: --end must be >= --start", file=sys.stderr)
        sys.exit(1)

    run(start_d, end_d, args.format, args.account_name)
