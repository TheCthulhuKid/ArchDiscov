"""The Bedrock Converse adapter: the only assistant module that imports boto3 or botocore.

The adapter has its own AWS session, separate from collection's: its configured `profile`
and `region`, and, with `role_arn`, a role assumed with its `session_name`. An explicit
profile already makes botocore skip environment credentials, and the client is built so
the rest of the environment cannot change its target or retries either:
`ignore_configured_endpoint_urls` ignores `AWS_ENDPOINT_URL*` and a profile's
`endpoint_url`, an empty proxy setting ignores proxy variables, FIPS and dual-stack
endpoints are off, and an explicit retry setting overrides `AWS_MAX_ATTEMPTS`,
`AWS_RETRY_MODE`, and the profile's own. The `boto3` and `botocore` loggers stay at
`WARNING`, because their debug records hold request bodies.

Building the adapter makes no request: a role is assumed on the first request, through
refreshable credentials, so an STS failure surfaces as that request's failure. A profile
that cannot be loaded fails before the run. Bedrock models differ in tool support, so the
capabilities are the ones the configuration declares. Until the Converse mapping arrives
(step 42.2), `complete` raises `ProviderUnavailableError` without sending anything.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import (
    CredentialProvider,
    CredentialResolver,
    DeferredRefreshableCredentials,
)
from botocore.exceptions import BotoCoreError, CredentialRetrievalError

from archdiscov.ingestion.aws_sessions import (
    assume_role_parameters,
    profile_session,
    role_credentials,
)

from .adapters import quiet_loggers
from .config import BedrockProviderConfig, ProviderConfigError, ProviderConfigErrorCode
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderUnavailableError,
)

_SERVICE = "bedrock-runtime"
_SDK_LOGGERS = ("boto3", "botocore")
_CREDENTIAL_METHOD = "assume-role"


class _AssumedRole(CredentialProvider):
    """The only credential source of the role session: the assumed role's credentials."""

    METHOD = _CREDENTIAL_METHOD

    def __init__(self, credentials: DeferredRefreshableCredentials) -> None:
        super().__init__()
        self._credentials = credentials

    def load(self) -> DeferredRefreshableCredentials:
        return self._credentials


class BedrockProvider:
    """A `Provider` over the Bedrock Converse API, built from one configuration.

    `session_factory` makes the profile session (and, with a role, the role session) in
    place of `boto3.Session`, so tests can register botocore event hooks on them.
    """

    def __init__(
        self,
        config: BedrockProviderConfig,
        *,
        session_factory: Callable[..., Any] = boto3.Session,
    ) -> None:
        quiet_loggers(_SDK_LOGGERS)
        self.model = config.model
        self.region = config.region.value
        self.profile = config.profile
        self.endpoint = None if config.endpoint is None else str(config.endpoint)
        self._capabilities = ProviderCapabilities(capabilities=config.capabilities)
        client_config = Config(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.request_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            ignore_configured_endpoint_urls=True,
            proxies={},
            use_fips_endpoint=False,
            use_dualstack_endpoint=False,
        )
        try:
            session = profile_session(session_factory, config.profile, self.region)
            if config.role_arn is not None:
                session = _role_session(session, session_factory, config, client_config)
            self._client = session.client(
                _SERVICE,
                region_name=self.region,
                endpoint_url=self.endpoint,
                config=client_config,
            )
        except BotoCoreError as error:
            raise ProviderConfigError(
                ProviderConfigErrorCode.PROFILE_UNAVAILABLE,
                profile=config.profile,
                kind=config.kind,
            ) from error

    def __repr__(self) -> str:
        return (
            f"BedrockProvider(model={self.model!r}, region={self.region!r}, "
            f"profile={self.profile!r})"
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        raise ProviderUnavailableError()

    def _converse(self, **params: Any) -> Any:
        """Send one Converse request; step 42.2 builds `params`."""
        return self._client.converse(**params)


def _role_session(
    base: Any,
    session_factory: Callable[..., Any],
    config: BedrockProviderConfig,
    client_config: Config,
) -> Any:
    """A session whose only credentials are the role's, assumed on first use from `base`."""
    sts = base.client("sts", region_name=config.region.value, config=client_config)
    params = assume_role_parameters(config.role_arn, config.session_name)

    def assume() -> dict[str, str]:
        credentials = role_credentials(sts.assume_role(**params))
        expiry = None if credentials is None else credentials.get("Expiration")
        if not isinstance(expiry, datetime):
            raise CredentialRetrievalError(
                provider=_CREDENTIAL_METHOD, error_msg="invalid assume-role credentials"
            )
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": expiry.isoformat(),
        }

    role = botocore.session.Session(profile=config.profile)
    deferred = DeferredRefreshableCredentials(refresh_using=assume, method=_CREDENTIAL_METHOD)
    role.register_component("credential_provider", CredentialResolver([_AssumedRole(deferred)]))
    return session_factory(botocore_session=role, region_name=config.region.value)
