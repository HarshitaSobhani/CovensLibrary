from pathlib import Path

from dotenv import load_dotenv

# Development only: load a git-ignored .env file (production uses real environment variables).
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from .base import *  # noqa: E402,F401,F403
from .base import build_database, env  # noqa: E402

DB_ROLE = "runtime"  # day-to-day development runs as the runtime role too (two-role parity)
DATABASES = build_database(env("DB_USER", required=True), env("DB_PASSWORD", required=True))
