"""
Notification system for trade signals and entry opportunities.

Channels:
  DesktopNotifier  — notify-send (Linux) / osascript (macOS)
  TelegramNotifier — Telegram Bot API (optional; needs bot_token + chat_id)
  FileNotifier     — writes to logs/signals.jsonl (always on; dashboard polls this)

Usage in AutoTrader:
    notifier = Notifier.from_config(cfg_dict)
    notifier.send_opportunity(underlying, candidate, opp_score, timing_score)
    notifier.send_exit(underlying, trigger, pnl)
"""
from __future__ import annotations

import json
import logging
import platform
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

SIGNALS_FILE = Path("logs/signals.jsonl")


# ── Signal record (always written to file for dashboard) ─────────────────────

@dataclass
class TradeSignal:
    signal_id: str
    signal_time: str               # ISO8601
    signal_type: str               # "ENTRY_OPPORTUNITY" | "ENTRY_BLOCKED" | "EXIT"
    underlying: str
    composite_score: float
    grade: str
    action: str                    # "STRONG_BUY" | "BUY" | "HOLD" | "SKIP"
    iv_rank: float
    vix: Optional[float]
    timing_window: str
    timing_score: float
    credit: Optional[float]
    ror: Optional[float]
    reason: str
    acted_on: bool = False
    strategy_type: str = ""   # e.g. "iron_condor", "bull_put_spread", etc.


def _write_signal(signal: TradeSignal) -> None:
    try:
        SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SIGNALS_FILE.open("a") as f:
            f.write(json.dumps(asdict(signal)) + "\n")
    except Exception as exc:
        logger.warning("signal_write_failed", extra={"error": str(exc)})


def read_recent_signals(n: int = 50) -> list[dict]:
    """Read the last n signals from the file (newest first)."""
    if not SIGNALS_FILE.exists():
        return []
    try:
        lines = SIGNALS_FILE.read_text().splitlines()
        rows  = []
        for line in reversed(lines):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            if len(rows) >= n:
                break
        return rows
    except Exception:
        return []


def mark_signal_acted_on(signal_id: str) -> None:
    """Rewrite the signals file marking one signal as acted on."""
    if not SIGNALS_FILE.exists():
        return
    try:
        lines = SIGNALS_FILE.read_text().splitlines()
        updated = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("signal_id") == signal_id:
                    d["acted_on"] = True
                updated.append(json.dumps(d))
            except json.JSONDecodeError:
                updated.append(line)
        SIGNALS_FILE.write_text("\n".join(updated) + "\n")
    except Exception as exc:
        logger.warning("signal_mark_failed", extra={"error": str(exc)})


# ── Channel interface ─────────────────────────────────────────────────────────

class NotificationChannel(ABC):
    @abstractmethod
    def notify(self, title: str, body: str, urgency: str = "normal") -> None:
        pass


class DesktopNotifier(NotificationChannel):
    """
    Desktop notification via notify-send (Linux) or osascript (macOS).
    Silent failure if tool not available.
    """

    def notify(self, title: str, body: str, urgency: str = "normal") -> None:
        try:
            sys_platform = platform.system()
            if sys_platform == "Linux":
                subprocess.Popen(
                    ["notify-send", "--urgency", urgency,
                     "--icon", "dialog-information",
                     "--app-name", "AutoTrader",
                     title, body],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif sys_platform == "Darwin":
                script = (
                    f'display notification "{body}" '
                    f'with title "{title}" '
                    f'subtitle "AutoTrader"'
                )
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except FileNotFoundError:
            pass   # notify-send not installed — silent skip
        except Exception as exc:
            logger.debug("desktop_notify_error", extra={"error": str(exc)})


class TelegramNotifier(NotificationChannel):
    """
    Telegram Bot API notifications.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars (or direct init).
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token   = bot_token
        self._chat_id = chat_id

    def notify(self, title: str, body: str, urgency: str = "normal") -> None:
        try:
            import urllib.request
            icon = {"critical": "🚨", "normal": "📊", "low": "ℹ️"}.get(urgency, "📊")
            text = f"{icon} *{title}*\n{body}"
            payload = json.dumps({
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }).encode()
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            logger.warning("telegram_notify_error", extra={"error": str(exc)})


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """
    Central notification dispatcher. Always writes to signals file.
    Optionally sends desktop + Telegram notifications.
    """

    def __init__(
        self,
        channels: Optional[list[NotificationChannel]] = None,
        min_score_to_notify: float = 60.0,
    ) -> None:
        self._channels           = channels or []
        self._min_score          = min_score_to_notify

    @classmethod
    def from_config(cls, notif_cfg: dict) -> "Notifier":
        """Build Notifier from auto_trader.yaml notifications section."""
        channels: list[NotificationChannel] = []

        if notif_cfg.get("desktop", True):
            channels.append(DesktopNotifier())

        tg = notif_cfg.get("telegram", {})
        if tg.get("enabled", False):
            token = tg.get("bot_token", "")
            cid   = tg.get("chat_id", "")
            if token and cid:
                channels.append(TelegramNotifier(token, cid))

        min_score = float(notif_cfg.get("min_score_to_notify", 60.0))
        return cls(channels=channels, min_score_to_notify=min_score)

    def _broadcast(self, title: str, body: str, urgency: str = "normal") -> None:
        for ch in self._channels:
            try:
                ch.notify(title, body, urgency)
            except Exception as exc:
                logger.debug("notify_channel_error", extra={"error": str(exc)})

    def send_opportunity(
        self,
        underlying: str,
        composite_score: float,
        grade: str,
        action: str,
        iv_rank: float,
        vix: Optional[float],
        timing_window: str,
        timing_score: float,
        credit: Optional[float],
        ror: Optional[float],
        reason: str,
        strategy_type: str = "",
    ) -> TradeSignal:
        import uuid as _uuid

        now   = datetime.now(IST)
        sig   = TradeSignal(
            signal_id       = str(_uuid.uuid4())[:16],
            signal_time     = now.isoformat(),
            signal_type     = "ENTRY_OPPORTUNITY",
            underlying      = underlying,
            composite_score = composite_score,
            grade           = grade,
            action          = action,
            iv_rank         = iv_rank,
            vix             = vix,
            timing_window   = timing_window,
            timing_score    = timing_score,
            credit          = credit,
            ror             = ror,
            reason          = reason,
            strategy_type   = strategy_type,
        )
        _write_signal(sig)

        if composite_score >= self._min_score:
            strat_label = strategy_type.replace("_", " ").title() if strategy_type else ""
            title = f"Trade Signal [{grade}] — {underlying}" + (f" ({strat_label})" if strat_label else "")
            credit_str = f"{credit:.1f} pts" if credit else "?"
            ror_str    = f"{ror:.1f}%" if ror else "?"
            vix_str    = f"VIX {vix:.1f}" if vix else ""
            body = (
                f"Action: {action}\n"
                f"Score: {composite_score:.0f}/100 | IV Rank: {iv_rank:.0f}\n"
                f"Credit: {credit_str} | RoR: {ror_str}\n"
                f"{vix_str} | Window: {timing_window}\n"
                f"Reason: {reason}"
            )
            urgency = "critical" if grade == "A" else "normal"
            self._broadcast(title, body, urgency)
            logger.info("signal_sent", extra={
                "underlying": underlying, "score": composite_score,
                "grade": grade, "action": action,
            })

        return sig

    def send_exit(
        self,
        underlying: str,
        trigger: str,
        pnl: Optional[float],
        strategy_name: str = "",
    ) -> None:
        now  = datetime.now(IST)
        pnl_str = f"₹{pnl:+,.0f}" if pnl is not None else "?"
        sig  = TradeSignal(
            signal_id       = str(__import__("uuid").uuid4())[:16],
            signal_time     = now.isoformat(),
            signal_type     = "EXIT",
            underlying      = underlying,
            composite_score = 0.0,
            grade           = "-",
            action          = "EXIT",
            iv_rank         = 0.0,
            vix             = None,
            timing_window   = "-",
            timing_score    = 0.0,
            credit          = None,
            ror             = None,
            reason          = f"Exit trigger: {trigger} | P&L: {pnl_str}",
        )
        _write_signal(sig)
        title = f"Exit Executed — {underlying}"
        body  = f"Strategy: {strategy_name}\nTrigger: {trigger}\nP&L: {pnl_str}" if strategy_name else f"Trigger: {trigger}\nP&L: {pnl_str}"
        self._broadcast(title, body, urgency="normal")

    def send_blocked(
        self,
        underlying: str,
        reason: str,
        composite_score: float,
    ) -> None:
        now = datetime.now(IST)
        sig = TradeSignal(
            signal_id       = str(__import__("uuid").uuid4())[:16],
            signal_time     = now.isoformat(),
            signal_type     = "ENTRY_BLOCKED",
            underlying      = underlying,
            composite_score = composite_score,
            grade           = "F",
            action          = "SKIP",
            iv_rank         = 0.0,
            vix             = None,
            timing_window   = "-",
            timing_score    = 0.0,
            credit          = None,
            ror             = None,
            reason          = reason,
        )
        _write_signal(sig)
        logger.info("entry_blocked", extra={
            "underlying": underlying, "reason": reason,
        })
