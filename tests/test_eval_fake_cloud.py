from datetime import date

import pytest
from botocore.exceptions import ClientError, ParamValidationError

from cost_watchdog import server
from cost_watchdog.aws import idle
from evals.fake_cloud import Meter, installed, make_client
from evals.world import EC2, STAGING, Line, World, baseline, flat, since

TODAY = date(2026, 11, 24)


def _world(*lines):
    return World(today=TODAY, lines=list(lines), inventory=baseline(TODAY).inventory, workloads=[])


def _ce(world, meter=None):
    return make_client(world, "ce", "us-east-1", meter or Meter())


def _amounts(resp, metric="UnblendedCost"):
    out = {}
    for p in resp["ResultsByTime"]:
        for g in p["Groups"]:
            k = tuple(g["Keys"])
            out[k] = out.get(k, 0) + float(g["Metrics"][metric]["Amount"])
    return out


def test_daily_and_monthly_sums_agree_and_filters_apply():
    w = _world(Line(EC2, "BoxUsage:a", flat(10), noise=0), Line(EC2, "BoxUsage:b", flat(5), noise=0),
               Line("S3", "Storage", flat(1), noise=0, account=STAGING))
    ce = _ce(w)
    kw = {"TimePeriod": {"Start": "2026-10-01", "End": "2026-11-24"}, "Metrics": ["UnblendedCost"],
          "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    daily = _amounts(ce.get_cost_and_usage(Granularity="DAILY", **kw))
    monthly_resp = ce.get_cost_and_usage(Granularity="MONTHLY", **kw)
    assert len(monthly_resp["ResultsByTime"]) == 2
    assert daily == pytest.approx(_amounts(monthly_resp))
    assert daily[(EC2,)] == pytest.approx(54 * 15)

    flt = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [STAGING]}}
    two = _amounts(ce.get_cost_and_usage(
        Granularity="MONTHLY", Filter=flt, TimePeriod=kw["TimePeriod"], Metrics=kw["Metrics"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]))
    assert two == pytest.approx({("S3", "Storage"): 54})


def test_no_data_for_today_or_the_future():
    ce = _ce(_world(Line(EC2, "x", flat(10), noise=0)))
    resp = ce.get_cost_and_usage(TimePeriod={"Start": "2026-11-23", "End": "2026-11-26"},
                                 Granularity="DAILY", Metrics=["UnblendedCost"])
    assert [p["Total"]["UnblendedCost"]["Amount"][:5] for p in resp["ResultsByTime"]] == \
        ["10.00", "0.000", "0.000"]


def test_step_changes_land_on_the_right_day():
    w = _world(Line(EC2, "gpu", since(3, 100), noise=0))
    resp = _ce(w).get_cost_and_usage(TimePeriod={"Start": "2026-11-19", "End": "2026-11-24"},
                                     Granularity="DAILY", Metrics=["UnblendedCost"])
    assert [round(float(p["Total"]["UnblendedCost"]["Amount"])) for p in resp["ResultsByTime"]] == \
        [0, 0, 100, 100, 100]


def test_botocore_still_validates_parameters():
    ce = _ce(_world())
    with pytest.raises(ParamValidationError):
        ce.get_cost_and_usage(TimePeriod={"Start": "2026-11-01"}, Metrics=["UnblendedCost"])


def test_api_errors_are_real_client_errors():
    ce = _ce(_world())
    with pytest.raises(ClientError) as err:
        ce.get_cost_and_usage(TimePeriod={"Start": "2026-11-10", "End": "2026-11-01"},
                              Granularity="DAILY", Metrics=["UnblendedCost"])
    assert err.value.response["Error"]["Code"] == "ValidationException"
    with pytest.raises(ClientError):
        ce.get_cost_forecast(TimePeriod={"Start": "2026-11-01", "End": "2026-12-01"},
                             Metric="UNBLENDED_COST", Granularity="MONTHLY")


def test_forecast_uses_recent_run_rate():
    ce = _ce(_world(Line(EC2, "x", flat(100), noise=0)))
    resp = ce.get_cost_forecast(TimePeriod={"Start": "2026-11-24", "End": "2026-12-01"},
                                Metric="UNBLENDED_COST", Granularity="MONTHLY",
                                PredictionIntervalLevel=80)
    assert float(resp["Total"]["Amount"]) == pytest.approx(700)


def test_meter_counts_and_mirrors_to_file(tmp_path):
    path = str(tmp_path / "m.json")
    meter = Meter(path)
    ce = _ce(_world(), meter)
    for _ in range(3):
        ce.get_cost_and_usage(TimePeriod={"Start": "2026-11-01", "End": "2026-11-02"},
                              Granularity="DAILY", Metrics=["UnblendedCost"])
    assert meter.ce_calls == 3
    assert Meter.read(path) == {"ce:GetCostAndUsage": 3}


def test_ec2_paginators_and_disabled_regions():
    w = baseline(TODAY)
    w.regions = ["eu-west-1", "ap-east-1"]
    meter = Meter()
    report = idle.find_idle(w.regions, lambda svc, r: make_client(w, svc, r, meter), now=w.now)
    assert "i-0d15a5732e1b0c4aa" in {r.resource_id for r in report.resources}
    assert report.errors and report.errors[0].startswith("ap-east-1")


def test_installed_restores_the_server():
    before = (server._ce, server._client, server._k8s, server._today, server.settings)
    with installed(baseline(TODAY), Meter()):
        assert server._today() == TODAY
    assert (server._ce, server._client, server._k8s, server._today, server.settings) == before


def test_meter_survives_parallel_tool_calls(tmp_path):
    """The server runs tools in worker threads; parallel calls must not trip over the meter file."""
    from concurrent.futures import ThreadPoolExecutor

    meter = Meter(str(tmp_path / "m.json"))
    with installed(baseline(TODAY), meter) as s:
        with ThreadPoolExecutor(8) as ex:
            futures = [ex.submit(s.explain_cost_spike, 7) for _ in range(8)]
            assert [f.exception() for f in futures] == [None] * 8
    assert Meter.read(str(tmp_path / "m.json")) == meter.calls
    assert meter.ce_calls == 8 * (2 + 2 * 3)
