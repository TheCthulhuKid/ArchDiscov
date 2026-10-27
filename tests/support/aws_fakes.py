from datetime import UTC, datetime, timedelta
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest

ACCOUNT = "123456789012"


class Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **params):
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            yield page


class Client:
    def __init__(self, account, pages):
        self.account = account
        self.pages = pages

    def get_caller_identity(self):
        return {"Account": self.account}

    def can_paginate(self, method):
        return method in self.pages

    def get_paginator(self, method):
        return Paginator(self.pages[method])


class Session:
    def __init__(self, account=ACCOUNT, pages=None):
        self.account = account
        self.pages = pages

    def client(self, name, region_name=None, **kwargs):
        if name == "sts":
            return Client(self.account, {})
        return Client(self.account, self.pages(region_name))


class Clock:
    def __init__(self):
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


def make_context(pages, account=ACCOUNT):
    return AdapterContext(
        clock=Clock(),
        uuid_factory=lambda: UUID(int=7),
        session_factory=lambda **kwargs: Session(account, pages),
    )


def request(*accounts, regions=("us-east-1",)):
    return AwsCollectionRequest(
        accounts=tuple(
            AwsAccountConfig(profile=f"p{a}", expected_account_id=a, regions=regions)
            for a in accounts
        )
    )


def empty_network_pages():
    """Zero-result pages for the network operations added after the first EC2 slice."""
    return {
        "describe_route_tables": [{"RouteTables": []}],
        "describe_internet_gateways": [{"InternetGateways": []}],
        "describe_network_acls": [{"NetworkAcls": []}],
        "describe_security_groups": [{"SecurityGroups": []}],
        "describe_security_group_rules": [{"SecurityGroupRules": []}],
        "describe_network_interfaces": [{"NetworkInterfaces": []}],
        "describe_nat_gateways": [{"NatGateways": []}],
        "describe_egress_only_internet_gateways": [{"EgressOnlyInternetGateways": []}],
        "describe_vpc_peering_connections": [{"VpcPeeringConnections": []}],
    }
