"""Typed configuration loader for the MM bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


@dataclass
class BotConfig:
    """Runtime configuration. Many fields are live-tunable from the dashboard
    via Strategy.apply_runtime_update() — see TUNABLE_FIELDS in strategy.py.
    Fields not in that list (network, mode, symbol, private_key, dashboard_*)
    require a process restart to change."""
    network: str
    mode: str
    symbol: str

    leverage: int
    margin_mode: str   # "cross" or "isolated"

    # Paper-mode fill probability per book update when price is touched.
    # 1.0 = instant fill (unrealistic), 0.05-0.15 = realistic queue wait.
    paper_fill_prob: float

    quote_notional_usd: float
    max_position_notional_usd: float

    min_half_spread_bps: float
    vol_factor: float
    vol_window_seconds: float
    # Asymmetric EWMA on rolling-window sigma: fast rise, slow decay after spikes.
    sigma_ewma_alpha_up: float
    sigma_ewma_alpha_down: float
    # Vol-driven half-spread on the side that *reduces* inventory is multiplied by
    # this (1.0 = symmetric). E.g. 0.65 = longs keep asks tighter vs vol than bids.
    inventory_flatten_vol_half_spread_mult: float
    inventory_skew_bps: float
    requote_threshold_bps: float

    loop_interval_seconds: float
    max_book_age_seconds: float
    cancel_all_on_start: bool
    cancel_all_on_exit: bool

    # When true, bid/ask are not placed until the dashboard "Start live quoting"
    # action runs (requires dashboard_enabled). Book/trades still feed the ML signal.
    manual_quoting_start: bool

    max_session_loss_usd: float
    single_fill_pnl_alarm_usd: float
    pause_seconds_after_alarm: float
    max_orders_per_minute: int

    ml_enabled: bool
    ml_horizon_s: float
    ml_forgetting_factor: float
    ml_max_signal_bps: float
    ml_min_updates: int

    # Adaptive inventory-skew controller (multi-armed bandit). When enabled,
    # the bot continuously trials different multipliers of `inventory_skew_bps`
    # and learns which produces the best per-fill edge in basis points.
    adaptive_skew_enabled: bool
    adaptive_skew_epsilon: float        # exploration rate (0..1)
    adaptive_skew_alpha: float          # EWMA smoothing for per-arm reward
    adaptive_skew_min_pulls: int        # closing fills per arm before reconsider
    adaptive_skew_multipliers: list     # multipliers around inventory_skew_bps

    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int

    private_key: str
    account_address: Optional[str]
    vault_address: Optional[str]

    def base_url(self) -> str:
        from hyperliquid.utils.constants import (
            MAINNET_API_URL,
            TESTNET_API_URL,
        )

        if self.network == "mainnet":
            return MAINNET_API_URL
        if self.network == "testnet":
            return TESTNET_API_URL
        raise ValueError(f"Unknown network: {self.network!r} (expected 'mainnet' or 'testnet')")

    def is_paper(self) -> bool:
        return self.mode == "paper"


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> BotConfig:
    """Load configuration from a YAML file and a .env file.

    The .env file is required for live mode (so we have a private key) but
    may be empty for paper mode.
    """
    if not Path(config_path).exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path!r}. "
            f"Copy config.example.yaml to {config_path} and edit it."
        )

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    load_dotenv(env_path)

    cfg = BotConfig(
        network=str(raw["network"]).lower(),
        mode=str(raw["mode"]).lower(),
        symbol=str(raw["symbol"]),
        leverage=int(raw.get("leverage", 1)),
        margin_mode=str(raw.get("margin_mode", "cross")).lower(),
        paper_fill_prob=float(raw.get("paper_fill_prob", 0.1)),
        quote_notional_usd=float(raw["quote_notional_usd"]),
        max_position_notional_usd=float(raw["max_position_notional_usd"]),
        min_half_spread_bps=float(raw["min_half_spread_bps"]),
        vol_factor=float(raw["vol_factor"]),
        vol_window_seconds=float(raw["vol_window_seconds"]),
        sigma_ewma_alpha_up=float(raw.get("sigma_ewma_alpha_up", 0.30)),
        sigma_ewma_alpha_down=float(raw.get("sigma_ewma_alpha_down", 0.07)),
        inventory_flatten_vol_half_spread_mult=float(
            raw.get("inventory_flatten_vol_half_spread_mult", 0.65)
        ),
        inventory_skew_bps=float(raw["inventory_skew_bps"]),
        requote_threshold_bps=float(raw["requote_threshold_bps"]),
        loop_interval_seconds=float(raw["loop_interval_seconds"]),
        max_book_age_seconds=float(raw["max_book_age_seconds"]),
        cancel_all_on_start=bool(raw["cancel_all_on_start"]),
        cancel_all_on_exit=bool(raw["cancel_all_on_exit"]),
        # Default True: no bid/ask until dashboard "Start live quoting" (or set false for auto).
        manual_quoting_start=bool(raw.get("manual_quoting_start", True)),
        max_session_loss_usd=float(raw["max_session_loss_usd"]),
        single_fill_pnl_alarm_usd=float(raw["single_fill_pnl_alarm_usd"]),
        pause_seconds_after_alarm=float(raw["pause_seconds_after_alarm"]),
        max_orders_per_minute=int(raw["max_orders_per_minute"]),
        ml_enabled=bool(raw.get("ml", {}).get("enabled", True)),
        ml_horizon_s=float(raw.get("ml", {}).get("horizon_s", 10.0)),
        ml_forgetting_factor=float(raw.get("ml", {}).get("forgetting_factor", 0.990)),
        ml_max_signal_bps=float(raw.get("ml", {}).get("max_signal_bps", 10.0)),
        ml_min_updates=int(raw.get("ml", {}).get("min_updates", 30)),
        adaptive_skew_enabled=bool(raw.get("adaptive_skew", {}).get("enabled", True)),
        adaptive_skew_epsilon=float(raw.get("adaptive_skew", {}).get("epsilon", 0.15)),
        adaptive_skew_alpha=float(raw.get("adaptive_skew", {}).get("ewma_alpha", 0.2)),
        adaptive_skew_min_pulls=int(raw.get("adaptive_skew", {}).get("min_pulls_per_switch", 5)),
        adaptive_skew_multipliers=list(
            raw.get("adaptive_skew", {}).get("multipliers", [0.5, 0.75, 1.0, 1.25, 1.5])
        ),
        dashboard_enabled=bool(raw.get("dashboard", {}).get("enabled", True)),
        dashboard_host=str(raw.get("dashboard", {}).get("host", "127.0.0.1")),
        dashboard_port=int(raw.get("dashboard", {}).get("port", 8765)),
        private_key=os.getenv("HL_PRIVATE_KEY", "").strip(),
        account_address=(os.getenv("HL_ACCOUNT_ADDRESS") or "").strip() or None,
        vault_address=(os.getenv("HL_VAULT_ADDRESS") or "").strip() or None,
    )

    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")
    if cfg.mode == "live" and not cfg.private_key:
        raise ValueError("HL_PRIVATE_KEY is required for live mode (set it in .env)")
    if cfg.min_half_spread_bps < 2.0:
        # Hard guard: at base tier, anything below ~2 bp/side is mathematically a money loser.
        raise ValueError(
            f"min_half_spread_bps={cfg.min_half_spread_bps} is below the 2.0 safety floor. "
            f"At the base maker fee, you would lose money on every round trip. Refusing to start."
        )
    if cfg.margin_mode not in ("cross", "isolated"):
        raise ValueError(
            f"margin_mode must be 'cross' or 'isolated', got {cfg.margin_mode!r}"
        )
    if cfg.leverage < 1 or cfg.leverage > 50:
        raise ValueError(
            f"leverage={cfg.leverage} must be between 1 and 50 (Hyperliquid max varies by symbol)."
        )
    if not (0.0 < cfg.sigma_ewma_alpha_up <= 1.0):
        raise ValueError("sigma_ewma_alpha_up must be in (0, 1]")
    if not (0.0 < cfg.sigma_ewma_alpha_down <= 1.0):
        raise ValueError("sigma_ewma_alpha_down must be in (0, 1]")
    if not (0.0 < cfg.inventory_flatten_vol_half_spread_mult <= 1.0):
        raise ValueError("inventory_flatten_vol_half_spread_mult must be in (0, 1]")

    return cfg
