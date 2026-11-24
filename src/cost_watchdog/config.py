"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    aws_profile: str | None = None
    # Regions scanned by find_idle_resources when the caller doesn't pass one.
    regions: list[str] = field(default_factory=lambda: ["eu-west-1"])
    # Cost Explorer metric. UnblendedCost matches the invoice; AmortizedCost spreads RIs/SPs.
    cost_metric: str = "UnblendedCost"
    k8s_enabled: bool = True
    # "incluster" when running as a pod, otherwise uses ~/.kube/config (optionally a named context).
    k8s_mode: str = "kubeconfig"
    k8s_context: str | None = None
    # Hard cap on rows returned per tool call so the agent's context isn't flooded.
    max_rows: int = 25

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            aws_profile=os.getenv("AWS_PROFILE") or None,
            regions=_csv(os.getenv("COST_WATCHDOG_REGIONS")) or ["eu-west-1"],
            cost_metric=os.getenv("COST_WATCHDOG_METRIC", "UnblendedCost"),
            k8s_enabled=os.getenv("COST_WATCHDOG_K8S", "true").lower() in ("1", "true", "yes"),
            k8s_mode=os.getenv("COST_WATCHDOG_K8S_MODE", "kubeconfig"),
            k8s_context=os.getenv("COST_WATCHDOG_K8S_CONTEXT") or None,
            max_rows=int(os.getenv("COST_WATCHDOG_MAX_ROWS", "25")),
        )
