"""Pricing logic: fair value, vol-aware spread, inventory skew, ML signal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .adaptive import SkewAdapter
from .config import BotConfig
from .hl_utils import micro_price, round_price, size_for_notional
from .signal import FairValueSignal
from .state import BookSnapshot, MarketState


@dataclass(frozen=True)
class Quote:
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float
    fair_px: float          # micro-price before ML adjustment
    adjusted_fair_px: float # micro-price + ML signal
    half_spread_bps: float  # average of bid/ask vol half-spreads (dashboard summary)
    bid_half_spread_bps: float
    ask_half_spread_bps: float
    skew_bps: float
    signal_bps: float       # raw ML prediction

    def __str__(self) -> str:
        hs = (
            f"{self.bid_half_spread_bps:.2f}/{self.ask_half_spread_bps:.2f}bp"
            if abs(self.bid_half_spread_bps - self.ask_half_spread_bps) > 0.02
            else f"{self.half_spread_bps:.2f}bp"
        )
        return (
            f"fair={self.fair_px:.6g} sig={self.signal_bps:+.2f}bp "
            f"hs={hs} skew={self.skew_bps:+.2f}bp "
            f"-> bid {self.bid_px:.6g}x{self.bid_sz} ask {self.ask_px:.6g}x{self.ask_sz}"
        )


class Quoter:
    """Compute desired bid/ask quotes from current market state."""

    def __init__(
        self,
        cfg: BotConfig,
        sz_decimals: int,
        signal: Optional[FairValueSignal] = None,
        skew_adapter: Optional[SkewAdapter] = None,
    ):
        self.cfg = cfg
        self.sz_decimals = sz_decimals
        self.signal = signal
        self.skew_adapter = skew_adapter

    def _base_skew_bps(self) -> float:
        """Effective inventory skew (config × adaptive multiplier if enabled)."""
        if self.skew_adapter is not None:
            return self.skew_adapter.effective_skew_bps()
        return self.cfg.inventory_skew_bps

    def desired_quote(self, state: MarketState) -> Optional[Quote]:
        book = state.book()
        if book is None:
            return None

        fair = micro_price(book.bid_px, book.bid_sz, book.ask_px, book.ask_sz)
        if fair <= 0:
            return None

        # ML signal: adjust fair value up/down based on predicted short-term move
        signal_bps = 0.0
        if self.signal is not None:
            signal_bps = self.signal.predict(
                book.bid_px, book.bid_sz, book.ask_px, book.ask_sz
            )
        adjusted_fair = fair * (1.0 + signal_bps * 1e-4)

        sigma_bps = state.vol_bps_per_sec()
        vol_half_spread = self.cfg.vol_factor * sigma_bps
        half_spread_bps = max(self.cfg.min_half_spread_bps, vol_half_spread)

        position_notional = state.position.signed_notional(adjusted_fair)
        max_pos = max(self.cfg.max_position_notional_usd, 1e-9)
        inventory_ratio = max(min(position_notional / max_pos, 1.0), -1.0)
        skew_bps = -self._base_skew_bps() * inventory_ratio

        # Split vol-driven widening across bid/ask: the side that *reduces* inventory
        # uses a smaller vol multiplier so flattening quotes do not jump as wide on spikes.
        min_hs = self.cfg.min_half_spread_bps
        vol_extra = max(0.0, half_spread_bps - min_hs)
        m_flat = max(min(self.cfg.inventory_flatten_vol_half_spread_mult, 1.0), 1e-6)
        if position_notional > 1e-9:
            bid_hs = min_hs + vol_extra
            ask_hs = min_hs + vol_extra * m_flat
        elif position_notional < -1e-9:
            bid_hs = min_hs + vol_extra * m_flat
            ask_hs = min_hs + vol_extra
        else:
            bid_hs = ask_hs = half_spread_bps

        # Center quotes on adjusted fair (ML-aware)
        center = adjusted_fair * (1.0 + skew_bps * 1e-4)
        bid_raw = center * (1.0 - bid_hs * 1e-4)
        ask_raw = center * (1.0 + ask_hs * 1e-4)

        bid_raw = min(bid_raw, book.ask_px - _epsilon(book))
        ask_raw = max(ask_raw, book.bid_px + _epsilon(book))

        bid_px = round_price(bid_raw, self.sz_decimals, is_spot=False)
        ask_px = round_price(ask_raw, self.sz_decimals, is_spot=False)

        if bid_px >= ask_px:
            return None

        bid_sz = size_for_notional(self.cfg.quote_notional_usd, bid_px, self.sz_decimals)
        ask_sz = size_for_notional(self.cfg.quote_notional_usd, ask_px, self.sz_decimals)

        if bid_sz <= 0 or ask_sz <= 0:
            return None

        if position_notional >= self.cfg.max_position_notional_usd:
            bid_sz = 0.0
        if position_notional <= -self.cfg.max_position_notional_usd:
            ask_sz = 0.0

        hs_avg = 0.5 * (bid_hs + ask_hs)
        return Quote(
            bid_px=bid_px,
            bid_sz=bid_sz,
            ask_px=ask_px,
            ask_sz=ask_sz,
            fair_px=fair,
            adjusted_fair_px=adjusted_fair,
            half_spread_bps=hs_avg,
            bid_half_spread_bps=bid_hs,
            ask_half_spread_bps=ask_hs,
            skew_bps=skew_bps,
            signal_bps=signal_bps,
        )


def _epsilon(book: BookSnapshot) -> float:
    """A tiny price offset so we never accidentally cross our own quote into the book."""
    spread = max(book.ask_px - book.bid_px, 0.0)
    return max(spread * 0.1, book.mid() * 1e-7)


def needs_requote(current_px: Optional[float], target_px: float, threshold_bps: float) -> bool:
    """True if we don't have a resting quote, or it has drifted past threshold."""
    if current_px is None:
        return True
    if target_px <= 0:
        return False
    drift_bps = abs(current_px - target_px) / target_px * 1e4
    return drift_bps > threshold_bps
