"""Blockroom Telegram bot - Flask entry point.

Production (Render):   gunicorn live:app --workers 2 --threads 8 --bind 0.0.0.0:$PORT
Local development:     python live.py   (then expose via HTTPS for webhooks)

Routes
------
GET  /health                 health check for Render (never fails on missing config)
GET  /                       service index
POST /webhook/telegram       Telegram updates (secret-token protected)
POST /api/auth/telegram      Mini App initData -> Firebase custom token (CORS enabled)

The bot runs in webhook mode only; there is no polling process.
"""

import logging
import os
import threading
from collections import OrderedDict

from flask import Flask, jsonify, request

import handlers
from config import load_config
from db import DatabaseError, create_adapter
from handlers import PendingStore
from media import MediaService
from rooms import RoomService, validate_room_id  # noqa: F401  (validate_room_id re-exported for tests)
from security import (
    InitDataError,
    RateLimiter,
    verify_init_data,
    verify_webhook_secret_header,
)
from telegram_api import TelegramClient, TelegramAPIError
from timeutil import utc_now_iso
from youtube_api import YouTubeClient

log = logging.getLogger("blockroom.live")

_WEBHOOK_SETUP_LOCK = threading.Lock()
_WEBHOOK_SETUP_DONE = False


class BlockroomState:
    """Service locator shared by routes and handlers."""

    def __init__(self, cfg, db, tg, rooms, media, yt, limiter, pending,
                 token_minter):
        self.cfg = cfg
        self.db = db
        self.tg = tg
        self.rooms = rooms
        self.media = media
        self.yt = yt
        self.limiter = limiter
        self.pending = pending
        self.token_minter = token_minter
        self.bot_username = None
        self.bot_display_name = "Blockroom Bot"
        self.bot_identity_lock = threading.Lock()
        self._seen_lock = threading.Lock()
        self._seen_updates = OrderedDict()

    def remember_update(self, update_id: int) -> bool:
        """Fast in-process duplicate check.  True = already seen."""
        with self._seen_lock:
            key = str(update_id)
            if key in self._seen_updates:
                self._seen_updates.move_to_end(key)
                return True
            self._seen_updates[key] = True
            while len(self._seen_updates) > 5000:
                self._seen_updates.popitem(last=False)
            return False


def _default_token_minter(uid: str, claims: dict) -> str:
    """Mint a Firebase custom token (lazy import keeps tests Firebase-free)."""
    from firebase_admin import auth  # imported lazily

    token = auth.create_custom_token(uid, claims)
    return token.decode("utf-8") if isinstance(token, (bytes, bytearray)) else token


def _resolve_database(cfg, injected):
    if injected is not None:
        return injected
    if cfg.testing:
        return None
    try:
        return create_adapter(cfg)
    except DatabaseError as exc:
        log.error("Firebase unavailable at startup: %s", exc)
        return None


def create_app(cfg=None, db_adapter=None, telegram=None, youtube=None,
               token_minter=None) -> Flask:
    """Application factory.  Tests inject fakes; production wires real clients."""
    cfg = cfg or load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # 1 MB request cap
    app.config["TESTING"] = bool(cfg.testing)

    db = _resolve_database(cfg, db_adapter)
    tg = telegram if telegram is not None else (
        TelegramClient(cfg.telegram_bot_token) if cfg.telegram_bot_token else None)
    yt = youtube if youtube is not None else (
        YouTubeClient(cfg.youtube_api_key) if cfg.youtube_api_key else None)

    state = BlockroomState(
        cfg=cfg,
        db=db,
        tg=tg,
        rooms=RoomService(db) if db is not None else None,
        media=MediaService(db) if db is not None else None,
        yt=yt,
        limiter=RateLimiter(),
        pending=PendingStore(),
        token_minter=token_minter or _default_token_minter,
    )
    app.extensions["blockroom"] = state
    _register_routes(app, state)
    _maybe_setup_webhook(state)
    return app


def _register_routes(app: Flask, state: BlockroomState):

    @app.get("/")
    def index():
        return jsonify({
            "service": "blockroom-telegram-bot",
            "status": "ok",
            "endpoints": ["GET /health", "POST /webhook/telegram",
                          "POST /api/auth/telegram"],
        })

    @app.get("/health")
    def health():
        cfg = state.cfg
        telegram_ok = state.tg is not None
        db_ok = False
        if state.db is not None:
            try:
                db_ok = bool(state.db.health_check())
            except Exception:
                db_ok = False
        reasons = []
        if not telegram_ok:
            reasons.append("TELEGRAM_BOT_TOKEN missing")
        if state.db is None:
            reasons.append("Firebase credentials missing or invalid")
        if not cfg.mini_app_url:
            reasons.append("MINI_APP_URL missing")
        if not cfg.youtube_api_key:
            reasons.append("YOUTUBE_API_KEY missing")
        return jsonify({
            "status": "ok",
            "service": "blockroom-telegram-bot",
            "time": utc_now_iso(),
            "checks": {
                "telegram": telegram_ok,
                "database": db_ok,
                "databaseBackend": (state.cfg.firebase_backend
                                    if state.db is not None else "unconfigured"),
                "miniAppConfigured": bool(cfg.mini_app_url),
                "youtubeConfigured": bool(cfg.youtube_api_key),
                "webhookSecretConfigured": bool(cfg.telegram_webhook_secret),
            },
            "degraded": reasons,
        })

    @app.post("/webhook/telegram")
    def telegram_webhook():
        cfg = state.cfg

        # 1) Authenticate the caller before parsing anything.
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if cfg.telegram_webhook_secret:
            if not verify_webhook_secret_header(header, cfg.telegram_webhook_secret):
                log.warning("Webhook rejected: invalid secret token")
                return jsonify({"ok": False, "error": "invalid secret token"}), 403
        elif not cfg.allow_insecure_webhook:
            return jsonify({"ok": False,
                            "error": "TELEGRAM_WEBHOOK_SECRET is not configured"}), 503

        if state.tg is None:
            return jsonify({"ok": False,
                            "error": "TELEGRAM_BOT_TOKEN is not configured"}), 503

        # 2) Validate the payload shape.
        update = request.get_json(silent=True, force=True)
        if not isinstance(update, dict):
            return jsonify({"ok": False, "error": "invalid update payload"}), 400
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return jsonify({"ok": False, "error": "update_id is required"}), 400

        # 3) De-duplicate deliveries (Telegram retries on network blips).
        #    In-memory first, then an atomic claim in the database so that
        #    concurrent workers can never process the same update twice -
        #    this is what guarantees duplicate /room deliveries cannot create
        #    unintended duplicate rooms.
        if state.remember_update(update_id):
            return jsonify({"ok": True, "duplicate": True})
        if state.db is not None:
            try:
                claimed = state.db.mark_update_processed(update_id)
                if not claimed:
                    return jsonify({"ok": True, "duplicate": True})
            except DatabaseError:
                log.warning("Dedupe store unavailable; relying on in-process dedupe")

        # 4) Process.  Handlers never raise; unexpected failures are logged
        #    (secret-free) and answered with ok:false so Telegram may retry.
        try:
            outcome = handlers.dispatch_update(state, update)
        except Exception:
            log.exception("Unhandled failure while processing update %s", update_id)
            return jsonify({"ok": False, "error": "internal error"}), 200
        return jsonify({"ok": True, "handled": outcome})

    @app.route("/api/auth/telegram", methods=["OPTIONS"])
    def auth_options():  # CORS preflight for the Mini App frontend
        return ("", 204)

    @app.post("/api/auth/telegram")
    def auth_telegram():
        """Exchange verified Mini App initData for a Firebase custom token.

        Request:  {"initData": "<raw initData>", "roomId": "optional-room-id"}
        Response: {"ok": true, "firebaseUid": "tg_<id>", "token": "<custom token>",
                   "user": {"id", "displayName", "username", "photoUrl"}}

        The frontend then calls signInWithCustomToken(auth, token).  Firebase
        Security Rules can trust request.auth.token.telegramId afterwards.
        Only signed initData is trusted here - never plain user ids from the
        browser.
        """
        cfg = state.cfg
        if cfg.telegram_bot_token is None:
            return jsonify({"ok": False,
                            "error": "Telegram bot token is not configured"}), 503

        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown")
        ip = ip.split(",")[0].strip()
        if not state.limiter.allow("auth:{}".format(ip),
                                   cfg.rate_limit_auth_per_minute, 60):
            return jsonify({"ok": False, "error": "too many requests"}), 429

        body = request.get_json(silent=True) or {}
        init_data = body.get("initData")
        if not init_data:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("tma "):
                init_data = auth_header[4:].strip()
        if not init_data or not isinstance(init_data, str):
            return jsonify({"ok": False, "error": "initData is required"}), 400

        try:
            verified = verify_init_data(init_data, cfg.telegram_bot_token,
                                        cfg.max_init_data_age_seconds)
        except InitDataError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 401

        tg_user = verified["user"]
        uid = "tg_{}".format(tg_user["id"])
        claims = {
            "telegramId": str(tg_user["id"]),
            "displayName": tg_user.get("display_name") or "",
            "username": tg_user.get("username") or "",
            "authProvider": "telegram",
        }

        # Optionally embed the room membership claim (verified server-side).
        room_id = body.get("roomId")
        if isinstance(room_id, str) and validate_room_id(room_id) and state.db is not None:
            try:
                member = state.db.get_room_member(room_id, tg_user["id"])
                if member:
                    claims["roomId"] = room_id
            except DatabaseError:
                log.warning("Membership lookup failed for room %s; claim omitted", room_id)

        try:
            token = state.token_minter(uid, claims)
        except Exception:
            log.exception("Custom token minting failed")
            return jsonify({"ok": False, "error": "Firebase Auth is not available"}), 503

        return jsonify({
            "ok": True,
            "firebaseUid": uid,
            "token": token,
            "user": {
                "id": tg_user["id"],
                "displayName": claims["displayName"],
                "username": claims["username"],
                "photoUrl": tg_user.get("photo_url") or "",
            },
        })

    @app.after_request
    def _cors_headers(response):
        # The Mini App frontend calls /api/* from inside Telegram's webview,
        # so permissive CORS is required.  No cookies are used; the payload is
        # self-contained and every request is independently verified.
        if request.path.startswith("/api/"):
            response.headers.setdefault("Access-Control-Allow-Origin", "*")
            response.headers.setdefault(
                "Access-Control-Allow-Headers", "Content-Type, Authorization")
            response.headers.setdefault(
                "Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        return response


def _maybe_setup_webhook(state: BlockroomState):
    """Optionally register the webhook once at boot (AUTO_SET_WEBHOOK=true)."""
    global _WEBHOOK_SETUP_DONE
    cfg = state.cfg
    if cfg.testing or not cfg.auto_set_webhook or _WEBHOOK_SETUP_DONE:
        return
    with _WEBHOOK_SETUP_LOCK:
        if _WEBHOOK_SETUP_DONE:
            return
        if not (cfg.telegram_bot_token and cfg.telegram_webhook_secret
                and cfg.render_external_url and state.tg is not None):
            log.warning("AUTO_SET_WEBHOOK is enabled but the configuration is "
                        "incomplete; webhook not registered")
            return
        url = cfg.render_external_url.rstrip("/") + "/webhook/telegram"
        try:
            state.tg.set_webhook(url, secret_token=cfg.telegram_webhook_secret)
            _WEBHOOK_SETUP_DONE = True
            log.info("Telegram webhook registered at %s", url)
        except TelegramAPIError as exc:
            log.error("Could not register the Telegram webhook: %s", exc)


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
