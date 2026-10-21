"""Provider harnesses for the shared contract suite.

A harness builds a provider that behaves as a list of fake-transcript turns describes,
and reports how many attempts reached it. The turns are the provider-neutral scenario
format: the fake replays them directly, and each real adapter (steps 40-42) adds a
harness that turns them into mocked transport responses for its mapping layer.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.provider import Provider, ProviderCapabilities


class ProviderHarness(Protocol):
    name: str

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
    ) -> Provider: ...

    def attempts(self, provider: Provider) -> int: ...


class FakeHarness:
    name = "fake"

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
    ) -> Provider:
        data: dict[str, Any] = {"turns": list(turns)}
        if capabilities is not None:
            data["capabilities"] = capabilities.model_dump(mode="json")
        return FakeProvider(
            FakeTranscript.model_validate(data), clock=FakeClock(), max_retries=max_retries
        )

    def attempts(self, provider: Provider) -> int:
        assert isinstance(provider, FakeProvider)
        return len(provider.requests)


HARNESSES: list[ProviderHarness] = [FakeHarness()]
