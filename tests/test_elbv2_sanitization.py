from copy import deepcopy

from archdiscov.ingestion.elbv2.sanitization import sanitize_source


def test_source_redacts_contextual_values_before_whole_item_evidence():
    item = {
        "Actions": [
            {
                "AuthenticateOidcConfig": {
                    "ClientSecret": "oidc-secret",
                    "Issuer": "https://issuer.example",
                    "AuthenticationRequestExtraParams": {"safe-looking": "extra-secret"},
                }
            },
            {"FixedResponseConfig": {"MessageBody": "body-secret", "StatusCode": "403"}},
        ],
        "Conditions": [
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": "Authorization",
                    "Values": ["header-secret"],
                },
            },
            {
                "Field": "query-string",
                "QueryStringConfig": {"Values": [{"Key": "q", "Value": "query-secret"}]},
            },
            {"Field": "path-pattern", "Values": ["/safe"]},
        ],
        "Tags": [{"Key": "password", "Value": "tag-secret"}],
    }
    before = deepcopy(item)
    safe, paths = sanitize_source(item)
    assert item == before
    assert "secret" not in str(safe)
    assert safe["Conditions"][2]["Values"] == ["/safe"]
    assert safe["Conditions"][0]["HttpHeaderConfig"]["HttpHeaderName"] == "Authorization"
    assert "/Actions/0/AuthenticateOidcConfig/ClientSecret" in paths
    assert (
        "/Actions/0/AuthenticateOidcConfig/AuthenticationRequestExtraParams/safe-looking" in paths
    )
    assert "/Conditions/1/QueryStringConfig/Values" in paths
    assert "/Tags/0/Value" in paths
    assert sanitize_source(safe)[0] == safe
