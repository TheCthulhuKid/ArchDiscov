import json

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import ValidationError

from archdiscov.models.enums import CoverageStatus
from archdiscov.models.resources.database import DbCluster, DbInstance, DbSubnetGroup
from tests.support.rds_data import (
    ACCOUNT,
    INSTANCE,
    REGION,
    ROLE,
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
    payload["describe_db_clusters"] = {"DBClusters": "not-a-list"}
    result = collect(payload)
    instance = by_type(result, DbInstance)
    assert instance.associated_role_arns is None
    assert instance.security_group_ids == []
    assert instance.subnet_ids is None
    assert result.coverage[0].status == CoverageStatus.INCOMPLETE
    # A malformed inventory is not a successful empty read.
    assert not any(isinstance(r, DbCluster) for r in result.resources)
    assert result.coverage[1].status == CoverageStatus.INCOMPLETE
    assert result.coverage[1].unresolved_values[0].configuration_path.endswith(".DBClusters")


def test_identity_must_use_the_matching_source_arn():
    identity = {"account_id": ACCOUNT, "region": REGION, "service": "rds", "physical_id": INSTANCE}
    with pytest.raises(ValidationError):
        DbInstance(identity={**identity, "physical_id": INSTANCE + "-other"}, arn=INSTANCE)


def test_source_secrets_are_excluded_from_resources_evidence_and_coverage():
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
    assert by_type(result, DbInstance).endpoint_address is None


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
