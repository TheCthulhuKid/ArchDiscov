"""Offline infrastructure-as-code adapters; Terraform execution is never used."""

from .requests import TerraformStateRequest
from .state import TerraformStateAdapter

__all__ = ["TerraformStateAdapter", "TerraformStateRequest"]
