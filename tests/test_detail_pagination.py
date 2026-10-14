import pytest

from archdiscov.ingestion.detail_pagination import PAGINATION_POLICIES


@pytest.mark.parametrize("mode", PAGINATION_POLICIES)
def test_pagination_completion_and_next_marker(mode):
    policy = PAGINATION_POLICIES[mode]
    assert policy.next_marker({}) is None
    page = {policy.response_token: "next"}
    if policy.more_results:
        assert policy.next_marker(page) is None
        page[policy.more_results] = True
    assert policy.next_marker(page) == "next"


@pytest.mark.parametrize("marker", [None, ""])
def test_truncated_iam_page_requires_marker(marker):
    with pytest.raises(ValueError, match="truncated pagination response omitted its marker"):
        PAGINATION_POLICIES["iam"].next_marker({"IsTruncated": True, "Marker": marker})
