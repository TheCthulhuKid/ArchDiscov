"""Offline infrastructure-as-code adapters; Terraform execution is never used."""

from .plan import TerraformPlanAdapter
from .requests import TerraformPlanRequest, TerraformStateRequest
from .state import TerraformStateAdapter

__all__ = [
    "TerraformPlanAdapter",
    "TerraformPlanRequest",
    "TerraformStateAdapter",
    "TerraformStateRequest",
]
