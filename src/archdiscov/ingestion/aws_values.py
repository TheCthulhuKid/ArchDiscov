"""AWS spellings of enumerated values, shared by the collector and the importers.

`Describe*` responses, Terraform AWS provider attributes, and CloudFormation properties spell
these values the same way, so one table serves all three.
"""

from ..models.enums import AddressFamily, VpcEndpointType

ENDPOINT_TYPES = {
    "Gateway": VpcEndpointType.GATEWAY,
    "Interface": VpcEndpointType.INTERFACE,
    "GatewayLoadBalancer": VpcEndpointType.GATEWAY_LOAD_BALANCER,
}
ADDRESS_FAMILIES = {"IPv4": AddressFamily.IPV4, "IPv6": AddressFamily.IPV6}
