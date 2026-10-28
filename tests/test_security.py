"""Webhook secret validation and Mini App initData verification."""

import time

import pytest

from conftest import (BOT_TOKEN, SECRET_HEADER, WEBHOOK_SECRET, make_init_data,
                      make_message, make_update, post_update)
from security import InitDataError, verify_init_data


# ---------------------------------------------------------------------------
# Webhook secret
# ---------------------------------------------------------------------------

def test_webhook_rejects_wrong_secret(components, client):
    response = post_update(client, make_update(
        {"message": make_message(text="/start")}), secret="wrong-secret")
    assert response.status_code == 403
    assert components["tg"].sent_messages == []
    assert components["db"].debug_room_ids() == []


def test_webhook_rejects_missing_secret_header(components, client):
    response = client.post("/webhook/telegram",
                           json={"update_id": 1, "message": {}},
                           headers={})  # no secret header at all
    assert response.status_code == 403
    assert components["tg"].sent_messages == []


def test_webhook_without_configured_secret_fails_closed(components, app_factory):
    application = app_factory(cfg_overrides={"telegram_webhook_secret": None})
    response = application.test_client().post("/webhook/telegram",
                                              json={"update_id": 1, "message": {}})
    assert response.status_code == 503


def test_allow_insecure_webhook_only_for_local_dev(components, app_factory):
    application = app_factory(cfg_overrides={
        "telegram_webhook_secret": None, "allow_insecure_webhook": True})
    response = post_update(application.test_client(), make_update(
        {"message": make_message(text="/start")}), secret=None)
    assert response.status_code == 200
    assert components["tg"].sent_messages


# ---------------------------------------------------------------------------
# initData signature verification
# ---------------------------------------------------------------------------

def test_valid_init_data_is_accepted():
    verified = verify_init_data(make_init_data(), BOT_TOKEN)
    assert verified["user"]["id"] == 777000
    assert verified["user"]["display_name"] == "Mini"
    assert verified["user"]["username"] == "miniuser"


def test_tampered_init_data_is_rejected():
    tampered = make_init_data(tamper_hash=True)
    with pytest.raises(InitDataError):
        verify_init_data(tampered, BOT_TOKEN)


def test_init_data_signed_by_wrong_bot_is_rejected():
    with pytest.raises(InitDataError):
        verify_init_data(make_init_data(), "999999:OTHER-BOT-TOKEN")


def test_expired_init_data_is_rejected():
    stale = make_init_data(auth_date=int(time.time()) - 3 * 86400)
    with pytest.raises(InitDataError, match="expired"):
        verify_init_data(stale, BOT_TOKEN, max_age_seconds=86400)


def test_init_data_without_user_is_rejected():
    from conftest import make_init_data as mid
    import hashlib, hmac as hmac_mod, json, urllib.parse
    params = {"auth_date": str(int(time.time())), "query_id": "AAG"}
    encoded = {k: urllib.parse.quote(v, safe="") for k, v in params.items()}
    dcs = "\n".join("{}={}".format(k, encoded[k]) for k in sorted(encoded))
    secret = hmac_mod.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac_mod.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    encoded["hash"] = signature
    init_data = "&".join("{}={}".format(k, encoded[k]) for k in sorted(encoded))
    with pytest.raises(InitDataError):
        verify_init_data(init_data, BOT_TOKEN)


def test_init_data_with_missing_hash_is_rejected():
    with pytest.raises(InitDataError, match="hash"):
        verify_init_data("auth_date=123&user=%7B%7D", BOT_TOKEN)


# ---------------------------------------------------------------------------
# /api/auth/telegram -> Firebase custom token mapping
# ---------------------------------------------------------------------------

def test_auth_endpoint_mints_custom_token_with_claims(components, client, room):
    response = client.post("/api/auth/telegram", json={
        "initData": make_init_data(user={"id": 111, "first_name": "Alice",
                                         "username": "alice"}),
        "roomId": room,
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["firebaseUid"] == "tg_111"
    assert data["token"] == "test-token-for-tg_111"

    call = components["minter_calls"][-1]
    assert call["uid"] == "tg_111"
    assert call["claims"]["telegramId"] == "111"  # string: matches member doc ids in rules
    assert call["claims"]["roomId"] == room       # verified membership
    assert call["claims"]["authProvider"] == "telegram"


def test_auth_endpoint_omits_room_claim_for_non_members(components, client, room):
    response = client.post("/api/auth/telegram", json={
        "initData": make_init_data(user={"id": 999, "first_name": "Eve"}),
        "roomId": room,
    })
    assert response.status_code == 200
    claims = components["minter_calls"][-1]["claims"]
    assert "roomId" not in claims  # Eve is not a verified member


def test_auth_endpoint_rejects_bad_signature(components, client):
    response = client.post("/api/auth/telegram", json={
        "initData": make_init_data(tamper_hash=True)})
    assert response.status_code == 401


def test_auth_endpoint_requires_init_data(components, client):
    response = client.post("/api/auth/telegram", json={})
    assert response.status_code == 400


def test_auth_endpoint_accepts_tma_authorization_header(components, client):
    response = client.post("/api/auth/telegram", json={},
                           headers={"Authorization": "tma " + make_init_data()})
    assert response.status_code == 200


def test_auth_endpoint_rate_limited(components, app_factory):
    application = app_factory(cfg_overrides={"rate_limit_auth_per_minute": 1})
    c = application.test_client()
    payload = {"initData": make_init_data()}
    assert c.post("/api/auth/telegram", json=payload).status_code == 200
    assert c.post("/api/auth/telegram", json=payload).status_code == 429


def test_auth_endpoint_never_echoes_init_data(components, client):
    init_data = make_init_data()
    response = client.post("/api/auth/telegram", json={"initData": init_data})
    body = response.get_data(as_text=True)
    assert init_data not in body
    assert "initData" not in body  # only the error/validation schema, no payload


def test_auth_endpoint_503_without_telegram_token(components, app_factory):
    application = app_factory(cfg_overrides={"telegram_bot_token": None})
    response = application.test_client().post("/api/auth/telegram",
                                              json={"initData": "x"})
    assert response.status_code == 503
