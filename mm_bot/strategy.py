"""Main market-making loop."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Union

from hyperliquid.info import Info

from .adaptive import SkewAdapter, disabled_snapshot as _disabled_skew_snapshot
from .config import BotConfig
from .executor import LiveExecutor
from .hl_utils import best_bid_ask
from .paper import PaperExecutor
from .quoter import Quote, Quoter, needs_requote
from .risk import RiskAction, RiskManager
from .signal import FairValueSignal
from .state import BookSnapshot, MarketState

log = logging.getLogger("strategy")

ExecutorAny = Union[LiveExecutor, PaperExecutor]


# ---------------------------------------------------------------------------
# Live-tunable configuration fields.
#   key:   BotConfig attribute name
#   value: (type, label, group, validator(value) -> Optional[str_error])
# Every entry here is exposed in the dashboard's "Live Tuning" panel and can
# be POSTed to /api/config. Anything not listed needs a process restart.
# ---------------------------------------------------------------------------
def _gt0(v):           return None if v > 0          else "must be > 0"
def _gte0(v):          return None if v >= 0         else "must be >= 0"
def _spread_floor(v):  return None if v >= 2.0       else "must be >= 2.0 bps (fee-loss floor)"
def _loop(v):          return None if 0.05 <= v <= 60 else "must be between 0.05 and 60"
def _forget(v):        return None if 0.5 <= v <= 1.0 else "must be between 0.5 and 1.0"
def _lev(v):           return None if 1 <= int(v) <= 50 else "must be between 1 and 50"
def _margin(v):        return None if v in ("cross", "isolated") else "must be 'cross' or 'isolated'"
def _eps(v):           return None if 0.0 <= v <= 1.0 else "must be between 0.0 and 1.0"
def _alpha(v):         return None if 0.0 < v <= 1.0 else "must be between 0.0 (exclusive) and 1.0"
def _sigma_alpha(v):   return None if 0.0 < v <= 1.0 else "must be in (0, 1]"
def _flatten_vol(v):   return None if 0.0 < v <= 1.0 else "must be in (0, 1] (1 = symmetric)"

TUNABLE_FIELDS: Dict[str, Dict[str, Any]] = {
    # Sizing
    "quote_notional_usd":         {"type": float, "group": "Sizing",   "label": "quote notional ($)",        "validate": _gt0},
    "max_position_notional_usd":  {"type": float, "group": "Sizing",   "label": "max position notional ($)", "validate": _gt0},
    # Pricing
    "min_half_spread_bps":        {"type": float, "group": "Pricing",  "label": "min half-spread (bps)",     "validate": _spread_floor},
    "vol_factor":                 {"type": float, "group": "Pricing",  "label": "vol factor",                "validate": _gte0},
    "sigma_ewma_alpha_up":        {"type": float, "group": "Pricing",  "label": "sigma EWMA alpha up",      "validate": _sigma_alpha},
    "sigma_ewma_alpha_down":      {"type": float, "group": "Pricing",  "label": "sigma EWMA alpha down",    "validate": _sigma_alpha},
    "inventory_flatten_vol_half_spread_mult": {
        "type": float,
        "group": "Pricing",
        "label": "flatten-side vol × on half-spread",
        "validate": _flatten_vol,
    },
    "inventory_skew_bps":         {"type": float, "group": "Pricing",  "label": "inventory skew (bps)",      "validate": _gte0},
    "requote_threshold_bps":      {"type": float, "group": "Pricing",  "label": "requote threshold (bps)",   "validate": _gte0},
    # Loop / freshness
    "loop_interval_seconds":      {"type": float, "group": "Loop",     "label": "loop interval (s)",         "validate": _loop},
    "max_book_age_seconds":       {"type": float, "group": "Loop",     "label": "max book age (s)",          "validate": _gt0},
    # Risk
    "max_session_loss_usd":       {"type": float, "group": "Risk",     "label": "max session loss ($)",      "validate": _gt0},
    "single_fill_pnl_alarm_usd":  {"type": float, "group": "Risk",     "label": "single-fill PnL alarm ($)", "validate": _gt0},
    "pause_seconds_after_alarm":  {"type": float, "group": "Risk",     "label": "pause after alarm (s)",     "validate": _gte0},
    "max_orders_per_minute":      {"type": int,   "group": "Risk",     "label": "max orders / minute",       "validate": _gt0},
    # ML
    "ml_max_signal_bps":          {"type": float, "group": "ML",       "label": "ML max signal (bps)",       "validate": _gt0},
    "ml_forgetting_factor":       {"type": float, "group": "ML",       "label": "ML forgetting factor",      "validate": _forget},
    "ml_min_updates":             {"type": int,   "group": "ML",       "label": "ML min updates",            "validate": _gte0},
    "ml_save":                    {"type": bool,  "group": "ML",       "label": "ML save to disk",           "validate": lambda v: None},
    # Adaptive skew (multi-armed bandit)
    "adaptive_skew_enabled":      {"type": bool,  "group": "Adaptive", "label": "adaptive skew enabled",      "validate": lambda v: None},
    "adaptive_skew_epsilon":      {"type": float, "group": "Adaptive", "label": "adaptive skew epsilon",      "validate": _eps},
    "adaptive_skew_alpha":        {"type": float, "group": "Adaptive", "label": "adaptive skew EWMA alpha",   "validate": _alpha},
    "adaptive_skew_min_pulls":    {"type": int,   "group": "Adaptive", "label": "adaptive min pulls/switch",  "validate": _gt0},
    # Leverage (live mode only — applied via exchange.update_leverage)
    "leverage":                   {"type": int,   "group": "Leverage", "label": "leverage (x)",              "validate": _lev},
    "margin_mode":                {"type": str,   "group": "Leverage", "label": "margin mode",               "validate": _margin},
}


class Strategy:
    def __init__(
        self,
        cfg: BotConfig,
        info: Info,
        executor: ExecutorAny,
        state: MarketState,
        sz_decimals: int,
        account_address: Optional[str] = None,
    ):
        self.cfg = cfg
        self.info = info
        self.executor = executor
        self.state = state
        self.account_address = account_address
        self.signal = FairValueSignal(
            horizon_s=cfg.ml_horizon_s,
            forgetting_factor=cfg.ml_forgetting_factor,
            max_signal_bps=cfg.ml_max_signal_bps,
            min_updates=cfg.ml_min_updates,
            load_saved=cfg.ml_load_saved,
            save_enabled=cfg.ml_save,
        )
        if cfg.adaptive_skew_enabled:
            self.skew_adapter: Optional[SkewAdapter] = SkewAdapter(
                base_skew_bps=cfg.inventory_skew_bps,
                multipliers=cfg.adaptive_skew_multipliers,
                epsilon=cfg.adaptive_skew_epsilon,
                ewma_alpha=cfg.adaptive_skew_alpha,
                min_pulls_per_switch=cfg.adaptive_skew_min_pulls,
            )
            if cfg.adaptive_skew_load_saved:
                try:
                    self.skew_adapter.load()
                except Exception as exc:
                    log.warning("[skew-adapter] load on init failed: %s", exc)
            else:
                log.info("adaptive_skew.load_saved=false — bandit starts fresh")
        else:
            self.skew_adapter = None
        self.quoter = Quoter(cfg, sz_decimals, signal=self.signal, skew_adapter=self.skew_adapter)
        self.risk = RiskManager(cfg)
        self._stop = threading.Event()
        self._book_sub_id: Optional[int] = None
        self._trades_sub_id: Optional[int] = None
        self._fills_sub_id: Optional[int] = None
        self._last_quote: Optional[Quote] = None
        self._status: str = "STARTING"
        # When cfg.manual_quoting_start is true, stays false until arm_quoting() (dashboard).
        self._quoting_armed: bool = not cfg.manual_quoting_start

    def arm_quoting(self) -> None:
        """Enable bid/ask placement (after ML warmup). Safe to call multiple times."""
        if self._quoting_armed:
            return
        log.warning("[quoting] manual arm — bid/ask placement ENABLED")
        self._quoting_armed = True

    def start(self) -> None:
        log.info(
            "starting strategy: network=%s mode=%s symbol=%s",
            self.cfg.network,
            self.cfg.mode,
            self.cfg.symbol,
        )

        self._book_sub_id = self.info.subscribe(
            {"type": "l2Book", "coin": self.cfg.symbol},
            self._on_book,
        )

        self._trades_sub_id = self.info.subscribe(
            {"type": "trades", "coin": self.cfg.symbol},
            self._on_trades,
        )

        if isinstance(self.executor, LiveExecutor) and self.account_address:
            self._fills_sub_id = self.info.subscribe(
                {"type": "userFills", "user": self.account_address},
                self._on_user_fill,
            )

        if self.cfg.cancel_all_on_start:
            self.executor.cancel_all()

        log.info("waiting for first book snapshot...")
        for _ in range(50):
            if self.state.book() is not None:
                break
            time.sleep(0.2)
        if self.state.book() is None:
            raise RuntimeError(
                f"No book data received for {self.cfg.symbol} within 10 seconds. "
                f"Check that the symbol exists on the {self.cfg.network} network."
            )

        try:
            self._loop()
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt -> graceful shutdown")
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        log.info("shutting down")
        if self.cfg.cancel_all_on_exit:
            try:
                self.executor.cancel_all()
            except Exception as exc:
                log.warning("cancel_all_on_exit failed: %s", exc)
        if self.risk.is_halted():
            log.warning("risk halted (%s) -> attempting flatten", self.risk.halt_reason())
            try:
                self.executor.flatten()
            except Exception as exc:
                log.warning("flatten failed: %s", exc)

        if self._book_sub_id is not None:
            try:
                self.info.unsubscribe(
                    {"type": "l2Book", "coin": self.cfg.symbol}, self._book_sub_id
                )
            except Exception:
                pass
        if self._trades_sub_id is not None:
            try:
                self.info.unsubscribe(
                    {"type": "trades", "coin": self.cfg.symbol}, self._trades_sub_id
                )
            except Exception:
                pass
        if self._fills_sub_id is not None and self.account_address:
            try:
                self.info.unsubscribe(
                    {"type": "userFills", "user": self.account_address},
                    self._fills_sub_id,
                )
            except Exception:
                pass
        try:
            self.info.disconnect_websocket()
        except Exception:
            pass

        if self.cfg.ml_save:
            try:
                self.signal.save()
                log.info("ML model saved on shutdown")
            except Exception as exc:
                log.warning("ML model save failed on shutdown: %s", exc)
        else:
            log.info("ML save disabled — not writing ml_model.json")

        if self.skew_adapter is not None and self.cfg.adaptive_skew_save:
            try:
                self.skew_adapter.save()
                log.info(
                    "skew adapter saved on shutdown (best arm: %.2fx mean_edge=%.2fbp)",
                    self.skew_adapter.arms[self.skew_adapter.best_arm_idx()].multiplier,
                    self.skew_adapter.arms[self.skew_adapter.best_arm_idx()].mean_edge_bps,
                )
            except Exception as exc:
                log.warning("skew adapter save failed on shutdown: %s", exc)
        elif self.skew_adapter is not None:
            log.info("adaptive skew save disabled — not writing skew_adapter.json")

        log.info(
            "final stats: fills=%d realized_pnl=%.4f USD position=%.6g",
            self.state.fills_count,
            self.state.realized_pnl_usd,
            self.state.position.size,
        )

    def _on_book(self, msg: Any) -> None:
        try:
            data = msg["data"]
            levels = data["levels"]
            bid_px, bid_sz, ask_px, ask_sz = best_bid_ask(levels)
            # Hyperliquid sends "time" in milliseconds. Sanity-check it: if the
            # resulting timestamp is more than 60 s away from now (clock skew
            # or SDK wrapping issue), fall back to local time so the stale-book
            # check doesn't permanently fire on valid data.
            raw_ts = data.get("time")
            if raw_ts is not None:
                candidate = float(raw_ts) / 1000.0
                ts = candidate if abs(candidate - time.time()) < 60 else time.time()
            else:
                ts = time.time()
            book = BookSnapshot(
                bid_px=bid_px,
                bid_sz=bid_sz,
                ask_px=ask_px,
                ask_sz=ask_sz,
                ts=ts,
            )
            self.state.update_book(book, raw_levels=levels)
            self.signal.on_book(ts, book.mid(), levels)
            if isinstance(self.executor, PaperExecutor):
                self.executor.check_fills(book, fill_prob=self.cfg.paper_fill_prob)
        except Exception as exc:
            log.warning("bad l2Book message: %s (%s)", exc, msg)

    def _on_trades(self, msg: Any) -> None:
        try:
            trades = msg["data"]
            for t in trades:
                ts = t.get("time", time.time() * 1000) / 1000.0
                side = t.get("side", "")
                is_buy = side == "B"
                sz = float(t.get("sz", 0))
                px = float(t.get("px", 0))
                if sz > 0 and px > 0:
                    self.signal.on_trade(ts, is_buy, sz, px)
        except Exception as exc:
            log.warning("bad trades message: %s (%s)", exc, msg)

    def _on_user_fill(self, msg: Any) -> None:
        try:
            fills = msg["data"].get("fills", [])
            for f in fills:
                if f.get("coin") != self.cfg.symbol:
                    continue
                side_is_buy = f.get("side") == "B"
                sz = float(f.get("sz", 0))
                px = float(f.get("px", 0))
                if sz <= 0 or px <= 0:
                    continue
                realized = self.state.record_fill(side_is_buy, sz, px)
                self._feed_skew_adapter(realized, sz, px)
                log.info(
                    "FILL %s %.6g @ %.6g realized=%.4f total_pnl=%.4f pos=%.6g",
                    "BUY" if side_is_buy else "SELL",
                    sz,
                    px,
                    realized,
                    self.state.total_pnl_usd(),
                    self.state.position.size,
                )
                if abs(realized) >= self.cfg.single_fill_pnl_alarm_usd:
                    log.warning(
                        "single-fill PnL alarm (%.2f USD) -> pausing %.1fs",
                        realized,
                        self.cfg.pause_seconds_after_alarm,
                    )
                    self.risk.trigger_pause(
                        self.cfg.pause_seconds_after_alarm, "single-fill alarm"
                    )
        except Exception as exc:
            log.warning("bad userFills message: %s (%s)", exc, msg)

    def _record_paper_fill(self, side_is_buy: bool, sz: float, px: float) -> None:
        realized = self.state.record_fill(side_is_buy, sz, px)
        self._feed_skew_adapter(realized, sz, px)
        log.info(
            "[paper] FILL %s %.6g @ %.6g realized=%.4f total_pnl=%.4f pos=%.6g",
            "BUY" if side_is_buy else "SELL",
            sz,
            px,
            realized,
            self.state.total_pnl_usd(),
            self.state.position.size,
        )
        if abs(realized) >= self.cfg.single_fill_pnl_alarm_usd:
            self.risk.trigger_pause(
                self.cfg.pause_seconds_after_alarm, "single-fill alarm"
            )

    def _feed_skew_adapter(self, realized_pnl: float, sz: float, px: float) -> None:
        """Forward closing fills to the adaptive-skew bandit. Opening fills
        (realized==0) carry no information about edge and are skipped."""
        if self.skew_adapter is None:
            return
        if abs(realized_pnl) < 1e-9:
            return
        notional = sz * px
        if notional <= 0:
            return
        try:
            self.skew_adapter.record_edge(realized_pnl, notional)
        except Exception as exc:
            log.warning("[skew-adapter] record_edge failed: %s", exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as exc:
                log.exception("tick failed: %s", exc)
            elapsed = time.time() - t0
            sleep_for = max(self.cfg.loop_interval_seconds - elapsed, 0.05)
            self._stop.wait(sleep_for)

    def _tick(self) -> None:
        self.state.record_tick()
        self.state.record_ml(self._ml_stats())

        decision = self.risk.evaluate(self.state)
        if decision.action == RiskAction.HALT:
            self._status = f"HALTED: {decision.reason}"
            log.error("HALT: %s", decision.reason)
            self._stop.set()
            return
        if decision.action == RiskAction.PAUSE:
            self._status = f"PAUSED: {decision.reason}"
            log.info("PAUSE: %s -> cancelling all", decision.reason)
            self.executor.cancel_all()
            return

        quote = self.quoter.desired_quote(self.state)
        self._last_quote = quote
        if quote is None:
            return

        if not self._quoting_armed:
            self._status = "RUNNING (ML warmup — orders off)"
            existing_bid = self.executor.state.by_side(True)
            existing_ask = self.executor.state.by_side(False)
            if existing_bid is not None:
                self.executor.cancel(existing_bid)
            if existing_ask is not None:
                self.executor.cancel(existing_ask)
            return

        self._status = "RUNNING"

        existing_bid = self.executor.state.by_side(True)
        existing_ask = self.executor.state.by_side(False)

        bid_drifted = needs_requote(
            existing_bid.px if existing_bid else None,
            quote.bid_px,
            self.cfg.requote_threshold_bps,
        )
        ask_drifted = needs_requote(
            existing_ask.px if existing_ask else None,
            quote.ask_px,
            self.cfg.requote_threshold_bps,
        )

        if quote.bid_sz > 0 and (bid_drifted or existing_bid is None):
            if self.risk.can_submit_order():
                self.executor.replace(True, quote.bid_px, quote.bid_sz)
                self.risk.record_order_submit()
        elif quote.bid_sz <= 0 and existing_bid is not None:
            self.executor.cancel(existing_bid)

        if quote.ask_sz > 0 and (ask_drifted or existing_ask is None):
            if self.risk.can_submit_order():
                self.executor.replace(False, quote.ask_px, quote.ask_sz)
                self.risk.record_order_submit()
        elif quote.ask_sz <= 0 and existing_ask is not None:
            self.executor.cancel(existing_ask)

        log.info(
            "tick %s | pos=%.6g pnl=%.4f sigma=%.2fbp/s | %s",
            self.cfg.symbol,
            self.state.position.size,
            self.state.total_pnl_usd(),
            self.state.vol_bps_per_sec(),
            quote,
        )

    def snapshot(self) -> Dict[str, Any]:
        """Combined snapshot of strategy + state + executor for the dashboard."""
        snap = self.state.snapshot()
        orders = []
        try:
            for o in list(self.executor.state.orders.values()):
                orders.append(
                    {
                        "side": "BUY" if o.is_buy else "SELL",
                        "px": o.px,
                        "sz": o.sz,
                        "cloid": getattr(o, "cloid_raw", None) or str(getattr(o, "cloid", "")),
                        "oid": getattr(o, "oid", None),
                    }
                )
        except Exception:
            orders = []

        snap.update(
            {
                "status": self._status,
                "quoting_armed": self._quoting_armed,
                "halted": self.risk.is_halted(),
                "halt_reason": self.risk.halt_reason(),
                "config": self._config_snapshot(),
                "tunable_spec": [
                    {
                        "key": k,
                        "label": spec["label"],
                        "group": spec["group"],
                        "type": spec["type"].__name__,
                    }
                    for k, spec in TUNABLE_FIELDS.items()
                ],
                "open_orders": orders,
                "quote": (
                    {
                        "bid_px": self._last_quote.bid_px,
                        "bid_sz": self._last_quote.bid_sz,
                        "ask_px": self._last_quote.ask_px,
                        "ask_sz": self._last_quote.ask_sz,
                        "fair_px": self._last_quote.fair_px,
                        "adjusted_fair_px": self._last_quote.adjusted_fair_px,
                        "half_spread_bps": self._last_quote.half_spread_bps,
                        "bid_half_spread_bps": self._last_quote.bid_half_spread_bps,
                        "ask_half_spread_bps": self._last_quote.ask_half_spread_bps,
                        "skew_bps": self._last_quote.skew_bps,
                        "signal_bps": self._last_quote.signal_bps,
                    }
                    if self._last_quote
                    else None
                ),
                "ml": self._ml_stats(),
                "skew_adapter": (
                    self.skew_adapter.snapshot()
                    if self.skew_adapter is not None
                    else _disabled_skew_snapshot(self.cfg.inventory_skew_bps)
                ),
            }
        )
        return snap

    def _config_snapshot(self) -> Dict[str, Any]:
        """Return the current values of every config field exposed to the UI.

        Includes both read-only fields (network/mode/symbol) and every entry
        in TUNABLE_FIELDS so the UI can pre-populate its inputs."""
        snap: Dict[str, Any] = {
            "network": self.cfg.network,
            "mode": self.cfg.mode,
            "symbol": self.cfg.symbol,
            "manual_quoting_start": self.cfg.manual_quoting_start,
            "ml_load_saved": self.cfg.ml_load_saved,
            "adaptive_skew_load_saved": self.cfg.adaptive_skew_load_saved,
            "adaptive_skew_save": self.cfg.adaptive_skew_save,
        }
        for key in TUNABLE_FIELDS:
            snap[key] = getattr(self.cfg, key, None)
        return snap

    def apply_runtime_update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Apply live config updates from the dashboard.

        Returns a dict of {ok, updated, errors}. Each requested field is
        coerced, validated, and assigned. Special fields (ML params,
        leverage) propagate to the relevant subsystems.
        """
        applied: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        for key, raw in (updates or {}).items():
            spec = TUNABLE_FIELDS.get(key)
            if spec is None:
                errors[key] = "field is not live-tunable"
                continue
            try:
                if spec["type"] is bool:
                    # bool("false") is True — be explicit about string inputs.
                    if isinstance(raw, bool):
                        value = raw
                    elif isinstance(raw, str):
                        value = raw.strip().lower() in ("1", "true", "yes", "on")
                    else:
                        value = bool(raw)
                else:
                    value = spec["type"](raw)
            except (TypeError, ValueError):
                errors[key] = f"could not coerce to {spec['type'].__name__}"
                continue
            err = spec["validate"](value)
            if err is not None:
                errors[key] = err
                continue

            old = getattr(self.cfg, key)
            if old == value:
                continue   # no-op

            try:
                setattr(self.cfg, key, value)
                self._propagate_runtime_change(key, value)
                applied[key] = {"old": old, "new": value}
                log.warning("[runtime] %s: %s -> %s", key, old, value)
            except Exception as exc:
                errors[key] = f"apply failed: {exc}"

        return {"ok": not errors, "updated": applied, "errors": errors}

    def _propagate_runtime_change(self, key: str, value: Any) -> None:
        """Push a freshly-set cfg value into any subsystem that cached it."""
        if key == "ml_max_signal_bps":
            self.signal.model.max_signal = float(value)
        elif key == "ml_forgetting_factor":
            self.signal.model.lam = float(value)
        elif key == "ml_min_updates":
            self.signal.model.min_updates = int(value)
        elif key == "ml_save":
            self.signal.set_save_enabled(bool(value))
        elif key == "inventory_skew_bps" and self.skew_adapter is not None:
            # Adapter's effective skew = base × multiplier; keep base in sync.
            self.skew_adapter.set_base_skew(float(value))
        elif key == "adaptive_skew_epsilon" and self.skew_adapter is not None:
            self.skew_adapter.set_epsilon(float(value))
        elif key == "adaptive_skew_alpha" and self.skew_adapter is not None:
            self.skew_adapter.alpha = float(value)
        elif key == "adaptive_skew_min_pulls" and self.skew_adapter is not None:
            self.skew_adapter.min_pulls_per_switch = int(value)
        elif key == "adaptive_skew_enabled":
            self._toggle_skew_adapter(bool(value))
        elif key in ("leverage", "margin_mode"):
            # Re-issue update_leverage on Hyperliquid (live mode only).
            if isinstance(self.executor, LiveExecutor):
                self.executor._configure_leverage()
            else:
                log.info("[runtime] leverage change saved but paper mode ignores it")
        elif key == "sigma_ewma_alpha_up":
            self.state._vol.sigma_ewma_alpha_up = float(value)
        elif key == "sigma_ewma_alpha_down":
            self.state._vol.sigma_ewma_alpha_down = float(value)

    def _toggle_skew_adapter(self, enable: bool) -> None:
        """Turn the adapter on/off live without restarting the bot."""
        if enable and self.skew_adapter is None:
            self.skew_adapter = SkewAdapter(
                base_skew_bps=self.cfg.inventory_skew_bps,
                multipliers=self.cfg.adaptive_skew_multipliers,
                epsilon=self.cfg.adaptive_skew_epsilon,
                ewma_alpha=self.cfg.adaptive_skew_alpha,
                min_pulls_per_switch=self.cfg.adaptive_skew_min_pulls,
            )
            if self.cfg.adaptive_skew_load_saved:
                try:
                    self.skew_adapter.load()
                except Exception:
                    pass
            self.quoter.skew_adapter = self.skew_adapter
            log.warning("[runtime] skew adapter ENABLED")
        elif (not enable) and self.skew_adapter is not None:
            if self.cfg.adaptive_skew_save:
                try:
                    self.skew_adapter.save()
                except Exception:
                    pass
            self.skew_adapter = None
            self.quoter.skew_adapter = None
            log.warning("[runtime] skew adapter DISABLED — using static cfg.inventory_skew_bps")

    def _ml_stats(self) -> Dict[str, Any]:
        try:
            s = self.signal.stats()
            return {
                "n_updates": s.n_updates,
                "mae_bps": round(s.mae_bps, 4),
                "correlation": round(s.correlation, 4),
                "last_signal_bps": round(self.signal.last_signal_bps(), 4),
                "warm": s.n_updates >= self.cfg.ml_min_updates,
                "weights": {
                    "bbo_imbalance": round(s.weights[0], 4) if len(s.weights) > 0 else 0,
                    "l2_imbalance": round(s.weights[1], 4) if len(s.weights) > 1 else 0,
                    "momentum_5s": round(s.weights[2], 4) if len(s.weights) > 2 else 0,
                    "momentum_15s": round(s.weights[3], 4) if len(s.weights) > 3 else 0,
                    "momentum_60s": round(s.weights[4], 4) if len(s.weights) > 4 else 0,
                    "trade_flow_5s": round(s.weights[5], 4) if len(s.weights) > 5 else 0,
                    "trade_flow_30s": round(s.weights[6], 4) if len(s.weights) > 6 else 0,
                    "spread_bps": round(s.weights[7], 4) if len(s.weights) > 7 else 0,
                    "funding": round(s.weights[8], 4) if len(s.weights) > 8 else 0,
                },
                "recent_pred": [round(x, 6) for x in s.recent_pred],
                "recent_actual": [round(x, 6) for x in s.recent_actual],
            }
        except Exception:
            return {}
