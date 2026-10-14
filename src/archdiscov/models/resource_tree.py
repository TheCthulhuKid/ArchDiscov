"""Shared pre-order traversal of resource containment trees."""

from collections.abc import Iterable

from .resource_base import Resource


def walk_resources(roots: Iterable[Resource]) -> Iterable[Resource]:
    for resource in roots:
        yield resource
        yield from walk_resources(resource.children)
