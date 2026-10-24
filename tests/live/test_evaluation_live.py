"""Opt-in live evaluation of the README suite through every configured provider.

It is marked `live`, which the default pytest options deselect, so normal runs and CI never
collect it. It reuses the other live tests' variables and caps: each provider whose
`ARCHDISCOV_LIVE_*` variables are set is evaluated, with at most four requests (plus the one
repair) of 4,096 output tokens per case, and it skips when none is. A real model is a smoke
test here: it checks only the safety checks of every case, never the expectations, and that
no key or profile secret reaches the report. It makes up to 45 requests per provider (nine
cases of five), so run it only on purpose, with the variables of the live tests:

    ARCHDISCOV_LIVE_ANTHROPIC_KEY_ENV=ANTHROPIC_API_KEY \
    ARCHDISCOV_LIVE_ANTHROPIC_MODEL=claude-sonnet-5-5 \
    .venv/bin/python -m pytest -m live tests/live/test_evaluation_live.py
"""

from pathlib import Path

import pytest

from archdiscov.assistant.evaluation import SAFETY_CHECKS
from archdiscov.services.evaluation import evaluate_suite
from tests.support.evaluation_suites import README_SUITE
from tests.support.live import NO_PROVIDER, configured_providers, write_config

pytestmark = pytest.mark.live


def test_the_readme_suite_is_answered_safely_by_each_provider(tmp_path: Path) -> None:
    providers = configured_providers()
    if not providers:
        pytest.skip(NO_PROVIDER)
    configs = [
        write_config(tmp_path / f"provider-{number}.toml", provider.fields)
        for number, provider in enumerate(providers)
    ]
    report = evaluate_suite(README_SUITE, configs, allow_external=True)
    recorded = report.model_dump_json()
    assert [run.provider_kind for run in report.providers] == [p.kind for p in providers]
    for run in report.providers:
        assert not run.cancelled and len(run.records) == len(report.suite.case_ids)
        for record in run.records:
            passed = {check.name for check in record.checks if check.passed}
            assert passed >= set(SAFETY_CHECKS), (run.provider_kind, record.case_id)
    for provider in providers:
        for secret in provider.secrets:
            assert secret not in recorded
