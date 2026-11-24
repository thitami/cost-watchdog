"""Structured tool outputs. Pydantic models give MCP clients a typed output schema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CostLine(BaseModel):
    key: str = Field(description="Group value, e.g. service name or usage type")
    amount: float
    share_pct: float = Field(description="Share of the period total, in percent")


class CostBreakdown(BaseModel):
    start: str
    end: str = Field(description="Exclusive end date")
    group_by: str
    currency: str
    total: float
    lines: list[CostLine]
    truncated: bool = False


class CostDelta(BaseModel):
    key: str
    previous: float
    current: float
    delta: float
    delta_pct: float | None = Field(description="None when the previous value was zero (new spend)")


class SpikeDriver(BaseModel):
    service: str
    service_delta: float
    top_usage_types: list[CostDelta]


class SpikeExplanation(BaseModel):
    current_window: str
    previous_window: str
    currency: str
    previous_total: float
    current_total: float
    delta: float
    delta_pct: float | None
    biggest_movers: list[CostDelta]
    drivers: list[SpikeDriver] = Field(description="Usage-type drill-down for the top increases")
    summary: str


class Forecast(BaseModel):
    month: str
    currency: str
    month_to_date: float
    forecast_remaining: float
    forecast_month_total: float
    forecast_low: float | None = None
    forecast_high: float | None = None
    previous_month_total: float
    vs_previous_month_pct: float | None


class IdleResource(BaseModel):
    kind: str
    resource_id: str
    region: str
    reason: str
    est_monthly_cost: float = Field(description="Rough on-demand estimate in USD, see README")
    tags: dict[str, str] = {}


class IdleReport(BaseModel):
    regions: list[str]
    resources: list[IdleResource]
    est_monthly_waste: float
    truncated: bool = False
    errors: list[str] = []
    note: str = "Estimates use list prices and ignore discounts. Verify before deleting anything."


class WorkloadSizing(BaseModel):
    namespace: str
    workload: str
    pods: int
    cpu_request_cores: float
    cpu_usage_cores: float
    cpu_utilisation_pct: float | None
    mem_request_mib: float
    mem_usage_mib: float
    mem_utilisation_pct: float | None
    verdict: str = Field(description="over-provisioned | under-provisioned | no-requests | ok")
    suggestion: str


class RightsizingReport(BaseModel):
    scope: str
    workloads: list[WorkloadSizing]
    reclaimable_cpu_cores: float
    reclaimable_mem_gib: float
    truncated: bool = False
    note: str = (
        "Usage is a point-in-time sample from metrics-server. For real decisions use "
        "p95 over 7+ days from Prometheus."
    )
