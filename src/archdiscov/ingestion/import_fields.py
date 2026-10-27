"""Typed-field helpers shared by the Terraform and CloudFormation field mappers.

A mapper holds the declared `values`, the model `data` it builds, the `facts` that cite each
field to its source, and its `limitations`. Subclasses say how a source is addressed, which
strings are usable, and how an omitted value is reported.
"""

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import IPvAnyNetwork, TypeAdapter, ValidationError

_NETWORK = TypeAdapter(IPvAnyNetwork)


class TypedFields:
    values: dict
    data: dict
    facts: dict[str, str]

    def source_pointer(self, source: str) -> str:
        raise NotImplementedError

    def usable(self, value: Any) -> bool:
        raise NotImplementedError

    def omit_value(self, source: str) -> None:
        raise NotImplementedError

    def choice(self, source: str, target: str, spellings: Mapping[str, Enum]) -> None:
        """An AWS spelling as its enum value; any other value is omitted, never guessed."""
        value = self.values.get(source)
        if value is None:
            return
        if not isinstance(value, str) or value not in spellings:
            self.omit_value(source)
            return
        self.data[target] = spellings[value]
        self.facts[f"/{target}"] = self.source_pointer(source)

    def string_list(self, source: str, target: str) -> None:
        """Usable identifiers, each cited to its position; others are omitted."""
        values = self.values.get(source)
        if values is None:
            return
        if not isinstance(values, list):
            self.omit_value(source)
            return
        kept = []
        for index, value in enumerate(values):
            if self.usable(value):
                self.facts[f"/{target}/{len(kept)}"] = f"{self.source_pointer(source)}/{index}"
                kept.append(value)
            else:
                self.omit_value(source)
        self.data[target] = kept
        if not values:
            self.facts[f"/{target}"] = self.source_pointer(source)

    def cidr_entries(self, source: str, target: str, key: str) -> None:
        """Every entry's CIDR, or none: a partial list would read as the whole one."""
        entries = self.values.get(source)
        if entries is None:
            return
        cidrs = []
        for entry in entries if isinstance(entries, list) else [None]:
            cidr = entry.get(key) if isinstance(entry, dict) else None
            try:
                if not self.usable(cidr):
                    raise ValueError(source)
                cidrs.append({"cidr": _NETWORK.validate_python(cidr)})
            except (ValueError, ValidationError):
                self.omit_value(source)
                return
        self.data[target] = cidrs
        for index in range(len(cidrs)):
            self.facts[f"/{target}/{index}/cidr"] = f"{self.source_pointer(source)}/{index}/{key}"
        if not cidrs:
            self.facts[f"/{target}"] = self.source_pointer(source)
