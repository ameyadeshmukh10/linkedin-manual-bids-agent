"""
Script 7 — Generate an interactive HTML dashboard from the bid recommendations.

Reads:
  data/recommendations.json  (produced by analyze.py)
  output/report.md           (produced by format_report.py — prose narrative)

Writes:
  output/report.html         (single-file, self-contained, opens in any browser)

The dashboard shows:
  - Portfolio KPI cards (spend, impressions, clicks, leads, conversions, avg CPL)
  - Direction pills (escalate / increase / decrease / hold counts)
  - Anomaly banner with links to flagged ad sets
  - Spend-by-Campaign bar chart (Chart.js via CDN)
  - Per-objective sections, collapsible, each containing:
      - Per-Campaign header card (pacing, group daily budget)
      - Sortable / filterable table of Ad Sets under that Campaign
        with bid change, reason, objective metrics, flags
  - The narrative markdown (from report.md) rendered as prose at the bottom
    so any edits there flow through

Usage:
    python3 generate_html_report.py [--account-name "Name"]
"""
import argparse
import html
import json
import os
import re
from collections import defaultdict

import config


DIR_EMOJI = {"ESCALATE": "🚨", "INCREASE": "🟢", "DECREASE": "🔴", "HOLD": "⚪"}
DIR_COLOR = {
    "ESCALATE": "#dc2626",
    "INCREASE": "#16a34a",
    "DECREASE": "#ea580c",
    "HOLD": "#6b7280",
}
OBJECTIVE_LABELS = {
    "LEAD_GENERATION": "Lead Generation",
    "WEBSITE_CONVERSION": "Website Conversions",
    "WEBSITE_VISIT": "Website Visits",
    "BRAND_AWARENESS": "Brand Awareness",
    "VIDEO_VIEW": "Video Views",
    "VIDEO_VIEWS": "Video Views",
    "ENGAGEMENT": "Engagement",
    "JOB_APPLICANTS": "Job Applicants",
}
OBJECTIVE_ORDER = list(OBJECTIVE_LABELS.keys())


# ---------- Formatting helpers ----------

def e(v):
    if v is None:
        return ""
    return html.escape(str(v))


def fmt_money(v):
    if v is None:
        return '<span class="dash">—</span>'
    return f"${v:,.2f}"


def fmt_pct(v):
    if v is None:
        return '<span class="dash">—</span>'
    return f"{v:.2f}%"


def fmt_int(v):
    if v is None:
        return '<span class="dash">—</span>'
    return f"{int(v):,}"


def fmt_bid(value, bid_type):
    if value is None:
        return '<span class="dash">—</span>'
    return f"${value:.2f} {e(bid_type or '')}".strip()


def dir_pill(direction):
    if not direction:
        direction = "HOLD"
    emoji = DIR_EMOJI.get(direction, "")
    return f'<span class="dir-pill dir-{direction}">{emoji} {direction}</span>'


def flag_badge(flag):
    emoji = flag.get("emoji", "")
    label = flag.get("label", "")
    detail = flag.get("detail", "")
    code = flag.get("code", "")
    title = html.escape(detail).replace('"', '&quot;')
    return (
        f'<span class="flag flag-{e(code)}" title="{title}">'
        f'{emoji} {e(label)}</span>'
    )


def slug(s):
    """Make a DOM-safe id from a string."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ---------- Markdown → HTML (subset for prose narrative) ----------

def md_to_html(md):
    """Minimal markdown renderer for the narrative from report.md.
    Supports: h1/h2/h3/h4, **bold**, *italic*, `code`, lists, blockquotes,
    blank-line paragraph breaks. Links to `output/report.md` internals are
    rendered as plain text."""
    out = []
    lines = md.splitlines()
    in_ul = False
    para = []

    def flush_para():
        nonlocal para
        if para:
            text = " ".join(para)
            out.append(f"<p>{_inline(text)}</p>")
            para = []

    def close_list():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def _inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        return t

    for raw in lines:
        line = raw.rstrip()
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_para()
            close_list()
            depth = len(m.group(1))
            out.append(f"<h{depth} class=\"prose-h{depth}\">{_inline(m.group(2))}</h{depth}>")
            continue
        ul_m = re.match(r"^(\s*)- (.*)$", line)
        if ul_m:
            flush_para()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(ul_m.group(2))}</li>")
            continue
        if not line.strip():
            flush_para()
            close_list()
            continue
        close_list()
        para.append(line)
    flush_para()
    close_list()
    return "\n".join(out)


# ---------- Per-objective metric cells ----------

def objective_metric_cells(a):
    """Return (label, value_html, sort_val) for extra metrics shown per ad set."""
    obj = a.get("objectiveType")
    m = a["metrics"]
    d = a["derived"]
    rows = []
    if obj == "LEAD_GENERATION":
        rows = [
            ("Leads", fmt_int(m.get("oneClickLeads", 0)), m.get("oneClickLeads", 0)),
            ("CPL", fmt_money(d.get("cpl")), d.get("cpl")),
            ("Form opens", fmt_int(m.get("oneClickLeadFormOpens", 0)), m.get("oneClickLeadFormOpens", 0)),
            ("Lead conv", fmt_pct(d.get("leadConvRatePct")), d.get("leadConvRatePct")),
        ]
    elif obj in ("WEBSITE_CONVERSION", "WEBSITE_VISIT", "JOB_APPLICANTS"):
        rows = [
            ("LP clicks", fmt_int(m.get("landingPageClicks", 0)), m.get("landingPageClicks", 0)),
            ("Conv.", fmt_int(m.get("externalWebsiteConversions", 0)), m.get("externalWebsiteConversions", 0)),
            ("Cost/conv", fmt_money(d.get("costPerConversion")), d.get("costPerConversion")),
        ]
    elif obj == "BRAND_AWARENESS":
        rows = [
            ("Follows", fmt_int(m.get("follows", 0)), m.get("follows", 0)),
            ("Likes", fmt_int(m.get("likes", 0)), m.get("likes", 0)),
            ("CPM", fmt_money(d.get("cpm")), d.get("cpm")),
        ]
    elif obj in ("VIDEO_VIEW", "VIDEO_VIEWS"):
        rows = [
            ("Views", fmt_int(m.get("videoViews", 0)), m.get("videoViews", 0)),
            ("Compl. %", fmt_pct(d.get("completionRatePct")), d.get("completionRatePct")),
            ("CPV", fmt_money(d.get("cpv")), d.get("cpv")),
        ]
    elif obj == "ENGAGEMENT":
        rows = [
            ("Engagements", fmt_int(m.get("totalEngagements", 0)), m.get("totalEngagements", 0)),
            ("Eng. rate", fmt_pct(d.get("engagementRatePct")), d.get("engagementRatePct")),
            ("Cost/eng.", fmt_money(d.get("costPerEngagement")), d.get("costPerEngagement")),
        ]
    return rows


# ---------- Ad set table row ----------

def ad_set_row(a, campaign_id):
    ass = a.get("assessment", {}) or {}
    pacing = a.get("pacing", {}) or {}
    m = a["metrics"]
    d = a["derived"]
    direction = ass.get("direction", "HOLD")
    bid_type = a.get("bidType", "")
    cur = ass.get("currentValue")
    rec = ass.get("recommendedValue")
    pct = ass.get("adjustmentPct", 0)

    # Bid change cell
    if direction in ("INCREASE", "DECREASE") and rec is not None:
        arrow = "↑" if direction == "INCREASE" else "↓"
        bid_change = (
            f'{fmt_bid(cur, bid_type)} <span class="arrow">{arrow}</span> '
            f'<strong>{fmt_bid(rec, bid_type)}</strong> '
            f'<span class="pct pct-{direction}">{pct:+.1f}%</span>'
        )
    elif cur is not None:
        bid_change = fmt_bid(cur, bid_type)
    else:
        bid_change = '<span class="dash">—</span>'

    # Pacing cell
    if pacing.get("effectiveDailyBudget") is not None:
        util = pacing.get("spendVsBudgetPct", 0) or 0
        src = pacing["budgetSource"]
        src_label = "ad set" if src == "AD_SET" else "group share"
        pacing_cell = (
            f'<div class="pacing">'
            f'<span class="pacing-pct">{util:.0f}%</span>'
            f'<span class="pacing-detail">${m["avgDailySpend"]:.2f} / '
            f'${pacing["effectiveDailyBudget"]:.2f}<br>'
            f'<span class="muted">({src_label})</span></span>'
            f'</div>'
        )
        pacing_sort = util
    else:
        pacing_cell = '<span class="dash">—</span>'
        pacing_sort = -1

    # Flags cell
    flags_html = "".join(flag_badge(f) for f in a.get("anomalyFlags", [])) or '<span class="dash">—</span>'

    # Objective metrics — we'll render these inline in a sub-row
    obj_cells = objective_metric_cells(a)
    obj_html = (
        '<div class="obj-metrics">'
        + "".join(
            f'<span class="obj-metric"><span class="obj-label">{e(lbl)}</span>'
            f'<span class="obj-value">{val}</span></span>'
            for lbl, val, _ in obj_cells
        )
        + "</div>"
    )

    name_cell = (
        f'<div class="adset-name"><a id="as-{e(a["id"])}"></a>'
        f'<strong>{e(a["name"])}</strong><br>'
        f'<span class="muted">{e(a.get("bidStrategy", ""))}'
        + (f' · {e(bid_type)}' if bid_type else '')
        + f' · confidence: {e(ass.get("confidence", "LOW"))}'
        + '</span></div>'
    )

    reason = ass.get("reason") or ""

    return (
        "<tr>"
        f'<td class="col-dir" data-sort="{e(direction)}">{dir_pill(direction)}</td>'
        f'<td class="col-name">{name_cell}</td>'
        f'<td class="col-bid">{bid_change}</td>'
        f'<td class="col-metrics">'
        f'<div class="base-metrics">'
        f'<span>${m["spend"]:,.2f}</span><span class="sep">·</span>'
        f'<span>{m["impressions"]:,} imps</span><span class="sep">·</span>'
        f'<span>CTR {fmt_pct(d.get("ctrPct"))}</span><span class="sep">·</span>'
        f'<span>CPC {fmt_money(d.get("cpc"))}</span>'
        f'</div>{obj_html}</td>'
        f'<td class="col-pacing" data-sort="{pacing_sort}">{pacing_cell}</td>'
        f'<td class="col-reason">{e(reason)}</td>'
        f'<td class="col-flags">{flags_html}</td>'
        "</tr>"
    )


# ---------- Campaign (group) block ----------

def campaign_block(group):
    gid = e(group["id"])
    gp = group.get("groupPacing") or {}
    util = gp.get("utilizationPct")
    t = group["totals"]

    pacing_badge = ""
    if util is not None:
        cls = "ok" if 70 <= util <= 110 else ("under" if util < 70 else "over")
        pacing_badge = f'<span class="pacing-badge pacing-{cls}">{util:.0f}% of daily</span>'

    budget_badge = ""
    if group.get("dailyBudget"):
        budget_badge = f'<span class="budget-badge">${group["dailyBudget"]:,.0f}/day</span>'

    # Sort ad sets: escalate first, then increase, then decrease, then hold, within each by spend desc
    dir_rank = {"ESCALATE": 0, "INCREASE": 1, "DECREASE": 2, "HOLD": 3}
    ad_sets = sorted(
        group["adSets"],
        key=lambda a: (
            dir_rank.get(a.get("assessment", {}).get("direction", "HOLD"), 9),
            -a["metrics"]["spend"],
        ),
    )
    rows_html = "".join(ad_set_row(a, gid) for a in ad_sets)

    # Direction count pills for this campaign
    dcounts = defaultdict(int)
    for a in ad_sets:
        dcounts[a.get("assessment", {}).get("direction", "HOLD")] += 1
    counts_html = " ".join(
        f'<span class="count-pill count-{d}">{DIR_EMOJI[d]} {dcounts[d]}</span>'
        for d in ["ESCALATE", "INCREASE", "DECREASE", "HOLD"] if dcounts[d]
    )

    return f"""
<div class="campaign" id="camp-{gid}">
  <div class="campaign-head">
    <h3>{e(group["name"])}</h3>
    <div class="campaign-meta">
      <span class="spend-badge">${t["spend"]:,.2f}</span>
      <span class="count-badge">{t["adSetCount"]} ad sets</span>
      {budget_badge}
      {pacing_badge}
      <span class="counts">{counts_html}</span>
    </div>
  </div>
  <div class="table-wrapper">
    <div class="table-controls">
      <input type="search" placeholder="Filter ad sets…">
      <span class="row-count"></span>
    </div>
    <table class="data">
      <thead>
        <tr>
          <th class="col-dir">Rec</th>
          <th class="col-name">Ad Set</th>
          <th class="col-bid">Bid change</th>
          <th class="col-metrics">Metrics</th>
          <th class="col-pacing">Pacing</th>
          <th class="col-reason">Reason</th>
          <th class="col-flags">Flags</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>
"""


# ---------- KPI cards + summary ----------

def portfolio_section(payload, account_name):
    p = payload["portfolio"]
    t = p["totals"]
    dr = payload["dateRange"]
    window = payload["windowDays"]
    dc = p["directionCounts"]
    ac = p["anomalyCounts"]

    actions = dc.get("ESCALATE", 0) + dc.get("INCREASE", 0) + dc.get("DECREASE", 0)
    kpis = [
        ("Spend", f"${t['spend']:,.2f}"),
        ("Impressions", f"{t['impressions']:,}"),
        ("Clicks", f"{t['clicks']:,}"),
        ("Leads", f"{t['leads']:,}"),
        ("Conv.", f"{t['conversions']:,}"),
        ("Avg CPL (LG)", f"${p['avgCplLeadGen']:,.2f}" if p.get("avgCplLeadGen") else "—"),
    ]
    kpi_cards = "".join(
        f'<div class="kpi"><div class="kpi-label">{e(l)}</div>'
        f'<div class="kpi-value">{v}</div></div>'
        for l, v in kpis
    )

    dir_pills = "".join(
        f'<div class="dir-card dir-{d}">'
        f'<div class="dir-count">{dc.get(d, 0)}</div>'
        f'<div class="dir-label">{DIR_EMOJI[d]} {d}</div>'
        '</div>'
        for d in ["ESCALATE", "INCREASE", "DECREASE", "HOLD"]
    )

    # Anomaly banner
    anomaly_html = ""
    if ac:
        items = []
        for g in payload["campaigns"]:
            for a in g["adSets"]:
                for f in a.get("anomalyFlags", []):
                    items.append({
                        "emoji": f["emoji"],
                        "label": f["label"],
                        "detail": f["detail"],
                        "adset_id": a["id"],
                        "adset_name": a["name"],
                        "campaign_name": g["name"],
                    })
        if items:
            # Sort: red first then amber then gray
            order = {"🔴": 0, "🟡": 1, "⚪": 2}
            items.sort(key=lambda x: order.get(x["emoji"], 9))
            li_html = "".join(
                f'<li>{x["emoji"]} <strong>{e(x["label"])}</strong> — '
                f'<a href="#as-{e(x["adset_id"])}">{e(x["adset_name"])}</a> '
                f'<span class="muted">({e(x["campaign_name"])})</span>: '
                f'{e(x["detail"])}</li>'
                for x in items
            )
            anomaly_html = (
                '<div class="anomaly-banner"><h3>Flags requiring review</h3>'
                f'<ul>{li_html}</ul></div>'
            )

    return f"""
<header class="page-header">
  <h1>{e(account_name)} — Bid Optimization Report</h1>
  <div class="meta">
    <span><strong>Date range:</strong> {e(dr["start"])} → {e(dr["end"])}</span>
    <span><strong>Window:</strong> {window} days</span>
    <span><strong>Campaigns:</strong> {len(payload["campaigns"])}</span>
    <span><strong>Ad Sets:</strong> {t["adSetCount"]}</span>
    <span><strong>Actions recommended:</strong> {actions}</span>
  </div>
</header>
<section class="kpi-row">{kpi_cards}</section>
<section class="dir-row">{dir_pills}</section>
{anomaly_html}
"""


# ---------- Chart data ----------

def chart_data(payload):
    """Compact JSON blob for chart rendering."""
    campaigns = sorted(
        payload["campaigns"], key=lambda g: g["totals"]["spend"], reverse=True
    )
    return {
        "campaignSpend": {
            "labels": [g["name"] for g in campaigns],
            "spend": [g["totals"]["spend"] for g in campaigns],
            "budget": [g.get("dailyBudget") or 0 for g in campaigns],
            "utilization": [
                (g.get("groupPacing") or {}).get("utilizationPct") for g in campaigns
            ],
        },
        "directionCounts": payload["portfolio"]["directionCounts"],
    }


# ---------- CSS + JS ----------

CSS = r"""
:root {
  --bg: #ffffff;
  --bg-subtle: #f9fafb;
  --bg-panel: #fafafa;
  --text: #0f172a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --border-strong: #d1d5db;
  --accent: #2563eb;
  --red: #dc2626;
  --amber: #d97706;
  --green: #16a34a;
  --orange: #ea580c;
  --gray-flag: #6b7280;
}
* { box-sizing: border-box; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
  margin: 0; padding: 0;
  font-size: 14px; line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
.container { max-width: 1400px; margin: 0 auto; padding: 24px 32px 64px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--bg-subtle); padding: 1px 6px; border-radius: 3px; font-size: 12.5px; }
.muted { color: var(--muted); }
.dash { color: var(--border-strong); }

.page-header { border-bottom: 1px solid var(--border); padding-bottom: 18px; margin-bottom: 24px; }
.page-header h1 { margin: 0 0 6px; font-size: 24px; font-weight: 700; letter-spacing: -0.015em; }
.page-header .meta { display: flex; flex-wrap: wrap; gap: 16px; color: var(--muted); font-size: 13px; }
.page-header .meta strong { color: var(--text); font-weight: 600; }

/* KPI cards */
.kpi-row {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 20px;
}
.kpi {
  border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 14px; background: var(--bg);
}
.kpi-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.kpi-value { font-size: 20px; font-weight: 700; margin-top: 3px; font-variant-numeric: tabular-nums; }

/* Direction cards */
.dir-row {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 10px; margin-bottom: 20px;
}
.dir-card {
  border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 16px; background: var(--bg-panel);
  border-left: 3px solid var(--border-strong);
}
.dir-card.dir-ESCALATE { border-left-color: var(--red); }
.dir-card.dir-INCREASE { border-left-color: var(--green); }
.dir-card.dir-DECREASE { border-left-color: var(--orange); }
.dir-card.dir-HOLD     { border-left-color: var(--muted); }
.dir-count { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; }
.dir-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-top: 4px; }

/* Anomaly banner */
.anomaly-banner {
  border: 1px solid var(--red); background: #fef2f2;
  padding: 12px 16px; border-radius: 5px; margin-bottom: 24px;
}
.anomaly-banner h3 {
  margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--red); font-weight: 700;
}
.anomaly-banner ul { margin: 0; padding-left: 20px; font-size: 13px; }
.anomaly-banner li { margin: 3px 0; }

/* Charts */
.chart-row { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin: 20px 0 28px; }
@media (max-width: 900px) { .chart-row { grid-template-columns: 1fr; } }
.chart-card {
  border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 18px; background: var(--bg);
}
.chart-card h3 { margin: 0 0 10px; font-size: 13px; font-weight: 700; letter-spacing: -0.005em; }
.chart-card canvas { max-width: 100%; height: 260px !important; }

/* Objective sections */
.obj-section { margin-top: 28px; border-top: 1px solid var(--border); padding-top: 20px; }
.obj-section-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 14px; cursor: pointer; user-select: none;
}
.obj-section-head h2 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.01em; }
.obj-section-head .toggle { font-size: 12px; color: var(--muted); }
.obj-section.collapsed .obj-body { display: none; }

/* Campaign block */
.campaign {
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); margin-bottom: 14px; overflow: hidden;
}
.campaign-head {
  padding: 14px 16px 12px; border-bottom: 1px solid var(--border);
  background: var(--bg-panel);
}
.campaign-head h3 { margin: 0 0 6px; font-size: 15px; font-weight: 700; letter-spacing: -0.005em; }
.campaign-meta { display: flex; flex-wrap: wrap; gap: 8px; font-size: 12px; align-items: center; }
.spend-badge, .count-badge, .budget-badge, .pacing-badge {
  display: inline-block; padding: 2px 8px; border-radius: 3px; font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.spend-badge { background: #eef2ff; color: #3730a3; }
.count-badge { background: var(--bg-subtle); color: var(--muted); }
.budget-badge { background: #ecfdf5; color: #047857; }
.pacing-badge.pacing-ok   { background: #d1fae5; color: #065f46; }
.pacing-badge.pacing-under { background: #fef3c7; color: #78350f; }
.pacing-badge.pacing-over  { background: #fee2e2; color: #991b1b; }
.count-pill {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: 11px; font-weight: 600; background: var(--bg-subtle);
}

/* Table controls */
.table-controls {
  display: flex; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--bg-panel); font-size: 12px;
}
.table-controls input[type="search"] {
  flex: 1; min-width: 200px; border: 1px solid var(--border-strong);
  padding: 5px 10px; font: inherit; font-size: 12px; border-radius: 4px;
  background: var(--bg); color: var(--text);
}
.table-controls input[type="search"]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.table-controls .row-count { color: var(--muted); white-space: nowrap; align-self: center; }

/* Data table */
table.data { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 13px; }
table.data thead { background: var(--bg-subtle); border-bottom: 1px solid var(--border-strong); }
table.data th {
  text-align: left; padding: 8px 10px; font-weight: 600; cursor: pointer;
  user-select: none; white-space: nowrap; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
}
table.data th::after { content: " ↕"; opacity: 0.35; font-size: 10px; }
table.data th.sort-asc::after  { content: " ↑"; opacity: 1; color: var(--accent); }
table.data th.sort-desc::after { content: " ↓"; opacity: 1; color: var(--accent); }
table.data td {
  padding: 8px 10px; border-bottom: 1px solid var(--border);
  vertical-align: top;
}
table.data tr:hover td { background: var(--bg-subtle); }

.col-dir      { width: 130px; }
.col-name     { max-width: 280px; }
.col-bid      { width: 200px; white-space: nowrap; }
.col-metrics  { min-width: 260px; }
.col-pacing   { width: 130px; }
.col-reason   { max-width: 340px; }
.col-flags    { width: 180px; }

.adset-name strong { font-weight: 600; }
.adset-name .muted { font-size: 11px; }

.dir-pill {
  display: inline-block; padding: 2px 8px; border-radius: 3px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.02em;
}
.dir-pill.dir-ESCALATE { background: #fef2f2; color: var(--red); }
.dir-pill.dir-INCREASE { background: #dcfce7; color: #166534; }
.dir-pill.dir-DECREASE { background: #ffedd5; color: #9a3412; }
.dir-pill.dir-HOLD     { background: var(--bg-subtle); color: var(--muted); }

.arrow { color: var(--muted); margin: 0 3px; }
.pct { font-weight: 600; font-size: 11px; padding: 1px 5px; border-radius: 3px; margin-left: 4px; }
.pct-INCREASE { background: #dcfce7; color: #166534; }
.pct-DECREASE { background: #ffedd5; color: #9a3412; }

.base-metrics { display: flex; flex-wrap: wrap; gap: 4px; font-size: 12px; }
.base-metrics .sep { color: var(--border-strong); }
.obj-metrics {
  display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px;
  padding-top: 4px; border-top: 1px dashed var(--border);
  font-size: 11.5px;
}
.obj-label { color: var(--muted); margin-right: 3px; }
.obj-value { font-weight: 600; }

.pacing .pacing-pct { font-weight: 700; font-size: 14px; display: block; }
.pacing .pacing-detail { font-size: 11px; color: var(--muted); }

.flag {
  display: inline-block; font-weight: 600; font-size: 11px;
  padding: 1px 6px; border-radius: 3px; margin: 1px 2px 1px 0; white-space: nowrap;
}
.flag-HIGH_CPL, .flag-ZERO_CONVERSIONS { background: #fef2f2; color: var(--red); }
.flag-NO_LEADS, .flag-LOW_CTR          { background: #fffbeb; color: var(--amber); }
.flag-NO_DATA                          { background: #f3f4f6; color: var(--muted); }

/* Prose narrative */
.narrative {
  margin-top: 32px; padding-top: 24px; border-top: 1px solid var(--border);
}
.narrative .prose-h1 { font-size: 18px; font-weight: 700; margin: 0 0 12px; }
.narrative .prose-h2 { font-size: 16px; font-weight: 700; margin: 20px 0 10px; letter-spacing: -0.005em; }
.narrative .prose-h3 { font-size: 14px; font-weight: 700; margin: 16px 0 8px; }
.narrative .prose-h4 { font-size: 13px; font-weight: 600; margin: 12px 0 6px; }
.narrative p { margin: 0 0 10px; }
.narrative ul { margin: 0 0 12px; padding-left: 20px; }
.narrative li { margin: 3px 0; }
.narrative em { color: var(--muted); }

footer {
  margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--muted);
}
"""


JS = r"""
// Collapsible objective sections
document.querySelectorAll('.obj-section-head').forEach(head => {
  head.addEventListener('click', () => {
    head.parentElement.classList.toggle('collapsed');
    const t = head.querySelector('.toggle');
    if (t) t.textContent = head.parentElement.classList.contains('collapsed') ? 'Show' : 'Hide';
  });
});

// Sortable tables
function cellVal(td) {
  const raw = td.dataset.sort !== undefined ? td.dataset.sort : td.textContent.trim();
  if (raw === '' || raw === '—' || raw === 'N/A') return null;
  const n = parseFloat(String(raw).replace(/[\$,%]/g, ''));
  if (!Number.isNaN(n)) return n;
  return String(raw).toLowerCase();
}
document.querySelectorAll('table.data').forEach(table => {
  const ths = table.querySelectorAll('thead th');
  ths.forEach((th, idx) => {
    th.addEventListener('click', () => {
      const dir = th.classList.contains('sort-asc') ? 'desc' : 'asc';
      ths.forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
      th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      const tbody = table.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort((a, b) => {
        const av = cellVal(a.cells[idx]);
        const bv = cellVal(b.cells[idx]);
        // Nulls sink
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        if (typeof av === 'number' && typeof bv === 'number') {
          return dir === 'asc' ? av - bv : bv - av;
        }
        return dir === 'asc'
          ? String(av).localeCompare(String(bv))
          : String(bv).localeCompare(String(av));
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
});

// Table filter
document.querySelectorAll('.table-wrapper').forEach(wrap => {
  const input = wrap.querySelector('input[type="search"]');
  const count = wrap.querySelector('.row-count');
  const tbody = wrap.querySelector('tbody');
  if (!input || !tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const total = rows.length;
  function render() {
    const q = input.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach(r => {
      const ok = !q || r.textContent.toLowerCase().includes(q);
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    if (count) count.textContent = shown === total ? `${total} rows` : `${shown} / ${total}`;
  }
  input.addEventListener('input', render);
  render();
});
"""


CHART_JS = r"""
(function() {
  if (typeof Chart === 'undefined') return;
  const d = window.__BID_DATA__;

  // Spend by Campaign bar chart
  const spendCanvas = document.getElementById('chart-spend');
  if (spendCanvas && d.campaignSpend) {
    new Chart(spendCanvas, {
      type: 'bar',
      data: {
        labels: d.campaignSpend.labels,
        datasets: [
          {
            label: 'Spend',
            data: d.campaignSpend.spend,
            backgroundColor: '#2563eb',
            borderRadius: 3,
          }
        ],
      },
      options: {
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const util = d.campaignSpend.utilization[ctx.dataIndex];
                const budget = d.campaignSpend.budget[ctx.dataIndex];
                const utilStr = util !== null && util !== undefined ? util.toFixed(0) + '%' : 'n/a';
                const budgetStr = budget ? '$' + budget.toLocaleString() + '/day' : 'no daily budget';
                return [
                  '$' + ctx.parsed.x.toLocaleString(),
                  'Budget: ' + budgetStr,
                  'Pacing: ' + utilStr,
                ];
              }
            }
          }
        },
        scales: {
          x: { ticks: { callback: (v) => '$' + v.toLocaleString() } },
          y: { ticks: { font: { size: 11 } } }
        },
        maintainAspectRatio: false,
      }
    });
  }

  // Direction distribution doughnut
  const dirCanvas = document.getElementById('chart-dir');
  if (dirCanvas && d.directionCounts) {
    const dirs = ['ESCALATE', 'INCREASE', 'DECREASE', 'HOLD'];
    const colors = {ESCALATE: '#dc2626', INCREASE: '#16a34a', DECREASE: '#ea580c', HOLD: '#9ca3af'};
    new Chart(dirCanvas, {
      type: 'doughnut',
      data: {
        labels: dirs,
        datasets: [{
          data: dirs.map(k => d.directionCounts[k] || 0),
          backgroundColor: dirs.map(k => colors[k]),
          borderWidth: 0,
        }]
      },
      options: {
        plugins: {
          legend: { position: 'bottom', labels: { font: { size: 11 } } }
        },
        maintainAspectRatio: false,
      }
    });
  }
})();
"""


# ---------- Main ----------

def _group_by_objective(payload):
    by_obj = defaultdict(list)
    for g in payload["campaigns"]:
        by_obj[g.get("objectiveType") or "UNKNOWN"].append(g)
    for obj in by_obj:
        by_obj[obj].sort(key=lambda g: g["totals"]["spend"], reverse=True)
    ordered = []
    for obj in OBJECTIVE_ORDER:
        if obj in by_obj:
            ordered.append((obj, by_obj.pop(obj)))
    for obj, groups in by_obj.items():
        ordered.append((obj, groups))
    return ordered


def build_html(payload, account_name, narrative_md):
    head = portfolio_section(payload, account_name)

    charts_block = """
<section class="chart-row">
  <div class="chart-card"><h3>Spend by Campaign</h3><canvas id="chart-spend"></canvas></div>
  <div class="chart-card"><h3>Recommendation split</h3><canvas id="chart-dir"></canvas></div>
</section>
"""

    obj_sections = []
    for obj, groups in _group_by_objective(payload):
        blocks = "".join(campaign_block(g) for g in groups)
        obj_sections.append(f"""
<section class="obj-section">
  <div class="obj-section-head">
    <h2>{e(OBJECTIVE_LABELS.get(obj, obj))} · {len(groups)} campaigns</h2>
    <span class="toggle">Hide</span>
  </div>
  <div class="obj-body">{blocks}</div>
</section>
""")

    narrative_html = md_to_html(narrative_md) if narrative_md else ""
    if narrative_html:
        narrative_block = (
            '<section class="narrative"><h2>Narrative (markdown source)</h2>'
            + narrative_html + "</section>"
        )
    else:
        narrative_block = ""

    data_json = json.dumps(chart_data(payload))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{e(account_name)} — Bid Optimization Report</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="container">
  {head}
  {charts_block}
  {''.join(obj_sections)}
  {narrative_block}
  <footer>Generated from data/recommendations.json + output/report.md · LinkedIn Ads bid optimizer</footer>
</div>
<script>window.__BID_DATA__ = {data_json};</script>
<script>{JS}</script>
<script>{CHART_JS}</script>
</body>
</html>
"""


def generate_html(account_name):
    config.ensure_dirs()
    recs_path = os.path.join(config.DATA_DIR, "recommendations.json")
    md_path = os.path.join(config.OUTPUT_DIR, "report.md")
    out_path = os.path.join(config.OUTPUT_DIR, "report.html")

    if not os.path.exists(recs_path):
        raise FileNotFoundError(
            f"{recs_path} missing. Run analyze.py first."
        )
    with open(recs_path) as f:
        payload = json.load(f)

    narrative_md = ""
    if os.path.exists(md_path):
        with open(md_path) as f:
            narrative_md = f.read()

    html_content = build_html(payload, account_name, narrative_md)
    with open(out_path, "w") as f:
        f.write(html_content)

    print(f"HTML dashboard saved to {out_path}")
    print(f"Open in browser: file://{os.path.abspath(out_path)}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-name", default="LinkedIn Ads Account")
    args = parser.parse_args()
    generate_html(args.account_name)
