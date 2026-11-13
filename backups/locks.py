"""One PostgreSQL advisory lock keeps backup retention and a restore apart on the same database.

A restore takes it EXCLUSIVELY (it is about to wipe and rewrite the database); retention takes it
SHARED (several retentions may overlap). Neither waits: whoever cannot get the lock stops with a
clear message (restore) or skips this round (retention, which runs again at the next launch). The
lock belongs to the database session, so it disappears if the process dies - nothing is left
locked."""

MAINTENANCE_LOCK = 0x436F76656E4C6962  # fixed key, fits a signed 64-bit integer


def try_lock(conn, shared=False):
    function = "pg_try_advisory_lock_shared" if shared else "pg_try_advisory_lock"
    with conn.cursor() as cur:
        cur.execute(f"SELECT {function}(%s)", [MAINTENANCE_LOCK])
        return bool(cur.fetchone()[0])


def unlock(conn, shared=False):
    function = "pg_advisory_unlock_shared" if shared else "pg_advisory_unlock"
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {function}(%s)", [MAINTENANCE_LOCK])
    except Exception:  # noqa: BLE001 - a lost session has already dropped the lock
        pass
