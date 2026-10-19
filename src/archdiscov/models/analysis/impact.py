from typing import Self

from pydantic import model_validator

from ..enums import ImpactKind, ImpactLabel, RuleDirection, RuleReferenceSide
from ..resource_base import Model, NonEmptyString


class SecurityGroupImpact(Model):
    """Structured impact of one selected security group on the finding's resources.

    The impacted resources are the finding's resource references. Direct attachments
    and rule references are recorded structure and are labeled observed; downstream
    dependencies are traced possibilities and are labeled potential. Labels are part
    of finding identity. None of this asserts reachability or effective access.
    Rule fields apply only to rule references.
    """

    selected_group_id: NonEmptyString
    kind: ImpactKind
    label: ImpactLabel
    rule_direction: RuleDirection | None = None
    reference_side: RuleReferenceSide | None = None

    @model_validator(mode="after")
    def validate_combination(self) -> Self:
        expected = (
            ImpactLabel.POTENTIAL
            if self.kind == ImpactKind.DOWNSTREAM_DEPENDENCY
            else ImpactLabel.OBSERVED
        )
        if self.label != expected:
            raise ValueError("impact label must match the impact kind")
        rule_fields = (self.rule_direction, self.reference_side)
        if self.kind == ImpactKind.RULE_REFERENCE:
            if any(field is None for field in rule_fields):
                raise ValueError("rule references require rule_direction and reference_side")
        elif any(field is not None for field in rule_fields):
            raise ValueError("rule fields apply only to rule references")
        return self
