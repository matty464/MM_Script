"""Risk checks and kill switches."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque

from .config import BotConfig
from .state import MarketState


class RiskAction(Enum):
    OK = "ok"
    PAUSE = "pause"
    HALT = "halt"


@dataclass
class RiskDecision:
    action: RiskAction
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._order_timestamps: Deque[float] = deque()
        self._paused_until: float = 0.0
        self._halted: bool = False
        self._halt_reason: str = ""

    def _prune_order_timestamps(self, now: float) -> None:
        """Drop submits older than 60s (rolling window for max_orders_per_minute)."""
        cutoff = now - 60.0
        while self._order_timestamps and self._order_timestamps[0] < cutoff:
            self._order_timestamps.popleft()

    def record_order_submit(self) -> None:
        now = time.time()
        self._order_timestamps.append(now)
        self._prune_order_timestamps(now)

    def can_submit_order(self) -> bool:
        # Must prune here too: if we hit the cap and skip submit, record_order_submit
        # is never called and old timestamps would never age out (deadlock).
        self._prune_order_timestamps(time.time())
        return len(self._order_timestamps) < self.cfg.max_orders_per_minute

    def trigger_pause(self, seconds: float, reason: str) -> None:
        self._paused_until = max(self._paused_until, time.time() + seconds)

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def is_halted(self) -> bool:
        return self._halted

    def halt_reason(self) -> str:
        return self._halt_reason

    def evaluate(self, state: MarketState) -> RiskDecision:
        """Run periodic risk checks against current state."""
        if self._halted:
            return RiskDecision(RiskAction.HALT, self._halt_reason)

        if state.book_age() > self.cfg.max_book_age_seconds:
            return RiskDecision(
                RiskAction.PAUSE,
                f"stale book (age={state.book_age():.1f}s > {self.cfg.max_book_age_seconds}s)",
            )

        total_pnl = state.total_pnl_usd()
        if total_pnl <= -abs(self.cfg.max_session_loss_usd):
            self.halt(f"session loss kill switch tripped: pnl={total_pnl:.2f} USD")
            return RiskDecision(RiskAction.HALT, self._halt_reason)

        now = time.time()
        if now < self._paused_until:
            return RiskDecision(
                RiskAction.PAUSE,
                f"paused for {self._paused_until - now:.1f}s after alarm",
            )

        return RiskDecision(RiskAction.OK)
