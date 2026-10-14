import re

from .cloud_identity import AzureResourceIdentity, ResourceIdentity
from .cloud_namespace import AZURE_NAMESPACE_PATTERN
from .cloud_scope import AzureScope, SupportedScope
from .enums import AwsPartition, AwsRegion, AwsService, CloudPlatform


def canonical_service_key(platform: CloudPlatform, service: str) -> str:
    return service.casefold() if platform == CloudPlatform.AZURE else service


def evidence_context_matches(
    identity: ResourceIdentity | AzureResourceIdentity,
    owning_scope: SupportedScope,
    evidence_scope: SupportedScope,
    location: str | None,
    service: str,
) -> bool:
    return (
        evidence_scope == owning_scope
        and location == identity_location(identity)
        and canonical_service_key(evidence_scope.platform, service)
        == canonical_service_key(identity.platform, identity.service)
    )


def identity_location(identity: ResourceIdentity | AzureResourceIdentity) -> str | None:
    if isinstance(identity, ResourceIdentity):
        return None if identity.region == AwsRegion.GLOBAL else identity.region.value
    return identity.location


def validate_context(scope: SupportedScope, location: str | None, service: str | None) -> None:
    if scope.platform == CloudPlatform.AWS:
        if location is not None:
            region = AwsRegion(location)
            if region == AwsRegion.GLOBAL:
                raise ValueError("scope-wide placement uses null location, not global")
            partition = (
                AwsPartition.AWS_CN
                if location.startswith("cn-")
                else AwsPartition.AWS_US_GOV
                if location.startswith("us-gov-")
                else AwsPartition.AWS
            )
            if scope.partition is not None and partition != scope.partition:
                raise ValueError("AWS location must match scope partition")
        if service is not None:
            AwsService(service)
    else:
        if location is not None and re.fullmatch(r"[a-z0-9][a-z0-9-]*", location) is None:
            raise ValueError("Azure location must be a canonical lowercase location")
        if service is not None and re.fullmatch(AZURE_NAMESPACE_PATTERN, service) is None:
            raise ValueError("Azure service must be a native provider namespace")


def validate_identity_scope(identity, scope: SupportedScope) -> None:
    if identity.platform != scope.platform:
        raise ValueError("resource platform must match snapshot scope")
    if isinstance(identity, ResourceIdentity):
        if identity.account_id != scope.account_id:
            raise ValueError("resource account must match snapshot scope")
        if identity.physical_id and identity.physical_id.startswith("arn:"):
            if identity.physical_id.split(":", 2)[1] != scope.partition:
                raise ValueError("resource ARN partition must match snapshot scope")
    elif isinstance(scope, AzureScope):
        if identity.scope != scope:
            raise ValueError("resource subscription/tenant must match snapshot scope")
    validate_context(scope, identity_location(identity), identity.service)
