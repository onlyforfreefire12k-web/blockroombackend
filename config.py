"""Environment configuration for the Blockroom Telegram bot.

All secrets come from environment variables (Render dashboard). Nothing is
hard-coded and nothing is ever sent to the browser except short-lived
Firebase custom tokens minted in /api/auth/telegram.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


def _env(env: Mapping[str, str], key: str, default: Optional[str] = None) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


@dataclass(frozen=True)
class Config:
    telegram_bot_token: Optional[str]
    telegram_webhook_secret: Optional[str]
    firebase_project_id: Optional[str]
    firebase_service_account_json: Optional[str]
    firebase_service_account_path: Optional[str]
    firebase_collection_prefix: str
    youtube_api_key: Optional[str]
    mini_app_url: Optional[str]
    render_external_url: Optional[str]
    auto_set_webhook: bool
    max_init_data_age_seconds: int
    room_capacity: int

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_webhook_secret)

    @property
    def firebase_ready(self) -> bool:
        return bool(
            self.firebase_project_id
            and (self.firebase_service_account_json or self.firebase_service_account_path)
        )

    @property
    def youtube_ready(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def mini_app_ready(self) -> bool:
        return bool(self.mini_app_url and self.mini_app_url.startswith("https://"))


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = env if env is not None else os.environ
    return Config(
        telegram_bot_token=_env(env, "TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_env(env, "TELEGRAM_WEBHOOK_SECRET"),
        firebase_project_id=_env(env, "FIREBASE_PROJECT_ID"),
        firebase_service_account_json=_env(env, "FIREBASE_SERVICE_ACCOUNT_JSON"),
        firebase_service_account_path=_env(env, "FIREBASE_SERVICE_ACCOUNT_PATH"),
        firebase_collection_prefix=_env(env, "FIREBASE_COLLECTION_PREFIX", "") or "",
        youtube_api_key=_env(env, "YOUTUBE_API_KEY"),
        mini_app_url=_env(env, "MINI_APP_URL"),
        render_external_url=_env(env, "RENDER_EXTERNAL_URL"),
        auto_set_webhook=_env(env, "AUTO_SET_WEBHOOK", "true").lower() == "true",
        max_init_data_age_seconds=int(_env(env, "MAX_INIT_DATA_AGE_SECONDS", "86400") or 86400),
        room_capacity=int(_env(env, "ROOM_CAPACITY", "12") or 12),
    )
