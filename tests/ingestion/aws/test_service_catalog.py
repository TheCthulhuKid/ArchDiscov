from archdiscov.ingestion.operations import ALLOWED_OPERATIONS, Ec2Operation
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.ingestion.service_catalog import (
    ACCOUNT_WIDE_SERVICES,
    COLLECTABLE_SERVICES,
    REGIONAL_OPERATIONS,
    SERVICE_CATALOG,
)
from archdiscov.models.enums import AwsService


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
