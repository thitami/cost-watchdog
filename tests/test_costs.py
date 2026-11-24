from datetime import date

import boto3
import pytest
from botocore.stub import ANY, Stubber

from cost_watchdog.aws import costs


def _ce():
    return boto3.client("ce", region_name="us-east-1",
                        aws_access_key_id="x", aws_secret_access_key="x")


def _groups(values: dict[str, float]) -> dict:
    return {"ResultsByTime": [{
        "TimePeriod": {"Start": "2026-11-01", "End": "2026-11-02"},
        "Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}}}
                   for k, v in values.items()],
        "Estimated": False,
    }]}


def test_breakdown_ranks_and_paginates():
    ce = _ce()
    with Stubber(ce) as st:
        page1 = _groups({"Amazon EC2": 70.0}) | {"NextPageToken": "t"}
        st.add_response("get_cost_and_usage", page1, {
            "TimePeriod": {"Start": "2026-10-19", "End": "2026-11-18"},
            "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]})
        st.add_response("get_cost_and_usage", _groups({"Amazon S3": 30.0, "Amazon EC2": 0.0}),
                        {"TimePeriod": ANY, "Granularity": ANY, "Metrics": ANY,
                         "GroupBy": ANY, "NextPageToken": "t"})
        out = costs.cost_breakdown(ce, date(2026, 10, 19), date(2026, 11, 18))
    assert out.total == 100.0
    assert [l.key for l in out.lines] == ["Amazon EC2", "Amazon S3"]
    assert out.lines[0].share_pct == 70.0


def test_breakdown_rejects_bad_group():
    with pytest.raises(ValueError):
        costs.cost_breakdown(_ce(), date(2026, 1, 1), date(2026, 2, 1), group_by="colour")


def test_explain_spike_drills_into_usage_types():
    ce = _ce()
    with Stubber(ce) as st:
        st.add_response("get_cost_and_usage",
                        _groups({"Amazon EC2": 100, "Amazon S3": 50, "Tax": 20}))
        st.add_response("get_cost_and_usage",
                        _groups({"Amazon EC2": 300, "Amazon S3": 40, "Tax": 60}))
        # Drill-down only for services that went up (EC2), filtered by service.
        flt = {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon EC2"]}}
        exp = {"TimePeriod": ANY, "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
               "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}], "Filter": flt}
        st.add_response("get_cost_and_usage", _groups({"BoxUsage:m5.large": 100}), exp)
        st.add_response("get_cost_and_usage",
                        _groups({"BoxUsage:m5.large": 120, "NatGateway-Bytes": 180}), exp)
        out = costs.explain_spike(ce, date(2026, 11, 18), window_days=7)
        st.assert_no_pending_responses()

    assert out.current_window == "2026-11-11..2026-11-18"
    assert out.previous_total == 150 and out.current_total == 340  # Tax excluded
    assert out.biggest_movers[0].key == "Amazon EC2"
    assert out.drivers[0].top_usage_types[0].key == "NatGateway-Bytes"
    assert out.drivers[0].top_usage_types[0].delta_pct is None  # new spend
    assert "NatGateway-Bytes" in out.summary


def test_forecast_combines_mtd_and_forecast():
    ce = _ce()
    total = lambda amt: {"ResultsByTime": [{"TimePeriod": {"Start": "a", "End": "b"},
                                             "Total": {"UnblendedCost": {"Amount": str(amt), "Unit": "USD"}},
                                             "Groups": [], "Estimated": True}]}
    with Stubber(ce) as st:
        st.add_response("get_cost_and_usage", total(600),
                        {"TimePeriod": {"Start": "2026-11-01", "End": "2026-11-18"},
                         "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"]})
        st.add_response("get_cost_and_usage", total(1000),
                        {"TimePeriod": {"Start": "2026-10-01", "End": "2026-11-01"},
                         "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"]})
        st.add_response("get_cost_forecast", {
            "Total": {"Amount": "500", "Unit": "USD"},
            "ForecastResultsByTime": [{"TimePeriod": {"Start": "2026-11-18", "End": "2026-12-01"},
                                       "MeanValue": "500", "PredictionIntervalLowerBound": "450",
                                       "PredictionIntervalUpperBound": "560"}]},
            {"TimePeriod": {"Start": "2026-11-18", "End": "2026-12-01"},
             "Metric": "UNBLENDED_COST", "Granularity": "MONTHLY", "PredictionIntervalLevel": 80})
        out = costs.forecast_month(ce, date(2026, 11, 18))
    assert out.month == "2026-11"
    assert out.forecast_month_total == 1100
    assert (out.forecast_low, out.forecast_high) == (1050, 1160)
    assert out.vs_previous_month_pct == 10.0
