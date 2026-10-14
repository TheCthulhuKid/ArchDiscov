from enum import StrEnum


class EvaluationIssueCode(StrEnum):
    REDACTED_INPUT = "redacted_input"
    INVALID_INTRINSIC = "invalid_intrinsic"
    RESOURCE_VALUE_UNAVAILABLE = "resource_value_unavailable"
    REFERENCE_UNAVAILABLE = "reference_unavailable"
    CONDITION_UNAVAILABLE = "condition_unavailable"
    CONDITION_CYCLE = "condition_cycle"
    INVALID_CONDITION = "invalid_condition"
    IMPORT_UNAVAILABLE = "import_unavailable"
    UNSUPPORTED_INTRINSIC = "unsupported_intrinsic"
    MAPPING_UNAVAILABLE = "mapping_unavailable"
    EVALUATION_LIMIT_OR_INVALID_SHAPE = "evaluation_limit_or_invalid_shape"
