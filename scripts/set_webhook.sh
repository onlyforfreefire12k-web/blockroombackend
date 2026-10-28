#!/usr/bin/env bash
# Registers the Telegram webhook pointing at the live Render URL.
# Usage:  TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... RENDER_EXTERNAL_URL=... ./set_webhook.sh
# (Prefer AUTO_SET_WEBHOOK=true in render.yaml - this script is a fallback.)
set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET is required}"
: "${RENDER_EXTERNAL_URL:?RENDER_EXTERNAL_URL is required, e.g. https://your-service.onrender.com}"

WEBHOOK_URL="${RENDER_EXTERNAL_URL%/}/webhook/telegram"

echo "Registering webhook -> ${WEBHOOK_URL}"
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WEBHOOK_URL}" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' \
  | python3 -m json.tool

echo "Verifying:"
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
