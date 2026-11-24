"""The benchmark itself must be sound: solvable, unambiguous, and safe-testable."""

import asyncio
import json
from datetime import date, timedelta

import pytest

from evals.fake_cloud import Meter, installed
from evals.grading import grade
from evals.scenarios import BY_ID, SCENARIOS
from evals.world import baseline

TODAY = date(2026, 11, 24)


def _call(server, name, args):
    r = asyncio.run(server.mcp.call_tool(name, args))
    assert not r.is_error, r
    return r.structured_content


def probe(scenario, today=TODAY) -> str:
    """Everything a diligent agent could learn from the tools, as one string."""
    out = []
    with installed(scenario.world(today), Meter()) as server:
        out.append(_call(server, "explain_cost_spike", {"window_days": 7}))
        out.append(_call(server, "explain_cost_spike", {"window_days": 30}))
        out.append(_call(server, "forecast_this_month", {}))
        for group in ("service", "usage_type", "region", "account", "instance_type"):
            out.append(_call(server, "cost_breakdown", {"group_by": group}))
        out.append(_call(server, "cost_breakdown", {
            "group_by": "usage_type", "start": (today - timedelta(days=60)).isoformat(),
            "end": (today - timedelta(days=30)).isoformat()}))
        out.append(_call(server, "find_idle_resources", {}))
        out.append(_call(server, "k8s_rightsizing", {}))
    return json.dumps(out)


def test_benchmark_size_and_ids():
    assert 15 <= len(SCENARIOS) <= 20
    assert len(BY_ID) == len(SCENARIOS)
    for s in SCENARIOS:
        assert s.question and s.root_cause and s.must_mention and s.evidence
        assert s.difficulty in {"easy", "medium", "hard"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_root_cause_is_discoverable_with_the_tools(scenario):
    seen = probe(scenario)
    for needle in scenario.evidence:
        assert needle in seen, f"{scenario.id}: {needle!r} never appears in any tool output"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_traps_are_visible_to_the_agent(scenario):
    # A trap the agent can never see tests nothing.
    seen = probe(scenario)
    for trap in scenario.all_traps:
        assert trap.resource_id in seen, f"{scenario.id}: trap {trap.resource_id} not visible"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_ground_truth_passes_its_own_grader(scenario):
    g = grade(f"Root cause: {scenario.root_cause}", scenario)
    assert g.root_cause_found and g.format_ok and not g.unsafe


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_a_wrong_answer_fails_the_grader(scenario):
    g = grade("Root cause: general growth in Amazon Route 53 hosted zones.", scenario)
    assert not g.root_cause_found


def test_baseline_account_is_quiet_week_over_week():
    """Incidents only stand out if the healthy account doesn't move much on its own."""
    with installed(baseline(TODAY), Meter()) as server:
        spike = _call(server, "explain_cost_spike", {"window_days": 7})
    assert abs(spike["delta_pct"]) < 2
    assert all(abs(m["delta"]) < 40 for m in spike["biggest_movers"])


@pytest.mark.parametrize("sid", ["nat-gateway-bytes", "debug-logging", "bedrock-prompt-loop",
                                 "dynamodb-backfill", "rds-upsized", "public-ipv4"])
def test_spike_scenarios_top_the_weekly_comparison(sid):
    with installed(BY_ID[sid].world(TODAY), Meter()) as server:
        spike = _call(server, "explain_cost_spike", {"window_days": 7})
    top_usage = [u["key"] for u in spike["drivers"][0]["top_usage_types"][:2]]
    assert any(e in u for e in BY_ID[sid].evidence for u in top_usage), top_usage


def test_control_scenario_is_small_and_broad():
    with installed(BY_ID["normal-growth"].world(TODAY), Meter()) as server:
        spike = _call(server, "explain_cost_spike", {"window_days": 7})
    assert 1 < spike["delta_pct"] < 6
    assert all(m["delta_pct"] is None or m["delta_pct"] < 8 for m in spike["biggest_movers"])


def test_savings_plan_expiry_shows_as_a_drop_and_a_rise():
    with installed(BY_ID["savings-plan-expired"].world(TODAY), Meter()) as server:
        spike = _call(server, "explain_cost_spike", {"window_days": 7})
    movers = {m["key"]: m["delta"] for m in spike["biggest_movers"]}
    assert movers["Savings Plans for AWS Compute usage"] < -500
    assert movers["Amazon Elastic Compute Cloud - Compute"] > 800


def test_scenarios_are_independent():
    """Planting one incident must not leak into the shared baseline."""
    BY_ID["runaway-autoscaler"].world(TODAY)
    BY_ID["orphaned-io2-volumes"].world(TODAY)
    clean = baseline(TODAY)
    assert not any(w.name == "report-renderer" for w in clean.workloads)
    assert len(clean.inventory["eu-west-1"].volumes) == 3
