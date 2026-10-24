"""Model-layer redaction for ELBv2 configuration, independent of ingestion."""

from pydantic import JsonValue

from ..json_pointer import escape_pointer_token
from ..sanitization import sanitize_value


def sanitize_configuration(
    value: dict[str, JsonValue], *, condition_field: str | None = None
) -> tuple[dict[str, JsonValue], list[str]]:
    """Copy and redact configuration; return RFC 6901 paths relative to its root.

    Accept wrapped AWS configurations or a typed condition's unwrapped contents.
    Authentication extra parameter names remain visible, but their values do not.
    """
    safe, paths = sanitize_value(value)

    def visit(item: JsonValue, path: str, protected: bool = False) -> JsonValue:
        if isinstance(item, list):
            return [visit(child, f"{path}/{index}", protected) for index, child in enumerate(item)]
        if not isinstance(item, dict):
            return item
        protected = protected or item.get("Field") in ("http-header", "query-string")
        result = {}
        for key, child in item.items():
            pointer = f"{path}/{escape_pointer_token(key)}"
            if key in ("ClientSecret", "MessageBody") or (
                protected and key in ("Values", "Value", "RegexValues")
            ):
                result[key] = "[REDACTED]"
                paths.append(pointer)
            elif key == "AuthenticationRequestExtraParams" and isinstance(child, dict):
                result[key] = dict.fromkeys(child, "[REDACTED]")
                paths.extend(f"{pointer}/{escape_pointer_token(name)}" for name in child)
            else:
                result[key] = visit(
                    child,
                    pointer,
                    protected or key in ("HttpHeaderConfig", "QueryStringConfig"),
                )
        return result

    return visit(safe, "", condition_field in ("http-header", "query-string")), sorted(set(paths))
