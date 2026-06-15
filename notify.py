"""Tiny stdlib-only Telegram notifier shared by the bot and the trade monitor.

Configured via TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; a no-op if unset. Kept
dependency-free (urllib) and in its own module so both main.py and
trade_monitor.py can use it without a circular import.
"""
import logging
import os

logger = logging.getLogger(__name__)


def notify_telegram(text: str) -> None:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)
