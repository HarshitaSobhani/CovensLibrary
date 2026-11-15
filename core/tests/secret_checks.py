"""Helpers so 'no secret in output' assertions cannot pass or fail by accident.

A very short secret (for example two characters) appears inside ordinary words, so a
substring assertion against it would raise false alarms instead of detecting disclosure.
"""

MIN_SECRET_LENGTH = 8


def checked_secret(testcase, secret):
    """Return `secret` after asserting it is long enough for a substring leak check."""
    testcase.assertGreaterEqual(
        len(secret or ""),
        MIN_SECRET_LENGTH,
        f"The test credential must be at least {MIN_SECRET_LENGTH} characters long, otherwise "
        "a leak check would match ordinary text. Use a long, unique password in .env/.env.owner.",
    )
    return secret
