import asyncio
from datetime import date

import boto3
import pytest
from botocore.stub import Stubber

from mcp.server.mcpserver.exceptions import ToolError

from cost_watchdog import server


def test_all_tools_are_read_only():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "cost_breakdown", "explain_cost_spike", "forecast_this_month",
        "find_idle_resources", "k8s_rightsizing"}
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
        assert t.output_schema is not None


def test_call_tool_end_to_end(monkeypatch):
    ce = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    st = Stubber(ce)
    st.add_response("get_cost_and_usage", {"ResultsByTime": [{
        "TimePeriod": {"Start": "2026-11-01", "End": "2026-11-02"},
        "Groups": [{"Keys": ["Amazon RDS"], "Metrics": {"UnblendedCost": {"Amount": "42.5", "Unit": "USD"}}}],
        "Estimated": False}]})
    st.activate()
    monkeypatch.setattr(server, "_ce", lambda: ce)
    monkeypatch.setattr(server, "_today", lambda: date(2026, 11, 18))

    result = asyncio.run(server.mcp.call_tool("cost_breakdown", {"group_by": "service"}))
    assert not result.is_error
    assert result.structured_content["total"] == 42.5
    assert result.structured_content["lines"][0]["key"] == "Amazon RDS"


def test_aws_errors_reach_the_agent_with_a_hint(monkeypatch):
    ce = boto3.client("ce", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    st = Stubber(ce)
    st.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException",
                        service_message="not authorized to perform ce:GetCostAndUsage")
    st.activate()
    monkeypatch.setattr(server, "_ce", lambda: ce)

    with pytest.raises(ToolError) as err:
        asyncio.run(server.mcp.call_tool("explain_cost_spike", {"window_days": 7}))
    assert "AccessDeniedException" in str(err.value) and "iam-policy.json" in str(err.value)


def test_bad_arguments_are_explained(monkeypatch):
    monkeypatch.setattr(server, "_ce", lambda: None)
    with pytest.raises(ToolError, match="group_by must be one of"):
        asyncio.run(server.mcp.call_tool("cost_breakdown", {"group_by": "colour"}))
