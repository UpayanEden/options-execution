"""Simple Telegram bot alert sender."""

import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

_BOT_TOKEN: Optional[str] = None
_CHAT_ID: Optional[str] = None


def configure(bot_token: str, chat_id: str) -> None:
    """
    Configure Telegram credentials for alert delivery.

    Args:
        bot_token: Telegram bot token (from BotFather).
        chat_id:   Telegram chat or channel ID to send messages to.
    """
    global _BOT_TOKEN, _CHAT_ID
    _BOT_TOKEN = bot_token
    _CHAT_ID = chat_id


def send_alert(message: str) -> bool:
    """
    Send a Telegram message.

    Args:
        message: Text message to send.

    Returns:
        True on success, False on failure or if not configured.
    """
    if not _BOT_TOKEN or not _CHAT_ID:
        logger.warning("alert_not_configured_telegram_skipped", extra={"msg": message[:100]})
        return False
    try:
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": _CHAT_ID, "text": message}, timeout=5)
        if resp.status_code == 200:
            return True
        logger.warning("telegram_send_failed", extra={"status": resp.status_code})
        return False
    except Exception as exc:
        logger.warning("telegram_send_error", extra={"error": str(exc)})
        return False
