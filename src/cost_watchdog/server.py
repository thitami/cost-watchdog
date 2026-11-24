"""MCP server entry point. Every tool is read-only: the server can explain and recommend, never change infra."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .aws import costs, idle
from .config import Settings
from .k8s import rightsizing
from .models import CostBreakdown, Forecast, IdleReport, RightsizingReport, SpikeExplanation

settings = Settings.from_env()

mcp = MCPServer(
    name="cost-watchdog",
    version="0.1.0",
    instructions=(
        "Read-only cloud cost analyst for AWS and Kubernetes. Start with forecast_this_month or "
        "explain_cost_spike for 'why is the bill up' questions, cost_breakdown for 'where does the "
        "money go', find_idle_resources and k8s_rightsizing for 'what can we cut'. Amounts are in "
        "the account's billing currency. Idle-resource costs are estimates: tell the user to verify "
        "before deleting anything. Cost Explorer charges $0.01 per API request, so avoid needless repeats."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=True)


# ---------- clients (created lazily so the server starts without credentials) ----------

@lru_cache(maxsize=1)
def _session() -> boto3.Session:
    return boto3.Session(profile_name=settings.aws_profile)


def _client(service: str, region: str) -> Any:
    return _session().client(service, region_name=region)


def _ce() -> Any:
    # Cost Explorer is a global service served from us-east-1.
    return _client("ce", "us-east-1")


@lru_cache(maxsize=1)
def _k8s() -> tuple[Any, Any]:
    from kubernetes import client, config

    if settings.k8s_mode == "incluster":
        config.load_incluster_config()
    else:
        config.load_kube_config(context=settings.k8s_context)
    return client.CoreV1Api(), client.CustomObjectsApi()


@contextmanager
def _friendly_errors():
    """Turn expected failures into ToolErrors so the agent sees why and can recover.

    Anything else stays an unexpected error, whose details MCP keeps server-side.
    """
    try:
        yield
    except ValueError as exc:
        raise ToolError(f"Invalid argument: {exc}") from exc
    except ClientError as exc:
        err = exc.response.get("Error", {})
        hint = {
            "AccessDeniedException": "Grant the permissions in deploy/iam-policy.json.",
            "UnauthorizedOperation": "Grant the permissions in deploy/iam-policy.json.",
            "DataUnavailableException": "Cost Explorer has no data yet; enable it in the Billing console.",
        }.get(err.get("Code", ""), "")
        raise ToolError(f"AWS error {err.get('Code')}: {err.get('Message')} {hint}".strip()) from exc
    except BotoCoreError as exc:
        raise ToolError(f"AWS client error (credentials or network?): {exc}") from exc


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse(d: str | None, default: date) -> date:
    return date.fromisoformat(d) if d else default


# ---------- tools ----------

@mcp.tool(annotations=READ_ONLY)
def cost_breakdown(start: str | None = None, end: str | None = None,
                   group_by: str = "service") -> CostBreakdown:
    """Where the money goes over a period, ranked.

    Args:
        start: ISO date (inclusive). Defaults to 30 days ago.
        end: ISO date (exclusive). Defaults to today.
        group_by: one of service, usage_type, region, account, instance_type.
    """
    today = _today()
    with _friendly_errors():
        return costs.cost_breakdown(_ce(), _parse(start, today - timedelta(days=30)),
                                    _parse(end, today), group_by, settings.cost_metric,
                                    settings.max_rows)


@mcp.tool(annotations=READ_ONLY)
def explain_cost_spike(window_days: int = 7) -> SpikeExplanation:
    """Compare the last N complete days with the N days before and explain what changed.

    Returns the biggest movers by service and drills into usage types for the top increases,
    e.g. 'EC2 +$412, mostly NatGateway-Bytes'.

    Args:
        window_days: size of each comparison window, 1 to 90. Use 1 for 'yesterday vs the day before'.
    """
    with _friendly_errors():
        return costs.explain_spike(_ce(), _today(), window_days, settings.cost_metric)


@mcp.tool(annotations=READ_ONLY)
def forecast_this_month() -> Forecast:
    """Month-to-date spend, AWS's forecast for the rest of the month, and comparison with last month."""
    with _friendly_errors():
        return costs.forecast_month(_ce(), _today(), settings.cost_metric)


@mcp.tool(annotations=READ_ONLY)
def find_idle_resources(regions: list[str] | None = None, stopped_days: int = 14,
                        snapshot_days: int = 90) -> IdleReport:
    """Find AWS resources that cost money while doing nothing, ranked by estimated monthly cost.

    Checks unattached EBS volumes, unassociated Elastic IPs, long-stopped EC2 instances,
    old snapshots not used by an AMI, and load balancers with no targets.

    Args:
        regions: AWS regions to scan. Defaults to COST_WATCHDOG_REGIONS.
        stopped_days: flag instances stopped at least this long.
        snapshot_days: flag snapshots older than this.
    """
    with _friendly_errors():
        return idle.find_idle(regions or settings.regions, _client, stopped_days=stopped_days,
                              snapshot_days=snapshot_days, max_rows=settings.max_rows)


@mcp.tool(annotations=READ_ONLY)
def k8s_rightsizing(namespace: str | None = None) -> RightsizingReport:
    """Compare Kubernetes CPU/memory requests with live usage per workload.

    Flags over-provisioned workloads (using under 30% of requests), under-provisioned ones,
    and pods with no requests set, and estimates reclaimable capacity. Needs metrics-server.

    Args:
        namespace: limit to one namespace. Defaults to all namespaces.
    """
    if not settings.k8s_enabled:
        raise ToolError("Kubernetes checks are disabled (COST_WATCHDOG_K8S=false).")
    from kubernetes.client.exceptions import ApiException
    from kubernetes.config.config_exception import ConfigException

    try:
        core, custom = _k8s()
        return rightsizing.rightsize(core, custom, namespace, settings.max_rows)
    except ConfigException as exc:
        raise ToolError(f"No Kubernetes access configured: {exc}") from exc
    except ApiException as exc:
        if exc.status == 404:
            raise ToolError("metrics.k8s.io not found: install metrics-server in the cluster.") from exc
        raise ToolError(f"Kubernetes API error {exc.status}: {exc.reason}") from exc


# ---------- prompt ----------

@mcp.prompt()
def weekly_cost_review() -> str:
    """A ready-made weekly FinOps review."""
    return (
        "Run a weekly cloud cost review. 1) Call forecast_this_month. 2) Call explain_cost_spike "
        "with window_days=7. 3) Call find_idle_resources. 4) If Kubernetes is available, call "
        "k8s_rightsizing. Then write a short report: headline number and trend, the top 3 cost "
        "drivers with their cause, the top 5 savings ranked by monthly impact with the exact "
        "resource IDs, and anything that needs a human decision. Do not suggest deleting "
        "anything without asking the owner first."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cost Watchdog MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
