from pathlib import Path

from dotenv import load_dotenv

# Development only: load a git-ignored .env file (production uses real environment variables).
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from .base import *  # noqa: E402,F401,F403
