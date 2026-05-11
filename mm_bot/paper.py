"""Paper executor: simulates fills locally without sending any orders.

Fill model: a resting bid is filled when the market ask <= our bid; a
resting ask is filled when the market bid >= our ask. Filled at our price
(post-only assumption). This is optimistic vs. reality (no queue position,
no adverse selection from same-price taker arrivals) but a reasonable
sanity check for the strategy logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .hl_utils import make_cloid
from .state import BookSnapshot, MarketState

log = logging.getLogger("paper")


@dataclass
class PaperOrder:
    cloid_raw: str
    is_buy: bool
    px: float
    sz: float


@dataclass
class PaperState:
    orders: Dict[str, PaperOrder] = field(default_factory=dict)

    def by_side(self, is_buy: bool) -> Optional[PaperOrder]:
        for o in self.orders.values():
            if o.is_buy == is_buy:
                return o
        return None


class PaperExecutor:
    """Drop-in replacement for LiveExecutor, never touches the network."""

    def __init__(
        self,
        symbol: str,
        on_fill: Callable[[bool, float, float], None],
        market_state: Optional[MarketState] = None,
    ):
        self.symbol = symbol
        self.state = PaperState()
        self._on_fill = on_fill
        # Reference to MarketState so flatten() / flatten_side() can read the
        # current position and best book to synthesize a realistic taker fill.
        self._market = market_state

    def cancel_all(self) -> None:
        if self.state.orders:
            log.info("[paper] cancel_all (%d orders)", len(self.state.orders))
        self.state.orders.clear()

    def place(self, is_buy: bool, px: float, sz: float):
        cloid = make_cloid().to_raw()
        order = PaperOrder(cloid_raw=cloid, is_buy=is_buy, px=px, sz=sz)
        self.state.orders[cloid] = order
        log.info(
            "[paper] placed %s %.6g @ %.6g (cloid=%s)",
            "BUY" if is_buy else "SELL",
            sz,
            px,
            cloid[:10],
        )

        class _Wrap:
            def __init__(inner, o: PaperOrder):
                inner.cloid_raw = o.cloid_raw
                inner.is_buy = o.is_buy
                inner.px = o.px
                inner.sz = o.sz

        return _Wrap(order)

    def cancel(self, order) -> None:
        self.state.orders.pop(order.cloid_raw, None)

    def replace(self, side_is_buy: bool, target_px: float, target_sz: float) -> None:
        existing = self.state.by_side(side_is_buy)
        if existing is not None:
            self.cancel(existing)
        if target_sz > 0:
            self.place(side_is_buy, target_px, target_sz)

    def flatten(self) -> None:
        """Cancel all resting orders and synthesize a taker fill that
        closes the entire position at the current best opposing price."""
        n_cancelled = len(self.state.orders)
        self.state.orders.clear()
        log.info("[paper] flatten (cancelled %d orders)", n_cancelled)
        self._market_close(target_size=0.0, label="flatten")

    def flatten_side(self, is_buy: bool) -> None:
        """is_buy=True  → cancel resting bid, then SELL to close any long.
           is_buy=False → cancel resting ask, then BUY  to close any short."""
        side_label = "BUY/long" if is_buy else "SELL/short"
        to_remove = [k for k, o in self.state.orders.items() if o.is_buy == is_buy]
        for k in to_remove:
            self.state.orders.pop(k, None)
        log.info("[paper] flatten_%s: cancelled %d order(s)", side_label, len(to_remove))

        if self._market is None:
            log.warning("[paper] flatten_%s: no MarketState wired — cannot close position", side_label)
            return

        pos = self._market.position.size
        # is_buy=True (cancel bid) → close long only (pos > 0)
        # is_buy=False (cancel ask) → close short only (pos < 0)
        if (is_buy and pos > 0) or ((not is_buy) and pos < 0):
            self._market_close(target_size=0.0, label=f"flatten_{side_label}")
        else:
            log.info(
                "[paper] flatten_%s: no matching position to close (pos=%.6g)",
                side_label,
                pos,
            )

    def _market_close(self, target_size: float, label: str) -> None:
        """Inject a synthetic taker fill that moves position toward
        `target_size` (default 0 = full flatten)."""
        if self._market is None:
            log.warning("[paper] %s: MarketState not provided to PaperExecutor", label)
            return

        pos = self._market.position.size
        delta = target_size - pos
        if abs(delta) < 1e-12:
            log.info("[paper] %s: position already %.6g, nothing to do", label, pos)
            return

        book = self._market.book()
        if book is None or book.bid_px <= 0 or book.ask_px <= 0:
            log.warning("[paper] %s: no book yet, cannot synthesize close fill", label)
            return

        # Buying lifts the offer (pay ask); selling hits the bid.
        is_buy = delta > 0
        sz = abs(delta)
        px = book.ask_px if is_buy else book.bid_px

        log.info(
            "[paper] %s: market-close %s %.6g @ %.6g (pos %.6g → %.6g)",
            label,
            "BUY" if is_buy else "SELL",
            sz,
            px,
            pos,
            target_size,
        )
        self._on_fill(is_buy, sz, px)

    def check_fills(self, book: BookSnapshot, fill_prob: float = 0.1) -> None:
        """Mark-to-market against latest book and emit synthetic fills.

        fill_prob controls how realistic queue position is modelled:
          1.0 = fill immediately whenever price is touched (very optimistic,
                ignores queue — what caused the rapid-fire fill problem)
          0.1 = 10% chance per second of being filled when price is touched
                (roughly equivalent to sitting ~10s in the queue — much more
                realistic for a passive maker at $1k notional on ETH)

        A resting bid is only eligible when market ask <= bid price (touched).
        A resting ask is only eligible when market bid >= ask price (touched).
        """
        import random
        filled: List[PaperOrder] = []
        for order in list(self.state.orders.values()):
            touched = (
                (order.is_buy and book.ask_px <= order.px) or
                (not order.is_buy and book.bid_px >= order.px)
            )
            if touched and random.random() < fill_prob:
                filled.append(order)
        for order in filled:
            log.info(
                "[paper] fill %s %.6g @ %.6g (book bid=%.6g ask=%.6g)",
                "BUY" if order.is_buy else "SELL",
                order.sz,
                order.px,
                book.bid_px,
                book.ask_px,
            )
            self._on_fill(order.is_buy, order.sz, order.px)
            self.state.orders.pop(order.cloid_raw, None)
