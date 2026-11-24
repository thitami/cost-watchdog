"""Run agents against the MCP server for each scenario, and score the runs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .agents import Agent, Transcript
from .fake_cloud import CE_PRICE_PER_REQUEST, Meter, installed
from .grading import Grade, grade, judge
from .scenarios import Scenario

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RunResult:
    scenario: str
    agent: str
    repeat: int
    root_cause_found: bool
    format_ok: bool
    unsafe: list[str]
    tool_calls: int
    tool_errors: int
    ce_calls: int
    ce_cost: float
    api_calls: dict[str, int]
    llm_cost: float | None
    input_tokens: int
    output_tokens: int
    seconds: float
    answer: str
    trajectory: list[dict[str, Any]]
    error: str | None = None
    judge: dict[str, Any] | None = None
    model: str | None = None

    @property
    def is_unsafe(self) -> bool:
        return bool(self.unsafe)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class McpToolbox:
    """Adapts an MCP client session to the agents' Toolbox protocol."""

    def __init__(self, client: Client, tools: list[Any],
                 server_params: StdioServerParameters | None = None):
        self.client = client
        self.tools = tools
        self.instructions = client.instructions
        # How to launch this scenario's server, for agents that bring their own MCP client
        # (Claude Code). It shares the meter file, so their backend calls are counted too.
        self.server_params = server_params

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        try:
            r = await self.client.call_tool(name, arguments)
        except Exception as exc:  # protocol-level errors still go back to the agent
            return f"Error: {exc}", True
        if r.structured_content is not None and not r.is_error:
            return json.dumps(r.structured_content), False
        text = "\n".join(getattr(c, "text", "") for c in r.content)
        return text or "(empty result)", r.is_error


def assert_read_only(tools: list[Any]) -> None:
    """The harness only ever drives read-only tools; refuse to run against anything else."""
    bad = [t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)]
    if bad:
        raise RuntimeError(f"Refusing to evaluate: tools without readOnlyHint: {bad}")


_inproc_lock = asyncio.Lock()


@asynccontextmanager
async def connect(scenario: Scenario, transport: str, today: date
                  ) -> AsyncIterator[tuple[Client, Callable[[], Counter[str]],
                                           StdioServerParameters | None]]:
    """Yield an MCP client wired to the scenario, a function returning backend call counts, and
    (over stdio) the parameters that launch another server for the same scenario and meter."""
    if transport == "inproc":
        # In-process patches module globals, so scenarios run one at a time.
        async with _inproc_lock:
            meter = Meter()
            with installed(scenario.world(today), meter) as server:
                async with Client(server.mcp) as client:
                    yield client, lambda: Counter(meter.calls), None
        return

    with tempfile.TemporaryDirectory(prefix="cw-eval-") as tmp:
        meter_path = os.path.join(tmp, "meter.json")
        env = {
            "CW_EVAL_METER": meter_path,
            "CW_EVAL_TODAY": today.isoformat(),
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src"), os.getenv("PYTHONPATH", "")]),
        }
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "evals.mock_server", scenario.id],
                                       env=env, cwd=str(ROOT))
        async with Client(params) as client:
            yield client, lambda: Meter.read(meter_path), params


async def run_one(scenario: Scenario, agent: Agent, repeat: int = 0, transport: str = "stdio",
                  judge_client: Any = None, judge_model: str | None = None,
                  today: date | None = None) -> RunResult:
    today = today or datetime.now(timezone.utc).date()
    started = time.monotonic()
    async with connect(scenario, transport, today) as (client, counts, params):
        tools = (await client.list_tools()).tools
        assert_read_only(tools)
        toolbox = McpToolbox(client, tools, params)
        try:
            transcript = await agent.run(scenario.question, toolbox)
        except Exception as exc:  # an API failure is a failed run, not a crashed benchmark
            transcript = Transcript(answer="", error=f"{type(exc).__name__}: {exc}")
        calls = counts()

    g: Grade = grade(transcript.answer, scenario)
    verdict = None
    if judge_client is not None and judge_model and transcript.answer:
        try:
            verdict = await judge(judge_client, judge_model, transcript.answer, scenario)
        except Exception as exc:
            verdict = {"error": f"{type(exc).__name__}: {exc}"}

    ce = sum(n for k, n in calls.items() if k.startswith("ce:"))
    model = transcript.model or getattr(agent, "model", None)
    llm_cost = (transcript.cost_usd if transcript.cost_usd is not None
                else transcript.usage.cost(model) if model else 0.0)
    return RunResult(
        scenario=scenario.id, agent=agent.name, repeat=repeat,
        root_cause_found=g.root_cause_found, format_ok=g.format_ok, unsafe=g.unsafe,
        tool_calls=len(transcript.tool_calls),
        tool_errors=sum(c.is_error for c in transcript.tool_calls),
        ce_calls=ce, ce_cost=round(ce * CE_PRICE_PER_REQUEST, 2), api_calls=dict(calls),
        llm_cost=llm_cost, model=model,
        input_tokens=transcript.usage.input_tokens + transcript.usage.cache_read_tokens
        + transcript.usage.cache_write_tokens,
        output_tokens=transcript.usage.output_tokens,
        seconds=round(time.monotonic() - started, 1), answer=transcript.answer,
        trajectory=[asdict(c) for c in transcript.tool_calls], error=transcript.error,
        judge=verdict)


async def run_suite(scenarios: list[Scenario], agents: list[Agent], repeats: int = 1,
                    concurrency: int = 4, transport: str = "stdio", judge_client: Any = None,
                    judge_model: str | None = None,
                    on_result: Callable[[RunResult], None] | None = None) -> list[RunResult]:
    sem = asyncio.Semaphore(concurrency)
    today = datetime.now(timezone.utc).date()

    async def one(s: Scenario, a: Agent, r: int) -> RunResult:
        async with sem:
            res = await run_one(s, a, r, transport, judge_client, judge_model, today)
        if on_result:
            on_result(res)
        return res

    jobs = [one(s, a, r) for a in agents for s in scenarios for r in range(repeats)]
    return list(await asyncio.gather(*jobs))
