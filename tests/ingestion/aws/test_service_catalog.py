from importlib import import_module
from uuid import uuid4

import pytest

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.configuration_services import collect_configuration_service
from archdiscov.ingestion.operations import ALLOWED_OPERATIONS, Ec2Operation
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.ingestion.service_catalog import (
    ACCOUNT_WIDE_SERVICES,
    COLLECTABLE_SERVICES,
    CONFIGURATION_SERVICES,
    REGIONAL_OPERATIONS,
    SERVICE_CATALOG,
    client_name,
)
from archdiscov.models.enums import AwsRegion, AwsService


def test_catalog_scope_and_operations_agree_with_security_allowlist():
    account = AwsAccountConfig(profile="dev", expected_account_id="123456789012", all_services=True)
    assert set(account.services) == COLLECTABLE_SERVICES
    assert {AwsService.IAM} == ACCOUNT_WIDE_SERVICES
    assert set(REGIONAL_OPERATIONS) == COLLECTABLE_SERVICES - ACCOUNT_WIDE_SERVICES
    allowed_collection_reads = {
        (service, operation)
        for service, operation in ALLOWED_OPERATIONS
        if service in COLLECTABLE_SERVICES and operation != Ec2Operation.DESCRIBE_REGIONS
    }
    assert {
        (service, operation)
        for service, definition in SERVICE_CATALOG.items()
        for operation in definition.operations
    } == allowed_collection_reads


@pytest.mark.parametrize("service", CONFIGURATION_SERVICES)
def test_catalog_dispatch_preserves_collector_arguments(monkeypatch, service):
    module_name, name = SERVICE_CATALOG[service].configuration_collector
    module = import_module(f"archdiscov.ingestion.{module_name}")
    arguments = (
        object(),
        AwsAccountConfig(profile="dev", expected_account_id="123456789012", regions=("us-east-1",)),
        AdapterContext(),
        uuid4(),
        "123456789012",
        AwsRegion.US_EAST_1,
    )
    observed = []
    sentinel = object()

    def collect(*args):
        observed.append(args)
        return sentinel

    monkeypatch.setattr(module, name, collect)
    assert collect_configuration_service(service, *arguments) is sentinel
    assert observed == [arguments]


def test_catalog_endpoint_names_and_unsupported_dispatch():
    assert client_name(AwsService.ELASTIC_LOAD_BALANCING) == "elbv2"
    assert client_name(AwsService.STS) == "sts"
    with pytest.raises(IngestionError, match="no configuration collector"):
        collect_configuration_service(
            AwsService.S3, None, None, None, uuid4(), "123456789012", AwsRegion.US_EAST_1
        )
