import asyncio
import json
import os
import stat
import sys

from evals.agents import ClaudeCodeAgent, parse_stream_json
from evals.harness import run_one
from evals.report import markdown
from evals.scenarios import BY_ID

NAT = BY_ID["nat-gateway-bytes"]

# Stands in for `claude -p`: records its argv, environment and MCP config, then prints a
# stream-json transcript with one tool call and a final answer.
FAKE_CLAUDE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
config = json.load(open(args[args.index("--mcp-config") + 1]))
json.dump({{"args": args, "config": config, "cwd": os.getcwd(),
           "api_key": "ANTHROPIC_API_KEY" in os.environ}}, open({record!r}, "w"))
events = [
    {{"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "id": "t1", "name": "mcp__cost-watchdog__explain_cost_spike", "input": {{"window_days": 7}}}},
        {{"type": "tool_use", "id": "t2", "name": "mcp__cost-watchdog__cost_breakdown", "input": {{"group_by": "colour"}}}}]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "t1", "content": "{{}}"}},
        {{"type": "tool_result", "tool_use_id": "t2", "content": "bad", "is_error": True}}]}}}},
    {{"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "stop_reason": "end_turn",
     "result": "Root cause: NAT gateway data processing (NatGateway-Bytes).", "total_cost_usd": 0.035,
     "usage": {{"input_tokens": 4, "output_tokens": 800, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 6000}}}},
]
for e in events:
    print(json.dumps(e))
'''


def _fake_claude(tmp_path):
    record = str(tmp_path / "record.json")
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable, record=record))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe), record


def test_claude_code_agent_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-passed")
    exe, record = _fake_claude(tmp_path)
    agent = ClaudeCodeAgent("sonnet", claude_bin=exe)
    r = asyncio.run(run_one(NAT, agent, transport="stdio"))

    assert r.error is None and r.root_cause_found
    assert r.agent == "claude-code:sonnet" and r.model == "claude-sonnet-5-5"
    assert [c["name"] for c in r.trajectory] == ["explain_cost_spike", "cost_breakdown"]
    assert r.tool_errors == 1 and r.llm_cost == 0.035

    seen = json.load(open(record))
    assert seen["api_key"] is False  # runs on the subscription, not an API key
    args = seen["args"]
    assert args[args.index("--tools") + 1] == ""  # no built-in tools
    assert "--strict-mcp-config" in args and args[args.index("--model") + 1] == "sonnet"
    server = seen["config"]["mcpServers"]["cost-watchdog"]
    assert server["args"] == ["-m", "evals.mock_server", NAT.id]
    assert "CW_EVAL_METER" in server["env"]  # shares the harness's meter
    assert os.path.realpath(seen["cwd"]) != os.path.realpath(os.getcwd())

    assert "API-equivalent" in markdown([r.to_dict()])


def test_claude_code_needs_stdio():
    r = asyncio.run(run_one(NAT, ClaudeCodeAgent("sonnet"), transport="inproc"))
    assert "--transport stdio" in r.error


def test_parse_reports_errors_and_missing_results():
    t = parse_stream_json(json.dumps({"type": "result", "subtype": "error_max_turns",
                                      "is_error": True, "result": "stopped"}), "mcp__x__")
    assert t.error.startswith("error_max_turns")
    assert parse_stream_json("not json\n", "mcp__x__").error == "no result event from claude"
