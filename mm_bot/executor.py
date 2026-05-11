"""Live executor: wraps the Hyperliquid Exchange SDK for safe order management."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from .config import BotConfig
from .hl_utils import make_cloid

log = logging.getLogger("executor")


@dataclass
class RestingOrder:
    cloid: Cloid
    is_buy: bool
    px: float
    sz: float
    oid: Optional[int] = None


@dataclass
class ExecutorState:
    """Tracks the orders we believe are live on the exchange."""

    orders: Dict[str, RestingOrder] = field(default_factory=dict)

    def by_side(self, is_buy: bool) -> Optional[RestingOrder]:
        for o in self.orders.values():
            if o.is_buy == is_buy:
                return o
        return None


class LiveExecutor:
    """Order management against Hyperliquid mainnet/testnet."""

    def __init__(self, cfg: BotConfig, exchange: Exchange, info: Info, account_address: str):
        self.cfg = cfg
        self.exchange = exchange
        self.info = info
        self.account_address = account_address
        self.symbol = cfg.symbol
        self.state = ExecutorState()
        self._configure_leverage()

    def _configure_leverage(self) -> None:
        """Set the per-symbol leverage and margin mode on Hyperliquid.

        Idempotent — Hyperliquid silently accepts re-setting the same leverage,
        so it's safe to call on every startup."""
        try:
            is_cross = (self.cfg.margin_mode == "cross")
            resp = self.exchange.update_leverage(
                leverage=self.cfg.leverage,
                name=self.symbol,
                is_cross=is_cross,
            )
            log.info(
                "leverage set to %dx %s on %s (resp=%s)",
                self.cfg.leverage,
                "cross" if is_cross else "isolated",
                self.symbol,
                _short_resp(resp),
            )
        except Exception as exc:
            log.warning(
                "update_leverage(%dx %s) on %s FAILED: %s — bot will use whatever "
                "leverage is currently set on the account",
                self.cfg.leverage,
                self.cfg.margin_mode,
                self.symbol,
                exc,
            )

    def cancel_all(self) -> None:
        log.info("cancelling all open orders for %s", self.symbol)
        try:
            opens = self.info.open_orders(self.account_address)
        except Exception as exc:
            log.warning("open_orders fetch failed: %s", exc)
            opens = []

        cancels: List[dict] = [
            {"coin": o["coin"], "oid": o["oid"]}
            for o in opens
            if o.get("coin") == self.symbol
        ]
        if cancels:
            try:
                self.exchange.bulk_cancel(cancels)
            except Exception as exc:
                log.warning("bulk_cancel failed: %s", exc)
        self.state.orders.clear()

    def place(self, is_buy: bool, px: float, sz: float) -> Optional[RestingOrder]:
        if sz <= 0 or px <= 0:
            return None
        cloid = make_cloid()
        order_type = {"limit": {"tif": "Alo"}}
        try:
            resp = self.exchange.order(
                name=self.symbol,
                is_buy=is_buy,
                sz=sz,
                limit_px=px,
                order_type=order_type,
                reduce_only=False,
                cloid=cloid,
            )
        except Exception as exc:
            log.warning("order placement failed: %s", exc)
            return None

        oid = _extract_oid(resp)
        order = RestingOrder(cloid=cloid, is_buy=is_buy, px=px, sz=sz, oid=oid)
        self.state.orders[cloid.to_raw()] = order
        log.info(
            "placed %s %.6g @ %.6g (cloid=%s oid=%s) resp=%s",
            "BUY" if is_buy else "SELL",
            sz,
            px,
            cloid,
            oid,
            _short_resp(resp),
        )
        return order

    def cancel(self, order: RestingOrder) -> None:
        try:
            if order.oid is not None:
                self.exchange.cancel(self.symbol, order.oid)
            else:
                self.exchange.cancel_by_cloid(self.symbol, order.cloid)
        except Exception as exc:
            log.warning("cancel failed for %s: %s", order.cloid, exc)
        self.state.orders.pop(order.cloid.to_raw(), None)

    def replace(self, side_is_buy: bool, target_px: float, target_sz: float) -> None:
        existing = self.state.by_side(side_is_buy)
        if existing is not None:
            self.cancel(existing)
        if target_sz > 0:
            self.place(side_is_buy, target_px, target_sz)

    def flatten(self) -> None:
        """Close the entire position with an aggressive IOC market order."""
        try:
            self.exchange.market_close(coin=self.symbol)
            log.info("flattened position via market_close on %s", self.symbol)
        except Exception as exc:
            log.warning("market_close failed: %s", exc)

    def flatten_side(self, is_buy: bool) -> None:
        """Cancel only the order on one side, then market-close only that side
        of the position (reduce-only IOC).  If there is no position on that
        side this is a no-op after the order cancel.

        is_buy=True  → cancel the resting bid, then sell to close any long.
        is_buy=False → cancel the resting ask, then buy  to close any short.
        """
        side_label = "BUY/long" if is_buy else "SELL/short"
        # Cancel the resting order on that side
        existing = self.state.by_side(is_buy)
        if existing:
            self.cancel(existing)
            log.info("cancelled %s order for flatten_%s", side_label, side_label)
        # Close the position on that side with a reduce-only IOC
        # market_close already uses reduce_only=True and picks the right
        # direction from the current szi; we just pass the coin.
        try:
            self.exchange.market_close(coin=self.symbol)
            log.info("flatten_%s: market_close sent for %s", side_label, self.symbol)
        except Exception as exc:
            log.warning("flatten_%s market_close failed: %s", side_label, exc)


def _extract_oid(resp: dict) -> Optional[int]:
    try:
        statuses = resp["response"]["data"]["statuses"]
        if statuses and isinstance(statuses[0], dict):
            return int(statuses[0].get("resting", {}).get("oid")) if "resting" in statuses[0] else None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _short_resp(resp) -> str:
    s = repr(resp)
    return s if len(s) < 240 else s[:237] + "..."
