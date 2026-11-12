#!/usr/bin/env python
"""Django's command-line entry point.

Settings selection is explicit:
  * Development (macOS): leave DJANGO_SETTINGS_MODULE unset -> config.settings.local, which loads
    a git-ignored .env file (needs the dev-only python-dotenv from requirements/dev.txt).
  * Runtime (Dell, Windows 11): set DJANGO_SETTINGS_MODULE=config.settings.production and real
    environment variables; python-dotenv is not installed or used there.
"""

import os
import sys


def main():
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        try:
            import dotenv  # noqa: F401
        except ImportError:
            sys.exit(
                "DJANGO_SETTINGS_MODULE is not set and python-dotenv (development only) is not "
                "installed.\nOn a runtime machine run with DJANGO_SETTINGS_MODULE="
                "config.settings.production and the environment variables described in "
                "docs/DEVELOPMENT.md / docs/DEPLOYMENT.md."
            )
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.local"
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
