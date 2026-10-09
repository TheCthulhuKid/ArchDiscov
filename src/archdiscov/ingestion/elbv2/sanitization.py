"""Redact protected ELBv2 values before constructing any source evidence."""

from typing import Any

from ...models.sanitization import sanitize_value
from ..evidence_builder import REDACTED, json_safe, redact_tag_pairs


def sanitize_source(value: Any) -> tuple[Any, list[str]]:
    safe, paths = redact_tag_pairs(json_safe(value))
    safe, generic_paths = sanitize_value(safe)
    paths.extend(generic_paths)

    def walk(item: Any, path: str = "", protected: bool = False) -> Any:
        if isinstance(item, list):
            return [walk(child, f"{path}/{index}", protected) for index, child in enumerate(item)]
        if not isinstance(item, dict):
            return item
        out = {}
        condition = item.get("Field") in ("http-header", "query-string")
        for key, child in item.items():
            pointer = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
            if key in ("ClientSecret", "MessageBody") or (
                protected and key in ("Values", "Value", "RegexValues")
            ):
                out[key] = REDACTED
                paths.append(pointer)
            elif key == "AuthenticationRequestExtraParams" and isinstance(child, dict):
                out[key] = {name: REDACTED for name in child}
                paths.extend(
                    f"{pointer}/{str(name).replace('~', '~0').replace('/', '~1')}" for name in child
                )
            elif key == "Certificates":
                # TLS certificate configuration is outside this collection boundary.
                out[key] = REDACTED
                paths.append(pointer)
            else:
                out[key] = walk(
                    child,
                    pointer,
                    protected or condition or key in ("HttpHeaderConfig", "QueryStringConfig"),
                )
        return out

    safe = walk(safe)
    return safe, sorted(set(paths))
