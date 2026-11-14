"""Application logging that cannot leak secrets or personal data.

Django and PostgreSQL error messages can contain values (``Key (mobile_e164)=(+91...) already
exists``), addresses and links. Every record is scrubbed BEFORE it reaches any handler: credential
shaped text (audit.redaction), e-mail addresses, phone numbers, database ``Key (..)=(..)`` details
and password-reset links. The log file lives in a protected folder created by the launcher.
"""

import logging
import logging.handlers
import re
from pathlib import Path

from audit.redaction import scrub_text

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w])")
_DB_KEY = re.compile(r"(Key \([^)]*\)=)\([^)]*\)")
_FAILING_ROW = re.compile(r"(Failing row contains )\(.*\)")
_URL_QUERY = re.compile(r"(\?[^\s\"']*)")
REDACTED = "[redacted]"


def scrub_log_text(text):
    if not text:
        return text
    text = scrub_text(text)
    text = _DB_KEY.sub(lambda m: m.group(1) + f"({REDACTED})", text)
    text = _FAILING_ROW.sub(lambda m: m.group(1) + f"({REDACTED})", text)
    text = _EMAIL.sub(REDACTED, text)
    text = _PHONE.sub(REDACTED, text)
    return _URL_QUERY.sub("?" + REDACTED, text)


class ScrubFilter(logging.Filter):
    """Rewrites the finished message and the exception text of every record."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            message = str(record.msg)
        record.msg, record.args = scrub_log_text(message), None
        if record.exc_info:
            record.exc_text = scrub_log_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub_log_text(record.exc_text)
        record.stack_info = scrub_log_text(record.stack_info) if record.stack_info else None
        return True


def build_logging(log_dir):
    """The LOGGING setting: scrubbed, to a rotating file when ``log_dir`` is given, else to the
    console. Nothing here logs request bodies, cookies or SQL."""
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["scrub"],
            "formatter": "plain",
        }
    }
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(Path(log_dir) / "application.log"),
            "maxBytes": 2_000_000,
            "backupCount": 5,
            "encoding": "utf-8",
            "filters": ["scrub"],
            "formatter": "plain",
        }
    chosen = ["file"] if log_dir else ["console"]
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"scrub": {"()": "core.safelog.ScrubFilter"}},
        "formatters": {
            "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
        },
        "handlers": handlers,
        "loggers": {
            "django": {"handlers": chosen, "level": "WARNING", "propagate": False},
            "django.server": {"handlers": chosen, "level": "WARNING", "propagate": False},
            "waitress": {"handlers": chosen, "level": "WARNING", "propagate": False},
            "communications": {"handlers": chosen, "level": "INFO", "propagate": False},
            "coven": {"handlers": chosen, "level": "INFO", "propagate": False},
        },
        "root": {"handlers": chosen, "level": "WARNING"},
    }
