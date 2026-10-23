"""The README's example questions, each keyed by the analyzer kind that answers it.

The README lists these questions as what the assistant answers; a test reads the README and
compares its list with this one, so the wording lives here once. The MCP server offers each
question as a prompt named by its analyzer kind, and the tests use them as `ask` questions.
"""

from collections.abc import Mapping
from types import MappingProxyType

from archdiscov.models.enums import AnalyzerKind

README_QUESTIONS: Mapping[AnalyzerKind, str] = MappingProxyType(
    {
        AnalyzerKind.INTERNET_EXPOSURE: "What receives traffic from the public internet?",
        AnalyzerKind.DATABASE_ACCESS: "Which services can access this database?",
        AnalyzerKind.SECURITY_GROUP_IMPACT: (
            "What would be affected if this security group changed?"
        ),
        AnalyzerKind.CROSS_ACCOUNT_TRUST: "Where are the cross-account trust relationships?",
        AnalyzerKind.OWNERSHIP: "Which resources appear to have no clear owner?",
    }
)
