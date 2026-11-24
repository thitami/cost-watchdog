"""Find resources that cost money while doing nothing.

All calls are Describe*/List* only. Prices are rough us-east-1 list prices so the
agent can rank findings; they are not a bill. Override PRICES if you need accuracy
(or swap in the Pricing API).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..models import IdleReport, IdleResource

HOURS_PER_MONTH = 730

PRICES = {
    "ebs_gb_month": {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "io2": 0.125,
                     "st1": 0.045, "sc1": 0.015, "standard": 0.05},
    "snapshot_gb_month": 0.05,
    # All public IPv4 addresses are billed hourly since Feb 2024.
    "public_ipv4_hour": 0.005,
    "alb_nlb_hour": 0.0225,
}

_STOP_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")


def _tags(raw: list[dict] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (raw or [])}


def _paginate(client: Any, op: str, key: str, **kwargs: Any) -> list[dict]:
    out: list[dict] = []
    for page in client.get_paginator(op).paginate(**kwargs):
        out.extend(page.get(key, []))
    return out


def _ebs_cost(vol: dict) -> float:
    per_gb = PRICES["ebs_gb_month"].get(vol.get("VolumeType", "gp2"), 0.10)
    return vol.get("Size", 0) * per_gb


def scan_region(region: str, ec2: Any, elbv2: Any, now: datetime,
                stopped_days: int = 14, snapshot_days: int = 90) -> list[IdleResource]:
    found: list[IdleResource] = []
    volumes = _paginate(ec2, "describe_volumes", "Volumes")

    # 1. Unattached EBS volumes
    for v in volumes:
        if v.get("State") == "available":
            found.append(IdleResource(
                kind="ebs_volume", resource_id=v["VolumeId"], region=region,
                reason=f"Unattached {v.get('VolumeType')} volume, {v.get('Size')} GiB",
                est_monthly_cost=round(_ebs_cost(v), 2), tags=_tags(v.get("Tags"))))

    # 2. Elastic IPs not associated with anything
    for a in ec2.describe_addresses().get("Addresses", []):
        if not a.get("AssociationId"):
            found.append(IdleResource(
                kind="elastic_ip", resource_id=a.get("AllocationId") or a.get("PublicIp", "?"),
                region=region, reason=f"Elastic IP {a.get('PublicIp')} not associated",
                est_monthly_cost=round(PRICES["public_ipv4_hour"] * HOURS_PER_MONTH, 2),
                tags=_tags(a.get("Tags"))))

    # 3. Instances stopped for a long time (still paying for their EBS)
    vol_cost_by_instance: dict[str, float] = {}
    for v in volumes:
        for att in v.get("Attachments", []):
            vol_cost_by_instance[att["InstanceId"]] = (
                vol_cost_by_instance.get(att["InstanceId"], 0.0) + _ebs_cost(v))
    reservations = _paginate(ec2, "describe_instances", "Reservations",
                             Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])
    for r in reservations:
        for i in r.get("Instances", []):
            m = _STOP_RE.search(i.get("StateTransitionReason", ""))
            if not m:
                continue
            stopped_at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            days = (now - stopped_at).days
            if days >= stopped_days:
                found.append(IdleResource(
                    kind="stopped_instance", resource_id=i["InstanceId"], region=region,
                    reason=f"{i.get('InstanceType')} stopped for {days} days; attached EBS still billed",
                    est_monthly_cost=round(vol_cost_by_instance.get(i["InstanceId"], 0.0), 2),
                    tags=_tags(i.get("Tags"))))

    # 4. Old snapshots not backing any of our AMIs
    ami_snaps: set[str] = set()
    for img in ec2.describe_images(Owners=["self"]).get("Images", []):
        for bdm in img.get("BlockDeviceMappings", []):
            sid = bdm.get("Ebs", {}).get("SnapshotId")
            if sid:
                ami_snaps.add(sid)
    cutoff = now - timedelta(days=snapshot_days)
    for s in _paginate(ec2, "describe_snapshots", "Snapshots", OwnerIds=["self"]):
        start = s.get("StartTime")
        if start and start < cutoff and s["SnapshotId"] not in ami_snaps:
            found.append(IdleResource(
                kind="ebs_snapshot", resource_id=s["SnapshotId"], region=region,
                reason=f"Snapshot is {(now - start).days} days old and not used by an AMI "
                       f"(cost is an upper bound, snapshots are incremental)",
                est_monthly_cost=round(s.get("VolumeSize", 0) * PRICES["snapshot_gb_month"], 2),
                tags=_tags(s.get("Tags"))))

    # 5. ALB/NLB with no registered targets
    for lb in _paginate(elbv2, "describe_load_balancers", "LoadBalancers"):
        arn = lb["LoadBalancerArn"]
        tgs = elbv2.describe_target_groups(LoadBalancerArn=arn).get("TargetGroups", [])
        registered = sum(
            len(elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                .get("TargetHealthDescriptions", []))
            for tg in tgs)
        if registered == 0:
            found.append(IdleResource(
                kind="load_balancer", resource_id=lb.get("LoadBalancerName", arn), region=region,
                reason=f"{lb.get('Type', 'application').upper()} load balancer with no registered targets",
                est_monthly_cost=round(PRICES["alb_nlb_hour"] * HOURS_PER_MONTH, 2)))

    return found


def find_idle(regions: list[str], client_factory: Callable[[str, str], Any],
              now: datetime | None = None, stopped_days: int = 14, snapshot_days: int = 90,
              max_rows: int = 25) -> IdleReport:
    """Scan each region. One region failing (e.g. AccessDenied, opt-in region) doesn't stop the rest."""
    now = now or datetime.now(timezone.utc)
    resources: list[IdleResource] = []
    errors: list[str] = []
    for region in regions:
        try:
            resources += scan_region(region, client_factory("ec2", region),
                                     client_factory("elbv2", region), now,
                                     stopped_days, snapshot_days)
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{region}: {type(exc).__name__}: {exc}")
    resources.sort(key=lambda r: r.est_monthly_cost, reverse=True)
    return IdleReport(
        regions=regions, resources=resources[:max_rows],
        est_monthly_waste=round(sum(r.est_monthly_cost for r in resources), 2),
        truncated=len(resources) > max_rows, errors=errors)
