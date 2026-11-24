"""Agents under test. Each takes a question and an MCP toolbox and returns a transcript.

- ClaudeAgent: any Claude model, a plain tool-use loop over the MCP server's tools.
- ClaudeCodeAgent: headless Claude Code (`claude -p`) with only the MCP server's tools. It runs
  on a Claude subscription instead of API credits.
- BaselineAgent: no LLM; runs the weekly_cost_review recipe and pastes the summaries.
  It is the floor every model should beat, and lets the harness run without an API key.

To benchmark another provider, implement the `Agent` protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

SYSTEM_PROMPT = """You are a cloud cost analyst with read-only access to the company's AWS and \
Kubernetes cost tools. Investigate the user's question with the tools, then answer.

Start your answer with a line "Root cause: ..." that names the specific service, usage type, \
workload or resource responsible, in one or two sentences. Then give the supporting evidence \
with amounts, and your recommendations. Be concise."""

FINAL_NUDGE = "You have reached the tool-call limit. Give your final answer now, without tools."

# USD per million tokens: (input, output, cache read). Cache writes are billed at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# Models that take thinking={"type": "enabled", budget_tokens} rather than adaptive thinking.
# We run those without thinking.
NO_ADAPTIVE_THINKING = {"claude-haiku-4-5"}


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, u: Any) -> None:
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        inp, out, read = PRICES[model]
        return (self.input_tokens * inp + self.output_tokens * out + self.cache_read_tokens * read
                + self.cache_write_tokens * inp * 1.25) / 1e6


@dataclass
class Transcript:
    answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    stop_reason: str | None = None
    error: str | None = None
    model: str | None = None  # the model that actually answered, when the agent knows it
    cost_usd: float | None = None  # when the agent reports its own cost

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Toolbox(Protocol):
    tools: list[Any]  # mcp Tool objects
    instructions: str | None

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]: ...


class Agent(Protocol):
    name: str

    async def run(self, question: str, toolbox: Toolbox) -> Transcript: ...


def anthropic_tools(tools: list[Any]) -> list[dict[str, Any]]:
    # Sorted so the tools prefix is byte-stable across runs and caches.
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in sorted(tools, key=lambda t: t.name)]


class ClaudeAgent:
    def __init__(self, model: str, client: Any = None, effort: str | None = None,
                 max_tool_calls: int = 20, max_tokens: int = 16000):
        if client is None:
            import anthropic
            client = anthropic.AsyncAnthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.name = model if not effort else f"{model}@{effort}"

    def _request(self, system: str, tools: list[dict], messages: list[dict],
                 allow_tools: bool) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "system": system,
            "tools": tools, "messages": messages,
            # Auto-cache the growing conversation; each turn re-reads the previous prefix.
            "cache_control": {"type": "ephemeral"},
        }
        if self.model not in NO_ADAPTIVE_THINKING:
            req["thinking"] = {"type": "adaptive"}
        if self.effort:
            req["output_config"] = {"effort": self.effort}
        if not allow_tools:
            req["tool_choice"] = {"type": "none"}
        return req

    async def run(self, question: str, toolbox: Toolbox) -> Transcript:
        system = SYSTEM_PROMPT
        if toolbox.instructions:
            system += f"\n\nInstructions from the cost-watchdog MCP server:\n{toolbox.instructions}"
        tools = anthropic_tools(toolbox.tools)
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        t = Transcript(answer="")
        allow_tools = True

        while True:
            resp = await self.client.messages.create(**self._request(system, tools, messages, allow_tools))
            t.turns += 1
            t.usage.add(resp.usage)
            t.stop_reason = resp.stop_reason
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                t.answer = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                if resp.stop_reason == "refusal":
                    t.error = "refusal"
                elif resp.stop_reason == "max_tokens":
                    t.error = "max_tokens"
                return t

            results: list[dict[str, Any]] = []
            for u in uses:
                text, is_error = await toolbox.call(u.name, dict(u.input or {}))
                t.tool_calls.append(ToolCall(u.name, dict(u.input or {}), is_error))
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": text,
                                "is_error": is_error})
            if len(t.tool_calls) >= self.max_tool_calls:
                allow_tools = False
                results.append({"type": "text", "text": FINAL_NUDGE})
            messages.append({"role": "user", "content": results})


class BaselineAgent:
    """The weekly_cost_review recipe with no reasoning: call the tools, paste the summaries."""

    name = "baseline"

    async def run(self, question: str, toolbox: Toolbox) -> Transcript:
        t = Transcript(answer="")

        async def call(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
            text, err = await toolbox.call(name, args)
            t.tool_calls.append(ToolCall(name, args, err))
            return None if err else json.loads(text)

        forecast = await call("forecast_this_month", {})
        spike = await call("explain_cost_spike", {"window_days": 7})
        idle = await call("find_idle_resources", {})
        k8s = await call("k8s_rightsizing", {})

        lines = [f"Root cause: {spike['summary'] if spike else 'unknown'}", ""]
        if forecast:
            lines.append(f"Forecast for {forecast['month']}: {forecast['forecast_month_total']} "
                         f"{forecast['currency']} ({forecast['vs_previous_month_pct']}% vs last month).")
        if idle and idle["resources"]:
            ids = ", ".join(r["resource_id"] for r in idle["resources"][:5])
            lines.append(f"Idle candidates for owner review (~{idle['est_monthly_waste']}/month): {ids}.")
        if k8s and k8s["workloads"]:
            over = [w["workload"] for w in k8s["workloads"] if w["verdict"] == "over-provisioned"][:3]
            if over:
                lines.append(f"Over-provisioned workloads: {', '.join(over)}.")
        t.answer = "\n".join(lines)
        t.turns, t.stop_reason = 1, "end_turn"
        return t


class ClaudeCodeAgent:
    """Headless Claude Code with every built-in tool off and only the scenario's MCP server.

    Claude Code launches its own copy of the server from `toolbox.server_params`, which shares the
    harness's meter file, so its Cost Explorer calls are counted like any other agent's. API key
    variables are removed from its environment so it uses the logged-in subscription; set
    `use_subscription=False` to let it bill an API key instead. The cost it reports is the
    API-equivalent price, not what a subscription is charged.
    """

    SERVER = "cost-watchdog"

    def __init__(self, model: str = "sonnet", claude_bin: str = "claude",
                 use_subscription: bool = True, timeout: float = 900):
        self.model = model
        self.claude_bin = claude_bin
        self.use_subscription = use_subscription
        self.timeout = timeout
        self.name = f"claude-code:{model}"

    def _command(self, question: str, system: str, mcp_config: str) -> list[str]:
        return [self.claude_bin, "-p", question, "--model", self.model,
                "--system-prompt", system, "--tools", "",
                "--strict-mcp-config", "--mcp-config", mcp_config,
                "--allowedTools", f"mcp__{self.SERVER}",
                "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--setting-sources", ""]

    async def run(self, question: str, toolbox: Toolbox) -> Transcript:
        params = getattr(toolbox, "server_params", None)
        if params is None:
            raise RuntimeError("claude-code agents need --transport stdio")
        system = SYSTEM_PROMPT
        if toolbox.instructions:
            system += f"\n\nInstructions from the cost-watchdog MCP server:\n{toolbox.instructions}"
        env = dict(os.environ)
        if self.use_subscription:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)

        with tempfile.TemporaryDirectory(prefix="cw-claude-") as tmp:
            config = os.path.join(tmp, "mcp.json")
            with open(config, "w") as f:
                json.dump({"mcpServers": {self.SERVER: {
                    "type": "stdio", "command": params.command, "args": list(params.args),
                    "env": dict(params.env or {})}}}, f)
            # An empty working directory, so no project CLAUDE.md or settings leak into the run.
            proc = await asyncio.create_subprocess_exec(
                *self._command(question, system, config), cwd=tmp, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return Transcript(answer="", error=f"timed out after {self.timeout:.0f}s")
        t = parse_stream_json(out.decode(), f"mcp__{self.SERVER}__")
        if proc.returncode and not t.error:
            t.error = f"claude exited {proc.returncode}: {err.decode().strip()[-300:]}"
        return t


def parse_stream_json(stream: str, tool_prefix: str) -> Transcript:
    """Build a Transcript from `claude -p --output-format stream-json` output."""
    t = Transcript(answer="")
    by_id: dict[str, ToolCall] = {}
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            t.model = event.get("model")
        elif kind in ("assistant", "user"):
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    call = ToolCall(name.removeprefix(tool_prefix), dict(block.get("input") or {}))
                    t.tool_calls.append(call)
                    by_id[block.get("id", "")] = call
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    if block.get("tool_use_id") in by_id:
                        by_id[block["tool_use_id"]].is_error = True
        elif kind == "result":
            t.answer = (event.get("result") or "").strip()
            t.turns = event.get("num_turns") or 0
            t.stop_reason = event.get("stop_reason")
            t.cost_usd = event.get("total_cost_usd")
            u = event.get("usage") or {}
            t.usage = Usage(u.get("input_tokens", 0), u.get("output_tokens", 0),
                            u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
            if event.get("is_error"):
                t.error = f"{event.get('subtype')}: {t.answer[:300]}"
    if not t.answer and not t.error:
        t.error = "no result event from claude"
    return t


def make_agent(spec: str, effort: str | None = None, client: Any = None,
               max_tool_calls: int = 20) -> Agent:
    if spec == "baseline":
        return BaselineAgent()
    if spec.startswith("claude-code:"):
        return ClaudeCodeAgent(spec.removeprefix("claude-code:"))
    return ClaudeAgent(spec, client=client, effort=effort, max_tool_calls=max_tool_calls)
