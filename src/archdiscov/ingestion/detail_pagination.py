"""Request and completion rules for supported detail pagination modes."""

from dataclasses import dataclass
from typing import Any, Literal

from .execution_common import MalformedResponse

PaginationMode = Literal["iam", "elbv2", "nextToken", "lambda", "rds", "sqs", "ec2"]


@dataclass(frozen=True)
class PaginationPolicy:
    request_token: str
    response_token: str
    more_results: str | None = None

    def next_marker(self, page: dict[str, Any]) -> Any:
        if self.more_results is not None and not page.get(self.more_results, False):
            return None
        marker = page.get(self.response_token)
        if self.more_results is not None and not marker:
            raise MalformedResponse("truncated pagination response omitted its marker")
        if marker is not None and not isinstance(marker, str):
            raise MalformedResponse("pagination marker must be a string")
        return marker or None


PAGINATION_POLICIES = {
    "iam": PaginationPolicy("Marker", "Marker", "IsTruncated"),
    "elbv2": PaginationPolicy("Marker", "NextMarker"),
    "nextToken": PaginationPolicy("nextToken", "nextToken"),
    "lambda": PaginationPolicy("Marker", "NextMarker"),
    "rds": PaginationPolicy("Marker", "Marker"),
    "sqs": PaginationPolicy("NextToken", "NextToken"),
    "ec2": PaginationPolicy("NextToken", "NextToken"),
}
PAGINATION_TOKENS = frozenset(policy.request_token for policy in PAGINATION_POLICIES.values())
