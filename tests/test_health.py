"""Health endpoint, index, and secret-hygiene checks."""

from conftest import BOT_TOKEN, WEBHOOK_SECRET, YOUTUBE_KEY, post_update, make_update, make_message, SECRET_HEADER


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["checks"]["telegram"] is True
    assert data["checks"]["database"] is True
    assert data["checks"]["miniAppConfigured"] is True
    assert data["checks"]["youtubeConfigured"] is True


def test_health_and_index_never_leak_secrets(client):
    for path in ("/health", "/"):
        response = client.get(path)
        body = response.get_data(as_text=True)
        assert BOT_TOKEN not in body
        assert WEBHOOK_SECRET not in body
        assert YOUTUBE_KEY not in body


def test_index_lists_endpoints(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "webhook/telegram" in response.get_data(as_text=True)


def test_health_reports_degradation_without_config(components, app_factory):
    application = app_factory(cfg_overrides={
        "telegram_bot_token": None,
        "mini_app_url": None,
        "youtube_api_key": None,
    }, services={"telegram": None})
    response = application.test_client().get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["checks"]["telegram"] is False
    assert data["checks"]["miniAppConfigured"] is False
    assert any("TELEGRAM_BOT_TOKEN" in reason for reason in data["degraded"])


def test_webhook_unavailable_without_telegram_token(components, app_factory):
    application = app_factory(cfg_overrides={"telegram_bot_token": None},
                              services={"telegram": None})
    response = post_update(application.test_client(), make_update(
        {"message": make_message(text="/start")}))
    assert response.status_code == 503


def test_invalid_json_body_rejected(client):
    response = client.post("/webhook/telegram", data="{not-json",
                           content_type="application/json",
                           headers={SECRET_HEADER: WEBHOOK_SECRET})
    assert response.status_code == 400
