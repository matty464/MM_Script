"""Entry point: load config, build clients, run the strategy."""

from __future__ import annotations

import signal
import sys
from dataclasses import replace

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from mm_bot.config import BotConfig, load_config
from mm_bot.dashboard import Dashboard
from mm_bot.executor import LiveExecutor
from mm_bot.hotkeys import HotkeyListener
from mm_bot.logging_setup import setup_logging
from mm_bot.paper import PaperExecutor
from mm_bot.state import MarketState
from mm_bot.strategy import Strategy


def build_info_and_executor(cfg: BotConfig, state: MarketState):
    base_url = cfg.base_url()
    info = Info(base_url=base_url, skip_ws=False)

    if cfg.symbol not in info.coin_to_asset:
        valid = sorted(info.coin_to_asset.keys())[:30]
        raise ValueError(
            f"Symbol {cfg.symbol!r} not found on {cfg.network}. "
            f"First 30 valid symbols: {valid}"
        )
    asset = info.coin_to_asset[cfg.symbol]
    sz_decimals = info.asset_to_sz_decimals[asset]

    if cfg.is_paper():
        def on_fill(side_is_buy: bool, sz: float, px: float):
            strategy_ref["s"]._record_paper_fill(side_is_buy, sz, px)

        executor = PaperExecutor(symbol=cfg.symbol, on_fill=on_fill, market_state=state)
        account_address = None
    else:
        wallet = eth_account.Account.from_key(cfg.private_key)
        account_address = cfg.account_address or wallet.address
        exchange = Exchange(
            wallet=wallet,
            base_url=base_url,
            account_address=account_address,
            vault_address=cfg.vault_address,
        )
        executor = LiveExecutor(
            cfg=cfg, exchange=exchange, info=info, account_address=account_address
        )

    return info, executor, sz_decimals, account_address


strategy_ref: dict = {}


def main() -> int:
    log = setup_logging()
    import os as _os
    log.info("hl-mm starting (pid=%d)", _os.getpid())

    try:
        cfg = load_config()
    except Exception as exc:
        log.error("config error: %s", exc)
        return 2

    if cfg.manual_quoting_start and not cfg.dashboard_enabled:
        log.warning(
            "manual_quoting_start is true but dashboard is disabled — cannot arm quoting from the UI; "
            "treating manual_quoting_start as false for this run."
        )
        cfg = replace(cfg, manual_quoting_start=False)

    log.info(
        "config: network=%s mode=%s symbol=%s quote=%.2f USD max_pos=%.2f USD%s",
        cfg.network,
        cfg.mode,
        cfg.symbol,
        cfg.quote_notional_usd,
        cfg.max_position_notional_usd,
        " (manual_quoting_start — use dashboard to arm quotes)" if cfg.manual_quoting_start else "",
    )

    state = MarketState(
        vol_window_seconds=cfg.vol_window_seconds,
        sigma_ewma_alpha_up=cfg.sigma_ewma_alpha_up,
        sigma_ewma_alpha_down=cfg.sigma_ewma_alpha_down,
    )
    info, executor, sz_decimals, account_address = build_info_and_executor(cfg, state)

    strategy = Strategy(
        cfg=cfg,
        info=info,
        executor=executor,
        state=state,
        sz_decimals=sz_decimals,
        account_address=account_address,
    )
    strategy_ref["s"] = strategy

    # --- Hotkey actions ---
    def _do_cancel():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] cancel all orders")
            s.executor.cancel_all()

    def _do_pause():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] pausing quoting for 60 s")
            s.risk.trigger_pause(60.0, "manual hotkey pause")
            s.executor.cancel_all()

    def _do_resume():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] resuming immediately")
            s.risk._paused_until = 0.0

    def _do_flatten_buy():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] FLATTEN BUY SIDE — cancelling bid + closing long")
            s.executor.flatten_side(is_buy=True)

    def _do_flatten_sell():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] FLATTEN SELL SIDE — cancelling ask + closing short")
            s.executor.flatten_side(is_buy=False)

    def _do_flatten():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] FLATTEN ALL — market-closing entire position")
            s.executor.cancel_all()
            s.executor.flatten()

    def _do_quit():
        s = strategy_ref.get("s")
        if s:
            log.warning("[hotkey] quit requested")
            s.stop()

    hotkeys = HotkeyListener(
        on_cancel=_do_cancel,
        on_flatten_buy=_do_flatten_buy,
        on_flatten_sell=_do_flatten_sell,
        on_flatten=_do_flatten,
        on_pause=_do_pause,
        on_resume=_do_resume,
        on_quit=_do_quit,
    )
    hotkeys.start()

    # Unix signals as backup (e.g. from another terminal or a script)
    def _signal_cancel(signum, frame):
        log.warning("signal %d → cancel all", signum)
        _do_cancel()

    # Unix-only signals (not defined on Windows).
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _signal_cancel)
    if hasattr(signal, "SIGQUIT"):
        signal.signal(signal.SIGQUIT, _signal_cancel)

    dashboard = None
    if cfg.dashboard_enabled:
        try:
            dashboard = Dashboard(
                host=cfg.dashboard_host,
                port=cfg.dashboard_port,
                snapshot_provider=strategy.snapshot,
                cancel_fn=_do_cancel,
                flatten_buy_fn=_do_flatten_buy,
                flatten_sell_fn=_do_flatten_sell,
                update_config_fn=strategy.apply_runtime_update,
                start_quoting_fn=strategy.arm_quoting,
                stop_quoting_fn=strategy.disarm_quoting,
            )
            dashboard.start()
            log.info(
                "open the dashboard at http://%s:%d",
                cfg.dashboard_host,
                cfg.dashboard_port,
            )
        except OSError as exc:
            log.warning(
                "dashboard failed to start on %s:%d (%s) — continuing without it",
                cfg.dashboard_host,
                cfg.dashboard_port,
                exc,
            )
            dashboard = None

    try:
        strategy.start()
    finally:
        hotkeys.stop()
        if dashboard is not None:
            dashboard.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
