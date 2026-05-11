"""Hyperliquid-specific math helpers (price/size rounding, cloid generation)."""

from __future__ import annotations

import secrets
from typing import Tuple

from hyperliquid.utils.types import Cloid


def round_price(px: float, sz_decimals: int, is_spot: bool = False) -> float:
    """Round a price to Hyperliquid's tick rules.

    Per the HL docs (and the SDK's _slippage_price helper):
    - Prices are rounded to **5 significant figures**.
    - And to at most **(6 - szDecimals)** decimal places for perps,
      or **(8 - szDecimals)** for spot.

    Integer prices (>= 100000) are exempt from sig-fig rounding by virtue
    of `:.5g` formatting.
    """
    if px <= 0:
        raise ValueError(f"price must be positive, got {px}")
    sig5 = float(f"{px:.5g}")
    max_dec = (8 if is_spot else 6) - sz_decimals
    if max_dec < 0:
        max_dec = 0
    return round(sig5, max_dec)


def round_size(sz: float, sz_decimals: int) -> float:
    """Round a size to the asset's lot size."""
    return round(sz, sz_decimals)


def size_for_notional(notional_usd: float, price: float, sz_decimals: int) -> float:
    """Compute order size for a target USD notional, rounded to lot size."""
    if price <= 0:
        return 0.0
    raw = notional_usd / price
    return round_size(raw, sz_decimals)


def make_cloid() -> Cloid:
    """Create a random 16-byte client order id."""
    return Cloid(f"0x{secrets.token_hex(16)}")


def best_bid_ask(l2_levels) -> Tuple[float, float, float, float]:
    """Extract (bid_px, bid_sz, ask_px, ask_sz) from an L2 snapshot.

    `l2_levels` is the value at `msg["data"]["levels"]` for an l2Book WS
    message: a 2-element list of [bids_desc, asks_asc], each a list of
    {"px": str, "sz": str, "n": int}.
    """
    if not l2_levels or len(l2_levels) < 2 or not l2_levels[0] or not l2_levels[1]:
        raise ValueError("empty l2 book")
    bid = l2_levels[0][0]
    ask = l2_levels[1][0]
    return float(bid["px"]), float(bid["sz"]), float(ask["px"]), float(ask["sz"])


def micro_price(bid_px: float, bid_sz: float, ask_px: float, ask_sz: float) -> float:
    """Size-weighted micro-price; a better fair-value estimate than the mid."""
    total = bid_sz + ask_sz
    if total <= 0:
        return 0.5 * (bid_px + ask_px)
    return (ask_sz * bid_px + bid_sz * ask_px) / total
