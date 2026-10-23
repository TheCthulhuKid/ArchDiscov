"""Output vocabulary shared by every tool: limits, truncation, errors, and marked data.

Tool outputs are explicit models that forbid extra fields, so only declared fields reach
the model: sanitized values, stable IDs, citations, and limitations. Text imported from
cloud configuration or templates (names, tags, descriptions) is wrapped in
`UntrustedText`, whose `untrusted_text` field marks it as data to quote, never as
instructions. IDs use the field names in `ID_FIELDS` so the run record can collect them.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Field, JsonValue, NonNegativeInt, model_validator

from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.sanitization import sanitize_value
from archdiscov.models.validation_errors import validation_error

from ..conversation import MAX_TOOL_RESULT_CHARS, Contract, ToolResult

if TYPE_CHECKING:
    from .views import FindingView

MAX_UNTRUSTED_TEXT_CHARS = 200


class IdKind(StrEnum):
    FINDING = "finding"
    EVIDENCE = "evidence"
    RESOURCE = "resource"


# Output fields whose values (one ID or a sequence of IDs) the run record collects,
# scoped to the nearest enclosing `snapshot_id` field.
ID_FIELDS: Mapping[str, IdKind] = {
    "finding_id": IdKind.FINDING,
    "finding_ids": IdKind.FINDING,
    "evidence_id": IdKind.EVIDENCE,
    "evidence_ids": IdKind.EVIDENCE,
    "resource_id": IdKind.RESOURCE,
    "resource_ids": IdKind.RESOURCE,
}


class ToolLimits(Contract):
    """Hard bounds on what one tool call returns; tools may tighten, never exceed them."""

    result_limit: int = Field(default=25, ge=1, le=100, strict=True)
    depth: int = Field(default=3, ge=1, le=5, strict=True)
    max_output_chars: int = Field(default=32_768, ge=1_024, le=MAX_TOOL_RESULT_CHARS, strict=True)


class TruncationReason(StrEnum):
    RESULT_LIMIT = "result_limit"
    DEPTH_LIMIT = "depth_limit"
    WORK_LIMIT = "work_limit"
    SIZE_LIMIT = "size_limit"


class Truncation(Contract):
    """Why a list stopped short, how many items were returned, and how many exist.

    `available` is None when the bound stopped exploration before the total was known.
    """

    reasons: tuple[TruncationReason, ...] = Field(min_length=1)
    returned: NonNegativeInt
    available: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        order = list(TruncationReason)
        if list(self.reasons) != sorted(set(self.reasons), key=order.index):
            raise validation_error("tool_truncation_reasons_order", "unique, declaration order")
        if self.available is not None and self.available <= self.returned:
            raise validation_error("tool_truncation_counts", "available must exceed returned")
        return self


class BoundedList[T](Contract):
    """A list that reports truncation explicitly instead of silently dropping items."""

    items: tuple[T, ...] = ()
    truncation: Truncation | None = None

    @model_validator(mode="after")
    def match_returned_count(self) -> Self:
        if self.truncation is not None and self.truncation.returned != len(self.items):
            raise validation_error("tool_truncation_counts", "returned must count items")
        return self


def bounded[T](items: Sequence[T], limit: int) -> BoundedList[T]:
    """Keep the first `limit` items of an ordered sequence, reporting what was dropped."""
    if limit < 1:
        raise ValueError("limit must be positive")
    kept = tuple(items[:limit])
    if len(items) <= limit:
        return BoundedList(items=kept)
    return BoundedList(
        items=kept,
        truncation=Truncation(
            reasons=(TruncationReason.RESULT_LIMIT,), returned=len(kept), available=len(items)
        ),
    )


def as_data(text: str) -> str:
    # Line breaks, control, and format characters (zero-width and bidirectional
    # overrides) can disguise text as structure; each becomes one space.
    return "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char for char in text
    )


class UntrustedText(Contract):
    """Imported text to quote as data, never to follow; normalized and truncated on input."""

    untrusted_text: str = Field(max_length=MAX_UNTRUSTED_TEXT_CHARS)
    truncated: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or not isinstance(data.get("untrusted_text"), str):
            return data
        text = data["untrusted_text"]
        cut = len(text) > MAX_UNTRUSTED_TEXT_CHARS
        return {
            **data,
            "untrusted_text": as_data(text[:MAX_UNTRUSTED_TEXT_CHARS]),
            "truncated": cut or data.get("truncated", False),
        }


def untrusted(text: str) -> UntrustedText:
    return UntrustedText(untrusted_text=text)


def sanitized(value: JsonValue) -> JsonValue:
    """A detached copy with sensitive keys redacted, as stored snapshots already are."""
    return sanitize_value(value)[0]


class ToolErrorCode(StrEnum):
    MALFORMED_ARGUMENTS = "tool_malformed_arguments"
    INVALID_ARGUMENTS = "tool_invalid_arguments"
    UNKNOWN_TOOL = "tool_unknown"
    NOT_FOUND = "tool_not_found"
    UNAVAILABLE = "tool_unavailable"
    OUTPUT_TOO_LARGE = "tool_output_too_large"
    INTERNAL_ERROR = "tool_internal_error"
    # Only an MCP server session has a call cap; `ask` runs bound calls with run limits.
    SESSION_LIMIT_REACHED = "tool_session_limit_reached"


TOOL_ERROR_MESSAGES: Mapping[ToolErrorCode, str] = {
    ToolErrorCode.MALFORMED_ARGUMENTS: (
        "Tool arguments were not a JSON object within the size limit."
    ),
    ToolErrorCode.INVALID_ARGUMENTS: "Tool arguments did not match the tool's input schema.",
    ToolErrorCode.UNKNOWN_TOOL: "No tool with that name is available.",
    ToolErrorCode.NOT_FOUND: "No recorded item has that ID in the selected snapshot.",
    ToolErrorCode.UNAVAILABLE: "This tool needs data that was not selected for this session.",
    ToolErrorCode.OUTPUT_TOO_LARGE: (
        "Tool output exceeded the size limit; narrow the request or lower its limit."
    ),
    ToolErrorCode.INTERNAL_ERROR: "The tool failed unexpectedly.",
    ToolErrorCode.SESSION_LIMIT_REACHED: (
        "This session has made its maximum number of tool calls; start a new session."
    ),
}

ArgumentLocation = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.]{1,200}$")]


class ToolError(Contract):
    """A failure the model can read; the message is fixed by the code.

    `locations` names input fields that failed validation, using only declared field
    names and list positions.
    """

    status: Literal["error"] = "error"
    code: ToolErrorCode
    message: NonEmptyString | None = None
    locations: tuple[ArgumentLocation, ...] = Field(default=(), max_length=20)

    @model_validator(mode="before")
    @classmethod
    def fill_message(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and data.get("message") is None and "code" in data:
            try:
                return {**data, "message": TOOL_ERROR_MESSAGES[ToolErrorCode(data["code"])]}
            except ValueError:
                return data
        return data

    @model_validator(mode="after")
    def require_fixed_message(self) -> Self:
        if self.message != TOOL_ERROR_MESSAGES[self.code]:
            raise validation_error("tool_error_message_mismatch", "message is fixed by code")
        return self


def tool_error(code: ToolErrorCode, locations: Sequence[str] = ()) -> ToolError:
    return ToolError(code=code, locations=tuple(locations))


@dataclass(frozen=True)
class DeliveredAnalysis:
    """The analysis result behind an output, and the views of the findings it carries."""

    result_key: str
    result: AnalysisResult
    findings: tuple["FindingView", ...]

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(view.finding_id for view in self.findings)


class ToolOutput(Contract):
    """Base for successful outputs; subclasses declare only allowlisted fields."""

    status: Literal["ok"] = "ok"

    def delivered_analysis(self) -> DeliveredAnalysis | None:
        """The analysis behind this output, for the run record; most outputs have none."""
        return None


def render(
    call_id: str, output: ToolOutput | ToolError, limits: ToolLimits
) -> tuple[ToolResult, ToolOutput | ToolError]:
    """The result sent to the model, and the output it actually carries.

    An output over `max_output_chars` is replaced by `tool_output_too_large`; only the
    delivered output may be recorded.
    """
    content = output.model_dump_json()
    if len(content) > limits.max_output_chars:
        output = tool_error(ToolErrorCode.OUTPUT_TOO_LARGE)
        content = output.model_dump_json()
    return ToolResult(
        call_id=call_id, content=content, is_error=isinstance(output, ToolError)
    ), output
