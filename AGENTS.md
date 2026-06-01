# Agent instructions — running the LinkedIn Ads bid optimizer

This file tells Claude Code how to operate this project on the user's behalf.
When the user asks to "run the pipeline", "pull recommendations", "check
LinkedIn Ads", or similar, follow this guide.

## Terminology mapping (important — these differ between the user and LinkedIn's API)

| User says | LinkedIn API calls it | Where set in code |
|---|---|---|
| **Campaign** | campaign group (`adCampaignGroups`) | `data/campaign_groups.json` |
| **Ad Set** | campaign (`adCampaigns`) | `data/campaigns.json` |

Always use the **user's terminology** in reports, chat responses, and any
questions back to the user. File paths and internal variable names use
LinkedIn's API names so they stay grep-friendly alongside the LinkedIn docs.

## Project overview

Hierarchical pipeline that:
1. Pulls all active Campaigns (campaign groups) for account **514696610**.
2. For each Campaign, pulls its active Ad Sets (LinkedIn campaigns) with bid
   config (manual bid amount, cost cap, or max delivery) and any ad-set daily
   budget.
3. Pulls adAnalytics per objective bucket (LEAD_GENERATION, WEBSITE_VISIT,
   WEBSITE_CONVERSION, BRAND_AWARENESS, VIDEO_VIEW, ENGAGEMENT, JOB_APPLICANTS).
4. Computes derived metrics (CTR, CPC, CPL, CPM, CPV, LP clicks, etc.) plus a
   pacing signal (uses ad-set daily budget if set; otherwise falls back to
   the ad set's historical share of the Campaign's daily budget).
5. Produces one recommendation per ad set: **ESCALATE / INCREASE / DECREASE /
   HOLD**, plus any anomaly flags (NO DATA, ZERO CONVERSIONS, NO LEADS,
   HIGH CPL, LOW CTR).
6. Renders a hierarchical report: Campaign → Ad Sets (sorted escalate first,
   then by spend desc).

Recommendations only — never applies bids.

## How the user will prompt you

Typical phrasings:
- "Run the LinkedIn pipeline" / "Run the LinkedIn bid agent"
- "Pull bid recommendations for last week" → ask for concrete dates if ambiguous
- "Give me the LinkedIn Ads report for April 1–7"
- "Refresh the LinkedIn token"
- "Re-run just the analyze step"
- "What are the worst-performing ad sets?"

**Always ask for or confirm the date range** — the pipeline requires
explicit `--start` and `--end` dates. Never guess. Common shorthand to resolve
into ISO dates:
- "last week" → last Mon–Sun
- "this week so far" → Mon of current week → today - 1
- "last 7 days" → T-7 to T-1
- "last 30 days" → T-30 to T-1

## The normal run — full pipeline

1. **Check the token first**. Read `token.json` if it exists. If missing, tell
   the user to run `python3 auth.py` in their terminal. You cannot run it
   yourself — it opens a browser for LinkedIn's consent flow. Say:
   > "I need a LinkedIn token. Please run `python3 auth.py` in your terminal,
   > complete the consent screen, and let me know when it's done."
2. **Confirm the date range** unless the user already gave absolute dates.
3. **Run the orchestrator**:
   ```bash
   python3 orchestrator.py --start YYYY-MM-DD --end YYYY-MM-DD \
       --format markdown --account-name "Your Account"
   ```
4. **Report back** with:
   - Date range and window length
   - Portfolio totals: spend, impressions, clicks, leads, conversions
   - Direction counts (escalate / increase / decrease / hold)
   - Anomaly counts (NO DATA, ZERO CONVERSIONS, NO LEADS, HIGH CPL, LOW CTR)
   - **Every escalation** with its ad set name, its Campaign, and the reason
   - Top 3 increases and top 3 decreases by 7-day spend, with reasons
   - Path to the report file in `output/`

Do NOT paste the full markdown report into chat — summarize and point to the
file instead.

## Running individual steps

Each script reads from `data/` produced by the previous step. If an upstream
step's output is fresh, you don't need to re-run it.

| Step | Script | Reads | Writes |
|---|---|---|---|
| 1 | `get_campaign_groups.py` | LinkedIn API | `data/campaign_groups.json` |
| 2 | `get_campaigns.py` | `data/campaign_groups.json` | `data/campaigns.json` |
| 3 | `get_analytics.py --start --end` | `data/campaigns.json` | `data/analytics_raw.json` |
| 4 | `build_context.py --start --end` | campaigns + analytics | `data/context_payload.json` |
| 5 | `analyze.py` | `data/context_payload.json` | `data/recommendations.json` |
| 6 | `format_report.py --format <fmt>` | `data/recommendations.json` | `output/report.*` |
| 7 | `generate_html_report.py --account-name <name>` | `data/recommendations.json` + `output/report.md` | `output/report.html` |

Common "just redo the analysis" path (after you've already pulled the data):
```bash
python3 analyze.py && python3 format_report.py --format markdown && \
    python3 generate_html_report.py --account-name "Your Account"
```

**The HTML dashboard (`output/report.html`) is the primary deliverable** for a
real user — open it in a browser. It has sortable/filterable tables, charts
for Campaign spend + recommendation split, and every ad set with its bid
change, reason, metrics, and anomaly flags. The markdown report is a
plain-text companion used both for review and as the prose source embedded
at the bottom of the HTML.

## The decision framework (for answering user questions about the output)

Per ad set, in this order (first match wins):

1. **NO DATA** → HOLD — zero impressions in window; bid change can't help.
2. **Anomaly blockers** → ESCALATE — if any of `ZERO_CONVERSIONS`,
   `NO_LEADS`, `HIGH_CPL` fired, a bid change won't fix the root cause
   (creative, audience, landing page, or tracking).
3. **MAX_DELIVERY strategy** → HOLD or ESCALATE (if under-delivering) — no
   bid knob to adjust.
4. **Objective-aware rules** combining pacing + efficiency:
   - **Lead Gen**: under-delivering + leads present + CTR ≥ 0.4% → INCREASE 20%;
     under-delivering + CTR ≥ 0.4% → INCREASE 10%; over-delivering + high CPC +
     low CTR → DECREASE 15%.
   - **Website objectives**: high CPC (>$15) + low CTR (<0.4%) → DECREASE 20%;
     under-delivering + CTR ≥ 0.4% → INCREASE 10%.
   - **Brand / Video**: high CPM (>$50) + low daily impressions (<1000) →
     DECREASE 15%.
5. **Default** → HOLD with a short performance summary.

**Post-rule**: floors ($3.50 CPC / $2.00 CPM / $0.01 CPV), ±25% cap, round to 2
decimals.

**Pacing signal sources**:
- If the ad set has its own `dailyBudget` → use that directly.
- Else → multiply the parent Campaign's `dailyBudget` by the ad set's share
  of that Campaign's total spend in the window (historical spend share).
- Else (no budget info anywhere) → skip pacing; rely on objective metrics only.

**Confidence**: HIGH (≥10k impressions), MEDIUM (≥1k), LOW (<1k).

## Anomaly thresholds

| Flag | Trigger |
|---|---|
| ⚪ NO DATA | zero impressions (any objective) |
| 🔴 ZERO CONVERSIONS | $500+ spend, zero leads AND zero website conversions (conversion-relevant objectives) |
| 🟡 NO LEADS | lead gen ad set, $300+ spend, 0 one-click leads |
| 🔴 HIGH CPL | CPL > 2× portfolio avg lead-gen CPL |
| 🟡 LOW CTR | ≥5k impressions, CTR < 0.2% (skipped for Brand Awareness — CTR isn't the signal) |

## Error handling

| Error | What to do |
|---|---|
| `AUTH_FAILURE` or 401 | Tell user to run `python3 auth.py`. Don't try it yourself. |
| `NO_ACTIVE_GROUPS` | Verify `ACCOUNT_ID` in `config.py` is `514696610`. If it is, the account really has no active campaigns. |
| 429 after retries | Wait 5–10 minutes, retry. |
| Analytics `400 "not present in schema"` | Field name drift from LinkedIn. Check the reference project `../linkedinreportingagent/get_metrics.py` for the current canonical field names. |
| Analytics `400 "Invalid value for param; ... dateRange"` | URL is double-encoding parens. Verify `get_analytics.py` still uses `_build_url` with the raw safe-chars list. |
| Per-Campaign campaign fetch failure | Logged to `data/failed_groups.json`; other Campaigns still process. Report which failed. |

## What NOT to do

- **Do not run `auth.py`.** It needs the user's browser.
- **Do not apply recommendations to LinkedIn.** This tool is recommendation-only.
  If the user asks to apply, confirm explicitly before building any auto-apply
  code (requires `rw_ads` scope and PATCH calls).
- **Do not modify anything under `../linkedinreportingagent/`.** User has
  explicitly asked to keep it untouched.
- **Do not edit `analyze.py` thresholds** without the user asking. The rules
  reflect explicit product calls.
- **Do not post reports to Slack, email, or any external destination** unless
  the user gives you a specific destination and authorizes it.
- **Do not commit `token.json`, `config.py`, or anything in `data/`**.

## Expected outputs after a successful run

```
data/
  campaign_groups.json   # active Campaigns (LinkedIn groups)
  campaigns.json         # active Ad Sets (LinkedIn campaigns) with bid config
  analytics_raw.json     # raw LinkedIn response per ad set URN
  context_payload.json   # hierarchical Campaign → Ad Set with derived metrics
  recommendations.json   # context payload + assessment + anomaly flags per ad set
output/
  report.md   (or report_slack.txt / report_email.html)
  report.html          # interactive dashboard (generated after report.md)
```

All files in `data/` are overwritten per run. Safe to delete anytime.

## LinkedIn API quirks (learned 2026-04 — keep in mind)

- **Pagination**: uses `metadata.nextPageToken`, not `start=N` offsets (start is
  ignored). The code handles this.
- **Search filter syntax**: old `search.status.values[0]=ACTIVE` is rejected;
  use Rest.li 2.0 tuples like `search=(status:(values:List(ACTIVE)))` OR fetch
  unfiltered and filter client-side (fallback path already in place).
- **`dateRange` parameter**: must be written literally as
  `(start:(year:YYYY,month:M,day:D),end:(...))` with unencoded parens. The
  `get_analytics.py` manual URL builder handles this.
- **`campaigns` parameter**: `List(urn%3Ali%3AsponsoredCampaign%3AID)` —
  URN colons percent-encoded, List parens/commas raw.
- **Budget fields** (`dailyBudget`, `totalBudget`, `unitCost.amount`): some
  responses return plain strings (`"30"`), some return `{amount, currencyCode}`
  dicts. `parse_amount` handles both.
- **`runSchedule.start` / `.end`**: now Unix millisecond timestamps, not
  `{year, month, day}` dicts. `parse_schedule_date` handles both.
- **Field name rename**: `bidOptimizationTarget` (old) → `optimizationTargetType`
  (new). Resolver reads both.
- **API version**: older versions (`202501`, `202504`) are sunset. Use a recent
  `YYYYMM` version like `202604`. Configurable in `config.py`.

## Self-tests

Before running a real pipeline (especially after code changes), run:

```bash
python3 selftest.py
```

This validates the deterministic pieces (parse functions, context build,
analyze, report format) against synthetic fixtures — no LinkedIn API calls.
It runs in seconds and catches regressions in the decision logic.

If the self-test fails, don't run the pipeline — fix the failure first.

## Config & version check

```bash
python3 config.py
```

Prints the project paths, account ID, the API version that will be used
(with auto-fallback to older versions on `NONEXISTENT_VERSION` errors), and
token status. Useful when the user's token might be expired.

## Quick commands cheatsheet

```bash
# Full pipeline
python3 orchestrator.py --start 2026-04-13 --end 2026-04-19 --format markdown \
    --account-name "Your Account"

# Re-analyze + re-format (data already pulled)
python3 analyze.py && python3 format_report.py --format markdown \
    --account-name "Your Account"

# Re-format only (e.g. switch to email)
python3 format_report.py --format email --account-name "Your Account"

# Validate logic (no API)
python3 selftest.py

# Print config/version/token status
python3 config.py

# OAuth (user only — opens browser)
python3 auth.py
```
