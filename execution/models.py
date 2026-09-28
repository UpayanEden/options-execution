"""
Core data models for the options execution system.
All monetary values use Decimal. All datetimes are Asia/Kolkata aware.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


# ── Enums ──────────────────────────────────────────────────────────────────

class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"

class PaperFillMode(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MIDPOINT = "MIDPOINT"
    TOUCH = "TOUCH"

class StrategyType(str, Enum):
    IRON_CONDOR           = "iron_condor"
    BULL_PUT_SPREAD       = "bull_put_spread"
    BEAR_CALL_SPREAD      = "bear_call_spread"
    CALENDAR_SPREAD       = "calendar_spread"
    BROKEN_WING_BUTTERFLY = "broken_wing_butterfly"

class StrategyState(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    WAITING_FOR_QUOTES = "WAITING_FOR_QUOTES"
    READY_TO_ENTER = "READY_TO_ENTER"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    HEDGES_FILLED = "HEDGES_FILLED"
    SHORTS_FILLED = "SHORTS_FILLED"
    ENTRY_COMPLETE = "ENTRY_COMPLETE"
    MONITORING = "MONITORING"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    EXIT_PARTIALLY_FILLED = "EXIT_PARTIALLY_FILLED"
    EXIT_COMPLETE = "EXIT_COMPLETE"
    ABORTED = "ABORTED"
    ERROR = "ERROR"

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"

class ExitTrigger(str, Enum):
    EXPIRY = "EXPIRY"
    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    STRIKE_BREACH = "STRIKE_BREACH"
    BREAKEVEN_BREACH = "BREAKEVEN_BREACH"
    DTE_EXIT = "DTE_EXIT"
    TIME_EXIT = "TIME_EXIT"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"
    RECONCILIATION_FAILURE = "RECONCILIATION_FAILURE"
    QUOTE_FAILURE = "QUOTE_FAILURE"


# ── Config Models ──────────────────────────────────────────────────────────

class LegConfig(BaseModel):
    side: Side
    option_type: OptionType
    strike: Decimal
    expiry: Optional[date] = None   # if None, inherits StrategyConfig.expiry

    @field_validator("strike", mode="before")
    @classmethod
    def coerce_strike(cls, v: object) -> Decimal:
        return Decimal(str(v))


class StrategyConfig(BaseModel):
    name: str
    underlying: str
    exchange: str = "NFO"
    expiry: date
    lots: int = Field(ge=1)
    legs: list[LegConfig] = Field(min_length=2, max_length=4)
    strategy_type: StrategyType = StrategyType.IRON_CONDOR

    @property
    def buy_legs(self) -> list[LegConfig]:
        return [l for l in self.legs if l.side == Side.BUY]

    @property
    def sell_legs(self) -> list[LegConfig]:
        return [l for l in self.legs if l.side == Side.SELL]


class RiskConfig(BaseModel):
    capital_limit: Decimal
    max_strategy_loss: Decimal
    max_daily_loss: Decimal
    min_net_credit: Decimal
    min_ror_pct: Decimal = Decimal("15.0")  # minimum return-on-risk % per spread
    target_net_credit: Decimal
    max_bid_ask_spread_pct: Decimal
    max_total_slippage: Decimal
    quote_staleness_seconds: int
    entry_start_time: str   # "HH:MM" IST
    entry_end_time: str
    exit_start_time: str
    exit_end_time: str
    limit_step: Decimal
    order_wait_seconds: int
    max_repricing_attempts: int
    thin_queue_threshold: int = 500   # qty below this → queue is thin → place aggressively

    @field_validator(
        "capital_limit", "max_strategy_loss", "max_daily_loss",
        "min_net_credit", "min_ror_pct", "target_net_credit", "max_bid_ask_spread_pct",
        "max_total_slippage", "limit_step",
        mode="before",
    )
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class ExitRulesConfig(BaseModel):
    hold_to_expiry: bool = True
    profit_target_pct: Optional[Decimal] = None
    profit_target_inr: Optional[Decimal] = None
    stop_loss_inr: Optional[Decimal] = None
    stop_loss_credit_multiple: Optional[Decimal] = None
    dte_exit: Optional[int] = None
    time_based_exit: Optional[str] = None
    emergency_exit_enabled: bool = False
    manual_approval_required: bool = True

    @field_validator("profit_target_pct", "profit_target_inr", "stop_loss_inr",
                     "stop_loss_credit_multiple", mode="before")
    @classmethod
    def coerce_optional_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        return Decimal(str(v))


class PaperConfig(BaseModel):
    fill_mode: PaperFillMode = PaperFillMode.CONSERVATIVE
    fill_delay_seconds: int = 2
    slippage_per_order: Decimal = Decimal("0.05")

    @field_validator("slippage_per_order", mode="before")
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


# ── Market Data Models ─────────────────────────────────────────────────────

class DepthLevel(BaseModel):
    price: Decimal
    quantity: int
    orders: int = 0

    @field_validator("price", mode="before")
    @classmethod
    def coerce_price(cls, v: object) -> Decimal:
        return Decimal(str(v))


class Quote(BaseModel):
    instrument_token: int
    trading_symbol: str
    exchange: str
    timestamp: datetime
    ltp: Decimal
    bid: Decimal
    bid_qty: int
    ask: Decimal
    ask_qty: int
    volume: int = 0
    oi: int = 0
    depth_bids: list[DepthLevel] = Field(default_factory=list)
    depth_asks: list[DepthLevel] = Field(default_factory=list)

    @field_validator("ltp", "bid", "ask", mode="before")
    @classmethod
    def coerce_price(cls, v: object) -> Decimal:
        return Decimal(str(v))

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        if self.midpoint == 0:
            return Decimal("999")
        return (self.spread / self.midpoint) * Decimal("100")

    def is_stale(self, staleness_seconds: int) -> bool:
        age = (now_ist() - self.timestamp).total_seconds()
        return age > staleness_seconds


# ── Instrument Model ───────────────────────────────────────────────────────

class ResolvedInstrument(BaseModel):
    instrument_token: int
    trading_symbol: str
    exchange: str
    underlying: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    lot_size: int
    tick_size: Decimal

    @field_validator("strike", "tick_size", mode="before")
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


# ── Order and Fill Models ──────────────────────────────────────────────────

class Order(BaseModel):
    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))
    broker_order_id: Optional[str] = None
    strategy_run_id: str
    leg_index: int
    trading_symbol: str
    instrument_token: int
    side: Side
    quantity: int
    limit_price: Decimal
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_fill_price: Optional[Decimal] = None
    submitted_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    reprice_count: int = 0
    initial_bid: Optional[Decimal] = None
    initial_ask: Optional[Decimal] = None
    notes: str = ""

    @field_validator("limit_price", "average_fill_price", "initial_bid", "initial_ask",
                     mode="before")
    @classmethod
    def coerce_optional_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        return Decimal(str(v))

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_complete(self) -> bool:
        return self.status == OrderStatus.COMPLETE

    @property
    def slippage_vs_midpoint(self) -> Optional[Decimal]:
        if self.average_fill_price is None or self.initial_bid is None or self.initial_ask is None:
            return None
        mid = (self.initial_bid + self.initial_ask) / Decimal("2")
        if self.side == Side.BUY:
            return self.average_fill_price - mid
        return mid - self.average_fill_price


class Fill(BaseModel):
    fill_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str
    broker_fill_id: Optional[str] = None
    quantity: int
    price: Decimal
    timestamp: datetime

    @field_validator("price", mode="before")
    @classmethod
    def coerce_price(cls, v: object) -> Decimal:
        return Decimal(str(v))


# ── Position and P&L ──────────────────────────────────────────────────────

class LegPosition(BaseModel):
    leg_index: int
    trading_symbol: str
    instrument_token: int
    side: Side
    option_type: OptionType
    strike: Decimal
    lot_size: int
    lots: int
    quantity: int        # lots × lot_size
    average_entry_price: Decimal
    current_bid: Optional[Decimal] = None
    current_ask: Optional[Decimal] = None
    current_ltp: Optional[Decimal] = None

    @field_validator("strike", "average_entry_price", "current_bid", "current_ask",
                     "current_ltp", mode="before")
    @classmethod
    def coerce_optional_decimal(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        return Decimal(str(v))

    @property
    def conservative_close_price(self) -> Optional[Decimal]:
        """Price to close this leg conservatively (worst for us)."""
        if self.side == Side.SELL:
            return self.current_ask  # buying back: pay ask
        return self.current_bid      # selling out: receive bid


class StrategyPosition(BaseModel):
    strategy_run_id: str
    strategy_name: str
    underlying: str
    expiry: date
    legs: list[LegPosition] = Field(default_factory=list)
    state: StrategyState = StrategyState.CREATED
    entry_credit: Optional[Decimal] = None      # actual net credit received at entry
    cost_to_close: Optional[Decimal] = None     # conservative cost to close now
    unrealized_pnl: Optional[Decimal] = None
    realized_pnl: Decimal = Decimal("0")
    entry_timestamp: Optional[datetime] = None
    last_updated: Optional[datetime] = None
    exit_trigger: Optional[ExitTrigger] = None
    exit_timestamp: Optional[datetime] = None
    exit_debit: Optional[Decimal] = None
    final_pnl: Optional[Decimal] = None


# ── Alert Model ────────────────────────────────────────────────────────────

class Alert(BaseModel):
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_run_id: Optional[str] = None
    level: AlertLevel
    event: str
    message: str
    timestamp: datetime = Field(default_factory=now_ist)
    acknowledged: bool = False
