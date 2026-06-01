# LinkedIn Ads Bid Optimization Engine

Deterministic Python pipeline that pulls LinkedIn Marketing API data, computes
delivery and pacing metrics, and produces bid adjustment recommendations grouped
by action priority. **Recommendations only — no auto-apply.**

## Prerequisites

- Python 3.10+
- A LinkedIn Developer App with Marketing Developer Platform access
- An ad account ID (numeric) for the account you want to analyze
- OAuth redirect URI `http://localhost:8000/callback` registered with the app

## Install

```bash
pip install -r requirements.txt
```

## Configure

Copy the example config and fill in your credentials:

```bash
cp config.example.py config.py
```

Then open `config.py` and set:

- `CLIENT_ID` and `CLIENT_SECRET` — from your LinkedIn Developer App
- `ACCOUNT_ID` — your numeric LinkedIn ad account ID (the digits after
  `urn:li:sponsoredAccount:` or from Campaign Manager URL)

`config.py` and `token.json` are gitignored — they hold secrets and should
never be committed.

## Authorize

Run the OAuth flow once. This opens a browser to LinkedIn, captures the
callback on `localhost:8000/callback`, exchanges the code for an access token,
and writes it to `token.json`:

```bash
python auth.py
```

Scopes requested: `r_ads r_ads_reporting rw_ads`.

Tokens typically last 60 days. Re-run `auth.py` when expired.

## Run the full pipeline

```bash
python orchestrator.py \
    --format markdown \
    --account-name "Acme Corp" \
    --date-range "April 14–20, 2026"
```

Options:

- `--start YYYY-MM-DD --end YYYY-MM-DD` — analytics date range (default T-7 to T-1)
- `--format slack|email|markdown` — report format (default markdown)
- `--account-name "Name"` — shown in report header
- `--date-range "April 14–20, 2026"` — shown in report header (default auto-computed)

## Run scripts individually

Each script is standalone and reads from `data/` produced by the previous step.

```bash
python get_campaign_groups.py     # -> data/campaign_groups.json
python get_campaigns.py           # -> data/campaigns.json
python get_analytics.py           # -> data/analytics_raw.json
python build_context.py           # -> data/context_payload.json
python analyze.py                 # -> data/recommendations.json
python format_report.py --format markdown
```

## Outputs

- `data/campaign_groups.json` — active groups with resolved budget type
- `data/campaigns.json` — flat campaign list with resolved budget + bid strategy + group context
- `data/analytics_raw.json` — campaignUrn → list of daily rows, normalized to standard schema per objective bucket
- `data/context_payload.json` — enriched campaigns with 7-day aggregates and derived metrics
- `data/recommendations.json` — one recommendation per campaign
- `output/report.md` (or `report_slack.txt` / `report_email.html`)

## Decision framework (summary)

1. **Data sufficiency** — `daysWithData < 3` → HOLD with LOW confidence
2. **Bid strategy gate**:
   - `MAX_DELIVERY` → HOLD (or ESCALATE if underdelivering with daily budget)
   - `MANUAL_BID`, `ENHANCED_CPC` → adjust `bidAmount`
   - `COST_CAP`, `TARGET_COST` → adjust `bidCap`
3. **Budget constraint gate**:
   - Projected to exhaust lifetime budget within 3 days → HOLD
   - `lifetimePacingRatio > 1.3` → block INCREASE (convert to HOLD)
   - `INHERITED` budget with group pressure → caveat on INCREASE
4. **Performance rules (first match wins)**:
   1. ESCALATE — conversion objectives with zero conversions on >$300 spend
   2. DECREASE — budget exhausting ≥ 4/7 days + CTR < 0.4%
   3. INCREASE (20%) — underdelivering ≥ 3/7 days + CTR > 0.6% + conversions
   4. INCREASE (10%) — underdelivering ≥ 3/7 days + CTR ≥ 0.4%
   5. DECREASE — CPC > $15 + CTR < 0.4% on website objectives
   6. DECREASE — CPM > $50 + < 1000 daily impressions on brand/video/engagement
   7. HOLD — default
5. **Post-rule adjustments**: floor ($3.50 CPC / $2.00 CPM / $0.01 CPV), cap at ±25%, round to 2 decimals
6. **Confidence**: HIGH (≥5d), MEDIUM (3-4d), LOW (<3d)

## Files

| File | Purpose |
|---|---|
| `config.py` | Credentials, endpoints, paths, header helpers |
| `api_utils.py` | Shared API GET with retry, money parsing |
| `auth.py` | OAuth 2.0 3-legged flow, saves `token.json` |
| `get_campaign_groups.py` | Script 1 |
| `get_campaigns.py` | Script 2 |
| `get_analytics.py` | Script 3 |
| `build_context.py` | Script 4 |
| `analyze.py` | Script 5 (decision engine) |
| `format_report.py` | Script 6 |
| `orchestrator.py` | Runs all steps in order |

## Error handling

- `401` → halts immediately with `AUTH_FAILURE` message; run `auth.py`
- `429` → retries after 60s, max 3 attempts per call
- Empty campaign groups → halts with `NO_ACTIVE_GROUPS`
- Per-group campaign fetch failure → logged to `data/failed_groups.json`, pipeline continues
- Analytics batch failure → logged, campaigns in that batch get zero rows (will HOLD on `daysWithData < 3`)
