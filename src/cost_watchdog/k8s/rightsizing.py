"""Compare container resource requests with live usage from metrics-server.

Requests, not usage, are what reserve node capacity, so over-requested workloads are
where Kubernetes money is usually wasted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from kubernetes.utils import parse_quantity

from ..models import RightsizingReport, WorkloadSizing

OVER_THRESHOLD = 0.30   # using less than 30% of request -> over-provisioned
UNDER_THRESHOLD = 1.00  # using more than request -> under-provisioned (throttling / eviction risk)
HEADROOM = 1.3          # suggested request = usage * 1.3

_RS_HASH = re.compile(r"-[a-z0-9]{8,10}$")


def workload_name(pod: Any) -> str:
    """Best-effort owner name: Deployment (via ReplicaSet), StatefulSet, DaemonSet, Job, or the pod."""
    owners = pod.metadata.owner_references or []
    if not owners:
        return f"pod/{pod.metadata.name}"
    o = owners[0]
    if o.kind == "ReplicaSet":
        return f"deployment/{_RS_HASH.sub('', o.name)}"
    return f"{o.kind.lower()}/{o.name}"


def _requests(pod: Any) -> tuple[float, float, bool]:
    cpu = mem = 0.0
    missing = False
    for c in pod.spec.containers:
        req = (c.resources.requests if c.resources else None) or {}
        if "cpu" not in req or "memory" not in req:
            missing = True
        cpu += float(parse_quantity(req.get("cpu", "0")))
        mem += float(parse_quantity(req.get("memory", "0")))
    return cpu, mem, missing


def _usage_index(metrics: dict) -> dict[tuple[str, str], tuple[float, float]]:
    idx = {}
    for item in metrics.get("items", []):
        cpu = sum(float(parse_quantity(c["usage"]["cpu"])) for c in item.get("containers", []))
        mem = sum(float(parse_quantity(c["usage"]["memory"])) for c in item.get("containers", []))
        idx[(item["metadata"]["namespace"], item["metadata"]["name"])] = (cpu, mem)
    return idx


def _ratio(usage: float, request: float) -> float | None:
    return usage / request if request else None


def rightsize(core: Any, custom: Any, namespace: str | None = None,
              max_rows: int = 25) -> RightsizingReport:
    """`core` is a CoreV1Api, `custom` a CustomObjectsApi (both injectable for tests)."""
    if namespace:
        pods = core.list_namespaced_pod(namespace, field_selector="status.phase=Running").items
        metrics = custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, "pods")
    else:
        pods = core.list_pod_for_all_namespaces(field_selector="status.phase=Running").items
        metrics = custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
    usage = _usage_index(metrics)

    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for pod in pods:
        ns = pod.metadata.namespace
        key = (ns, workload_name(pod))
        cpu_r, mem_r, missing = _requests(pod)
        cpu_u, mem_u = usage.get((ns, pod.metadata.name), (0.0, 0.0))
        a = agg[key]
        a["pods"] += 1
        a["cpu_r"] += cpu_r
        a["mem_r"] += mem_r
        a["cpu_u"] += cpu_u
        a["mem_u"] += mem_u
        a["missing"] = max(a["missing"], float(missing))

    rows: list[WorkloadSizing] = []
    reclaim_cpu = reclaim_mem = 0.0
    for (ns, name), a in agg.items():
        cpu_ratio = _ratio(a["cpu_u"], a["cpu_r"])
        mem_ratio = _ratio(a["mem_u"], a["mem_r"])
        pods = int(a["pods"])

        if a["missing"]:
            verdict = "no-requests"
            suggestion = (f"Set requests. Current usage per pod is ~{a['cpu_u'] / pods * 1000:.0f}m CPU, "
                          f"{a['mem_u'] / pods / 2**20:.0f}Mi memory.")
        elif (cpu_ratio or 0) > UNDER_THRESHOLD or (mem_ratio or 0) > UNDER_THRESHOLD:
            verdict = "under-provisioned"
            suggestion = "Usage exceeds requests: raise requests to avoid throttling and eviction."
        elif (cpu_ratio is not None and cpu_ratio < OVER_THRESHOLD) or \
             (mem_ratio is not None and mem_ratio < OVER_THRESHOLD):
            verdict = "over-provisioned"
            new_cpu = max(a["cpu_u"] * HEADROOM / pods, 0.01)
            new_mem = max(a["mem_u"] * HEADROOM / pods, 32 * 2**20)
            suggestion = f"Lower per-pod requests to about {new_cpu * 1000:.0f}m CPU / {new_mem / 2**20:.0f}Mi."
            reclaim_cpu += max(a["cpu_r"] - new_cpu * pods, 0)
            reclaim_mem += max(a["mem_r"] - new_mem * pods, 0)
        else:
            verdict, suggestion = "ok", "No change."

        rows.append(WorkloadSizing(
            namespace=ns, workload=name, pods=pods,
            cpu_request_cores=round(a["cpu_r"], 3), cpu_usage_cores=round(a["cpu_u"], 3),
            cpu_utilisation_pct=round(cpu_ratio * 100, 1) if cpu_ratio is not None else None,
            mem_request_mib=round(a["mem_r"] / 2**20, 1), mem_usage_mib=round(a["mem_u"] / 2**20, 1),
            mem_utilisation_pct=round(mem_ratio * 100, 1) if mem_ratio is not None else None,
            verdict=verdict, suggestion=suggestion))

    # Most wasteful first: biggest unused CPU request, then memory.
    rows.sort(key=lambda r: (r.verdict == "ok",
                             -(r.cpu_request_cores - r.cpu_usage_cores),
                             -(r.mem_request_mib - r.mem_usage_mib)))
    return RightsizingReport(
        scope=namespace or "all namespaces", workloads=rows[:max_rows],
        reclaimable_cpu_cores=round(reclaim_cpu, 2), reclaimable_mem_gib=round(reclaim_mem / 2**30, 2),
        truncated=len(rows) > max_rows)
