"""Feature extraction for the ML fair-value signal.

All features are dimensionless (z-score-able) so the online regression
weight vector stays numerically stable regardless of price scale.

Feature vector (in order):
  0  bbo_imbalance       (bid_sz - ask_sz) / (bid_sz + ask_sz)  [-1, 1]
  1  l2_imbalance_3      same but summed over top-3 levels      [-1, 1]
  2  price_momentum_s5   (mid_now - mid_5s_ago) / mid_now       [bps-ish]
  3  price_momentum_s15  (mid_now - mid_15s_ago) / mid_now
  4  price_momentum_s60  (mid_now - mid_60s_ago) / mid_now
  5  trade_flow_s5       signed trade volume last 5s (buys - sells) / avg sz
  6  trade_flow_s30      same over 30s
  7  spread_bps          (ask - bid) / mid  [bps]
  8  funding_premium     (mark - oracle) / oracle if available, else 0
  9  const               1.0  (bias term)
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple


@dataclass
class TradeEvent:
    ts: float
    is_buy: bool
    sz: float
    px: float


@dataclass
class MidHistory:
    ts: float
    mid: float


class FeatureExtractor:
    """Maintains rolling buffers and computes the feature vector each tick."""

    N_FEATURES = 10  # 9 real features + bias

    def __init__(self, max_trade_history_s: float = 120.0):
        self._max_trade_s = max_trade_history_s
        self._trades: Deque[TradeEvent] = deque()
        self._mids: Deque[MidHistory] = deque(maxlen=300)  # ~5 min at 1s
        self._last_book_levels: Optional[list] = None
        self._funding_premium: float = 0.0

    def on_trade(self, ts: float, is_buy: bool, sz: float, px: float) -> None:
        self._trades.append(TradeEvent(ts=ts, is_buy=is_buy, sz=sz, px=px))
        cutoff = ts - self._max_trade_s
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def on_book(self, ts: float, mid: float, levels: Optional[list] = None) -> None:
        self._mids.append(MidHistory(ts=ts, mid=mid))
        if levels is not None:
            self._last_book_levels = levels

    def on_funding(self, premium: float) -> None:
        self._funding_premium = premium

    def extract(self, bid_px: float, bid_sz: float, ask_px: float, ask_sz: float) -> Optional[List[float]]:
        """Return feature vector, or None if we don't have enough data yet."""
        now = time.time()
        mid = 0.5 * (bid_px + ask_px)
        if mid <= 0:
            return None

        # --- Feature 0: BBO imbalance ---
        bbo_total = bid_sz + ask_sz
        bbo_imbalance = (bid_sz - ask_sz) / bbo_total if bbo_total > 0 else 0.0

        # --- Feature 1: L2 imbalance over top 3 levels ---
        l2_imbalance = self._l2_imbalance(3)

        # --- Features 2-4: Price momentum at 5s, 15s, 60s ---
        mom5  = self._momentum(now, mid, 5.0)
        mom15 = self._momentum(now, mid, 15.0)
        mom60 = self._momentum(now, mid, 60.0)
        if mom5 is None or mom15 is None:
            # Not enough history yet
            return None

        # --- Features 5-6: Signed trade flow (buys-sells) normalised by avg sz ---
        flow5  = self._trade_flow(now, mid, 5.0)
        flow30 = self._trade_flow(now, mid, 30.0)

        # --- Feature 7: Spread in bps ---
        spread_bps = (ask_px - bid_px) / mid * 1e4 if mid > 0 else 0.0

        # --- Feature 8: Funding premium ---
        funding = self._funding_premium

        # --- Feature 9: Bias ---
        bias = 1.0

        return [
            bbo_imbalance,
            l2_imbalance,
            mom5  * 1e4,   # convert to bps
            mom15 * 1e4,
            (mom60 or 0.0) * 1e4,
            flow5,
            flow30,
            spread_bps,
            funding * 1e4,
            bias,
        ]

    def _momentum(self, now: float, mid: float, lookback_s: float) -> Optional[float]:
        target_ts = now - lookback_s
        # Find the mid closest to target_ts from the past
        best: Optional[MidHistory] = None
        for h in self._mids:
            if h.ts <= target_ts + 0.5:  # allow 0.5s tolerance
                best = h
        if best is None:
            return None
        if mid <= 0 or best.mid <= 0:
            return None
        return math.log(mid / best.mid)

    def _trade_flow(self, now: float, mid: float, lookback_s: float) -> float:
        cutoff = now - lookback_s
        buy_vol = 0.0
        sell_vol = 0.0
        for t in self._trades:
            if t.ts >= cutoff:
                if t.is_buy:
                    buy_vol += t.sz
                else:
                    sell_vol += t.sz
        total = buy_vol + sell_vol
        if total < 1e-9:
            return 0.0
        # Net flow in [-1, 1]
        return (buy_vol - sell_vol) / total

    def _l2_imbalance(self, n_levels: int) -> float:
        levels = self._last_book_levels
        if not levels or len(levels) < 2:
            return 0.0
        bids = levels[0][:n_levels]
        asks = levels[1][:n_levels]
        bid_vol = sum(float(b.get("sz", 0)) for b in bids)
        ask_vol = sum(float(a.get("sz", 0)) for a in asks)
        total = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0
