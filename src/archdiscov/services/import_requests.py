"""Format-option validation and typed requests for offline imports."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..ingestion.adapter import IngestionError
from ..ingestion.cloudformation import CloudFormationRequest
from ..ingestion.cloudformation.parameters import load_parameters
from ..ingestion.requests import SourceRequest
from ..ingestion.terraform import TerraformPlanRequest, TerraformStateRequest
from ..models.enums import AwsRegion


class IngestFormat(StrEnum):
    SNAPSHOT = "snapshot"
    TERRAFORM_STATE = "terraform-state"
    TERRAFORM_PLAN = "terraform-plan"
    CLOUDFORMATION = "cloudformation"


_TERRAFORM_REQUESTS = {
    IngestFormat.TERRAFORM_STATE: TerraformStateRequest,
    IngestFormat.TERRAFORM_PLAN: TerraformPlanRequest,
}


@dataclass(frozen=True)
class ImportOptions:
    account_id: str | None = None
    region: AwsRegion | str | None = None
    workspace: str | None = None
    stack_name: str | None = None
    stack_id: str | None = None
    parameters_file: Path | None = None
    sensitive_paths: tuple[str, ...] = ()

    def validate_format(self, source_format: IngestFormat) -> None:
        cloudformation_options = (
            self.stack_name is not None
            or self.stack_id is not None
            or self.parameters_file is not None
            or bool(self.sensitive_paths)
        )
        if source_format == IngestFormat.SNAPSHOT:
            if (
                any(value is not None for value in (self.account_id, self.region, self.workspace))
                or cloudformation_options
            ):
                raise IngestionError(
                    "invalid_request", "Snapshot imports do not accept source context."
                )
        elif source_format in _TERRAFORM_REQUESTS:
            if cloudformation_options:
                raise IngestionError(
                    "invalid_request", "CloudFormation options require that format."
                )
        else:
            if self.workspace is not None:
                raise IngestionError(
                    "invalid_request", "Workspace is only valid for Terraform imports."
                )
            if self.account_id is None or self.region is None:
                raise IngestionError(
                    "invalid_request", "CloudFormation requires account-id and region."
                )


def _terraform_request(
    source_format: IngestFormat, path: Path, options: ImportOptions
) -> SourceRequest:
    return _TERRAFORM_REQUESTS[source_format](
        path=path,
        account_id=options.account_id,
        region=options.region,
        workspace=options.workspace,
    )


def _cloudformation_request(path: Path, options: ImportOptions) -> CloudFormationRequest:
    return CloudFormationRequest(
        path=path,
        account_id=options.account_id,
        region=options.region,
        stack_name=options.stack_name,
        stack_id=options.stack_id,
        parameters=load_parameters(options.parameters_file)
        if options.parameters_file is not None
        else {},
        sensitive_paths=options.sensitive_paths,
    )


def source_request(
    source_format: IngestFormat, path: Path, options: ImportOptions
) -> SourceRequest | None:
    options.validate_format(source_format)
    if source_format == IngestFormat.SNAPSHOT:
        return None
    if source_format in _TERRAFORM_REQUESTS:
        return _terraform_request(source_format, path, options)
    return _cloudformation_request(path, options)
