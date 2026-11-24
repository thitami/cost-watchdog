"""Serve a World through real boto3 clients and fake Kubernetes APIs, and meter every call.

boto3 clients are real: botocore still validates parameters, runs paginators and raises ClientError.
A `before-call` hook answers from the World instead of the network, so the server code under
test is exactly what runs in production.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace as NS
from typing import Any, Iterator

import boto3

from .world import World, Workload

CE_PRICE_PER_REQUEST = 0.01

_DIMENSIONS = {
    "SERVICE": "service",
    "USAGE_TYPE": "usage_type",
    "REGION": "region",
    "LINKED_ACCOUNT": "account",
    "INSTANCE_TYPE": "instance_type",
}


class FakeAPIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Meter:
    """Counts backend API calls. Optionally mirrors counts to a JSON file (for subprocess servers).

    The server runs sync tools in worker threads, so parallel tool calls record concurrently.
    """

    def __init__(self, path: str | None = None):
        self.calls: Counter[str] = Counter()
        self.path = path
        self._lock = threading.Lock()

    def record(self, service: str, op: str) -> None:
        with self._lock:
            self.calls[f"{service}:{op}"] += 1
            if self.path:
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w") as f:
                    json.dump(dict(self.calls), f)
                os.replace(tmp, self.path)

    @property
    def ce_calls(self) -> int:
        return sum(n for k, n in self.calls.items() if k.startswith("ce:"))

    @staticmethod
    def read(path: str) -> Counter[str]:
        try:
            with open(path) as f:
                return Counter(json.load(f))
        except FileNotFoundError:
            return Counter()


# ---------- Cost Explorer ----------

def _d(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise FakeAPIError("ValidationException", f"Invalid date {s!r}") from exc


def _matches(line: Any, flt: dict | None) -> bool:
    if not flt:
        return True
    if "And" in flt:
        return all(_matches(line, f) for f in flt["And"])
    if "Or" in flt:
        return any(_matches(line, f) for f in flt["Or"])
    if "Not" in flt:
        return not _matches(line, flt["Not"])
    if "Dimensions" in flt:
        dim = flt["Dimensions"]
        attr = _DIMENSIONS.get(dim["Key"])
        if attr is None:
            raise FakeAPIError("ValidationException", f"Unsupported filter dimension {dim['Key']}")
        return getattr(line, attr) in dim.get("Values", [])
    raise FakeAPIError("ValidationException", f"Unsupported filter {sorted(flt)}")


def _periods(start: date, end: date, granularity: str) -> list[tuple[date, date]]:
    out, d = [], start
    while d < end:
        if granularity == "DAILY":
            nxt = d + timedelta(days=1)
        else:
            nxt = min((d.replace(day=28) + timedelta(days=4)).replace(day=1), end)
        out.append((d, nxt))
        d = nxt
    return out


class CostExplorer:
    def __init__(self, world: World):
        self.world = world

    def get_cost_and_usage(self, p: dict) -> dict:
        start, end = _d(p["TimePeriod"]["Start"]), _d(p["TimePeriod"]["End"])
        if end <= start:
            raise FakeAPIError("ValidationException", "Start date must be before end date")
        if start < self.world.today - timedelta(days=14 * 30):
            raise FakeAPIError("ValidationException", "Start date is too far in the past")
        gran = p["Granularity"]
        if gran not in ("DAILY", "MONTHLY"):
            raise FakeAPIError("ValidationException", f"Granularity {gran} not enabled")
        group_by = p.get("GroupBy") or []
        if len(group_by) > 2:
            raise FakeAPIError("ValidationException", "At most two GroupBy keys")
        attrs = []
        for g in group_by:
            if g.get("Type") != "DIMENSION" or g.get("Key") not in _DIMENSIONS:
                raise FakeAPIError("ValidationException", f"Unsupported GroupBy {g}")
            attrs.append(_DIMENSIONS[g["Key"]])
        flt = p.get("Filter")

        results = []
        for pstart, pend in _periods(start, end, gran):
            sums: dict[tuple[str, ...], float] = defaultdict(float)
            for _, line, amt in self.world.daily_rows(pstart, pend):
                if _matches(line, flt):
                    sums[tuple(getattr(line, a) for a in attrs)] += amt
            metric = lambda v: {m: {"Amount": f"{v:.10f}", "Unit": "USD"} for m in p["Metrics"]}
            period: dict[str, Any] = {
                "TimePeriod": {"Start": pstart.isoformat(), "End": pend.isoformat()},
                "Estimated": pend >= self.world.today - timedelta(days=1),
            }
            if attrs:
                period["Total"] = {}
                period["Groups"] = [{"Keys": list(k), "Metrics": metric(v)}
                                    for k, v in sorted(sums.items())]
            else:
                period["Total"] = metric(sum(sums.values()))
                period["Groups"] = []
            results.append(period)
        return {"ResultsByTime": results, "DimensionValueAttributes": [],
                "GroupDefinitions": group_by}

    def get_cost_forecast(self, p: dict) -> dict:
        start, end = _d(p["TimePeriod"]["Start"]), _d(p["TimePeriod"]["End"])
        if start < self.world.today:
            raise FakeAPIError("ValidationException", "Start date must be today or later")
        if end <= start:
            raise FakeAPIError("ValidationException", "Start date must be before end date")
        # Forecast = recent 7-day run rate, like a naive version of AWS's model.
        recent = sum(a for _, _, a in self.world.daily_rows(self.world.day(7), self.world.today))
        per_day = recent / 7
        out = []
        for pstart, pend in _periods(start, end, p["Granularity"]):
            mean = per_day * (pend - pstart).days
            out.append({"TimePeriod": {"Start": pstart.isoformat(), "End": pend.isoformat()},
                        "MeanValue": f"{mean:.4f}",
                        "PredictionIntervalLowerBound": f"{mean * 0.92:.4f}",
                        "PredictionIntervalUpperBound": f"{mean * 1.08:.4f}"})
        total = sum(float(r["MeanValue"]) for r in out)
        return {"Total": {"Amount": f"{total:.4f}", "Unit": "USD"}, "ForecastResultsByTime": out}


# ---------- EC2 / ELBv2 ----------

class Ec2:
    def __init__(self, world: World, region: str):
        self.inv = world.inventory.get(region)
        if self.inv is None:
            raise FakeAPIError("UnauthorizedOperation", f"Region {region} is not enabled", 403)

    def describe_volumes(self, p: dict) -> dict:
        return {"Volumes": self.inv.volumes}

    def describe_addresses(self, p: dict) -> dict:
        return {"Addresses": self.inv.addresses}

    def describe_instances(self, p: dict) -> dict:
        states = next((f["Values"] for f in p.get("Filters", [])
                       if f["Name"] == "instance-state-name"), None)
        insts = [i for i in self.inv.instances if not states or i["State"]["Name"] in states]
        return {"Reservations": [{"ReservationId": f"r-{i['InstanceId'][2:]}", "Instances": [i]}
                                 for i in insts]}

    def describe_images(self, p: dict) -> dict:
        return {"Images": self.inv.images}

    def describe_snapshots(self, p: dict) -> dict:
        return {"Snapshots": self.inv.snapshots}


class Elbv2:
    def __init__(self, world: World, region: str):
        inv = world.inventory.get(region)
        if inv is None:
            raise FakeAPIError("AccessDenied", f"Region {region} is not enabled", 403)
        self.lbs = inv.load_balancers

    def describe_load_balancers(self, p: dict) -> dict:
        return {"LoadBalancers": [x["lb"] for x in self.lbs]}

    def describe_target_groups(self, p: dict) -> dict:
        for x in self.lbs:
            if x["lb"]["LoadBalancerArn"] == p.get("LoadBalancerArn"):
                return {"TargetGroups": [{"TargetGroupArn": arn} for arn in x["target_groups"]]}
        raise FakeAPIError("LoadBalancerNotFound", "Load balancer not found")

    def describe_target_health(self, p: dict) -> dict:
        for x in self.lbs:
            if p.get("TargetGroupArn") in x["target_groups"]:
                n = x["target_groups"][p["TargetGroupArn"]]
                return {"TargetHealthDescriptions": [
                    {"Target": {"Id": f"i-{k:017x}"}, "TargetHealth": {"State": "healthy"}}
                    for k in range(n)]}
        raise FakeAPIError("TargetGroupNotFound", "Target group not found")


_BACKENDS = {"ce": lambda w, r: CostExplorer(w), "ec2": Ec2, "elbv2": Elbv2}


def _snake(name: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")


class _Http:
    def __init__(self, status: int):
        self.status_code = status
        self.headers: dict[str, str] = {}


def make_client(world: World, service: str, region: str, meter: Meter) -> Any:
    """A real boto3 client whose API calls are answered by the World and counted by `meter`."""
    client = boto3.client(service, region_name=region, aws_access_key_id="eval",
                          aws_secret_access_key="eval")

    def capture(params: dict, context: dict, **_: Any) -> None:
        context["eval_params"] = dict(params)

    def respond(model: Any, context: dict, **_: Any) -> tuple[_Http, dict]:
        meter.record(service, model.name)
        try:
            backend = _BACKENDS[service](world, region)
            handler = getattr(backend, _snake(model.name), None)
            if handler is None:
                raise FakeAPIError("UnsupportedOperation", f"{model.name} is not faked")
            return _Http(200), handler(context.get("eval_params", {}))
        except FakeAPIError as e:
            return _Http(e.status), {"Error": {"Code": e.code, "Message": e.message},
                                     "ResponseMetadata": {"HTTPStatusCode": e.status}}

    client.meta.events.register("before-parameter-build", capture)
    client.meta.events.register("before-call", respond)
    return client


# ---------- Kubernetes ----------

def _pods(w: Workload) -> list[tuple[Any, dict]]:
    out = []
    for i in range(w.replicas):
        if w.kind == "Deployment":
            rs = f"{w.name}-7d4b9c8f6d"
            name, owner = f"{rs}-{i:05x}", NS(kind="ReplicaSet", name=rs)
        else:
            name, owner = f"{w.name}-{i}", NS(kind=w.kind, name=w.name)
        req = {k: v for k, v in (("cpu", w.cpu_request), ("memory", w.mem_request)) if v}
        pod = NS(metadata=NS(name=name, namespace=w.namespace, owner_references=[owner]),
                 spec=NS(containers=[NS(name="main", resources=NS(requests=req))]))
        metric = {"metadata": {"name": name, "namespace": w.namespace},
                  "containers": [{"name": "main",
                                  "usage": {"cpu": w.cpu_usage, "memory": w.mem_usage}}]}
        out.append((pod, metric))
    return out


class FakeCore:
    def __init__(self, world: World, meter: Meter):
        self.pods = [p for w in world.workloads for p, _ in _pods(w)]
        self.meter = meter

    def list_pod_for_all_namespaces(self, field_selector: str | None = None) -> Any:
        self.meter.record("k8s", "ListPodForAllNamespaces")
        return NS(items=self.pods)

    def list_namespaced_pod(self, namespace: str, field_selector: str | None = None) -> Any:
        self.meter.record("k8s", "ListNamespacedPod")
        return NS(items=[p for p in self.pods if p.metadata.namespace == namespace])


class FakeCustom:
    def __init__(self, world: World, meter: Meter):
        self.items = [m for w in world.workloads for _, m in _pods(w)]
        self.meter = meter

    def list_cluster_custom_object(self, group: str, version: str, plural: str) -> dict:
        self.meter.record("k8s", "ListPodMetrics")
        return {"items": self.items}

    def list_namespaced_custom_object(self, group: str, version: str, namespace: str,
                                      plural: str) -> dict:
        self.meter.record("k8s", "ListPodMetrics")
        return {"items": [i for i in self.items if i["metadata"]["namespace"] == namespace]}


# ---------- wiring into the unchanged server ----------

@contextmanager
def installed(world: World, meter: Meter) -> Iterator[Any]:
    """Point cost_watchdog.server's client factories at the World. Restores them on exit.

    Only module-level factories are swapped; tool code, models and annotations are untouched.
    """
    from cost_watchdog import server
    from cost_watchdog.config import Settings

    clients: dict[tuple[str, str], Any] = {}

    def client(service: str, region: str) -> Any:
        if (service, region) not in clients:
            clients[(service, region)] = make_client(world, service, region, meter)
        return clients[(service, region)]

    patches = {
        "_ce": lambda: client("ce", "us-east-1"),
        "_client": client,
        "_k8s": lambda: (FakeCore(world, meter), FakeCustom(world, meter)),
        "_today": lambda: world.today,
        "settings": Settings(regions=list(world.regions)),
    }
    saved = {k: getattr(server, k) for k in patches}
    for k, v in patches.items():
        setattr(server, k, v)
    try:
        yield server
    finally:
        for k, v in saved.items():
            setattr(server, k, v)
