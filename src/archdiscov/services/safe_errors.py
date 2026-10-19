from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar


class SafeServiceError(Exception):
    """Expected service failure exposed only as a stable code and fixed message.

    Subclasses bind a code enum to its messages. Messages contain no request or
    parameter values and no validation exception text.
    """

    code_type: ClassVar[type[StrEnum]]
    messages: ClassVar[Mapping[Any, str]]

    def __init__(self, code: Any) -> None:
        self.code = self.code_type(code)
        self.message = self.messages[self.code]
        super().__init__(self.message)
