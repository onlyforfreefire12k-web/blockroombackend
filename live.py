"""Flask entrypoint (Render web service).

Routes
  GET  /health                     readiness probe
  POST /webhook/telegram           Telegram updates (secret header checked)
  POST /api/auth/telegram          Mini App initData -> Firebase custom token
  OPTIONS /api/auth/telegram       CORS preflight

Start:  gunicorn live:app --workers 2 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from flask import Flask, jsonify, request

from config import load_config
from db import make_db
from handlers import Bot
from security import InitDataError, verify_init_data, verify_webhook_secret
from telegram_api import Telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("blockroom")

cfg = load_config()
db = make_db(cfg)
tg = Telegram(cfg.telegram_bot_token or "")
bot = Bot(cfg, db, tg)

app = Flask(__name__)


@app.after_request
def cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        telegram=cfg.telegram_ready,
        firebase=cfg.firebase_ready,
        youtube=cfg.youtube_ready,
        miniApp=cfg.mini_app_ready,
    )


@app.post("/webhook/telegram")
def telegram_webhook():
    if not verify_webhook_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), cfg.telegram_webhook_secret):
        return "", 401
    update: Dict[str, Any] = request.get_json(silent=True) or {}
    try:
        bot.handle(update)
    except Exception:  # never 500 back to Telegram (it would retry forever)
        log.exception("handler failed")
    return "", 204


@app.post("/api/auth/telegram")
def auth_telegram():
    if not cfg.telegram_bot_token:
        return jsonify(ok=False, error="bot token not configured"), 503
    body = request.get_json(silent=True) or {}
    init_data = (body.get("init_data") or request.headers.get("X-Telegram-Init-Data") or "").strip()
    if not init_data:
        return jsonify(ok=False, error="initData is required"), 400
    try:
        verified = verify_init_data(init_data, cfg.telegram_bot_token, cfg.max_init_data_age_seconds)
    except InitDataError as exc:
        return jsonify(ok=False, error=str(exc)), 401
    if not cfg.firebase_ready:
        return jsonify(ok=False, error="firebase admin not configured"), 503

    from firebase_admin import auth as fb_auth

    user = verified["user"]
    uid = f"tg_{user['id']}"
    try:
        token = fb_auth.create_custom_token(
            uid,
            {"telegramId": int(user["id"]), "telegramUsername": user.get("username")},
        )
        if isinstance(token, bytes):
            token = token.decode()
    except Exception as exc:  # pragma: no cover
        log.exception("custom token failed")
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, firebaseUid=uid, token=token, startParam=verified.get("start_param"))


@app.get("/")
def root():
    return jsonify(service="blockroom-bot", ok=True, commands=["/room", "/play", "/vplay", "/stop", "/skip"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
