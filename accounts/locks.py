from django.db import connection, transaction


def advisory_locks(*keys):
    """Take PostgreSQL transaction-level advisory locks (released at commit/rollback).

    Keys are locked in sorted order so concurrent callers cannot deadlock. Must be called inside
    ``transaction.atomic()``; it serialises the check-then-record sequence of the rate limiters.
    """
    if not connection.in_atomic_block:  # pragma: no cover - programming error guard
        raise RuntimeError("advisory_locks() must run inside transaction.atomic()")
    with connection.cursor() as cur:
        for key in sorted({k for k in keys if k}):
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


__all__ = ["advisory_locks", "transaction"]
