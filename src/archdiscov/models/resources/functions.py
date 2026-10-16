"""Lambda configuration without code, environment values, or free-text diagnostics."""

import re
from typing import Any, Literal, Self

from pydantic import JsonValue, field_validator, model_validator

from ..enums import AwsService, ResourceType
from ..resource_base import NonEmptyString, Resource
from ..sanitization import sanitize_value

_FUNCTION_ARN = (
    r"arn:aws[a-z-]*:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_$-]+)?"
)
_ROLE_ARN = r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+"
_RUNTIMES = {
    "nodejs",
    "nodejs4.3",
    "nodejs4.3-edge",
    "nodejs6.10",
    "nodejs8.10",
    "nodejs10.x",
    "nodejs12.x",
    "nodejs14.x",
    "nodejs16.x",
    "nodejs18.x",
    "nodejs20.x",
    "nodejs22.x",
    "nodejs24.x",
    "python2.7",
    "python3.6",
    "python3.7",
    "python3.8",
    "python3.9",
    "python3.10",
    "python3.11",
    "python3.12",
    "python3.13",
    "python3.14",
    "java8",
    "java8.al2",
    "java11",
    "java17",
    "java21",
    "java25",
    "dotnetcore1.0",
    "dotnetcore2.0",
    "dotnetcore2.1",
    "dotnetcore3.1",
    "dotnet6",
    "dotnet8",
    "dotnet9",
    "dotnet10",
    "go1.x",
    "ruby2.5",
    "ruby2.7",
    "ruby3.2",
    "ruby3.3",
    "ruby3.4",
    "provided",
    "provided.al2",
    "provided.al2023",
}
SOURCE_FIELDS = {
    "arn": "FunctionArn",
    "name": "FunctionName",
    "role_arn": "Role",
    "runtime": "Runtime",
    "package_type": "PackageType",
    "state": "State",
    "last_update_status": "LastUpdateStatus",
    "memory_size": "MemorySize",
    "timeout": "Timeout",
    "architectures": "Architectures",
    "vpc_id": "VpcId",
    "subnet_ids": "SubnetIds",
    "security_group_ids": "SecurityGroupIds",
}
_EXCLUDED = {
    "Environment",
    "Description",
    "Code",
    "CodeLocation",
    "Location",
    "SigningJobArn",
    "SigningProfileVersionArn",
    "StateReason",
    "LastUpdateStatusReason",
    "RuntimeVersionConfig",
    "ImageConfigResponse",
    "ResponseMetadata",
    "AuthToken",
    "Token",
    "Secret",
    "Password",
}


def valid_source_field(key: str, value: Any) -> bool:
    if key in {"MemorySize", "Timeout"}:
        low, high = (128, 10240) if key == "MemorySize" else (1, 900)
        return type(value) is int and low <= value <= high
    if key in {"Architectures", "SubnetIds", "SecurityGroupIds"}:
        if not isinstance(value, list):
            return False
        if key == "Architectures":
            return len(value) == 1 and value[0] in ("x86_64", "arm64")
        prefix = "subnet" if key == "SubnetIds" else "sg"
        return all(isinstance(v, str) and re.fullmatch(rf"{prefix}-[a-zA-Z0-9]+", v) for v in value)
    if not isinstance(value, str):
        return False
    patterns = {
        "FunctionArn": _FUNCTION_ARN,
        "Role": _ROLE_ARN,
        "FunctionName": r"[A-Za-z0-9_-]{1,64}",
        "VpcId": r"(?:vpc-[a-zA-Z0-9]+)?",
    }
    if key in patterns:
        return re.fullmatch(patterns[key], value) is not None
    choices = {
        "Runtime": _RUNTIMES,
        "PackageType": {"Zip", "Image"},
        "State": {"Pending", "Active", "Inactive", "Failed"},
        "LastUpdateStatus": {"Successful", "Failed", "InProgress"},
    }
    return value in choices.get(key, set())


def safe_configuration(value: Any) -> tuple[dict[str, JsonValue], list[str]]:
    """Allowlist before evidence creation; never traverse environment values."""
    if not isinstance(value, dict):
        return {}, []
    safe = {}
    paths = [f"/{key}" for key in sorted(_EXCLUDED & value.keys())]
    for key in SOURCE_FIELDS.values():
        if key in value and valid_source_field(key, value[key]):
            safe[key] = value[key]
    if isinstance(value.get("VpcConfig"), dict):
        safe["VpcConfig"] = {
            key: child
            for key, child in value["VpcConfig"].items()
            if key in {"VpcId", "SubnetIds", "SecurityGroupIds"} and valid_source_field(key, child)
        }
    return safe, paths


class LambdaFunction(Resource):
    resource_type: Literal[ResourceType.LAMBDA_FUNCTION] = ResourceType.LAMBDA_FUNCTION
    arn: NonEmptyString
    role_arn: str | None = None
    vpc_id: str | None = None
    subnet_ids: list[str] | None = None
    security_group_ids: list[str] | None = None
    runtime: str | None = None
    package_type: str | None = None
    state: str | None = None
    last_update_status: str | None = None
    memory_size: int | None = None
    timeout: int | None = None
    architectures: list[str] | None = None
    configuration: dict[str, JsonValue] | None = None

    @field_validator(*SOURCE_FIELDS, mode="before")
    @classmethod
    def validate_configuration_fact(cls, value, info):
        if value is not None and not valid_source_field(SOURCE_FIELDS[info.field_name], value):
            raise ValueError("Malformed or unsupported Lambda configuration fact")
        return value

    @field_validator("configuration", mode="before")
    @classmethod
    def project_configuration(cls, value):
        return None if value is None else safe_configuration(value)[0]

    @field_validator("attributes", mode="before")
    @classmethod
    def protect_attributes(cls, value):
        def project(item):
            if isinstance(item, dict):
                return {k: project(v) for k, v in item.items() if k not in _EXCLUDED}
            if isinstance(item, list):
                return [project(v) for v in item]
            return item

        return sanitize_value(project(value))[0]

    @model_validator(mode="after")
    def validate_arn_identity(self) -> Self:
        if self.identity.service != AwsService.LAMBDA or self.identity.physical_id != self.arn:
            raise ValueError("Lambda identity must use its function ARN and lambda service")
        return self
