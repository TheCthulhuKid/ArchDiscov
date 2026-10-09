import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from botocore import xform_name

from ..models.architecture import CollectionFailure, Coverage
from ..models.enums import AwsRegion, AwsService, CoverageStatus
from .adapter import AdapterContext, IngestionError
from .execution_common import client_config, require_allowed, sanitize_failure
from .requests import AwsAccountConfig


@dataclass(frozen=True)
class DetailCall:
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    result_key: str | None = None
    paginate: bool = False


@dataclass(frozen=True)
class DetailResponse:
    call: DetailCall
    value: Any
    path: str


@dataclass(frozen=True)
class DetailResult:
    responses: list[DetailResponse]
    coverage: Coverage


@dataclass(frozen=True)
class _CallResult:
    responses: list[DetailResponse]
    pages: int
    failure: CollectionFailure | None = None


class DetailRunner:
    """Run bounded detail reads with request-specific evidence locations."""

    def __init__(
        self,
        session: Any,
        account: AwsAccountConfig,
        context: AdapterContext,
        *,
        service: AwsService,
        region: AwsRegion,
        client_name: str,
        pagination: Literal["iam", "elbv2", "nextToken", "lambda", "rds", "sqs"] = "iam",
    ) -> None:
        if pagination not in {"iam", "elbv2", "nextToken", "lambda", "rds", "sqs"}:
            raise ValueError("unsupported detail pagination")
        self._session = session
        self._account = account
        self._context = context
        self._service = service
        self._region = region
        self._client_name = client_name
        self._pagination = pagination
        self._client_obj: Any = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def _client(self) -> Any:
        with self._lock:
            if self._client_obj is None:
                kwargs: dict[str, Any] = {"config": client_config(self._account)}
                if self._region != AwsRegion.GLOBAL:
                    kwargs["region_name"] = self._region.value
                self._client_obj = self._session.client(self._client_name, **kwargs)
            return self._client_obj

    def _claim(self, operation: str) -> None:
        with self._lock:
            if operation in self._seen:
                raise IngestionError(
                    "invalid_request", "duplicate operation for account/region/service"
                )
            self._seen.add(operation)

    def _execute(self, client: Any, call: DetailCall, canonical: str) -> _CallResult:
        responses: list[DetailResponse] = []
        pages = 0
        try:
            params = dict(call.params)
            token_key = {"nextToken": "nextToken", "sqs": "NextToken"}.get(
                self._pagination, "Marker"
            )
            markers = {params[token_key]} if token_key in params else set()
            method = getattr(client, xform_name(call.operation))
            while True:
                with self._context.sdk_calls.permit(
                    self._account.expected_account_id, self._account.max_in_flight_calls
                ):
                    page = method(**params)
                path = f"Request[{canonical}].Page[{pages}]"
                pages += 1
                if call.result_key is None:
                    responses.append(DetailResponse(call, page, path))
                else:
                    if call.result_key not in page:
                        raise ValueError("response omitted the requested result field")
                    value = page[call.result_key]
                    path += f".{call.result_key}"
                    if isinstance(value, list):
                        responses.extend(
                            DetailResponse(call, item, f"{path}[{index}]")
                            for index, item in enumerate(value)
                        )
                    else:
                        responses.append(DetailResponse(call, value, path))
                if not call.paginate:
                    break
                if self._pagination == "iam":
                    if not page.get("IsTruncated", False):
                        break
                    marker = page.get("Marker")
                elif self._pagination in {"elbv2", "lambda"}:
                    marker = page.get("NextMarker")
                    if not marker:
                        break
                else:
                    marker = page.get(token_key)
                    if not marker:
                        break
                if not marker or marker in markers:
                    raise ValueError(f"{self._client_name.upper()} pagination did not advance")
                markers.add(marker)
                params[token_key] = marker
        except Exception as error:
            return _CallResult(responses, pages, sanitize_failure(error, call.operation))
        return _CallResult(responses, pages)

    def run(
        self,
        operation: str,
        calls: list[DetailCall],
        *,
        prerequisite: str | None = None,
    ) -> DetailResult:
        # Validate the whole batch before creating or accessing a client.
        require_allowed(self._service, operation)
        for call in calls:
            require_allowed(self._service, call.operation)
        ordered: list[tuple[str, DetailCall]] = []
        seen: set[str] = set()
        label = self._client_name.upper()
        for call in calls:
            if call.operation != operation:
                raise IngestionError(
                    "invalid_request", f"{label} call operation does not match batch"
                )
            try:
                if not isinstance(call.params, dict):
                    raise ValueError(f"{label} request parameters must be a dictionary")
                json.dumps(call.params, sort_keys=True, separators=(",", ":"), allow_nan=False)
                canonical = json.dumps(
                    {
                        key: value
                        for key, value in call.params.items()
                        if key not in {"Marker", "nextToken", "NextToken"}
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as error:
                raise IngestionError(
                    "invalid_request", f"invalid {label} request parameters"
                ) from error
            if canonical in seen:
                raise IngestionError("invalid_request", f"duplicate {label} request parameters")
            seen.add(canonical)
            ordered.append((canonical, call))
        ordered.sort(key=lambda item: item[0])
        self._claim(operation)
        started = self._context.clock()
        responses: list[DetailResponse] = []
        failures: list[CollectionFailure] = []
        pages = 0
        if ordered:
            try:
                client = self._client()
            except Exception as error:
                failures.append(sanitize_failure(error, operation))
            else:
                with ThreadPoolExecutor(max_workers=self._account.max_concurrency) as executor:
                    results = executor.map(
                        lambda item: self._execute(client, item[1], item[0]), ordered
                    )
                    for result in results:
                        responses.extend(result.responses)
                        pages += result.pages
                        if result.failure is not None:
                            failures.append(result.failure)
        fields: dict[str, Any] = {}
        if not ordered and prerequisite is not None:
            fields.update(status=CoverageStatus.SKIPPED, reason=prerequisite)
        elif failures and pages == 0:
            fields.update(status=CoverageStatus.FAILED, failures=failures, reason=prerequisite)
        elif failures or prerequisite is not None:
            fields.update(
                status=CoverageStatus.INCOMPLETE,
                result_count=len(responses),
                failures=failures,
                reason=prerequisite,
            )
        else:
            fields.update(status=CoverageStatus.SUCCESS, result_count=len(responses))
        return DetailResult(
            responses,
            Coverage(
                account_id=self._account.expected_account_id,
                region=self._region,
                service=self._service,
                operation=operation,
                started_at=started,
                completed_at=self._context.clock(),
                **fields,
            ),
        )
