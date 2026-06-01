"""
Script 5 — Produce per-ad-set assessments combining delivery/pacing + objective
achievement, with bid adjustment recommendations.

Each ad set gets:
  - direction: INCREASE | DECREASE | HOLD | ESCALATE
  - recommendedValue (if INCREASE/DECREASE and bid-adjustable)
  - reason (citing the numbers that drove the call)
  - anomalyFlags[]: e.g. "⚪ NO DATA", "🔴 ZERO CONVERSIONS", "🟡 LOW CTR", "🔴 HIGH CPL"
  - confidence: HIGH | MEDIUM | LOW (based on impressions volume)

Output: data/recommendations.json with structure:
  {
    dateRange, windowDays,
    portfolio: { totals... },
    campaigns: [ { group fields, adSets: [ {context..., assessment, anomalyFlags, bid} ] } ]
  }
"""
import json
import os
from collections import Counter

import config


# --- Bid decision constants ---

BID_FLOORS = {"CPC": 3.50, "CPM": 2.00, "CPV": 0.01}
MAX_INCREASE_FACTOR = 1.25
MAX_DECREASE_FACTOR = 0.75

# Anomaly thresholds (from reference reporting agent, adjusted per prior tuning)
ZERO_CONVERSIONS_SPEND_THRESHOLD = 500    # $ spent with 0 conversions → flag
NO_LEADS_SPEND_THRESHOLD = 300            # lead gen, 0 leads, >$ → flag
LOW_CTR_IMPRESSIONS_MIN = 5000            # min impressions to even check CTR
LOW_CTR_THRESHOLD_PCT = 0.2               # < 0.2% CTR with enough impressions
HIGH_CPL_MULTIPLIER = 2.0                 # > 2x portfolio avg CPL → flag


def _fmt_money(v, currency=""):
    if v is None:
        return "N/A"
    return f"${v:,.2f} {currency}".strip()


def _fmt_pct(v):
    if v is None:
        return "N/A"
    return f"{v:.2f}%"


def _confidence(impressions, ad_sets_with_data):
    """Confidence is a blend of volume + signal. Very low impressions = LOW.
    Low = 0-1000, Medium = 1000-10000, High = 10000+."""
    if impressions is None or impressions < 1000:
        return "LOW"
    if impressions < 10000:
        return "MEDIUM"
    return "HIGH"


def _portfolio_avg_cpl(campaigns):
    cpls = []
    for g in campaigns:
        for a in g["adSets"]:
            if a["objectiveType"] == "LEAD_GENERATION":
                cpl = a["derived"].get("cpl")
                if cpl is not None and cpl > 0:
                    cpls.append(cpl)
    if not cpls:
        return None
    return sum(cpls) / len(cpls)


# --- Anomaly detection ---

def _detect_anomalies(ad_set, avg_cpl):
    flags = []
    m = ad_set["metrics"]
    impressions = m["impressions"]
    spend = m["spend"]
    obj = ad_set["objectiveType"]

    if impressions == 0:
        flags.append({
            "code": "NO_DATA",
            "emoji": "⚪",
            "label": "NO DATA",
            "detail": f"Zero impressions over window; spend=${spend:,.2f}.",
        })
        return flags  # other flags meaningless without impressions

    # Zero conversions with material spend (applies broadly — objective-agnostic
    # view: if you're paying $500+ and got nothing trackable, investigate)
    ext_conv = m.get("externalWebsiteConversions", 0)
    leads = m.get("oneClickLeads", 0)
    if (
        obj in ("LEAD_GENERATION", "WEBSITE_CONVERSION", "WEBSITE_VISIT", "JOB_APPLICANTS")
        and spend >= ZERO_CONVERSIONS_SPEND_THRESHOLD
        and ext_conv == 0
        and leads == 0
    ):
        flags.append({
            "code": "ZERO_CONVERSIONS",
            "emoji": "🔴",
            "label": "ZERO CONVERSIONS",
            "detail": (
                f"${spend:,.2f} spent with zero leads and zero website "
                f"conversions over window."
            ),
        })

    if obj == "LEAD_GENERATION":
        if leads == 0 and spend >= NO_LEADS_SPEND_THRESHOLD:
            flags.append({
                "code": "NO_LEADS",
                "emoji": "🟡",
                "label": "NO LEADS",
                "detail": f"Lead gen ad set spent ${spend:,.2f} with 0 one-click leads.",
            })
        cpl = ad_set["derived"].get("cpl")
        if cpl and avg_cpl and avg_cpl > 0 and cpl > HIGH_CPL_MULTIPLIER * avg_cpl:
            flags.append({
                "code": "HIGH_CPL",
                "emoji": "🔴",
                "label": "HIGH CPL",
                "detail": (
                    f"CPL ${cpl:,.2f} is {cpl / avg_cpl:.1f}× portfolio "
                    f"avg CPL of ${avg_cpl:,.2f}."
                ),
            })

    # LOW CTR — skip BRAND_AWARENESS (CTR is not the signal for brand)
    if obj != "BRAND_AWARENESS" and impressions >= LOW_CTR_IMPRESSIONS_MIN:
        ctr_pct = ad_set["derived"].get("ctrPct")
        if ctr_pct is not None and ctr_pct < LOW_CTR_THRESHOLD_PCT:
            flags.append({
                "code": "LOW_CTR",
                "emoji": "🟡",
                "label": "LOW CTR",
                "detail": (
                    f"CTR {ctr_pct:.2f}% on {impressions:,} impressions "
                    f"(threshold {LOW_CTR_THRESHOLD_PCT}%)."
                ),
            })

    return flags


# --- Bid recommendation ---

def _clamp_and_round(current, recommended, bid_type):
    if current is None or recommended is None:
        return None, 0.0
    # Floor
    floor = BID_FLOORS.get(bid_type)
    if floor is not None and recommended < floor:
        recommended = floor
    # Cap at ±25%
    max_inc = current * MAX_INCREASE_FACTOR
    min_dec = current * MAX_DECREASE_FACTOR
    if recommended > max_inc:
        recommended = max_inc
    if recommended < min_dec:
        recommended = min_dec
    recommended = round(recommended, 2)
    pct = round(((recommended - current) / current) * 100, 2) if current else 0
    return recommended, pct


def _recommend_bid(ad_set, anomaly_codes):
    """Produce direction + recommendedValue + reason. Hierarchy:
       1. ESCALATE when anomalies indicate non-bid issues (HIGH_CPL, ZERO_CONVERSIONS,
          NO_LEADS in LG). Bid change won't fix these.
       2. If bidStrategy is MAX_DELIVERY: HOLD or ESCALATE only.
       3. Otherwise use pacing + objective signals to pick INCREASE/DECREASE/HOLD.
    """
    strategy = ad_set.get("bidStrategy")
    bid_type = ad_set.get("bidType")
    pacing = ad_set.get("pacing", {})
    objective = ad_set.get("objectiveType")
    m = ad_set["metrics"]
    derived = ad_set["derived"]

    result = {
        "direction": "HOLD",
        "adjustmentTarget": None,
        "currentValue": None,
        "recommendedValue": None,
        "adjustmentPct": 0,
        "reason": "",
    }

    # Identify adjustment target and current bid value
    if strategy in ("MANUAL_BID", "ENHANCED_CPC"):
        result["adjustmentTarget"] = "bidAmount"
        result["currentValue"] = ad_set.get("bidAmount")
    elif strategy in ("COST_CAP", "TARGET_COST"):
        result["adjustmentTarget"] = "bidCap"
        result["currentValue"] = ad_set.get("bidCap")
    current_value = result["currentValue"]

    # --- Hard blockers first ---

    if "NO_DATA" in anomaly_codes:
        result["direction"] = "HOLD"
        result["reason"] = (
            "No impressions in window. Bid change won't help until delivery "
            "starts — investigate creative, targeting, or ad set activation."
        )
        return result

    if {"ZERO_CONVERSIONS", "NO_LEADS", "HIGH_CPL"} & set(anomaly_codes):
        result["direction"] = "ESCALATE"
        result["reason"] = (
            "Anomalies indicate a non-bid issue (creative/audience/landing page "
            "or tracking). Bid adjustment won't fix this."
        )
        return result

    # MAX_DELIVERY: no bid to adjust
    if strategy == "MAX_DELIVERY":
        if pacing.get("underDelivering"):
            result["direction"] = "ESCALATE"
            result["reason"] = (
                f"MAX_DELIVERY ad set under-delivering ({pacing.get('spendVsBudgetPct', 0)}% "
                f"of implied daily). Review budget, audience size, or creative quality."
            )
        else:
            result["direction"] = "HOLD"
            result["reason"] = "MAX_DELIVERY strategy. No bid to adjust."
        return result

    if current_value is None:
        result["direction"] = "HOLD"
        result["reason"] = f"Bid strategy {strategy} but no bid value captured."
        return result

    # --- Delivery + objective signals ---

    # Signal 1: delivery pacing
    under_delivering = pacing.get("underDelivering")
    over_delivering = pacing.get("overDelivering")

    # Signal 2: objective achievement
    ctr_pct = derived.get("ctrPct") or 0
    cpc = derived.get("cpc")
    cpm = derived.get("cpm")

    # Lead gen: under-delivering + some leads = increase bid to buy more
    if objective == "LEAD_GENERATION":
        leads = m.get("oneClickLeads", 0)
        if under_delivering and leads > 0 and ctr_pct >= 0.4:
            recommended = current_value * 1.20
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "INCREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"Under-delivering at {pacing.get('spendVsBudgetPct')}% of implied daily, "
                    f"CTR {ctr_pct:.2f}%, {leads} leads. Bid is below clearing price."
                ),
            })
            return result
        if under_delivering and ctr_pct >= 0.4:
            recommended = current_value * 1.10
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "INCREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"Under-delivering at {pacing.get('spendVsBudgetPct')}% of implied daily, "
                    f"CTR {ctr_pct:.2f}% (acceptable). Moderate bid increase to test clearing price."
                ),
            })
            return result
        if over_delivering and cpc is not None and cpc > 15 and ctr_pct < 0.4:
            recommended = current_value * 0.85
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "DECREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"Over-delivering at {pacing.get('spendVsBudgetPct')}% of implied daily, "
                    f"CPC ${cpc:.2f}, CTR {ctr_pct:.2f}%. Bid is above efficient level."
                ),
            })
            return result

    # Website objectives: high CPC + low CTR = decrease bid
    if objective in ("WEBSITE_VISIT", "WEBSITE_CONVERSION", "JOB_APPLICANTS"):
        if cpc is not None and cpc > 15 and ctr_pct < 0.4:
            recommended = current_value * 0.80
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "DECREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"CPC ${cpc:.2f}, CTR {ctr_pct:.2f}%. Paying above efficient "
                    f"rate for audience engagement level."
                ),
            })
            return result
        if under_delivering and ctr_pct >= 0.4:
            recommended = current_value * 1.10
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "INCREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"Under-delivering at {pacing.get('spendVsBudgetPct')}% of implied "
                    f"daily, CTR {ctr_pct:.2f}%. Moderate bid increase."
                ),
            })
            return result

    # Brand / video: high CPM + low reach = decrease bid
    if objective in ("BRAND_AWARENESS", "VIDEO_VIEW", "VIDEO_VIEWS"):
        avg_daily_imps = m["impressions"] / max(1, 7)  # proxy
        if cpm is not None and cpm > 50 and avg_daily_imps < 1000:
            recommended = current_value * 0.85
            recommended, pct = _clamp_and_round(current_value, recommended, bid_type)
            result.update({
                "direction": "DECREASE",
                "recommendedValue": recommended,
                "adjustmentPct": pct,
                "reason": (
                    f"CPM ${cpm:.2f} with low daily impression volume "
                    f"({avg_daily_imps:.0f}/day). High cost relative to reach."
                ),
            })
            return result

    # Default HOLD
    result["direction"] = "HOLD"
    pieces = []
    if ctr_pct:
        pieces.append(f"CTR {ctr_pct:.2f}%")
    if cpc is not None:
        pieces.append(f"CPC ${cpc:.2f}")
    if pacing.get("spendVsBudgetPct") is not None:
        pieces.append(f"{pacing['spendVsBudgetPct']}% of implied daily")
    result["reason"] = (
        "Performance within acceptable range. " + ", ".join(pieces) + "."
        if pieces else "No actionable signal."
    )
    return result


def analyze():
    config.ensure_dirs()
    with open(os.path.join(config.DATA_DIR, "context_payload.json")) as f:
        payload = json.load(f)

    avg_cpl = _portfolio_avg_cpl(payload["campaigns"])

    total_direction_counts = Counter()
    total_anomaly_counts = Counter()
    portfolio_totals = {
        "spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "leads": 0,
        "conversions": 0,
        "adSetCount": 0,
    }

    for group in payload["campaigns"]:
        for a in group["adSets"]:
            m = a["metrics"]
            portfolio_totals["spend"] += m["spend"]
            portfolio_totals["impressions"] += m["impressions"]
            portfolio_totals["clicks"] += m["clicks"]
            portfolio_totals["leads"] += m.get("oneClickLeads", 0)
            portfolio_totals["conversions"] += m.get("externalWebsiteConversions", 0)
            portfolio_totals["adSetCount"] += 1

            flags = _detect_anomalies(a, avg_cpl)
            codes = [f["code"] for f in flags]
            bid = _recommend_bid(a, codes)
            conf = _confidence(m["impressions"], group["totals"]["adSetsWithData"])

            a["anomalyFlags"] = flags
            a["assessment"] = {
                **bid,
                "confidence": conf,
            }

            total_direction_counts[bid["direction"]] += 1
            for code in codes:
                total_anomaly_counts[code] += 1

    portfolio_totals["spend"] = round(portfolio_totals["spend"], 2)
    payload["portfolio"] = {
        "totals": portfolio_totals,
        "avgCplLeadGen": round(avg_cpl, 2) if avg_cpl else None,
        "directionCounts": dict(total_direction_counts),
        "anomalyCounts": dict(total_anomaly_counts),
    }

    out = os.path.join(config.DATA_DIR, "recommendations.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Analyzed {portfolio_totals['adSetCount']} ad sets → {out}")
    print(f"Direction counts: {dict(total_direction_counts)}")
    if total_anomaly_counts:
        print(f"Anomaly counts:   {dict(total_anomaly_counts)}")
    print(f"Portfolio spend:  ${portfolio_totals['spend']:,.2f}")
    return payload


if __name__ == "__main__":
    analyze()
