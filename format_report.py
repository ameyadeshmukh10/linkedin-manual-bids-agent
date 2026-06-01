"""
Script 6 — Format recommendations into a hierarchical report.

Structure:
  Portfolio summary (totals, direction counts, anomaly counts)
  For each Campaign (LinkedIn group), grouped by objective:
    - Campaign header: name, objective, daily budget, group pacing
    - Ad Set rows, each with:
        - direction (🚨 / 🟢 / 🔴 / ⚪) and reason
        - bid: current → recommended (%)
        - metrics summary
        - anomaly flags (if any)

Formats: markdown (default), slack (plain text), email (HTML).
"""
import argparse
import html
import json
import os
from collections import defaultdict

import config


DIR_EMOJI = {"ESCALATE": "🚨", "INCREASE": "🟢", "DECREASE": "🔴", "HOLD": "⚪"}
OBJECTIVE_ORDER = [
    "LEAD_GENERATION", "WEBSITE_CONVERSION", "WEBSITE_VISIT",
    "BRAND_AWARENESS", "VIDEO_VIEW", "VIDEO_VIEWS", "ENGAGEMENT",
    "JOB_APPLICANTS",
]

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


def _fmt_money(v):
    if v is None:
        return "N/A"
    return f"${v:,.2f}"


def _fmt_pct(v):
    if v is None:
        return "N/A"
    return f"{v:.2f}%"


def _fmt_int(v):
    if v is None:
        return "N/A"
    return f"{int(v):,}"


def _fmt_bid(value, bid_type):
    if value is None:
        return "—"
    return f"${value:.2f} {bid_type or ''}".strip()


def _group_by_objective(payload):
    by_obj = defaultdict(list)
    for g in payload["campaigns"]:
        by_obj[g.get("objectiveType") or "UNKNOWN"].append(g)
    # Sort campaigns within each objective by total spend desc
    for obj in by_obj:
        by_obj[obj].sort(key=lambda g: g["totals"]["spend"], reverse=True)
    # Return in canonical objective order
    ordered = []
    for obj in OBJECTIVE_ORDER:
        if obj in by_obj:
            ordered.append((obj, by_obj.pop(obj)))
    for obj, groups in by_obj.items():  # any remaining
        ordered.append((obj, groups))
    return ordered


def _portfolio_summary_lines(payload, account_name):
    p = payload["portfolio"]
    t = p["totals"]
    dr = payload["dateRange"]
    window_days = payload["windowDays"]
    dir_counts = p["directionCounts"]
    anomaly_counts = p["anomalyCounts"]

    lines = [
        f"{account_name} · {dr['start']} → {dr['end']} ({window_days} days)",
        f"{len(payload['campaigns'])} campaigns · {t['adSetCount']} ad sets",
        f"Total spend: ${t['spend']:,.2f}  |  "
        f"Impressions: {t['impressions']:,}  |  "
        f"Clicks: {t['clicks']:,}  |  "
        f"Leads: {t['leads']:,}  |  "
        f"Conversions: {t['conversions']:,}",
        f"Recommendations: "
        f"{dir_counts.get('ESCALATE', 0)} escalate · "
        f"{dir_counts.get('INCREASE', 0)} increase · "
        f"{dir_counts.get('DECREASE', 0)} decrease · "
        f"{dir_counts.get('HOLD', 0)} hold",
    ]
    if anomaly_counts:
        friendly = {
            "NO_DATA": "⚪ no data",
            "ZERO_CONVERSIONS": "🔴 zero conversions",
            "NO_LEADS": "🟡 no leads",
            "HIGH_CPL": "🔴 high CPL",
            "LOW_CTR": "🟡 low CTR",
        }
        parts = [
            f"{friendly.get(k, k)}: {v}" for k, v in anomaly_counts.items()
        ]
        lines.append("Anomalies: " + " · ".join(parts))
    if p.get("avgCplLeadGen"):
        lines.append(f"Lead Gen avg CPL (portfolio): ${p['avgCplLeadGen']:,.2f}")
    return lines


def _objective_specific_metrics(a):
    """Return (label, value_str) pairs specific to the ad set's objective."""
    obj = a.get("objectiveType")
    m = a["metrics"]
    d = a["derived"]
    pairs = []
    if obj == "LEAD_GENERATION":
        pairs.append(("Leads", _fmt_int(m.get("oneClickLeads", 0))))
        pairs.append(("CPL", _fmt_money(d.get("cpl"))))
        pairs.append(("Form opens", _fmt_int(m.get("oneClickLeadFormOpens", 0))))
        pairs.append(("Lead conv%", _fmt_pct(d.get("leadConvRatePct"))))
    elif obj in ("WEBSITE_CONVERSION", "WEBSITE_VISIT", "JOB_APPLICANTS"):
        pairs.append(("LP clicks", _fmt_int(m.get("landingPageClicks", 0))))
        pairs.append(("Conversions", _fmt_int(m.get("externalWebsiteConversions", 0))))
        pairs.append(("Cost/conv", _fmt_money(d.get("costPerConversion"))))
    elif obj == "BRAND_AWARENESS":
        pairs.append(("Follows", _fmt_int(m.get("follows", 0))))
        pairs.append(("Likes", _fmt_int(m.get("likes", 0))))
        pairs.append(("CPM", _fmt_money(d.get("cpm"))))
    elif obj in ("VIDEO_VIEW", "VIDEO_VIEWS"):
        pairs.append(("Views", _fmt_int(m.get("videoViews", 0))))
        pairs.append(("Completion%", _fmt_pct(d.get("completionRatePct"))))
        pairs.append(("CPV", _fmt_money(d.get("cpv"))))
    elif obj == "ENGAGEMENT":
        pairs.append(("Engagements", _fmt_int(m.get("totalEngagements", 0))))
        pairs.append(("Eng rate", _fmt_pct(d.get("engagementRatePct"))))
        pairs.append(("Cost/eng", _fmt_money(d.get("costPerEngagement"))))
    return pairs


# --- Markdown ---

def _md_ad_set(a):
    ass = a.get("assessment", {})
    direction = ass.get("direction", "HOLD")
    emoji = DIR_EMOJI.get(direction, "")
    bid_type = a.get("bidType") or ""
    cur = ass.get("currentValue")
    rec = ass.get("recommendedValue")
    pct = ass.get("adjustmentPct", 0)

    lines = [
        f"    - **{emoji} {a['name']}** — "
        f"{a.get('bidStrategy', '—')} · {bid_type} · "
        f"current bid: {_fmt_bid(cur, bid_type)}"
    ]
    if direction in ("INCREASE", "DECREASE") and rec is not None:
        lines.append(
            f"      - **{direction}** to {_fmt_bid(rec, bid_type)} "
            f"(**{pct:+.2f}%**)"
        )
    else:
        lines.append(f"      - **{direction}**")

    if ass.get("reason"):
        lines.append(f"      - {ass['reason']}")

    pacing = a.get("pacing", {})
    m = a["metrics"]
    lines.append(
        f"      - Spend ${m['spend']:,.2f} · "
        f"{m['impressions']:,} imps · "
        f"CTR {_fmt_pct(a['derived'].get('ctrPct'))} · "
        f"CPC {_fmt_money(a['derived'].get('cpc'))}"
    )
    obj_metrics = _objective_specific_metrics(a)
    if obj_metrics:
        lines.append(
            "      - "
            + " · ".join(f"{k}: {v}" for k, v in obj_metrics)
        )

    if pacing.get("effectiveDailyBudget") is not None:
        src = pacing["budgetSource"]
        src_label = "ad set" if src == "AD_SET" else "implied (group share)"
        lines.append(
            f"      - Daily pacing: ${m['avgDailySpend']:,.2f} / "
            f"${pacing['effectiveDailyBudget']:,.2f} ({src_label}) = "
            f"{pacing['spendVsBudgetPct']}%"
        )

    for flag in a.get("anomalyFlags", []):
        lines.append(f"      - {flag['emoji']} **{flag['label']}** — {flag['detail']}")

    lines.append(f"      - Confidence: {ass.get('confidence', 'LOW')}")
    return "\n".join(lines)


def _md_campaign(group):
    obj = group.get("objectiveType") or "UNKNOWN"
    t = group["totals"]
    header = (
        f"### {group['name']}\n"
        f"_Objective:_ {OBJECTIVE_LABELS.get(obj, obj)}  |  "
        f"_Ad sets:_ {t['adSetCount']} ({t['adSetsWithData']} with data)  |  "
        f"_Spend:_ ${t['spend']:,.2f}"
    )
    if group.get("dailyBudget"):
        header += f"  |  _Group daily budget:_ ${group['dailyBudget']:,.2f}"
    gp = group.get("groupPacing")
    if gp and gp.get("utilizationPct") is not None:
        header += f"  |  _Group pacing:_ {gp['utilizationPct']}%"
    # Sort ad sets: escalations first, then by spend desc
    dir_rank = {"ESCALATE": 0, "INCREASE": 1, "DECREASE": 2, "HOLD": 3}
    sorted_ad_sets = sorted(
        group["adSets"],
        key=lambda a: (
            dir_rank.get(a.get("assessment", {}).get("direction", "HOLD"), 9),
            -a["metrics"]["spend"],
        ),
    )
    ad_set_blocks = [_md_ad_set(a) for a in sorted_ad_sets]
    return header + "\n" + "\n".join(ad_set_blocks)


def format_markdown(payload, account_name):
    lines = [
        "# LinkedIn Ads — Bid Optimization Report",
        "",
    ]
    for s in _portfolio_summary_lines(payload, account_name):
        lines.append(s)
    lines.append("")
    for obj, groups in _group_by_objective(payload):
        lines.append(f"## {OBJECTIVE_LABELS.get(obj, obj)}")
        lines.append("")
        for g in groups:
            lines.append(_md_campaign(g))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- Slack (plain text) ---

def _slack_ad_set(a):
    ass = a.get("assessment", {})
    emoji = DIR_EMOJI.get(ass.get("direction", "HOLD"), "")
    bid_type = a.get("bidType") or ""
    cur = ass.get("currentValue")
    rec = ass.get("recommendedValue")
    direction = ass.get("direction", "HOLD")
    lines = [f"  {emoji} *{a['name']}* · {a.get('bidStrategy')} · current: {_fmt_bid(cur, bid_type)}"]
    if direction in ("INCREASE", "DECREASE") and rec is not None:
        lines.append(
            f"     → {direction} to {_fmt_bid(rec, bid_type)} "
            f"({ass.get('adjustmentPct', 0):+.2f}%)"
        )
    else:
        lines.append(f"     → {direction}")
    if ass.get("reason"):
        lines.append(f"     {ass['reason']}")
    m = a["metrics"]
    lines.append(
        f"     Spend ${m['spend']:,.2f} · {m['impressions']:,} imps · "
        f"CTR {_fmt_pct(a['derived'].get('ctrPct'))}"
    )
    for flag in a.get("anomalyFlags", []):
        lines.append(f"     {flag['emoji']} {flag['label']}: {flag['detail']}")
    return "\n".join(lines)


def format_slack(payload, account_name):
    lines = []
    for s in _portfolio_summary_lines(payload, account_name):
        lines.append(s)
    lines.append("")
    for obj, groups in _group_by_objective(payload):
        lines.append(f"=== {OBJECTIVE_LABELS.get(obj, obj)} ===")
        for g in groups:
            t = g["totals"]
            lines.append(
                f"\n*{g['name']}* · Spend ${t['spend']:,.2f} · "
                f"{t['adSetCount']} ad sets"
            )
            dir_rank = {"ESCALATE": 0, "INCREASE": 1, "DECREASE": 2, "HOLD": 3}
            sorted_ad_sets = sorted(
                g["adSets"],
                key=lambda a: (
                    dir_rank.get(a.get("assessment", {}).get("direction", "HOLD"), 9),
                    -a["metrics"]["spend"],
                ),
            )
            for a in sorted_ad_sets:
                lines.append(_slack_ad_set(a))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- HTML / email ---

def format_email(payload, account_name):
    dr = payload["dateRange"]
    direction_total = payload["portfolio"]["directionCounts"]
    actions = (
        direction_total.get("ESCALATE", 0)
        + direction_total.get("INCREASE", 0)
        + direction_total.get("DECREASE", 0)
    )
    subject = (
        f"LinkedIn Ads Bid Report — {dr['start']} to {dr['end']} — "
        f"{actions} actions recommended"
    )
    parts = [
        "<!DOCTYPE html><html><head>"
        f"<meta charset=\"UTF-8\"><title>{html.escape(subject)}</title></head>"
        "<body style='font-family:Arial,sans-serif;max-width:1100px;margin:auto'>"
        f"<h1>{html.escape(subject)}</h1>"
    ]
    for s in _portfolio_summary_lines(payload, account_name):
        parts.append(f"<p>{html.escape(s)}</p>")

    for obj, groups in _group_by_objective(payload):
        parts.append(
            f"<h2 style='border-bottom:2px solid #2b6cb0;padding-bottom:4px'>"
            f"{html.escape(OBJECTIVE_LABELS.get(obj, obj))}</h2>"
        )
        for g in groups:
            t = g["totals"]
            parts.append(
                f"<h3>{html.escape(g['name'])}</h3>"
                f"<p style='color:#555'>Objective: {html.escape(OBJECTIVE_LABELS.get(obj, obj))} "
                f"· {t['adSetCount']} ad sets · Spend ${t['spend']:,.2f}"
                + (f" · Group daily: ${g['dailyBudget']:,.2f}"
                   if g.get("dailyBudget") else "")
                + "</p>"
            )
            parts.append(
                "<table cellpadding='6' cellspacing='0' border='1' "
                "style='border-collapse:collapse;font-size:13px;width:100%'>"
                "<thead><tr style='background:#f4f4f4'>"
                "<th>Ad Set</th><th>Direction</th><th>Current → Recommended</th>"
                "<th>Reason</th><th>Metrics</th><th>Flags</th>"
                "</tr></thead><tbody>"
            )
            dir_rank = {"ESCALATE": 0, "INCREASE": 1, "DECREASE": 2, "HOLD": 3}
            sorted_ad_sets = sorted(
                g["adSets"],
                key=lambda a: (
                    dir_rank.get(a.get("assessment", {}).get("direction", "HOLD"), 9),
                    -a["metrics"]["spend"],
                ),
            )
            for a in sorted_ad_sets:
                ass = a.get("assessment", {})
                direction = ass.get("direction", "HOLD")
                bid_type = a.get("bidType") or ""
                cur = ass.get("currentValue")
                rec = ass.get("recommendedValue")
                pct = ass.get("adjustmentPct", 0)
                bid_cell = _fmt_bid(cur, bid_type)
                if direction in ("INCREASE", "DECREASE") and rec is not None:
                    bid_cell += f" → <strong>{_fmt_bid(rec, bid_type)}</strong> ({pct:+.2f}%)"
                m = a["metrics"]
                metrics_cell = (
                    f"${m['spend']:,.2f} spend · "
                    f"{m['impressions']:,} imps · "
                    f"CTR {_fmt_pct(a['derived'].get('ctrPct'))}"
                )
                obj_metrics = _objective_specific_metrics(a)
                if obj_metrics:
                    metrics_cell += "<br>" + " · ".join(
                        f"{k}: {v}" for k, v in obj_metrics
                    )
                flag_cell = "<br>".join(
                    f"{f['emoji']} <strong>{f['label']}</strong>: {html.escape(f['detail'])}"
                    for f in a.get("anomalyFlags", [])
                ) or "—"
                parts.append(
                    f"<tr>"
                    f"<td><strong>{html.escape(a['name'])}</strong><br>"
                    f"<span style='color:#777'>{html.escape(a.get('bidStrategy', '') or '')}</span></td>"
                    f"<td>{DIR_EMOJI.get(direction, '')} {direction}</td>"
                    f"<td>{bid_cell}</td>"
                    f"<td>{html.escape(ass.get('reason', ''))}</td>"
                    f"<td>{metrics_cell}</td>"
                    f"<td>{flag_cell}</td>"
                    f"</tr>"
                )
            parts.append("</tbody></table>")

    parts.append("</body></html>")
    return subject, "\n".join(parts)


def format_report(fmt, account_name):
    config.ensure_dirs()
    with open(os.path.join(config.DATA_DIR, "recommendations.json")) as f:
        payload = json.load(f)

    if fmt == "markdown":
        content = format_markdown(payload, account_name)
        out_path = os.path.join(config.OUTPUT_DIR, "report.md")
        with open(out_path, "w") as f:
            f.write(content)
    elif fmt == "slack":
        content = format_slack(payload, account_name)
        out_path = os.path.join(config.OUTPUT_DIR, "report_slack.txt")
        with open(out_path, "w") as f:
            f.write(content)
    elif fmt == "email":
        subject, body = format_email(payload, account_name)
        out_path = os.path.join(config.OUTPUT_DIR, "report_email.html")
        with open(out_path, "w") as f:
            f.write(body)
        content = f"Subject: {subject}\n\n{body}"
    else:
        raise ValueError(f"Unknown format: {fmt}")

    print(content)
    print(f"\nReport saved to {out_path}")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=["markdown", "slack", "email"],
        default="markdown",
    )
    parser.add_argument("--account-name", default="LinkedIn Ads Account")
    args = parser.parse_args()
    format_report(args.format, args.account_name)
