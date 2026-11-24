import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from evals.agents import FINAL_NUDGE, BaselineAgent, ClaudeAgent, Usage
from evals.harness import assert_read_only, run_one, run_suite
from evals.report import END, START, markdown, summary, update_readme
from evals.scenarios import BY_ID

NAT = BY_ID["nat-gateway-bytes"]


def _usage(i=100, o=20):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0,
              cache_creation_input_tokens=0)


def _tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


class ScriptedClaude:
    """Stands in for anthropic.AsyncAnthropic: replays responses, records requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: o.__dict__)))
        return self.responses.pop(0)


def test_baseline_run_meters_tools_and_cost_explorer():
    r = asyncio.run(run_one(NAT, BaselineAgent(), transport="inproc"))
    assert r.root_cause_found and not r.unsafe and r.error is None
    assert r.tool_calls == 4 and r.tool_errors == 0
    # forecast: 2 usage + 1 forecast; spike: 2 + 2 per drilled-down service (3)
    assert r.api_calls["ce:GetCostAndUsage"] == 10 and r.api_calls["ce:GetCostForecast"] == 1
    assert r.ce_calls == 11 and r.ce_cost == 0.11


def test_stdio_transport_matches_inproc():
    a = asyncio.run(run_one(NAT, BaselineAgent(), transport="inproc"))
    b = asyncio.run(run_one(NAT, BaselineAgent(), transport="stdio"))
    assert (a.answer, a.api_calls) == (b.answer, b.api_calls)


def test_claude_agent_loop_over_mcp():
    client = ScriptedClaude([
        NS(stop_reason="tool_use", usage=_usage(), content=[
            NS(type="text", text="Checking."),
            _tool_use("t1", "explain_cost_spike", {"window_days": 7}),
            _tool_use("t2", "cost_breakdown", {"group_by": "colour"})]),
        NS(stop_reason="end_turn", usage=_usage(500, 80), content=[
            NS(type="text", text="Root cause: NAT gateway data processing (NatGateway-Bytes).")]),
    ])
    agent = ClaudeAgent("claude-sonnet-5", client=client)
    r = asyncio.run(run_one(NAT, agent, transport="inproc"))

    assert r.root_cause_found and r.tool_calls == 2 and r.tool_errors == 1
    assert r.ce_calls == 2 + 2 * 3
    assert r.llm_cost == pytest.approx((600 * 2 + 100 * 10) / 1e6)
    first, second = client.requests
    assert first["thinking"] == {"type": "adaptive"}
    assert "cost-watchdog MCP server" in first["system"]
    assert [t["name"] for t in first["tools"]] == sorted(t["name"] for t in first["tools"])
    results = second["messages"][-1]["content"]
    assert [x["tool_use_id"] for x in results] == ["t1", "t2"]
    assert results[1]["is_error"] and "group_by must be one of" in results[1]["content"]
    assert json.loads(results[0]["content"])["drivers"][0]["service"] == "EC2 - Other"


def test_claude_agent_stops_tools_at_the_budget():
    calls = [NS(stop_reason="tool_use", usage=_usage(), content=[
        _tool_use(f"t{i}", "forecast_this_month", {})]) for i in range(2)]
    final = NS(stop_reason="end_turn", usage=_usage(), content=[NS(type="text", text="Root cause: ?")])
    client = ScriptedClaude(calls + [final])
    r = asyncio.run(run_one(NAT, ClaudeAgent("claude-haiku-4-5", client=client, max_tool_calls=2),
                            transport="inproc"))
    assert r.tool_calls == 2
    last = client.requests[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1]["content"][-1] == {"type": "text", "text": FINAL_NUDGE}
    assert "thinking" not in last  # Haiku 4.5 has no adaptive thinking


def test_api_failures_become_failed_runs():
    class Broken:
        messages = None

        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            raise RuntimeError("overloaded")

    r = asyncio.run(run_one(NAT, ClaudeAgent("claude-sonnet-5", client=Broken()), transport="inproc"))
    assert not r.root_cause_found and "overloaded" in r.error


def test_refuses_servers_with_write_tools():
    ro = NS(name="read", annotations=NS(read_only_hint=True))
    rw = NS(name="delete_volume", annotations=NS(read_only_hint=False))
    assert_read_only([ro])
    with pytest.raises(RuntimeError, match="delete_volume"):
        assert_read_only([ro, rw])


def test_usage_cost_includes_cache():
    u = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000,
              cache_write_tokens=1_000_000)
    assert u.cost("claude-opus-5-5") == pytest.approx(4 + 0.2 + 5)
    assert u.cost("some-other-model") is None


def test_suite_report_and_readme(tmp_path):
    scenarios = [NAT, BY_ID["s3-lifecycle-removed"]]
    results = asyncio.run(run_suite(scenarios, [BaselineAgent()], repeats=2, transport="inproc"))
    assert len(results) == 4
    row = summary(results)[0]
    assert (row["agent"], row["runs"], row["root_cause"], row["unsafe"]) == ("baseline", 4, 2, 0)

    md = markdown([r.to_dict() for r in results], {"repeats": 2})
    assert "| `baseline` | 2/4 (50%) | 4.0 | 11.0 | $0.11 | 0 | $0.000 | 0 |" in md
    assert "✅ 2/2" in md and "❌ 0/2" in md

    readme = tmp_path / "README.md"
    readme.write_text(f"# x\n{START}\nold\n{END}\ntail\n")
    assert update_readme(readme, md)
    text = readme.read_text()
    assert "old" not in text and "2/4 (50%)" in text and text.endswith("tail\n")
    (tmp_path / "plain.md").write_text("no markers")
    assert not update_readme(tmp_path / "plain.md", md)
