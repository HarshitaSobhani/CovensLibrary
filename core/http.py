from urllib.parse import urlencode


def int_param(request, name):
    """Return (value, ok): a positive integer from the query string, or (None, False) if invalid."""
    raw = (request.GET.get(name) or "").strip()
    if not raw:
        return None, True
    if raw.isascii() and raw.isdigit() and len(raw) <= 18:
        return int(raw), True
    return None, False


def querystring(**params):
    """URL-encode filter parameters for pagination links (ends with '&' so 'page=' can follow)."""
    kept = {k: v for k, v in params.items() if v not in (None, "", False)}
    return urlencode(kept) + "&" if kept else ""


def clean_query(text, limit=200):
    """Make a free-text search term safe for the database (no NUL/control characters)."""
    cleaned = "".join(ch for ch in (text or "") if ch.isprintable() or ch in " \t")
    return cleaned.strip()[:limit]
