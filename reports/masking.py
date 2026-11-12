"""Masking of member-linked free text for ANONYMISED members (decision D-28i).

The immutable records are never changed; reports, statements and exports show this text instead of
reasons and notes that may contain what anonymisation removed (a name typed into a reason)."""

REDACTED = "Redacted \u2014 member anonymised"


def mask(anonymised, text):
    """``anonymised`` is the member's ``anonymised_at`` (or any truthy flag)."""
    if anonymised and text:
        return REDACTED
    return text
