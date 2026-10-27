"""Allowlisted field conversion and source pointers for Terraform mappings."""

from ipaddress import AddressValueError, IPv4Address

from pydantic import IPvAnyNetwork, TypeAdapter, ValidationError

from ...models.json_pointer import escape_pointer_token

_NETWORK = TypeAdapter(IPvAnyNetwork)


def is_nonblank_unredacted_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "[REDACTED]" not in value


class MappingValues:
    def __init__(self, values: dict, data: dict, facts: dict[str, str], limitations: list[str]):
        self.values = values
        self.data = data
        self.facts = facts
        self.limitations = limitations

    def omitted(self, field: str) -> None:
        # Field names here are fixed mapping constants, never provider values or tag keys.
        self.limitations.append(f"Malformed or redacted Terraform {field} was omitted.")

    def string(self, source: str, target: str, *, attribute: bool = False) -> None:
        if source not in self.values:
            return
        value = self.values[source]
        if value is not None and not is_nonblank_unredacted_string(value):
            self.omitted(source)
            return
        container = self.data["attributes"] if attribute else self.data
        # A target may name a nested field, such as a peering side's `requester/vpc_id`.
        *parents, leaf = target.split("/")
        for parent in parents:
            container = container.setdefault(parent, {})
        container[leaf] = value
        self.facts[f"/attributes/{target}" if attribute else f"/{target}"] = f"/values/{source}"

    def iam(self, string_fields: tuple[tuple[str, str], ...]) -> bool:
        arn = self.values.get("arn")
        if not is_nonblank_unredacted_string(arn) or not arn.startswith("arn:"):
            self.limitations.append(
                "IAM typed mapping requires a source ARN; only its type is retained."
            )
            return False
        self.string("arn", "arn")
        for source, target in string_fields:
            self.string(source, target)
        if any(
            self.values.get(field)
            for field in ("assume_role_policy", "inline_policy", "policy", "roles")
        ):
            self.limitations.append("IAM policy documents and profile role names are not mapped.")
        return True

    def tags_and_name(self) -> None:
        tags = self.values.get("tags")
        if "tags" in self.values:
            self._tags(tags)
        if isinstance(tags, dict) and is_nonblank_unredacted_string(tags.get("Name")):
            self.data["name"] = tags["Name"]
            self.facts["/name"] = "/values/tags/Name"
        else:
            self.string("name", "name")

    def _tags(self, tags: object) -> None:
        if not isinstance(tags, dict):
            if tags is not None:
                self.omitted("tags")
            return
        safe_tags = {
            key: value
            for key, value in tags.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if len(safe_tags) != len(tags):
            self.omitted("tags")
        if safe_tags or not tags:
            self.data["attributes"]["tags"] = safe_tags
            for key in safe_tags:
                token = escape_pointer_token(key)
                self.facts[f"/attributes/tags/{token}"] = f"/values/tags/{token}"
            if not tags:
                self.facts["/attributes/tags"] = "/values/tags"

    def security_groups(self) -> None:
        for source in ("vpc_security_group_ids", "security_groups"):
            if source not in self.values:
                continue
            groups = self.values[source]
            if groups is None:
                self.data["attributes"]["security_group_ids"] = None
                self.facts["/attributes/security_group_ids"] = f"/values/{source}"
                return
            if not isinstance(groups, list):
                self.omitted(source)
                continue
            clean = []
            for index, group in enumerate(groups):
                if is_nonblank_unredacted_string(group):
                    self.facts[f"/attributes/security_group_ids/{len(clean)}"] = (
                        f"/values/{source}/{index}"
                    )
                    clean.append(group)
                else:
                    self.omitted(source)
            if clean or not groups:
                self.data["attributes"]["security_group_ids"] = clean
                if not groups:
                    self.facts["/attributes/security_group_ids"] = f"/values/{source}"
                return

    def cidrs(self, sources: tuple[str, ...]) -> None:
        if not sources:
            return
        blocks = []
        for source in sources:
            if source not in self.values or self.values[source] is None:
                continue
            try:
                if not is_nonblank_unredacted_string(self.values[source]):
                    self.omitted(source)
                    continue
                network = _NETWORK.validate_python(self.values[source])
            except ValidationError:
                self.omitted(source)
                continue
            self.facts[f"/cidr_blocks/{len(blocks)}"] = f"/values/{source}"
            blocks.append(network)
        if blocks:
            self.data["cidr_blocks"] = blocks
        elif any(source in self.values for source in sources) and all(
            self.values.get(source) is None for source in sources
        ):
            self.data["cidr_blocks"] = None
            source = next(source for source in sources if source in self.values)
            self.facts["/cidr_blocks"] = f"/values/{source}"

    def addresses(self, sources: tuple[tuple[str, str], ...]) -> None:
        """Record each single IPv4 address as a one-item list; an empty value is no address."""
        for source, target in sources:
            value = self.values.get(source)
            if value in (None, ""):
                continue
            try:
                if not is_nonblank_unredacted_string(value):
                    raise AddressValueError(source)
                address = IPv4Address(value)
            except AddressValueError:
                self.omitted(source)
                continue
            self.data[target] = [address]
            self.facts[f"/{target}/0"] = f"/values/{source}"

    def inline_collections(self, collections: tuple[tuple[str, str], ...]) -> None:
        for source, target in collections:
            if source in self.values and self.values[source] == []:
                self.data[target] = []
                self.facts[f"/{target}"] = f"/values/{source}"
            else:
                self.data["attributes"].setdefault("unavailable_fields", []).append(target)
                self.limitations.append(
                    "Terraform inline rules/routes are unavailable or not mapped."
                )
