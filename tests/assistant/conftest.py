"""Fixtures shared by the suites that run over every provider harness."""

import pytest

from tests.assistant.provider_harness import HARNESSES, ProviderHarness


@pytest.fixture(params=HARNESSES, ids=lambda harness: harness.name)
def harness(request) -> ProviderHarness:
    return request.param
