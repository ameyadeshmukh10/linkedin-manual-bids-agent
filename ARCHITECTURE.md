# Architecture

A detailed walkthrough of what this agent does, how the pipeline is wired,
and the logic behind every bid recommendation it produces.

---

## 1. What this agent is (and what it isn't)

This is a **deterministic, read-only LinkedIn Ads bid optimization engine**.

- **Read-only:** it produces recommendations. It never writes back to
  LinkedIn — no API mutations, no auto-applied bid changes. A human reviews
  the report and decides what to act on.
- **Deterministic:** no LLM, no ML model, no probabilistic scoring. Given
  the same inputs (account state + analytics window) it produces the
  same output every time. All decisions come from a fixed rule hierarchy
  expressed in plain Python.
- **Per-ad-set:** each recommendation is scoped to one LinkedIn ad set
  (the bid lives there, not on the group). Recommendations include a
  direction (`INCREASE` / `DECREASE` / `HOLD` / `ESCALATE`), a target bid
  value when applicable, a confidence level, and a human-readable reason.
- **Bounded by guard rails:** any bid change is floored at platform
  minimums (`$3.50 CPC`, `$2.00 CPM`, `$0.01 CPV`) and capped at ±25% of
  the current bid. Anomalies that bidding can't fix (zero conversions on
  $500+ spend, broken tracking, etc.) are routed to `ESCALATE` instead of
  a bid nudge.

## 2. Terminology — Campaign vs. Ad Set

LinkedIn's API names mismatch the UI vocabulary most marketers use. The
codebase consistently uses the **user-facing** vocabulary, so this is the
mapping you need to keep straight:

| User-facing term  | LinkedIn API term         | URN prefix                            |
|-------------------|---------------------------|---------------------------------------|
| **Campaign**      | `campaignGroup`           | `urn:li:sponsoredCampaignGroup:<id>`  |
| **Ad Set**        | `campaign`                | `urn:li:sponsoredCampaign:<id>`       |

So when the code says "campaigns" in user-visible output it means
**groups**, and when it says "ad sets" it means LinkedIn API
**campaigns**. The endpoint names (`/adCampaignGroups`, `/adCampaigns`)
follow the API vocabulary; the JSON keys (`campaigns: [...]`, `adSets:
[...]`) follow the user vocabulary.

## 3. Pipeline at a glance

The orchestrator runs seven sequential steps. Each one reads what the
previous step wrote, so any step can also be run standalone for
debugging.

```
                ┌───────────────────────┐
                │  auth.py (one-time)   │  → token.json
                └───────────┬───────────┘
                            │
                            ▼
 ┌────────────────────────────────────────────────────────────┐
 │ Step 1  get_campaign_groups.py                             │
 │   GET /adAccounts/{id}/adCampaignGroups (paginated)        │
 │   → data/campaign_groups.json   (ACTIVE groups only)       │
 ├────────────────────────────────────────────────────────────┤
 │ Step 2  get_campaigns.py                                   │
 │   GET /adAccounts/{id}/adCampaigns (one paginated pass)    │
 │   Resolve bid strategy + budget shape per ad set           │
 │   → data/campaigns.json         (ACTIVE ad sets, flat)     │
 ├────────────────────────────────────────────────────────────┤
 │ Step 3  get_analytics.py                                   │
 │   POST batches to /adAnalytics, one bucket per objective   │
 │   timeGranularity=ALL over [start, end] window             │
 │   → data/analytics_raw.json     ({adSetUrn: {metrics…}})   │
 ├────────────────────────────────────────────────────────────┤
 │ Step 4  build_context.py                                   │
 │   Join groups + ad sets + analytics; compute derived       │
 │   metrics + pacing per ad set                              │
 │   → data/context_payload.json   (hierarchical)             │
 ├────────────────────────────────────────────────────────────┤
 │ Step 5  analyze.py                                         │
 │   Anomaly detection + bid recommendation per ad set        │
 │   → data/recommendations.json   (context + assessments)    │
 ├────────────────────────────────────────────────────────────┤
 │ Step 6  format_report.py                                   │
 │   → output/report.md  |  report_slack.txt  |  report_email.html │
 ├────────────────────────────────────────────────────────────┤
 │ Step 7  generate_html_report.py                            │
 │   → output/report.html  (interactive dashboard)            │
 └────────────────────────────────────────────────────────────┘
```

`orchestrator.py` is the glue. It parses `--start` / `--end` / `--format`
/ `--account-name`, calls each step in order, and converts
`AuthFailure` into a "run `auth.py`" message with exit code 2.

## 4. Authentication

`auth.py` implements LinkedIn's 3-legged OAuth 2.0 flow:

1. Spins up a local HTTP server on `localhost:8000`.
2. Opens the browser to LinkedIn's authorization URL with scopes
   `r_ads r_ads_reporting rw_ads` (the `rw_ads` scope is requested for
   parity with the dashboard scope set, but **the agent never makes
   write calls** — it's read-only by design).
3. Captures the `code` from the redirect, exchanges it for an
   `access_token` + `refresh_token` against
   `https://www.linkedin.com/oauth/v2/accessToken`.
4. Writes the token blob to `token.json`, stamping it with
   `issued_at_epoch` and `expires_at_epoch` so `config.py` can warn
   about imminent expiry on subsequent runs.

Tokens last ~60 days. `config.preflight_check()` warns when the
remaining TTL drops below 3 days and hard-fails when it's expired.

Every API call's `Authorization: Bearer …` header is built fresh from
`token.json` via `config.get_headers()`, so re-running `auth.py`
mid-pipeline-day is enough to recover from a token expiry.

## 5. The LinkedIn API layer

`api_utils.py` is the only place that talks to LinkedIn directly
(except `get_analytics.py`, which builds its own URLs to preserve
Rest.li tuple encoding — see §6.3).

### 5.1 `get_with_retry()` — what it handles for you

| Situation                       | Behavior                                           |
|---------------------------------|----------------------------------------------------|
| `200 OK`                        | Cache the working API version, return JSON         |
| `401 Unauthorized`              | Raise `AuthFailure` — pipeline halts immediately   |
| `426 NONEXISTENT_VERSION`       | Step back one month in the version candidate list and retry |
| `429 Too Many Requests`         | Sleep 60s, retry up to 3 times                     |
| `ConnectionError` / `Timeout`   | Exponential backoff (1s, 2s), retry up to 3 times  |
| Any other `4xx`/`5xx`           | Raise `RuntimeError` with LinkedIn's response body |

### 5.2 API version auto-discovery

LinkedIn versions are dated (`YYYYMM`) and sunset on a rolling ~12-month
schedule. Hardcoding a version means the agent breaks one day per year.
Instead, `config._candidate_versions()` generates the current month and
the prior 13 — `get_with_retry` walks that list and caches the first
version that LinkedIn accepts. An env var `LINKEDIN_API_VERSION` can
override the search.

### 5.3 Tolerant field parsers

LinkedIn's response shapes have drifted across versions. `api_utils`
exposes parsers that accept every shape ever observed:

- **`parse_amount`** — accepts `None`, `""`, raw numbers, raw numeric
  strings, `{amount: "12.50", currencyCode: "USD"}`, and `{amount:
  12.50}`. Returns `float` or `None`. Never throws.
- **`parse_currency`** — pulls `currencyCode` from the dict form when
  present, else `None`.
- **`parse_schedule_date`** — accepts `{day, month, year}` dicts (old
  shape) and Unix-millisecond integers (new shape). Returns ISO
  `YYYY-MM-DD` strings. Never throws.

This is why every normalization step in `get_campaign_groups.py` and
`get_campaigns.py` calls these helpers instead of touching raw JSON.

## 6. Pipeline steps in detail

### 6.1 Step 1 — `get_campaign_groups.py`

- Endpoint: `GET /adAccounts/{ACCOUNT_ID}/adCampaignGroups`.
- Paginates via `metadata.nextPageToken` (LinkedIn silently ignores
  `start`/`count` on newer versions). Safety cap at 200 pages.
- Filters to `status == "ACTIVE"` client-side (server-side `search`
  filters are inconsistent across versions).
- Resolves each group's `budgetType` to one of `DAILY` / `LIFETIME` /
  `UNCAPPED` based on which budget field is populated.
- Halts with exit code 1 if zero active groups remain.

### 6.2 Step 2 — `get_campaigns.py`

- Endpoint: `GET /adAccounts/{ACCOUNT_ID}/adCampaigns`.
- Performs **one** paginated pass for the entire account, then buckets
  ad sets by their `campaignGroup` URN client-side. (A previous version
  fetched per-group; the per-group filter is broken across versions and
  the unfiltered response is already paginated, so a single pass is
  both correct and ~N× faster.)
- For each ad set, resolves two derived shapes used everywhere
  downstream:

  **Budget shape** (`_resolve_campaign_budget`):

  | `dailyBudget` | `totalBudget` | Resolved `budgetType`     |
  |---------------|---------------|---------------------------|
  | set           | set           | `DAILY_AND_LIFETIME`      |
  | set           | unset         | `DAILY`                   |
  | unset         | set           | `LIFETIME`                |
  | unset         | unset         | `INHERITED` (group budget)|

  **Bid strategy** (`_resolve_bid_strategy`): maps LinkedIn's
  `optimizationTargetType` enum (or its legacy alias
  `bidOptimizationTarget`) plus the presence/absence of `unitCost` into
  the agent's internal taxonomy:

  | LinkedIn target                              | Internal `bidStrategy` | `bidType` |
  |----------------------------------------------|------------------------|-----------|
  | no `unitCost`                                | `MAX_DELIVERY`         | from `costType` |
  | `MAX_CLICK` / `MAX_LEAD` / `MAX_QUALIFIED_LEAD` / `MAX_CONVERSION` | `MANUAL_BID` (`bidAmount`)        | `CPC` |
  | `MAX_IMPRESSION` / `MAX_REACH`               | `MANUAL_BID` (`bidAmount`)        | `CPM` |
  | `MAX_VIDEO_VIEW`                             | `MANUAL_BID` (`bidAmount`)        | `CPV` |
  | `CAP_COST_AND_MAXIMIZE_{CLICKS,LEADS,QL,CONVERSIONS}` / `TARGET_COST_PER_CLICK` | `COST_CAP` (`bidCap`) | `CPC` |
  | `CAP_COST_AND_MAXIMIZE_{IMPRESSIONS,REACH}` / `TARGET_COST_PER_IMPRESSION` | `COST_CAP` (`bidCap`) | `CPM` |
  | `CAP_COST_AND_MAXIMIZE_VIDEO_VIEWS`          | `COST_CAP` (`bidCap`)             | `CPV` |
  | `ENHANCED_CONVERSION`                        | `TARGET_COST` (`bidCap`)          | `CPC` |
  | `ENHANCED_CPC`                               | `ENHANCED_CPC` (`bidAmount`)      | `CPC` |
  | unknown target with `unitCost` set           | `MANUAL_BID` (`bidAmount`)        | from `costType` |

  This taxonomy is what drives the recommendation logic in Step 5 — every
  rule branches on `bidStrategy` and `bidType`, not on LinkedIn's raw
  enum.

- Parent group context (`groupId`, `groupName`, group budget, group
  `scheduleEnd`) is denormalized onto every ad set record so downstream
  steps don't need to re-join.

### 6.3 Step 3 — `get_analytics.py`

- Endpoint: `GET /adAnalytics` (Rest.li 2.0 finder `q=analytics`).
- Buckets ad sets by `objectiveType`. Each objective gets a tailored
  field list (e.g., `oneClickLeads` only requested for
  `LEAD_GENERATION`, `videoViews`/`videoCompletions` only for
  `VIDEO_VIEW(S)`). This keeps each request under LinkedIn's hard
  20-field cap.
- Batches **10 ad sets per call**. When a bucket's field list still
  exceeds 20 fields, `_split_fields` splits it into chunks — each chunk
  keeps `pivotValues` so response rows can still be mapped back to an
  ad set URN.
- Uses `timeGranularity=ALL` with the user-supplied `[start, end]`
  window, so each ad set returns exactly one aggregate row.
- URL is built manually via `_build_url` because Rest.li expects
  literal parentheses, colons, and commas in the `dateRange` and
  `campaigns` tuple syntax — `requests`' default URL encoding would
  break it.
- 429 → sleep 60s, retry up to 3 attempts. 401 → `AuthFailure`. Any
  other failure on one batch is logged and skipped; the remaining
  batches still run. Ad sets that returned no row land in the output
  with an empty dict.

### 6.4 Step 4 — `build_context.py`

Joins the three previous outputs and computes everything the rule
engine needs.

For every ad set:

- **Window aggregates** — `impressions`, `clicks`, `spend`,
  `avgDailySpend = spend / windowDays`, plus the objective-specific
  metric fields actually returned.

- **Derived metrics** — `ctrPct`, `cpc`, `cpm` always; then objective-
  specific extras:
  - `LEAD_GENERATION` → `cpl`, `costPerExtConv`, `formOpenRatePct`,
    `leadConvRatePct`
  - `WEBSITE_VISIT` / `WEBSITE_CONVERSION` / `JOB_APPLICANTS` →
    `costPerConversion`, `lpcRatePct`
  - `ENGAGEMENT` → `engagementRatePct`, `costPerEngagement`
  - `VIDEO_VIEW(S)` → `viewRatePct`, `completionRatePct`, `cpv`

- **Pacing assessment** — the linchpin signal for the recommendation
  engine. The challenge: a lot of ad sets don't carry their own
  `dailyBudget` (they sit under a group-level budget). For those, the
  context builder computes an **implied daily budget** as the ad set's
  historical share of total group spend times the group's
  `dailyBudget`. If the group had zero spend, it falls back to an even
  split across the ad sets.

  ```
  effectiveDailyBudget = adSet.dailyBudget                  if set ("AD_SET")
                       ; group.dailyBudget * spendShare      if group has daily budget ("GROUP_SHARE")
                       ; None                                otherwise ("NONE")

  spendVsBudgetPct = avgDailySpend / effectiveDailyBudget * 100

  underDelivering  : spendVsBudgetPct < 50
  pacingFine       : 70 ≤ spendVsBudgetPct ≤ 110
  overDelivering   : spendVsBudgetPct > 110
  ```

  These three booleans are what the recommendation engine reads — it
  never re-derives pacing from raw spend.

The output (`context_payload.json`) is a hierarchical document:
`campaigns` (groups) → each carrying `groupPacing` + `totals` + an
`adSets` list with the per-ad-set context above.

### 6.5 Step 5 — `analyze.py` (the decision engine)

This is where every recommendation is actually made. The logic is a
**strict hierarchy**: each ad set runs through anomaly detection first,
then the bid recommender; the bid recommender uses the anomaly codes
as hard blockers before considering performance signals.

#### 6.5.1 Anomaly detection

Run for every ad set, against a portfolio-wide reference (`avg_cpl`
across all `LEAD_GENERATION` ad sets that have a positive CPL):

| Code               | Trigger                                                                                            | Emoji |
|--------------------|----------------------------------------------------------------------------------------------------|-------|
| `NO_DATA`          | `impressions == 0` over the window                                                                 | ⚪    |
| `ZERO_CONVERSIONS` | Conversion objective + spend ≥ $500 + zero leads + zero ext. website conversions                   | 🔴    |
| `NO_LEADS`         | `LEAD_GENERATION` + spend ≥ $300 + zero `oneClickLeads`                                            | 🟡    |
| `HIGH_CPL`         | `LEAD_GENERATION` + ad-set CPL > 2× portfolio average CPL                                          | 🔴    |
| `LOW_CTR`          | Non-brand objective + impressions ≥ 5,000 + CTR < 0.2%                                             | 🟡    |

Thresholds live as module constants at the top of `analyze.py` —
adjust there to retune.

#### 6.5.2 Bid recommendation (`_recommend_bid`)

The hierarchy, top-to-bottom (first match wins):

1. **`NO_DATA` → HOLD.** No impressions means there's nothing to
   optimize; the issue is upstream of bidding (creative, targeting,
   activation).
2. **`ZERO_CONVERSIONS` / `NO_LEADS` / `HIGH_CPL` → ESCALATE.** These
   are non-bid problems (creative, audience, landing page, or
   tracking). Bidding more won't fix a broken funnel.
3. **`MAX_DELIVERY` strategy:**
   - Under-delivering → `ESCALATE` (review budget / audience size /
     creative).
   - Otherwise → `HOLD` ("no bid to adjust").
4. **No `currentValue` (defensive guard) → HOLD.**
5. **Objective-specific performance rules:**
   - **`LEAD_GENERATION`:**
     - Under-delivering + leads > 0 + CTR ≥ 0.4% → **INCREASE +20%**
       ("bid is below clearing price").
     - Under-delivering + CTR ≥ 0.4% → **INCREASE +10%** ("moderate
       bid increase to test clearing price").
     - Over-delivering + CPC > $15 + CTR < 0.4% → **DECREASE −15%**.
   - **`WEBSITE_VISIT` / `WEBSITE_CONVERSION` / `JOB_APPLICANTS`:**
     - CPC > $15 + CTR < 0.4% → **DECREASE −20%**.
     - Under-delivering + CTR ≥ 0.4% → **INCREASE +10%**.
   - **`BRAND_AWARENESS` / `VIDEO_VIEW(S)`:**
     - CPM > $50 + < 1,000 impressions/day → **DECREASE −15%**.
6. **Default → HOLD** ("performance within acceptable range").

Any recommended value passes through `_clamp_and_round`:

- Floor: `CPC` ≥ $3.50, `CPM` ≥ $2.00, `CPV` ≥ $0.01 (platform
  minimums).
- Cap: at most `current × 1.25` (INCREASE) or at least `current × 0.75`
  (DECREASE). The ±25% ceiling protects against single-pass
  overcorrection.
- Round to 2 decimals; the % adjustment is recomputed after clamping.

#### 6.5.3 Confidence

Volume-driven, not signal-driven:

| Impressions over window | Confidence |
|-------------------------|------------|
| < 1,000                 | `LOW`      |
| 1,000 – 9,999           | `MEDIUM`   |
| ≥ 10,000                | `HIGH`     |

#### 6.5.4 Output

`data/recommendations.json` is the full `context_payload.json` with
each ad set's `anomalyFlags[]` and `assessment{}` (direction,
adjustment target, current value, recommended value, % change, reason,
confidence) attached, plus a `portfolio` summary with portfolio
totals, `directionCounts`, and `anomalyCounts`.

### 6.6 Step 6 — `format_report.py`

Three output formats, selected via `--format`:

- **`markdown`** → `output/report.md` — full report with grouped
  Campaign/Ad Set sections, decision tables, and reasoning text.
- **`slack`** → `output/report_slack.txt` — terse summary suitable for
  posting in a channel.
- **`email`** → `output/report_email.html` — HTML with light styling
  for a mail client.

### 6.7 Step 7 — `generate_html_report.py`

`output/report.html` — a self-contained interactive dashboard
(filterable / sortable). The orchestrator runs this in addition to the
text format, so you always get the HTML view even if you also asked for
markdown/Slack/email.

## 7. Data flow (file map)

```
data/campaign_groups.json    Step 1 output → Steps 2 + 4 input
data/campaigns.json          Step 2 output → Steps 3 + 4 input
data/analytics_raw.json      Step 3 output → Step 4 input
data/context_payload.json    Step 4 output → Step 5 input
data/recommendations.json    Step 5 output → Steps 6 + 7 input
data/failed_groups.json      Step 2 — only present if any group fetch failed

output/report.md             Step 6 output (markdown format)
output/report_slack.txt      Step 6 output (slack format)
output/report_email.html     Step 6 output (email format)
output/report.html           Step 7 output (always produced)
```

Every step's input is a file. That means you can stop after any step,
edit the JSON by hand to test a hypothesis, and resume by running the
next step directly (`python3 analyze.py`, `python3 format_report.py
--format markdown`, etc.).

## 8. Error handling & resilience

- **`AUTH_FAILURE` (401)** → pipeline halts immediately, exits with
  code 2, prints "Run auth.py to refresh the token." No partial state
  is left in an inconsistent shape.
- **Rate limit (429)** → 60-second sleep, up to 3 attempts per call.
- **API version sunset (426)** → walk backwards through 14 months of
  candidates and cache the first one that works.
- **Per-group fetch failure** in Step 2 → logged to
  `data/failed_groups.json`; the rest of the pipeline still runs.
- **Per-batch analytics failure** in Step 3 → logged; ad sets in that
  batch end up with empty metric rows, which Step 5 reads as `NO_DATA`
  and routes to `HOLD` with `LOW` confidence.
- **No active groups** → halt with `NO_ACTIVE_GROUPS` (nothing to
  optimize).
- **`config.py` preflight** runs at the top of every script:
  - Missing `ACCOUNT_ID` / `CLIENT_ID` / `CLIENT_SECRET` → exit 1.
  - Missing `token.json` → exit 2 with "Run python3 auth.py".
  - Expired token → exit 2.
  - Token expiring in < 3 days → warn but continue.

## 9. Self-test

`selftest.py` exercises the rule engine against synthetic ad-set
contexts to verify the recommendation hierarchy. Run it after changing
thresholds in `analyze.py` to confirm nothing in the table above
regressed.

## 10. Running it

End to end (the typical case):

```bash
python3 orchestrator.py \
    --start 2026-05-25 \
    --end   2026-05-31 \
    --format markdown \
    --account-name "Acme Corp"
```

Step-by-step (for debugging or partial reruns):

```bash
python3 get_campaign_groups.py
python3 get_campaigns.py
python3 get_analytics.py     --start 2026-05-25 --end 2026-05-31
python3 build_context.py     --start 2026-05-25 --end 2026-05-31
python3 analyze.py
python3 format_report.py     --format markdown
python3 generate_html_report.py
```

Re-authorize when the token expires:

```bash
python3 auth.py
```

---

## 11. Worked example — one ad set, end to end

To make the rule hierarchy concrete, here's a single ad set traced
through every step of the pipeline. All numbers are illustrative.

**Setup**

- Account `514696610`, analytics window `2026-05-25 → 2026-05-31`
  (7 days).
- Campaign (group) `700100200` — "EMEA — Q2 Pipeline":
  - `dailyBudget = $1,000`, `status = ACTIVE`.
  - Contains two ad sets. The other one spent `$1,000` over the
    window. (Used below for the pacing share computation.)
- Ad Set `800300400` — "EMEA / Whitepaper / LG" (the one we'll
  trace).

### Step 1 — `get_campaign_groups.py`

The group comes back from `/adCampaignGroups` and is normalized into
`data/campaign_groups.json`:

```json
{
  "id": "700100200",
  "urn": "urn:li:sponsoredCampaignGroup:700100200",
  "name": "EMEA — Q2 Pipeline",
  "status": "ACTIVE",
  "budgetType": "DAILY",
  "dailyBudget": 1000.0,
  "lifetimeBudget": null,
  "budgetCurrency": "USD"
}
```

`budgetType` is `DAILY` because `dailyBudget` is set and `totalBudget`
is not (per `_resolve_group_budget_type`).

### Step 2 — `get_campaigns.py`

The raw `/adCampaigns` response for our ad set looks roughly like:

```json
{
  "id": 800300400,
  "name": "EMEA / Whitepaper / LG",
  "status": "ACTIVE",
  "objectiveType": "LEAD_GENERATION",
  "optimizationTargetType": "MAX_LEAD",
  "costType": "CPC",
  "unitCost": { "amount": "4.00", "currencyCode": "USD" },
  "campaignGroup": "urn:li:sponsoredCampaignGroup:700100200",
  "leadGenerationFormEnabled": true
}
```

`_resolve_bid_strategy` runs:

- `unitCost` is set → not `MAX_DELIVERY`.
- `optimizationTargetType = "MAX_LEAD"` ∈ the `manual_bid_cpc` set
  (`{MAX_CLICK, MAX_LEAD, MAX_QUALIFIED_LEAD, MAX_CONVERSION}`).
- → `bidStrategy = "MANUAL_BID"`, `bidType = "CPC"`,
  `bidAmount = 4.00`, `bidCap = null`.

`_resolve_campaign_budget` sees neither `dailyBudget` nor `totalBudget`
on the ad set → `budgetType = "INHERITED"`. Parent group context is
denormalized onto the record, producing the entry in
`data/campaigns.json`:

```json
{
  "id": "800300400",
  "urn": "urn:li:sponsoredCampaign:800300400",
  "name": "EMEA / Whitepaper / LG",
  "objectiveType": "LEAD_GENERATION",
  "bidStrategy": "MANUAL_BID",
  "bidType": "CPC",
  "bidAmount": 4.0,
  "bidCap": null,
  "budgetType": "INHERITED",
  "dailyBudget": null,
  "lifetimeBudget": null,
  "groupId": "700100200",
  "groupDailyBudget": 1000.0,
  ...
}
```

### Step 3 — `get_analytics.py`

`LEAD_GENERATION` ad sets go into the lead-gen bucket, which requests
this field list (all 8 fit under the 20-field cap, so no chunking):

```
impressions, clicks, costInLocalCurrency,
oneClickLeads, oneClickLeadFormOpens, landingPageClicks,
follows, pivotValues
```

The `/adAnalytics` row that comes back for our ad set:

```json
{
  "pivotValues": ["urn:li:sponsoredCampaign:800300400"],
  "impressions": 45000,
  "clicks": 270,
  "costInLocalCurrency": "1500.00",
  "oneClickLeads": 18,
  "oneClickLeadFormOpens": 60,
  "landingPageClicks": 250
}
```

`_extract_urn` pulls the URN out of `pivotValues` and the row is keyed
by it in `data/analytics_raw.json`.

### Step 4 — `build_context.py`

**Window:** `(2026-05-31 − 2026-05-25).days + 1 = 7 days`.

**Window aggregates:**

| Metric          | Value                          |
|-----------------|--------------------------------|
| `impressions`   | 45,000                         |
| `clicks`        | 270                            |
| `spend`         | $1,500.00                      |
| `avgDailySpend` | $1,500 / 7 = **$214.29**       |

**Derived (LEAD_GENERATION branch of `_derived_metrics`):**

| Metric                 | Formula                  | Value      |
|------------------------|--------------------------|------------|
| `ctrPct`               | 270 / 45,000 × 100       | **0.60%**  |
| `cpc`                  | 1,500 / 270              | $5.56      |
| `cpm`                  | 1,500 / 45,000 × 1,000   | $33.33     |
| `cpl`                  | 1,500 / 18               | **$83.33** |
| `formOpenRatePct`      | 60 / 45,000 × 100        | 0.13%      |
| `leadConvRatePct`      | 18 / 60 × 100            | 30.0%      |

**Pacing.** The ad set has no `dailyBudget` of its own, so
`_allocate_group_budget_share` is used. The group's two ad sets spent
$1,500 + $1,000 = $2,500 total over the window. Our share:

```
spendShare              = 1,500 / 2,500           = 0.60
effectiveDailyBudget    = 1,000 × 0.60            = $600   (budgetSource = "GROUP_SHARE")
utilization             = 214.29 / 600            = 0.357
spendVsBudgetPct        = 35.7%
underDelivering         = (0.357 < 0.50)          = True
pacingFine              = (0.70 ≤ 0.357 ≤ 1.10)   = False
overDelivering          = (0.357 > 1.10)          = False
```

So in `data/context_payload.json` our ad set carries:

```json
{
  "objectiveType": "LEAD_GENERATION",
  "bidStrategy": "MANUAL_BID",
  "bidType": "CPC",
  "bidAmount": 4.0,
  "metrics": { "impressions": 45000, "clicks": 270, "spend": 1500.0,
               "avgDailySpend": 214.29, "oneClickLeads": 18, ... },
  "derived": { "ctrPct": 0.60, "cpc": 5.56, "cpl": 83.33, ... },
  "pacing": {
    "effectiveDailyBudget": 600,
    "budgetSource": "GROUP_SHARE",
    "spendShareOfGroup": 0.6,
    "spendVsBudgetPct": 35.7,
    "underDelivering": true,
    "pacingFine": false,
    "overDelivering": false
  }
}
```

### Step 5 — `analyze.py`

Assume the portfolio average lead-gen CPL across all active ad sets
this window is **$90.00** (computed by `_portfolio_avg_cpl`).

**Anomaly detection** (each check from §6.5.1):

| Check               | Predicate                                                     | Result    |
|---------------------|---------------------------------------------------------------|-----------|
| `NO_DATA`           | impressions == 0?                                             | No (45k)  |
| `ZERO_CONVERSIONS`  | conv-objective + spend ≥ $500 + ext_conv == 0 + leads == 0?   | No (18 leads) |
| `NO_LEADS`          | LG + spend ≥ $300 + leads == 0?                               | No (18 leads) |
| `HIGH_CPL`          | LG + cpl > 2 × $90 = $180?                                    | No (cpl = $83.33) |
| `LOW_CTR`           | non-brand + impressions ≥ 5,000 + ctrPct < 0.2%?              | No (0.60%) |

→ `anomalyFlags = []`.

**Bid recommendation** (`_recommend_bid`, applying the hierarchy from
§6.5.2):

1. `NO_DATA` in codes? No → continue.
2. Any of `ZERO_CONVERSIONS / NO_LEADS / HIGH_CPL` in codes? No →
   continue.
3. `bidStrategy == "MAX_DELIVERY"`? No (`MANUAL_BID`) → continue.
4. `currentValue is None`? No ($4.00) → continue.
5. **Objective branch — `LEAD_GENERATION`:**
   - First sub-rule: `under_delivering AND leads > 0 AND ctr_pct ≥
     0.4`?
     - `under_delivering = True` ✓
     - `leads = 18 > 0` ✓
     - `ctr_pct = 0.60 ≥ 0.4` ✓
   - **Match. Direction = `INCREASE`, factor = ×1.20.**

`_clamp_and_round(current=4.00, recommended=4.80, bid_type="CPC")`:

- Floor: `CPC` floor is $3.50. `4.80 > 3.50` → no change.
- Ceiling: `max_inc = 4.00 × 1.25 = $5.00`. `4.80 ≤ 5.00` → no change.
- Round: $4.80.
- `adjustmentPct = (4.80 − 4.00) / 4.00 × 100 = +20.0%`.

**Confidence** (`_confidence`): impressions 45,000 ≥ 10,000 → `HIGH`.

The assessment attached to the ad set in `data/recommendations.json`:

```json
{
  "direction": "INCREASE",
  "adjustmentTarget": "bidAmount",
  "currentValue": 4.0,
  "recommendedValue": 4.80,
  "adjustmentPct": 20.0,
  "reason": "Under-delivering at 35.7% of implied daily, CTR 0.60%, 18 leads. Bid is below clearing price.",
  "confidence": "HIGH"
}
```

### Steps 6–7 — the reports

The markdown report (`output/report.md`) shows this ad set under its
parent Campaign, in the **INCREASE** section, with the reason and
confidence above. The interactive HTML dashboard
(`output/report.html`) renders the same recommendation as a row that
can be filtered/sorted alongside every other ad set.

### Side note — when the guard rails actually engage

The rule hierarchy never emits a raw factor outside `[0.80, 1.20]`, so
the **±25% clamp** is a defense-in-depth check, not a frequent
participant. The **floor**, on the other hand, regularly bites on
already-cheap bids. Two illustrative variants of the same example:

- **Floor engages.** Imagine the rule had fired `DECREASE` at ×0.80
  on a current bid of $3.80:
  - raw recommended = `3.80 × 0.80 = $3.04`
  - CPC floor = $3.50 → recommended bumped **up** to $3.50
  - `adjustmentPct = (3.50 − 3.80) / 3.80 × 100 = −7.9%`
    (not the rule's nominal −20%)

- **Ceiling engages.** Imagine the rule had somehow proposed +50% on
  $4.00:
  - raw recommended = `4.00 × 1.50 = $6.00`
  - `max_inc = 4.00 × 1.25 = $5.00` → recommended clamped **down** to
    $5.00
  - `adjustmentPct = +25.0%` (not the proposed +50%)

These two clamps together are why no single pass of the agent can move
a bid more than 25% in either direction, and why a recommended CPC
can never land below $3.50.

