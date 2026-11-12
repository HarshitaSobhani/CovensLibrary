"""Redaction of credentials and sensitive values before anything is written to the audit log.

The audit table is append-only, so a leaked secret can never be removed afterwards. Redaction
therefore works on both keys and values, recursively, and errs on the side of redacting
credential-shaped data while leaving ordinary business data (titles, names, IDs, sentences,
UUIDs, amounts, the library's refundable-token fields such as ``token_cycle``) untouched.
"""

import re

REDACTED = "[redacted]"
MAX_DEPTH = 8

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

# A key containing any of these words is always sensitive.
_ALWAYS_WORDS = {
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "secrets",
    "apikey",
    "otp",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "bearer",
    "jwt",
}
# Words that make "token" / "key" / "session" / "code" sensitive (credential qualifiers).
_QUALIFIERS = {
    "reset",
    "access",
    "refresh",
    "auth",
    "csrf",
    "api",
    "id",
    "verification",
    "activation",
    "login",
    "private",
    "signing",
    "encryption",
    "recovery",
    "secret",
    "ssh",
    "license",
    "session",
    "bearer",
    "passkey",
}


def _words(key):
    return [w.lower() for w in _WORD_RE.findall(str(key))]


def is_sensitive_key(key):
    words = _words(key)
    wset = set(words)
    joined = "".join(words)
    if wset & _ALWAYS_WORDS or joined in {"apikey", "passkey", "sessionid", "sessionkey"}:
        return True
    if "api" in wset and "key" in wset:
        return True
    if "token" in wset:
        # bare "token" or a credential-qualified token (reset_token, access_token, ...)
        return len(words) == 1 or bool(wset & _QUALIFIERS)
    if "key" in wset:
        return len(words) == 1 or bool(wset & _QUALIFIERS)
    if "session" in wset:
        return len(words) == 1 or bool(wset & {"id", "key", "token", "cookie", "data"})
    if "code" in wset and wset & {"recovery", "reset", "hash", "otp", "verification"}:
        return True
    if "hash" in wset and wset & {"password", "code", "recovery", "reset"}:
        return True
    return False


_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|api[_-]?key|token|authorization|otp)\b"
    r"(\s*[:=]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_RESET_URL = re.compile(r"(/reset/)[^/\s]+/[^/\s]+/?")
_RECOVERY_CODE = re.compile(r"\b[A-HJ-NP-Z2-9]{5}-[A-HJ-NP-Z2-9]{5}\b")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_OPAQUE = re.compile(
    r"(?<![\w+/=.-])(?=[A-Za-z0-9_+/=.-]*[A-Za-z])(?=[A-Za-z0-9_+/=.-]*\d)"
    r"[A-Za-z0-9_+/=.-]{32,}(?![\w+/=.-])"
)


def _opaque_sub(match):
    return match.group(0) if _UUID.match(match.group(0)) else REDACTED


def scrub_text(text):
    """Redact credential-shaped content inside a string, keeping the rest."""
    if not text:
        return text
    text = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _RESET_URL.sub(lambda m: m.group(1) + REDACTED, text)
    text = _RECOVERY_CODE.sub(REDACTED, text)
    return _OPAQUE.sub(_opaque_sub, text)


def scrub(value, _depth=0):
    """Return a redacted copy of any JSON-like structure (the input is never mutated)."""
    if _depth > MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(k): (REDACTED if is_sensitive_key(k) else scrub(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_text(str(value))
