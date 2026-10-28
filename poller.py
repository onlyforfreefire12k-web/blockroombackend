"""Long-polling worker.

Use this when the app runs as a single plain Python process (`python live.py`)
instead of gunicorn + webhook. No public HTTPS URL, no setWebhook step, no
secret header — the bot simply asks Telegram for updates.

BOT_MODE:
  webhook  (default) register the webhook and serve /webhook/telegram
  polling  delete any webhook and read updates in a background thread
  off      do nothing automatically (manual /telegram/set-webhook)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("blockroom.poller")


class Poller:
    def __init__(self, tg, bot):
        self.tg = tg
        self.bot = bot
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.offset: Optional[int] = None
        self.updates = 0
        self.errors = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-poller", daemon=True)
        self._thread.start()
        log.info("long-polling started")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # a webhook and getUpdates are mutually exclusive
        self.tg.delete_webhook()
        while not self._stop.is_set():
            try:
                batch = self.tg.get_updates(self.offset)
                if not batch:
                    continue
                for update in batch:
                    self.offset = int(update.get("update_id", 0)) + 1
                    try:
                        self.bot.handle(update)
                        self.updates += 1
                    except Exception:
                        self.errors += 1
                        log.exception("polling handler failed")
            except Exception:
                self.errors += 1
                log.exception("polling loop error")
                time.sleep(2)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
