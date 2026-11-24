"""Cost Explorer queries: breakdowns, spike explanation and forecast.

Every function takes a boto3 Cost Explorer client so it can be stubbed in tests.
Note: each Cost Explorer API request costs $0.01, so calls are kept few and grouped.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from ..models import CostBreakdown, CostDelta, CostLine, Forecast, SpikeDriver, SpikeExplanation

GROUP_KEYS = {
    "service": "SERVICE",
    "usage_type": "USAGE_TYPE",
    "region": "REGION",
    "account": "LINKED_ACCOUNT",
    "instance_type": "INSTANCE_TYPE",
}

# Cost Explorer splits out these lines; they distort "what got more expensive".
_NOISE = {"Tax", "Refund", "Credit"}


def _iso(d: date) -> str:
    return d.isoformat()


def _sum_by_group(ce: Any, start: date, end: date, group_key: str, metric: str,
                  flt: dict | None = None) -> tuple[dict[str, float], str]:
    """Return {group_value: amount} summed over the whole period, plus currency."""
    totals: dict[str, float] = defaultdict(float)
    currency = "USD"
    kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": _iso(start), "End": _iso(end)},
        "Granularity": "MONTHLY" if (end - start).days > 62 else "DAILY",
        "Metrics": [metric],
        "GroupBy": [{"Type": "DIMENSION", "Key": group_key}],
    }
    if flt:
        kwargs["Filter"] = flt
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                m = g["Metrics"][metric]
                totals[g["Keys"][0]] += float(m["Amount"])
                currency = m.get("Unit", currency)
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token
    return dict(totals), currency


def cost_breakdown(ce: Any, start: date, end: date, group_by: str = "service",
                   metric: str = "UnblendedCost", max_rows: int = 25) -> CostBreakdown:
    if group_by not in GROUP_KEYS:
        raise ValueError(f"group_by must be one of {sorted(GROUP_KEYS)}")
    if end <= start:
        raise ValueError("end must be after start")
    totals, currency = _sum_by_group(ce, start, end, GROUP_KEYS[group_by], metric)
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    lines = [
        CostLine(key=k, amount=round(v, 2), share_pct=round(100 * v / total, 1) if total else 0.0)
        for k, v in ranked[:max_rows] if round(v, 2) != 0
    ]
    return CostBreakdown(start=_iso(start), end=_iso(end), group_by=group_by, currency=currency,
                         total=round(total, 2), lines=lines, truncated=len(ranked) > max_rows)


def _pct(prev: float, cur: float) -> float | None:
    return round(100 * (cur - prev) / prev, 1) if prev else None


def _deltas(prev: dict[str, float], cur: dict[str, float]) -> list[CostDelta]:
    keys = (set(prev) | set(cur)) - _NOISE
    out = [
        CostDelta(key=k, previous=round(prev.get(k, 0.0), 2), current=round(cur.get(k, 0.0), 2),
                  delta=round(cur.get(k, 0.0) - prev.get(k, 0.0), 2),
                  delta_pct=_pct(prev.get(k, 0.0), cur.get(k, 0.0)))
        for k in keys
    ]
    return sorted(out, key=lambda d: abs(d.delta), reverse=True)


def explain_spike(ce: Any, today: date, window_days: int = 7, metric: str = "UnblendedCost",
                  drill_down: int = 3, max_rows: int = 10) -> SpikeExplanation:
    """Compare the last `window_days` complete days with the window before it.

    Today's partial data is excluded because Cost Explorer lags by up to 24h.
    """
    if not 1 <= window_days <= 90:
        raise ValueError("window_days must be between 1 and 90")
    cur_end = today
    cur_start = cur_end - timedelta(days=window_days)
    prev_start = cur_start - timedelta(days=window_days)

    prev, currency = _sum_by_group(ce, prev_start, cur_start, "SERVICE", metric)
    cur, _ = _sum_by_group(ce, cur_start, cur_end, "SERVICE", metric)
    movers = _deltas(prev, cur)

    drivers: list[SpikeDriver] = []
    for m in [m for m in movers if m.delta > 0][:drill_down]:
        flt = {"Dimensions": {"Key": "SERVICE", "Values": [m.key]}}
        p, _ = _sum_by_group(ce, prev_start, cur_start, "USAGE_TYPE", metric, flt)
        c, _ = _sum_by_group(ce, cur_start, cur_end, "USAGE_TYPE", metric, flt)
        ups = [d for d in _deltas(p, c) if d.delta > 0][:5]
        drivers.append(SpikeDriver(service=m.key, service_delta=m.delta, top_usage_types=ups))

    prev_total = sum(v for k, v in prev.items() if k not in _NOISE)
    cur_total = sum(v for k, v in cur.items() if k not in _NOISE)
    delta = cur_total - prev_total

    if drivers:
        top = drivers[0]
        ut = f" (mostly {top.top_usage_types[0].key})" if top.top_usage_types else ""
        summary = (f"Spend went from {prev_total:.2f} to {cur_total:.2f} {currency} "
                   f"({delta:+.2f}). Largest increase: {top.service} {top.service_delta:+.2f}{ut}.")
    else:
        summary = (f"Spend went from {prev_total:.2f} to {cur_total:.2f} {currency} "
                   f"({delta:+.2f}). No service increased.")

    return SpikeExplanation(
        current_window=f"{_iso(cur_start)}..{_iso(cur_end)}",
        previous_window=f"{_iso(prev_start)}..{_iso(cur_start)}",
        currency=currency, previous_total=round(prev_total, 2), current_total=round(cur_total, 2),
        delta=round(delta, 2), delta_pct=_pct(prev_total, cur_total),
        biggest_movers=movers[:max_rows], drivers=drivers, summary=summary,
    )


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def forecast_month(ce: Any, today: date, metric: str = "UnblendedCost") -> Forecast:
    month_start = _month_start(today)
    next_month = _next_month(today)
    prev_month_start = _month_start(month_start - timedelta(days=1))

    def total(start: date, end: date) -> tuple[float, str]:
        if end <= start:
            return 0.0, "USD"
        resp = ce.get_cost_and_usage(TimePeriod={"Start": _iso(start), "End": _iso(end)},
                                     Granularity="MONTHLY", Metrics=[metric])
        amt, unit = 0.0, "USD"
        for p in resp.get("ResultsByTime", []):
            m = p["Total"][metric]
            amt += float(m["Amount"])
            unit = m.get("Unit", unit)
        return amt, unit

    mtd, currency = total(month_start, today)
    prev_total, _ = total(prev_month_start, month_start)

    # Forecast metric names are SCREAMING_SNAKE (UnblendedCost -> UNBLENDED_COST).
    fmetric = "".join("_" + c if c.isupper() else c for c in metric).lstrip("_").upper()
    # today is always before next_month, so there is at least one day left to forecast.
    resp = ce.get_cost_forecast(TimePeriod={"Start": _iso(today), "End": _iso(next_month)},
                                Metric=fmetric, Granularity="MONTHLY",
                                PredictionIntervalLevel=80)
    remaining = float(resp["Total"]["Amount"])
    low = high = None
    fr = resp.get("ForecastResultsByTime") or []
    if fr and "PredictionIntervalLowerBound" in fr[0]:
        low = mtd + sum(float(r["PredictionIntervalLowerBound"]) for r in fr)
        high = mtd + sum(float(r["PredictionIntervalUpperBound"]) for r in fr)

    month_total = mtd + remaining
    return Forecast(
        month=month_start.strftime("%Y-%m"), currency=currency, month_to_date=round(mtd, 2),
        forecast_remaining=round(remaining, 2), forecast_month_total=round(month_total, 2),
        forecast_low=round(low, 2) if low is not None else None,
        forecast_high=round(high, 2) if high is not None else None,
        previous_month_total=round(prev_total, 2),
        vs_previous_month_pct=_pct(prev_total, month_total),
    )
