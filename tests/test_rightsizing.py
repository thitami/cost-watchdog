from types import SimpleNamespace as NS

from cost_watchdog.k8s import rightsizing


def pod(name, ns, owner, cpu=None, mem=None):
    req = {k: v for k, v in (("cpu", cpu), ("memory", mem)) if v}
    owners = [NS(kind=owner[0], name=owner[1])] if owner else None
    return NS(metadata=NS(name=name, namespace=ns, owner_references=owners),
              spec=NS(containers=[NS(resources=NS(requests=req))]))


def metric(name, ns, cpu, mem):
    return {"metadata": {"name": name, "namespace": ns},
            "containers": [{"usage": {"cpu": cpu, "memory": mem}}]}


class FakeCore:
    def __init__(self, pods):
        self.pods = pods

    def list_pod_for_all_namespaces(self, field_selector):
        return NS(items=self.pods)

    def list_namespaced_pod(self, ns, field_selector):
        return NS(items=[p for p in self.pods if p.metadata.namespace == ns])


class FakeCustom:
    def __init__(self, items):
        self.items = items

    def list_cluster_custom_object(self, group, version, plural):
        return {"items": self.items}

    def list_namespaced_custom_object(self, group, version, ns, plural):
        return {"items": [i for i in self.items if i["metadata"]["namespace"] == ns]}


PODS = [
    # api: 2 replicas, each asks 1 CPU / 1Gi, uses 100m / 200Mi  -> over-provisioned
    pod("api-7d4b9c8f6d-abcde", "prod", ("ReplicaSet", "api-7d4b9c8f6d"), "1", "1Gi"),
    pod("api-7d4b9c8f6d-fghij", "prod", ("ReplicaSet", "api-7d4b9c8f6d"), "1", "1Gi"),
    # worker: asks 100m, uses 300m -> under-provisioned
    pod("worker-0", "prod", ("StatefulSet", "worker"), "100m", "256Mi"),
    # cron: no requests
    pod("cron-x", "batch", ("Job", "nightly"), None, None),
]
METRICS = [
    metric("api-7d4b9c8f6d-abcde", "prod", "100m", "200Mi"),
    metric("api-7d4b9c8f6d-fghij", "prod", "100m", "200Mi"),
    metric("worker-0", "prod", "300m", "200Mi"),
    metric("cron-x", "batch", "50m", "64Mi"),
]


def test_rightsizing_verdicts_and_grouping():
    report = rightsizing.rightsize(FakeCore(PODS), FakeCustom(METRICS))
    by = {w.workload: w for w in report.workloads}
    assert set(by) == {"deployment/api", "statefulset/worker", "job/nightly"}

    api = by["deployment/api"]
    assert api.pods == 2 and api.verdict == "over-provisioned"
    assert api.cpu_utilisation_pct == 10.0
    assert "130m" in api.suggestion
    assert by["statefulset/worker"].verdict == "under-provisioned"
    assert by["job/nightly"].verdict == "no-requests"

    # 2 cores requested, 0.26 suggested in total
    assert report.reclaimable_cpu_cores == 1.74
    assert report.workloads[0].workload == "deployment/api"


def test_rightsizing_namespace_filter():
    report = rightsizing.rightsize(FakeCore(PODS), FakeCustom(METRICS), namespace="batch")
    assert [w.workload for w in report.workloads] == ["job/nightly"]
