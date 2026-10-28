"""Flask entrypoint for the Blockroom bot (plain Python, no gunicorn needed).

Run locally / on Render:
    python live.py

Routes
  GET  /health                 readiness + bot identity + webhook state
  GET  /telegram/status        getMe / getWebhookInfo (no secrets)
  POST /webhook/telegram       Telegram updates (secret header checked)
  POST /api/auth/telegram      Mini App initData -> Firebase custom token
  OPTIONS /api/auth/telegram   CORS preflight

Startup behaviour (BOT_MODE env):
  polling  delete the webhook and long-poll in a background thread  <- works
           with `python live.py`, no public URL required
  webhook  register RENDER_EXTERNAL_URL/webhook/telegram automatically
  off      do nothing; configure manually
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from flask import Flask, jsonify, request

from config import load_config
from db import make_db
from handlers import Bot
from poller import Poller
from security import InitDataError, verify_init_data, verify_webhook_secret
from telegram_api import Telegram, build_join_link, join_keyboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("blockroom")

# local dev convenience: load backend/.env if python-dotenv is installed
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:  # pragma: no cover - optional dependency
    pass

cfg = load_config()
db = make_db(cfg)
tg = Telegram(cfg.telegram_bot_token or "")
bot = Bot(cfg, db, tg)
poller = Poller(tg, bot)

app = Flask(__name__)

STARTUP: Dict[str, Any] = {"mode": cfg.bot_mode, "webhook": None, "bot": None, "error": None}


@app.after_request
def cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


def _join_preview() -> Dict[str, Any]:
    """What /room will send, without calling Telegram."""
    try:
        link = build_join_link(cfg.mini_app_url or "", "testroom12")
        keyboard, kind = join_keyboard(link, chat_type="group", mini_app_url=cfg.mini_app_url or "")
        return {"link": link, "buttonType": kind, "button": (keyboard[0][0] if keyboard else None)}
    except ValueError as exc:
        return {"error": str(exc)}


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        telegram=cfg.telegram_ready,
        firebase=cfg.firebase_ready,
        youtube=cfg.youtube_ready,
        miniApp=cfg.mini_app_ready,
        mode=cfg.bot_mode,
        polling=poller.running,
        bot=STARTUP.get("bot"),
        webhook=STARTUP.get("webhook"),
        error=STARTUP.get("error"),
        joinPreview=_join_preview(),
    )


@app.get("/telegram/status")
def telegram_status():
    me = tg.me()
    info = tg.webhook_info()
    return jsonify(
        ok=bool(me),
        me=me,
        webhook=info,
        mode=cfg.bot_mode,
        polling=poller.running,
        updates_handled=poller.updates,
        poll_errors=poller.errors,
    )


@app.post("/telegram/set-webhook")
def telegram_set_webhook():
    """Manual hook: /telegram/set-webhook?url=https://...&secret=..."""
    url = (request.args.get("url") or (cfg.public_url or "") + "/webhook/telegram").strip()
    secret = request.args.get("secret") or cfg.telegram_webhook_secret or ""
    if not url.startswith("https://") or not secret:
        return jsonify(ok=False, error="need an https url and a secret"), 400
    result = tg.set_webhook(url, secret)
    poller.stop()
    return jsonify(ok=result is not None, result=result, url=url)


@app.post("/telegram/test-room-button")
def telegram_test_room_button():
    """Real acceptance test: send the /room keyboard to a group and report
    Telegram's verbatim verdict. POST/GET with ?chat_id=-100..."""
    chat_id = request.args.get("chat_id") or (request.get_json(silent=True) or {}).get("chat_id")
    if not chat_id:
        return jsonify(ok=False, error="chat_id is required"), 400
    try:
        chat_id_int = int(chat_id)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="chat_id must be a number"), 400

    room_id = request.args.get("room") or "testroom12"
    chat_type = request.args.get("chat_type") or "group"
    try:
        link = build_join_link(cfg.mini_app_url or "", room_id)
    except ValueError as exc:
        return jsonify(ok=False, stage="build_link", error=str(exc)), 400

    keyboard, button_type = join_keyboard(
        link, chat_type=chat_type, mini_app_url=cfg.mini_app_url or ""
    )
    body = tg.send_raw(
        chat_id_int,
        "🎬 <b>Blockroom</b> · button check\nRoom <code>{}</code> — tap below to join.".format(room_id),
        keyboard,
    )
    return jsonify(
        ok=bool(body.get("ok")),
        buttonType=button_type,
        link=link,
        telegramErrorCode=body.get("error_code"),
        telegramDescription=body.get("description"),
        messageId=(body.get("result") or {}).get("message_id") if body.get("ok") else None,
    )


@app.post("/webhook/telegram")
def telegram_webhook():
    if not verify_webhook_secret(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token"), cfg.telegram_webhook_secret
    ):
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
    try:
        from firebase_admin import auth as fb_auth

        user = verified["user"]
        uid = f"tg_{user['id']}"
        token = fb_auth.create_custom_token(
            uid, {"telegramId": int(user["id"]), "telegramUsername": user.get("username")}
        )
        if isinstance(token, bytes):
            token = token.decode()
    except Exception as exc:
        log.exception("custom token failed")
        return jsonify(ok=False, error=str(exc)), 500
    return jsonify(ok=True, firebaseUid=uid, token=token, startParam=verified.get("start_param"))


@app.get("/")
def root():
    return jsonify(
        service="blockroom-bot",
        ok=True,
        commands=["/room", "/play", "/vplay", "/stop", "/skip"],
        mode=cfg.bot_mode,
    )


def bootstrap() -> None:
    """Called once per process (also under `python live.py`)."""
    if not cfg.telegram_bot_token:
        STARTUP["error"] = "TELEGRAM_BOT_TOKEN is missing"
        log.error("TELEGRAM_BOT_TOKEN is missing — the bot cannot receive updates")
        return

    me = tg.me()
    STARTUP["bot"] = (me or {}).get("username")
    if not me:
        STARTUP["error"] = "getMe failed — check TELEGRAM_BOT_TOKEN"
        log.error("getMe failed: the bot token is invalid or Telegram is unreachable")
        return
    log.info("bot identity @%s", STARTUP["bot"])

    mode = cfg.bot_mode
    if mode == "polling":
        poller.start()
        STARTUP["webhook"] = None
        log.info("BOT_MODE=polling — webhook not used")
    elif mode == "webhook":
        if cfg.public_url and cfg.telegram_webhook_secret:
            url = f"{cfg.public_url}/webhook/telegram"
            ok = tg.set_webhook(url, cfg.telegram_webhook_secret) is not None
            STARTUP["webhook"] = url if ok else "setWebhook failed"
            log.info("webhook %s -> %s", "set" if ok else "NOT set", url)
        else:
            STARTUP["webhook"] = "missing RENDER_EXTERNAL_URL or TELEGRAM_WEBHOOK_SECRET"
            log.warning(
                "Cannot auto-register the webhook: %s. Set BOT_MODE=polling to run without it.",
                STARTUP["webhook"],
            )


bootstrap()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    log.info("starting Flask on 0.0.0.0:%s (mode=%s)", port, cfg.bot_mode)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
