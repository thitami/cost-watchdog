from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber

from cost_watchdog.aws import idle

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _client(service):
    return boto3.client(service, region_name="eu-west-1",
                        aws_access_key_id="x", aws_secret_access_key="x")


def test_scan_region_finds_each_kind_of_waste():
    ec2, elb = _client("ec2"), _client("elbv2")
    with Stubber(ec2) as s, Stubber(elb) as e:
        s.add_response("describe_volumes", {"Volumes": [
            {"VolumeId": "vol-free", "State": "available", "VolumeType": "gp3", "Size": 500,
             "Tags": [{"Key": "team", "Value": "data"}]},
            {"VolumeId": "vol-att", "State": "in-use", "VolumeType": "gp2", "Size": 100,
             "Attachments": [{"InstanceId": "i-old"}]},
        ]})
        s.add_response("describe_addresses", {"Addresses": [
            {"AllocationId": "eipalloc-1", "PublicIp": "1.2.3.4"},
            {"AllocationId": "eipalloc-2", "PublicIp": "5.6.7.8", "AssociationId": "a-1"},
        ]})
        s.add_response("describe_instances", {"Reservations": [{"Instances": [
            {"InstanceId": "i-old", "InstanceType": "m5.xlarge",
             "StateTransitionReason": "User initiated (2026-09-01 10:00:00 GMT)"},
            {"InstanceId": "i-new", "InstanceType": "t3.micro",
             "StateTransitionReason": "User initiated (2026-11-15 10:00:00 GMT)"},
        ]}]})
        s.add_response("describe_images", {"Images": [
            {"ImageId": "ami-1", "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-ami"}}]}]},
            {"Owners": ["self"]})
        s.add_response("describe_snapshots", {"Snapshots": [
            {"SnapshotId": "snap-old", "VolumeSize": 200, "StartTime": datetime(2025, 1, 1, tzinfo=timezone.utc)},
            {"SnapshotId": "snap-ami", "VolumeSize": 50, "StartTime": datetime(2025, 1, 1, tzinfo=timezone.utc)},
            {"SnapshotId": "snap-new", "VolumeSize": 50, "StartTime": datetime(2026, 11, 1, tzinfo=timezone.utc)},
        ]})
        e.add_response("describe_load_balancers", {"LoadBalancers": [
            {"LoadBalancerArn": "arn:lb/empty", "LoadBalancerName": "empty-alb", "Type": "application"},
            {"LoadBalancerArn": "arn:lb/busy", "LoadBalancerName": "busy-alb", "Type": "application"},
        ]})
        e.add_response("describe_target_groups", {"TargetGroups": [{"TargetGroupArn": "arn:tg/1"}]})
        e.add_response("describe_target_health", {"TargetHealthDescriptions": []})
        e.add_response("describe_target_groups", {"TargetGroups": [{"TargetGroupArn": "arn:tg/2"}]})
        e.add_response("describe_target_health", {"TargetHealthDescriptions": [
            {"Target": {"Id": "i-abc"}}]})

        found = {r.resource_id: r for r in idle.scan_region("eu-west-1", ec2, elb, NOW)}

    assert set(found) == {"vol-free", "eipalloc-1", "i-old", "snap-old", "empty-alb"}
    assert found["vol-free"].est_monthly_cost == 40.0
    assert found["vol-free"].tags == {"team": "data"}
    assert found["i-old"].est_monthly_cost == 10.0  # its 100 GiB gp2 volume
    assert "77 days" in found["i-old"].reason
    assert found["eipalloc-1"].est_monthly_cost == 3.65


def test_find_idle_survives_a_failing_region():
    def factory(service, region):
        if region == "ap-east-1":
            raise PermissionError("region not enabled")
        c = _client(service)
        st = Stubber(c)
        if service == "ec2":
            st.add_response("describe_volumes", {"Volumes": [
                {"VolumeId": "vol-1", "State": "available", "VolumeType": "gp2", "Size": 10}]})
            st.add_response("describe_addresses", {"Addresses": []})
            st.add_response("describe_instances", {"Reservations": []})
            st.add_response("describe_images", {"Images": []})
            st.add_response("describe_snapshots", {"Snapshots": []})
        else:
            st.add_response("describe_load_balancers", {"LoadBalancers": []})
        st.activate()
        return c

    report = idle.find_idle(["eu-west-1", "ap-east-1"], factory, now=NOW)
    assert [r.resource_id for r in report.resources] == ["vol-1"]
    assert report.est_monthly_waste == 1.0
    assert report.errors and report.errors[0].startswith("ap-east-1")
