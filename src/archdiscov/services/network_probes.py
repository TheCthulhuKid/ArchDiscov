"""Representative traffic used when a primitive needs one concrete address or port."""

PROTOCOL = "tcp"
EPHEMERAL_PORT = 49152  # start of the IANA dynamic range, as the representative reply port
INTERNET_CIDR = "0.0.0.0/0"  # bounds an unknown internet client for security groups and NACLs
PROBE_ADDRESS = "203.0.113.10"  # a documentation-range public address, for route selection
