"""
Alert system. Console + log output initially.
Telegram channel fires on CRITICAL/EMERGENCY when configured.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from execution.models import Alert, AlertLevel

logger = logging.getLogger(__name__)


class AlertChannel(ABC):
    @abstractmethod
    def send(self, alert: Alert) -> None:
        pass


class ConsoleAlertChannel(AlertChannel):
    _ICONS = {
        AlertLevel.INFO: "[INFO]",
        AlertLevel.WARNING: "[WARN]",
        AlertLevel.CRITICAL: "[CRIT]",
        AlertLevel.EMERGENCY: "[EMERGENCY]",
    }

    def send(self, alert: Alert) -> None:
        icon = self._ICONS.get(alert.level, "[????]")
        ts = alert.timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
        print(f"\n{icon} {ts} | {alert.event}")
        print(f"  {alert.message}")


class LogAlertChannel(AlertChannel):
    def send(self, alert: Alert) -> None:
        level_map = {
            AlertLevel.INFO: logging.INFO,
            AlertLevel.WARNING: logging.WARNING,
            AlertLevel.CRITICAL: logging.CRITICAL,
            AlertLevel.EMERGENCY: logging.CRITICAL,
        }
        log_level = level_map.get(alert.level, logging.INFO)
        logger.log(
            log_level,
            "alert",
            extra={
                "event": alert.event,
                "alert_message": alert.message,
                "strategy_id": alert.strategy_run_id,
                "level": alert.level.value,
            },
        )


class TelegramAlertChannel(AlertChannel):
    """Sends alerts to a Telegram chat via Bot API. Only fires at or above min_level."""

    _LEVEL_ORDER = [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.CRITICAL, AlertLevel.EMERGENCY]
    _ICONS = {
        AlertLevel.INFO: "ℹ️",
        AlertLevel.WARNING: "⚠️",
        AlertLevel.CRITICAL: "🚨",
        AlertLevel.EMERGENCY: "🆘",
    }

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        min_level: AlertLevel = AlertLevel.CRITICAL,
    ) -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._min_level = min_level

    def send(self, alert: Alert) -> None:
        if self._LEVEL_ORDER.index(alert.level) < self._LEVEL_ORDER.index(self._min_level):
            return
        icon = self._ICONS.get(alert.level, "")
        ts = alert.timestamp.strftime("%H:%M:%S %Z")
        text = f"{icon} *{alert.level.value}* | `{alert.event}`\n{alert.message}\n_{ts}_"
        try:
            import requests
            resp = requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=5,
            )
            if not resp.ok:
                logger.warning("telegram_send_failed", extra={"status": resp.status_code, "body": resp.text[:200]})
        except Exception as exc:
            logger.error("telegram_send_error", extra={"error": str(exc)})


class AlertManager:
    """Dispatches alerts to all registered channels and optionally persists them."""

    def __init__(
        self,
        channels: Optional[list[AlertChannel]] = None,
        repository: Optional[object] = None,
    ) -> None:
        self._channels: list[AlertChannel] = channels or [
            ConsoleAlertChannel(),
            LogAlertChannel(),
        ]
        self._repo = repository

    def send(
        self,
        level: AlertLevel,
        event: str,
        message: str,
        strategy_run_id: Optional[str] = None,
    ) -> Alert:
        alert = Alert(
            level=level,
            event=event,
            message=message,
            strategy_run_id=strategy_run_id,
        )
        for channel in self._channels:
            try:
                channel.send(alert)
            except Exception as exc:
                logger.error("alert_channel_error", extra={"error": str(exc)})
        if self._repo is not None:
            try:
                self._repo.save(alert)
            except Exception as exc:
                logger.error("alert_persist_error", extra={"error": str(exc)})
        return alert

    # Convenience methods
    def info(self, event: str, message: str, strategy_run_id: Optional[str] = None) -> Alert:
        return self.send(AlertLevel.INFO, event, message, strategy_run_id)

    def warning(self, event: str, message: str, strategy_run_id: Optional[str] = None) -> Alert:
        return self.send(AlertLevel.WARNING, event, message, strategy_run_id)

    def critical(self, event: str, message: str, strategy_run_id: Optional[str] = None) -> Alert:
        return self.send(AlertLevel.CRITICAL, event, message, strategy_run_id)

    def emergency(self, event: str, message: str, strategy_run_id: Optional[str] = None) -> Alert:
        return self.send(AlertLevel.EMERGENCY, event, message, strategy_run_id)
