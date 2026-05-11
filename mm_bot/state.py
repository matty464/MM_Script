"""Live mutable state: order book, position, fills, rolling volatility."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

# Cap how many recent fills / ticks we keep in memory for the dashboard.
MAX_FILL_HISTORY = 500
MAX_TICK_HISTORY = 600    # ~10 min at 1s loop
MAX_ML_HISTORY = 600      # ~10 min at 1s loop — tracks weight/accuracy evolution


@dataclass
class BookSnapshot:
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float
    ts: float

    def mid(self) -> float:
        return 0.5 * (self.bid_px + self.ask_px)

    def spread_bps(self) -> float:
        m = self.mid()
        if m <= 0:
            return 0.0
        return 1e4 * (self.ask_px - self.bid_px) / m


@dataclass
class Position:
    """Net position in base units (positive = long)."""

    size: float = 0.0
    avg_entry_px: float = 0.0

    def notional(self, mark_px: float) -> float:
        return self.size * mark_px

    def signed_notional(self, mark_px: float) -> float:
        return self.size * mark_px

    def apply_fill(self, side_is_buy: bool, fill_sz: float, fill_px: float) -> float:
        """Apply a fill, return realized PnL produced by this fill (USD).

        Positive size = long. Realized PnL is generated when the fill reduces
        or flips the existing position.
        """
        signed_sz = fill_sz if side_is_buy else -fill_sz
        new_size = self.size + signed_sz
        realized = 0.0

        if self.size == 0 or (self.size > 0) == (signed_sz > 0):
            new_avg = (
                (self.avg_entry_px * self.size + fill_px * signed_sz) / new_size
                if new_size != 0
                else 0.0
            )
            self.avg_entry_px = new_avg
        else:
            closing_sz = min(abs(self.size), abs(signed_sz))
            direction = 1.0 if self.size > 0 else -1.0
            realized = (fill_px - self.avg_entry_px) * closing_sz * direction
            if abs(signed_sz) > abs(self.size):
                self.avg_entry_px = fill_px
            elif new_size == 0:
                self.avg_entry_px = 0.0

        self.size = new_size
        if self.size == 0:
            self.avg_entry_px = 0.0
        return realized


@dataclass
class RollingVolEstimator:
    """Estimate annualized realized volatility from a rolling window of mid log-returns.

    We treat 1 year ~= 365 * 24 * 3600 seconds for annualization, which is the
    standard crypto convention.

    Raw window sigma is then passed through an **asymmetric EWMA**: faster when
    vol rises, slower when it falls (so spikes decay gradually).
    """

    window_seconds: float
    sigma_ewma_alpha_up: float = 0.30
    sigma_ewma_alpha_down: float = 0.07
    samples: Deque[Tuple[float, float]] = field(default_factory=deque)
    _sigma_smooth: float = field(default=0.0, repr=False)
    _sigma_smooth_init: bool = field(default=False, repr=False)

    def update(self, ts: float, mid: float) -> None:
        if mid <= 0:
            return
        self.samples.append((ts, math.log(mid)))
        cutoff = ts - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def realized_vol_bps_per_sec(self) -> float:
        """Stdev of log returns per second, expressed in basis points."""
        if len(self.samples) < 3:
            return 0.0
        rets = []
        prev_t, prev_v = self.samples[0]
        for t, v in list(self.samples)[1:]:
            dt = max(t - prev_t, 1e-6)
            rets.append((v - prev_v) / math.sqrt(dt))
            prev_t, prev_v = t, v
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        sigma_per_sqrt_sec = math.sqrt(var)
        return sigma_per_sqrt_sec * 1e4

    def smoothed_sigma_bps_per_sec(self) -> float:
        """EWMA of raw window sigma: quick reaction up, gradual cooldown."""
        raw = self.realized_vol_bps_per_sec()
        if not self._sigma_smooth_init:
            self._sigma_smooth = raw
            self._sigma_smooth_init = True
            return raw
        a = self.sigma_ewma_alpha_up if raw > self._sigma_smooth else self.sigma_ewma_alpha_down
        self._sigma_smooth = a * raw + (1.0 - a) * self._sigma_smooth
        return self._sigma_smooth


@dataclass
class FillRecord:
    ts: float
    side_is_buy: bool
    sz: float
    px: float
    realized_pnl: float
    position_after: float


@dataclass
class TickRecord:
    ts: float
    mid: float
    sigma_bps_per_sec: float
    position: float
    realized_pnl: float
    unrealized_pnl: float


@dataclass
class MLHistoryRecord:
    """Snapshot of the ML model's evolving state at a point in time.

    Used by the dashboard to chart weight/accuracy adaptation."""
    ts: float
    n_updates: int
    mae_bps: float
    correlation: float
    last_signal_bps: float
    weights: Dict[str, float]


class MarketState:
    """Thread-safe container for everything the strategy reads/writes."""

    def __init__(
        self,
        vol_window_seconds: float,
        sigma_ewma_alpha_up: float = 0.30,
        sigma_ewma_alpha_down: float = 0.07,
    ):
        self._lock = threading.RLock()
        self._book: Optional[BookSnapshot] = None
        self._raw_levels: Optional[List] = None   # full L2 depth from WS
        self._vol = RollingVolEstimator(
            window_seconds=vol_window_seconds,
            sigma_ewma_alpha_up=sigma_ewma_alpha_up,
            sigma_ewma_alpha_down=sigma_ewma_alpha_down,
        )
        self.position = Position()
        self.realized_pnl_usd = 0.0
        self.fills_count = 0
        self._last_fill_ts: float = 0.0
        self.start_ts = time.time()
        self.fills: Deque[FillRecord] = deque(maxlen=MAX_FILL_HISTORY)
        self.ticks: Deque[TickRecord] = deque(maxlen=MAX_TICK_HISTORY)
        self.ml_history: Deque[MLHistoryRecord] = deque(maxlen=MAX_ML_HISTORY)

    def update_book(self, book: BookSnapshot, raw_levels: Optional[List] = None) -> None:
        with self._lock:
            self._book = book
            if raw_levels is not None:
                self._raw_levels = raw_levels
            self._vol.update(book.ts, book.mid())

    def book(self) -> Optional[BookSnapshot]:
        with self._lock:
            return self._book

    def vol_bps_per_sec(self) -> float:
        """Smoothed sigma used for quoting and dashboard (EWMA of rolling-window vol)."""
        with self._lock:
            return self._vol.smoothed_sigma_bps_per_sec()

    def vol_raw_bps_per_sec(self) -> float:
        """Unsmoothed rolling-window sigma (diagnostics)."""
        with self._lock:
            return self._vol.realized_vol_bps_per_sec()

    def book_age(self, now: Optional[float] = None) -> float:
        with self._lock:
            if self._book is None:
                return float("inf")
            return (now if now is not None else time.time()) - self._book.ts

    def record_fill(self, side_is_buy: bool, sz: float, px: float) -> float:
        """Apply fill to position. Returns realized PnL from this fill (USD)."""
        with self._lock:
            realized = self.position.apply_fill(side_is_buy, sz, px)
            self.realized_pnl_usd += realized
            self.fills_count += 1
            self._last_fill_ts = time.time()
            self.fills.append(
                FillRecord(
                    ts=self._last_fill_ts,
                    side_is_buy=side_is_buy,
                    sz=sz,
                    px=px,
                    realized_pnl=realized,
                    position_after=self.position.size,
                )
            )
            return realized

    def record_tick(self) -> None:
        """Append a row to the tick history (for the dashboard chart)."""
        with self._lock:
            book = self._book
            if book is None:
                return
            self.ticks.append(
                TickRecord(
                    ts=time.time(),
                    mid=book.mid(),
                    sigma_bps_per_sec=self._vol.smoothed_sigma_bps_per_sec(),
                    position=self.position.size,
                    realized_pnl=self.realized_pnl_usd,
                    unrealized_pnl=self._unrealized_unlocked(),
                )
            )

    def record_ml(self, ml_stats: Dict[str, Any]) -> None:
        """Append a snapshot of the ML model's stats so the dashboard can
        chart how weights & accuracy adapt over time."""
        if not ml_stats:
            return
        with self._lock:
            self.ml_history.append(
                MLHistoryRecord(
                    ts=time.time(),
                    n_updates=int(ml_stats.get("n_updates", 0) or 0),
                    mae_bps=float(ml_stats.get("mae_bps", 0.0) or 0.0),
                    correlation=float(ml_stats.get("correlation", 0.0) or 0.0),
                    last_signal_bps=float(ml_stats.get("last_signal_bps", 0.0) or 0.0),
                    weights=dict(ml_stats.get("weights", {}) or {}),
                )
            )

    def _unrealized_unlocked(self) -> float:
        if self._book is None or self.position.size == 0:
            return 0.0
        return (self._book.mid() - self.position.avg_entry_px) * self.position.size

    def unrealized_pnl_usd(self) -> float:
        with self._lock:
            return self._unrealized_unlocked()

    def total_pnl_usd(self) -> float:
        with self._lock:
            return self.realized_pnl_usd + self._unrealized_unlocked()

    def last_fill_ts(self) -> float:
        with self._lock:
            return self._last_fill_ts

    def _depth_snapshot(self, n: int = 15) -> Dict[str, Any]:
        """Return up to n levels of bids and asks as plain lists."""
        levels = self._raw_levels
        if not levels or len(levels) < 2:
            return {"bids": [], "asks": []}
        bids = [{"px": float(l["px"]), "sz": float(l["sz"])} for l in (levels[0] or [])[:n]]
        asks = [{"px": float(l["px"]), "sz": float(l["sz"])} for l in (levels[1] or [])[:n]]
        return {"bids": bids, "asks": asks}

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of all state. Used by the dashboard."""
        with self._lock:
            book = self._book
            mid = book.mid() if book else None
            spread_bps = book.spread_bps() if book else None
            return {
                "uptime_s": time.time() - self.start_ts,
                "book": (
                    {
                        "bid_px": book.bid_px,
                        "bid_sz": book.bid_sz,
                        "ask_px": book.ask_px,
                        "ask_sz": book.ask_sz,
                        "mid": mid,
                        "spread_bps": spread_bps,
                        "ts": book.ts,
                        "age_s": time.time() - book.ts,
                    }
                    if book
                    else None
                ),
                "sigma_bps_per_sec": self._vol.smoothed_sigma_bps_per_sec(),
                "sigma_raw_bps_per_sec": self._vol.realized_vol_bps_per_sec(),
                "position": {
                    "size": self.position.size,
                    "avg_entry_px": self.position.avg_entry_px,
                    "notional": self.position.signed_notional(mid) if mid else 0.0,
                },
                "pnl": {
                    "realized": self.realized_pnl_usd,
                    "unrealized": self._unrealized_unlocked(),
                    "total": self.realized_pnl_usd + self._unrealized_unlocked(),
                },
                "depth": self._depth_snapshot(15),
                "fills_count": self.fills_count,
                "last_fill_ts": self._last_fill_ts,
                "fills": [
                    {
                        "ts": f.ts,
                        "side": "BUY" if f.side_is_buy else "SELL",
                        "sz": f.sz,
                        "px": f.px,
                        "realized_pnl": f.realized_pnl,
                        "position_after": f.position_after,
                    }
                    for f in list(self.fills)[-100:]
                ],
                "ticks": [
                    {
                        "ts": t.ts,
                        "mid": t.mid,
                        "sigma": t.sigma_bps_per_sec,
                        "position": t.position,
                        "realized": t.realized_pnl,
                        "unrealized": t.unrealized_pnl,
                    }
                    for t in self.ticks
                ],
                "ml_history": [
                    {
                        "ts": h.ts,
                        "n_updates": h.n_updates,
                        "mae_bps": h.mae_bps,
                        "correlation": h.correlation,
                        "last_signal_bps": h.last_signal_bps,
                        "weights": h.weights,
                    }
                    for h in self.ml_history
                ],
            }
