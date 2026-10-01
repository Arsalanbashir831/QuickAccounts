"""Local-only settings for the Docker Compose development services."""

from pathlib import Path

import environ

local_env_file = Path(__file__).resolve().parents[2] / ".env"
if local_env_file.is_file():
    environ.Env.read_env(local_env_file, overwrite=False)

from config.settings.base import *  # noqa: E402,F403

AUTH_REFRESH_COOKIE_SECURE = False
