"""Environment-driven configuration for the Blockroom Telegram bot.

Every secret is read from environment variables:

* production -> configured in the Render dashboard (see render.yaml)
* local dev  -> a ``.env`` file next to ``live.py`` (python-dotenv is optional;
                the app works without it)

No secret value is ever logged by this module: only presence/absence matters
to the rest of the application.
"""

import os
from dataclasses import dataclass
from typing import Optional

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_str(env, key, default=None):
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(env, key, default=False):
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_int(env, key, default):
    raw = env.get(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Config:
    """All runtime settings.  ``None`` means "not configured"."""

    # --- Telegram ---
    telegram_bot_token: Optional[str] = None
    telegram_webhook_secret: Optional[str] = None

    # --- Firebase ---
    firebase_project_id: Optional[str] = None
    firebase_service_account_json: Optional[str] = None
    firebase_service_account_path: Optional[str] = None
    firebase_backend: str = "firestore"          # "firestore" | "rtdb"
    firebase_rtdb_url: Optional[str] = None
    firebase_collection_prefix: str = ""         # e.g. "blockroom_"

    # --- Game / Mini App ---
    mini_app_url: Optional[str] = None

    # --- Media search ---
    youtube_api_key: Optional[str] = None

    # --- Deployment ---
    render_external_url: Optional[str] = None
    auto_set_webhook: bool = False
    allow_insecure_webhook: bool = False         # local development only

    # --- Tuning ---
    max_init_data_age_seconds: int = 86400
    rate_limit_rooms_per_hour: int = 5
    rate_limit_searches_per_minute: int = 12
    rate_limit_controls_per_minute: int = 20
    rate_limit_callbacks_per_minute: int = 60
    rate_limit_auth_per_minute: int = 30
    log_level: str = "INFO"
    testing: bool = False

    @property
    def mini_app_configured(self) -> bool:
        """True when MINI_APP_URL looks like a usable http(s) URL."""
        return bool(
            self.mini_app_url
            and self.mini_app_url.startswith(("https://", "http://"))
        )

    @property
    def firebase_credentials_available(self) -> bool:
        return bool(self.firebase_service_account_json or self.firebase_service_account_path)


def load_config(env=None) -> Config:
    """Build a Config from an environment mapping.

    ``env=None`` loads the real process environment (and a local ``.env`` file
    when python-dotenv is installed).  Tests pass an explicit mapping or build
    a ``Config`` directly.
    """
    if env is None:
        try:
            from dotenv import load_dotenv  # optional convenience for local dev
            load_dotenv()
        except ImportError:
            pass
        env = os.environ

    backend = (_env_str(env, "FIREBASE_BACKEND", "firestore") or "firestore").lower()
    if backend in ("realtimedb", "realtime", "rtdb"):
        backend = "rtdb"

    return Config(
        telegram_bot_token=_env_str(env, "TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_env_str(env, "TELEGRAM_WEBHOOK_SECRET"),
        firebase_project_id=_env_str(env, "FIREBASE_PROJECT_ID"),
        firebase_service_account_json=_env_str(env, "FIREBASE_SERVICE_ACCOUNT_JSON"),
        firebase_service_account_path=_env_str(env, "FIREBASE_SERVICE_ACCOUNT_PATH"),
        firebase_backend=backend,
        firebase_rtdb_url=_env_str(env, "FIREBASE_RTDB_URL"),
        firebase_collection_prefix=_env_str(env, "FIREBASE_COLLECTION_PREFIX", "") or "",
        mini_app_url=_env_str(env, "MINI_APP_URL"),
        youtube_api_key=_env_str(env, "YOUTUBE_API_KEY"),
        render_external_url=_env_str(env, "RENDER_EXTERNAL_URL"),
        auto_set_webhook=_env_bool(env, "AUTO_SET_WEBHOOK", False),
        allow_insecure_webhook=_env_bool(env, "ALLOW_INSECURE_WEBHOOK", False),
        max_init_data_age_seconds=_env_int(env, "MAX_INIT_DATA_AGE_SECONDS", 86400),
        rate_limit_rooms_per_hour=_env_int(env, "RATE_LIMIT_ROOMS_PER_HOUR", 5),
        rate_limit_searches_per_minute=_env_int(env, "RATE_LIMIT_SEARCHES_PER_MINUTE", 12),
        rate_limit_controls_per_minute=_env_int(env, "RATE_LIMIT_CONTROLS_PER_MINUTE", 20),
        rate_limit_callbacks_per_minute=_env_int(env, "RATE_LIMIT_CALLBACKS_PER_MINUTE", 60),
        rate_limit_auth_per_minute=_env_int(env, "RATE_LIMIT_AUTH_PER_MINUTE", 30),
        log_level=_env_str(env, "LOG_LEVEL", "INFO") or "INFO",
        testing=_env_bool(env, "BLOCKROOM_TESTING", False),
    )
