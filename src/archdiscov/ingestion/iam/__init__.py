"""Account-wide IAM collection; execution and normalization stay in focused modules."""


def collect_iam(session, account, context, snapshot_id, account_id):
    from .collector import collect_inventory
    from .normalization import normalize_inventory

    return normalize_inventory(
        snapshot_id, account_id, collect_inventory(session, account, context)
    )
