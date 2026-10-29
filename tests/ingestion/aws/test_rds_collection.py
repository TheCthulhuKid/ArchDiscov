import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.enums import CoverageStatus
from archdiscov.models.resources.database import DbCluster, DbInstance, DbSubnetGroup
from archdiscov.services import load_snapshot, save_snapshot
from tests.support.rds_data import (
    ACCOUNT,
    CLUSTER,
    GROUP,
    INSTANCE,
    PREFIX,
    REGION,
    ROLE,
    SNAPSHOT,
    collect,
    data,
)


def by_type(result, cls):
    return next(resource for resource in result.resources if isinstance(resource, cls))


def test_typed_configuration_and_exact_relationship_citations():
    result = collect()
    assert len(result.resources) == 3
    assert all(c.status == CoverageStatus.SUCCESS for c in result.coverage)
    instance = by_type(result, DbInstance)
    cluster = by_type(result, DbCluster)
    group = by_type(result, DbSubnetGroup)
    assert instance.arn == instance.identity.physical_id == INSTANCE
    assert instance.cluster_identifier == cluster.identifier == "database"
    assert instance.db_subnet_group_name == group.name == group.group_name == "private"
    assert instance.publicly_accessible is False
    assert instance.allocated_storage == instance.backup_retention_period == 0
    assert cluster.members[0].is_cluster_writer is True
    assert cluster.members[0].promotion_tier == 0
    assert cluster.reader_endpoint_address == "reader.example.rds.amazonaws.com"
    assert cluster.security_group_ids == []
    for resource, pointer, source, value in [
        (instance, "/cluster_identifier", "DBClusterIdentifier", "database"),
        (instance, "/endpoint_address", "Endpoint.Address", "writer.example.rds.amazonaws.com"),
        (instance, "/endpoint_port", "Endpoint.Port", 5432),
        (instance, "/subnet_ids/0", "DBSubnetGroup.Subnets[0].SubnetIdentifier", "subnet-a"),
        (instance, "/security_group_ids/0", "VpcSecurityGroups[0].VpcSecurityGroupId", "sg-a"),
        (instance, "/associated_role_arns/0", "AssociatedRoles[0].RoleArn", ROLE),
        (group, "/subnet_ids/0", "Subnets[0].SubnetIdentifier", "subnet-a"),
        (cluster, "/members/0/is_cluster_writer", "DBClusterMembers[0].IsClusterWriter", True),
    ]:
        evidence = result.evidence[resource.fact_evidence[pointer][0]]
        assert evidence.configuration_path.endswith("." + source)
        assert evidence.value == value
        assert evidence.resource_id == resource.id


def test_real_stubber_marker_pagination_retains_first_page_after_second_page_failure():
    client = boto3.client(
        "rds",
        region_name=REGION.value,
        aws_access_key_id="a",
        aws_secret_access_key="b",
    )
    payload = data()
    with Stubber(client) as stub:
        stub.add_response(
            "describe_db_instances",
            {
                **payload["describe_db_instances"],
                "Marker": "next-page",
            },
            {},
        )
        stub.add_client_error(
            "describe_db_instances",
            service_error_code="AccessDenied",
            expected_params={"Marker": "next-page"},
        )
        stub.add_response("describe_db_clusters", payload["describe_db_clusters"], {})
        stub.add_response("describe_db_subnet_groups", payload["describe_db_subnet_groups"], {})
        result = collect(client=client)
        stub.assert_no_pending_responses()
    assert len(result.resources) == 3
    outcomes = {item.operation: item for item in result.coverage}
    assert outcomes["DescribeDBInstances"].status == CoverageStatus.INCOMPLETE
    assert outcomes["DescribeDBInstances"].failures[0].code == "AccessDenied"
    assert outcomes["DescribeDBClusters"].status == CoverageStatus.SUCCESS
    assert outcomes["DescribeDBSubnetGroups"].status == CoverageStatus.SUCCESS


def test_malformed_fields_retain_valid_siblings_with_precise_limitations():
    payload = data()
    instance = payload["describe_db_instances"]["DBInstances"][0]
    instance["Endpoint"]["Port"] = 0
    instance["VpcSecurityGroups"] = [{"VpcSecurityGroupId": 23}, {"VpcSecurityGroupId": "sg-ok"}]
    instance["DBSubnetGroup"]["Subnets"].append({"SubnetIdentifier": None})
    instance["AssociatedRoles"].append({"RoleArn": {"secret": "must-not-persist"}})
    payload["describe_db_instances"]["DBInstances"].append({"DBInstanceIdentifier": "missing-arn"})
    result = collect(payload)
    instance = by_type(result, DbInstance)
    assert instance.endpoint_port is None
    assert instance.endpoint_address is not None
    assert instance.security_group_ids == ["sg-ok"]
    assert instance.subnet_ids == ["subnet-a"]
    outcome = result.coverage[0]
    assert outcome.status == CoverageStatus.INCOMPLETE
    paths = {value.configuration_path for value in outcome.unresolved_values}
    for suffix in [
        "DBInstances[0].Endpoint.Port",
        "DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId",
        "DBInstances[0].DBSubnetGroup.Subnets[1].SubnetIdentifier",
        "DBInstances[0].AssociatedRoles[1].RoleArn",
        "DBInstances[1].DBInstanceArn",
    ]:
        assert any(path.endswith(suffix) for path in paths)
    evidence = result.evidence[instance.fact_evidence["/security_group_ids/0"][0]]
    assert evidence.configuration_path.endswith("VpcSecurityGroups[1].VpcSecurityGroupId")
    assert "must-not-persist" not in json.dumps(
        [e.model_dump(mode="json") for e in result.evidence.values()]
    )


def test_missing_empty_and_malformed_collections_are_distinct():
    payload = data()
    instance = payload["describe_db_instances"]["DBInstances"][0]
    del instance["AssociatedRoles"]
    instance["VpcSecurityGroups"] = []
    instance["DBSubnetGroup"]["Subnets"] = "bad"
    result = collect(payload)
    instance = by_type(result, DbInstance)
    assert instance.associated_role_arns is None
    assert instance.security_group_ids == []
    assert instance.subnet_ids is None
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE


@pytest.mark.parametrize(
    "port", [0, 65536, True], ids=["below-minimum", "above-maximum", "not-integer"]
)
def test_model_rejects_invalid_port(port):
    with pytest.raises(ValidationError):
        DbInstance(
            identity={
                "account_id": ACCOUNT,
                "region": REGION,
                "service": "rds",
                "physical_id": INSTANCE,
            },
            arn=INSTANCE,
            endpoint_port=port,
        )


def test_source_secret_exclusion_and_persistence_roundtrip(tmp_path):
    payload = data()
    instance = payload["describe_db_instances"]["DBInstances"][0]
    instance.update(
        {
            "MasterUsername": "private-admin-name",
            "MasterUserPassword": "private-password",
            "MasterUserSecret": {"SecretArn": "private-secret-arn"},
            "AuthenticationDetails": {"Credentials": "private-credentials"},
            "TagList": [
                {"Key": "password", "Value": "private-tag"},
                {"Key": "team", "Value": "infra"},
            ],
        }
    )
    instance["Endpoint"]["Address"] = "https://admin:private-url@host/?token=private-token"
    result = collect(payload)
    serialized = json.dumps(
        {
            "resources": [item.model_dump(mode="json") for item in result.resources],
            "evidence": {
                key: value.model_dump(mode="json") for key, value in result.evidence.items()
            },
            "coverage": [item.model_dump(mode="json") for item in result.coverage],
        }
    )
    for secret in [
        "private-admin-name",
        "private-password",
        "private-secret-arn",
        "private-credentials",
        "private-tag",
        "private-url",
        "private-token",
    ]:
        assert secret not in serialized
    for resource in result.resources:
        assert type(resource).model_validate_json(resource.model_dump_json()) == resource
    assert by_type(result, DbInstance).endpoint_address is None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ArchitectureSnapshot(
        schema_version="1",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=now,
        collection_completed_at=now,
        source={"kind": "aws", "profile": "offline"},
        coverage={"regions": [REGION], "account_wide": False, "operations": result.coverage},
        regions={REGION: {"resources": result.resources}},
        evidence=result.evidence,
    )
    path = save_snapshot(snapshot, tmp_path / "rds.json")
    assert load_snapshot(path) == snapshot
    assert "private-password" not in path.read_text()
    assert "private-tag" not in path.read_text()
    assert isinstance(load_snapshot(path).regions[REGION].resources[0], DbInstance)


def test_real_stubber_successful_marker_pages_for_all_inventories():
    client = boto3.client(
        "rds",
        region_name=REGION.value,
        aws_access_key_id="a",
        aws_secret_access_key="b",
    )
    with Stubber(client) as stub:
        for method, response in data().items():
            key = next(iter(response))
            stub.add_response(method, {key: [], "Marker": "second"}, {})
            stub.add_response(method, response, {"Marker": "second"})
        result = collect(client=client)
        stub.assert_no_pending_responses()
    assert all(item.status == CoverageStatus.SUCCESS for item in result.coverage)
    assert all(item.result_count == 1 for item in result.coverage)
    assert all(
        ".Page[1]." in result.evidence[r.evidence_ids[0]].configuration_path
        for r in result.resources
    )


def test_subnet_group_inventory_failure_does_not_hide_instances_or_clusters():
    client = boto3.client(
        "rds",
        region_name=REGION.value,
        aws_access_key_id="a",
        aws_secret_access_key="b",
    )
    payload = data()
    with Stubber(client) as stub:
        stub.add_response("describe_db_instances", payload["describe_db_instances"], {})
        stub.add_response("describe_db_clusters", payload["describe_db_clusters"], {})
        stub.add_client_error(
            "describe_db_subnet_groups", service_error_code="AccessDenied", expected_params={}
        )
        result = collect(client=client)
        stub.assert_no_pending_responses()
    assert [type(r) for r in result.resources] == [DbInstance, DbCluster]
    assert result.coverage[2].status == CoverageStatus.FAILED
    assert by_type(result, DbInstance).db_subnet_group_name == "private"
    assert all(item.status == CoverageStatus.SUCCESS for item in result.coverage[:2])


@pytest.mark.parametrize("value", [None, "malformed", {}, [None, {"VpcSecurityGroupId": None}]])
def test_unavailable_security_groups_are_not_reported_as_empty(value):
    payload = data()
    payload["describe_db_instances"]["DBInstances"][0]["VpcSecurityGroups"] = value
    result = collect(payload)
    assert by_type(result, DbInstance).security_group_ids is None
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE


def test_malformed_inventory_is_not_a_successful_empty_read():
    payload = data()
    payload["describe_db_instances"] = {"DBInstances": "not-a-list"}
    result = collect(payload)
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE
    assert result.coverage[0].unresolved_values[0].configuration_path.endswith(".DBInstances")
    assert len(result.resources) == 2


def test_missing_inventory_key_and_empty_inventory_have_different_coverage():
    payload = data()
    payload["describe_db_instances"] = {}
    payload["describe_db_clusters"] = {"DBClusters": []}
    result = collect(payload)
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE
    assert result.coverage[1].status == CoverageStatus.SUCCESS
    assert result.coverage[1].result_count == 0


@pytest.mark.parametrize(
    "cls,arn", [(DbInstance, INSTANCE), (DbCluster, CLUSTER), (DbSubnetGroup, GROUP)]
)
def test_arn_required_identity_matching_and_optional_collections(cls, arn):
    identity = {"account_id": ACCOUNT, "region": REGION, "service": "rds", "physical_id": arn}
    with pytest.raises(ValidationError):
        cls(identity=identity)
    with pytest.raises(ValidationError):
        cls(identity={**identity, "physical_id": arn + "-other"}, arn=arn)
    with pytest.raises(ValidationError):
        cls(identity={**identity, "service": "ec2"}, arn=arn)
    missing = cls(identity=identity, arn=arn)
    empty = cls(
        identity=identity, arn=arn, subnet_ids=[], security_group_ids=[], associated_role_arns=[]
    )
    for field in ["subnet_ids", "security_group_ids", "associated_role_arns"]:
        assert getattr(missing, field) is None
        assert getattr(empty, field) == []


def test_subnet_group_arn_allows_documented_name_characters():
    payload = data()
    group = payload["describe_db_subnet_groups"]["DBSubnetGroups"][0]
    group["DBSubnetGroupArn"] = PREFIX + "subgrp:private.db_group blue"
    group["DBSubnetGroupName"] = "private.db_group blue"
    result = collect(payload)
    assert by_type(result, DbSubnetGroup).name == "private.db_group blue"
    assert result.coverage[2].status == CoverageStatus.SUCCESS


def test_malformed_cluster_member_and_reader_endpoint_preserve_writer_and_siblings():
    payload = data()
    cluster = payload["describe_db_clusters"]["DBClusters"][0]
    cluster["DBClusterMembers"] = [
        None,
        {"DBInstanceIdentifier": "reader", "IsClusterWriter": "false"},
        *cluster["DBClusterMembers"],
    ]
    cluster["ReaderEndpoint"] = {"Credentials": "private-reader-material"}
    result = collect(payload)
    cluster = by_type(result, DbCluster)
    assert cluster.reader_endpoint_address is None
    assert cluster.endpoint_address == "cluster.example.rds.amazonaws.com"
    assert len(cluster.members) == 2
    assert cluster.members[0].is_cluster_writer is None
    assert cluster.members[1].is_cluster_writer is True
    citation = result.evidence[cluster.fact_evidence["/members/1/is_cluster_writer"][0]]
    assert citation.configuration_path.endswith("DBClusterMembers[2].IsClusterWriter")
    assert result.coverage[1].status == CoverageStatus.INCOMPLETE


def test_empty_endpoint_has_precise_missing_field_limitations():
    payload = data()
    payload["describe_db_instances"]["DBInstances"][0]["Endpoint"] = {}
    result = collect(payload)
    paths = [item.configuration_path for item in result.coverage[0].unresolved_values]
    assert any(path.endswith(".Endpoint.Address") for path in paths)
    assert any(path.endswith(".Endpoint.Port") for path in paths)


def test_malformed_subnet_group_keeps_valid_group_and_subnet_siblings():
    payload = data()
    groups = payload["describe_db_subnet_groups"]["DBSubnetGroups"]
    groups[0]["Subnets"] = [None, {"SubnetIdentifier": "subnet-good"}]
    groups.extend([None, {"DBSubnetGroupArn": GROUP + "-second", "VpcId": []}])
    result = collect(payload)
    groups = [resource for resource in result.resources if isinstance(resource, DbSubnetGroup)]
    assert len(groups) == 2
    assert groups[0].subnet_ids == ["subnet-good"]
    assert groups[1].vpc_id is None
    assert result.coverage[2].status == CoverageStatus.INCOMPLETE
    citation = result.evidence[groups[0].fact_evidence["/subnet_ids/0"][0]]
    assert citation.configuration_path.endswith("DBSubnetGroups[0].Subnets[1].SubnetIdentifier")
    assert all(item.status == CoverageStatus.SUCCESS for item in result.coverage[:2])


def test_real_stubber_repeated_marker_is_bounded_and_keeps_observed_pages():
    client = boto3.client(
        "rds", region_name=REGION.value, aws_access_key_id="a", aws_secret_access_key="b"
    )
    payload = data()
    with Stubber(client) as stub:
        stub.add_response(
            "describe_db_instances",
            {**payload["describe_db_instances"], "Marker": "repeated"},
            {},
        )
        stub.add_response(
            "describe_db_instances",
            {"DBInstances": [], "Marker": "repeated"},
            {"Marker": "repeated"},
        )
        stub.add_response("describe_db_clusters", payload["describe_db_clusters"], {})
        stub.add_response("describe_db_subnet_groups", payload["describe_db_subnet_groups"], {})
        result = collect(client=client)
        stub.assert_no_pending_responses()
    assert len(result.resources) == 3
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE
    assert result.coverage[0].result_count == 1


def test_invalid_source_arn_never_creates_an_invented_identity():
    payload = data()
    payload["describe_db_instances"]["DBInstances"][0]["DBInstanceArn"] = INSTANCE.replace(
        ACCOUNT, "999999999999"
    )
    result = collect(payload)
    assert not any(isinstance(resource, DbInstance) for resource in result.resources)
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE
    assert result.coverage[0].unresolved_values[0].configuration_path.endswith(".DBInstanceArn")
