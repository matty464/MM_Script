"""Adaptive inventory-skew controller (epsilon-greedy multi-armed bandit).

The bandit operates over a small set of multipliers applied to the user's
configured ``inventory_skew_bps``. After every closing fill (a fill that
realizes PnL because it reduces or flips the existing position), we feed
the per-fill edge in basis points into the active arm's reward EWMA.

Periodically — once an arm has been observed at least
``min_pulls_per_switch`` times — we reconsider:

* with probability ``epsilon`` we pick a random arm to keep exploring,
* otherwise we lock onto the arm with the highest mean edge.

State is persisted to ``state/skew_adapter.json`` so the bandit's belief
about which multiplier is best survives restarts (analogous to the RLS
weights for the ML signal).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("adaptive")

# Skew multipliers around the user's configured base. 1.0 = exactly your
# config; <1.0 = hold inventory longer (collect more spread, more risk);
# >1.0 = push out faster (less risk, fewer fills).
DEFAULT_MULTIPLIERS: Sequence[float] = (0.5, 0.75, 1.0, 1.25, 1.5)
DEFAULT_PATH = "state/skew_adapter.json"

# Sanity clamp for per-fill edge — a single freak fill shouldn't dominate
# the EWMA. ±100 bps = ±1% per round-trip is way beyond anything realistic
# for a market-making strategy.
EDGE_CLAMP_BPS = 100.0


@dataclass
class ArmStats:
    multiplier: float
    pulls: int = 0
    mean_edge_bps: float = 0.0   # EWMA of per-fill edge while this arm was active
    last_edge_bps: float = 0.0
    cum_realized_usd: float = 0.0
    cum_notional_usd: float = 0.0
    last_used_ts: float = 0.0


class SkewAdapter:
    """Online bandit choosing the best multiplier for ``inventory_skew_bps``."""

    def __init__(
        self,
        base_skew_bps: float,
        multipliers: Optional[Sequence[float]] = None,
        epsilon: float = 0.15,
        ewma_alpha: float = 0.2,
        min_pulls_per_switch: int = 5,
        save_path: str = DEFAULT_PATH,
    ):
        mults = list(multipliers) if multipliers else list(DEFAULT_MULTIPLIERS)
        # Deduplicate + sort so the dashboard renders nicely.
        mults = sorted({round(float(m), 6) for m in mults if m > 0})
        if not mults:
            raise ValueError("SkewAdapter requires at least one positive multiplier")

        self.arms: List[ArmStats] = [ArmStats(multiplier=m) for m in mults]
        self.epsilon = float(epsilon)
        self.alpha = float(ewma_alpha)
        self.min_pulls_per_switch = int(min_pulls_per_switch)
        self.save_path = save_path
        self.base_skew_bps = float(base_skew_bps)
        self.current_arm_idx = self._closest_to_one()
        self.pulls_in_current_arm = 0
        self.total_observations = 0
        self.last_switch_ts = 0.0

    # ------------------------------------------------------------------
    # Read access
    # ------------------------------------------------------------------
    def current_arm(self) -> ArmStats:
        return self.arms[self.current_arm_idx]

    def effective_skew_bps(self) -> float:
        return self.base_skew_bps * self.current_arm().multiplier

    def best_arm_idx(self) -> int:
        """Index of the highest-mean arm with at least one observation;
        falls back to the closest-to-1.0 arm if no data yet."""
        observed = [i for i, a in enumerate(self.arms) if a.pulls > 0]
        if not observed:
            return self._closest_to_one()
        return max(observed, key=lambda i: self.arms[i].mean_edge_bps)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def set_base_skew(self, base: float) -> None:
        self.base_skew_bps = float(base)

    def set_epsilon(self, eps: float) -> None:
        self.epsilon = max(0.0, min(1.0, float(eps)))

    def record_edge(self, realized_pnl_usd: float, fill_notional_usd: float) -> None:
        """Feed the result of a single closing fill into the active arm.

        ``realized_pnl_usd`` may be negative (losing fill).
        ``fill_notional_usd`` should be ``fill_size * fill_price``.
        Non-closing fills (``realized == 0``) should never be passed in.
        """
        if fill_notional_usd <= 0:
            return
        edge_bps = (realized_pnl_usd / fill_notional_usd) * 1e4
        # Defensive clamp — outliers shouldn't whip the EWMA around.
        edge_bps = max(-EDGE_CLAMP_BPS, min(EDGE_CLAMP_BPS, edge_bps))

        arm = self.current_arm()
        arm.pulls += 1
        arm.last_edge_bps = edge_bps
        arm.last_used_ts = time.time()
        arm.cum_realized_usd += realized_pnl_usd
        arm.cum_notional_usd += fill_notional_usd
        if arm.pulls == 1:
            arm.mean_edge_bps = edge_bps
        else:
            arm.mean_edge_bps = (1 - self.alpha) * arm.mean_edge_bps + self.alpha * edge_bps

        self.pulls_in_current_arm += 1
        self.total_observations += 1
        self._maybe_switch()

    # ------------------------------------------------------------------
    # Internal: arm selection
    # ------------------------------------------------------------------
    def _closest_to_one(self) -> int:
        return min(
            range(len(self.arms)),
            key=lambda i: abs(self.arms[i].multiplier - 1.0),
        )

    def _maybe_switch(self) -> None:
        if self.pulls_in_current_arm < self.min_pulls_per_switch:
            return

        if random.random() < self.epsilon:
            # Explore a different arm (uniformly across the others).
            others = [i for i in range(len(self.arms)) if i != self.current_arm_idx]
            new_idx = random.choice(others) if others else self.current_arm_idx
            mode = "explore"
        else:
            new_idx = self.best_arm_idx()
            mode = "exploit"

        if new_idx != self.current_arm_idx:
            old = self.arms[self.current_arm_idx]
            new = self.arms[new_idx]
            log.warning(
                "[skew-adapter] %s switch: %.2fx (mean=%.2fbp pulls=%d) -> %.2fx (mean=%.2fbp pulls=%d)",
                mode,
                old.multiplier,
                old.mean_edge_bps,
                old.pulls,
                new.multiplier,
                new.mean_edge_bps,
                new.pulls,
            )
            self.current_arm_idx = new_idx
            self.pulls_in_current_arm = 0
            self.last_switch_ts = time.time()

    # ------------------------------------------------------------------
    # Snapshot for dashboard
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        best_idx = self.best_arm_idx()
        return {
            "enabled": True,
            "base_skew_bps": round(self.base_skew_bps, 4),
            "effective_skew_bps": round(self.effective_skew_bps(), 4),
            "current_arm_idx": self.current_arm_idx,
            "best_arm_idx": best_idx,
            "current_multiplier": self.current_arm().multiplier,
            "best_multiplier": self.arms[best_idx].multiplier,
            "epsilon": self.epsilon,
            "alpha": self.alpha,
            "min_pulls_per_switch": self.min_pulls_per_switch,
            "total_observations": self.total_observations,
            "pulls_in_current_arm": self.pulls_in_current_arm,
            "last_switch_ts": self.last_switch_ts,
            "arms": [
                {
                    "multiplier": a.multiplier,
                    "pulls": a.pulls,
                    "mean_edge_bps": round(a.mean_edge_bps, 3),
                    "last_edge_bps": round(a.last_edge_bps, 3),
                    "cum_realized_usd": round(a.cum_realized_usd, 4),
                    "cum_notional_usd": round(a.cum_notional_usd, 2),
                    "is_active": i == self.current_arm_idx,
                    "is_best": i == best_idx,
                }
                for i, a in enumerate(self.arms)
            ],
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self, path: Optional[str] = None) -> None:
        p = Path(path or self.save_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "current_arm_idx": self.current_arm_idx,
                "pulls_in_current_arm": self.pulls_in_current_arm,
                "total_observations": self.total_observations,
                "base_skew_bps": self.base_skew_bps,
                "epsilon": self.epsilon,
                "alpha": self.alpha,
                "min_pulls_per_switch": self.min_pulls_per_switch,
                "last_switch_ts": self.last_switch_ts,
                "arms": [asdict(a) for a in self.arms],
            }
            with open(p, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            log.warning("[skew-adapter] save failed: %s", exc)

    def load(self, path: Optional[str] = None) -> None:
        p = Path(path or self.save_path)
        if not p.exists():
            return
        try:
            with open(p) as f:
                data = json.load(f)
            # Rehydrate arms by matching multiplier (so changing the multiplier
            # set in config doesn't blow up: unmatched saved arms are dropped,
            # new ones start fresh).
            for sa in data.get("arms", []):
                m = float(sa.get("multiplier", -1))
                for arm in self.arms:
                    if abs(arm.multiplier - m) < 1e-6:
                        arm.pulls = int(sa.get("pulls", 0))
                        arm.mean_edge_bps = float(sa.get("mean_edge_bps", 0.0))
                        arm.last_edge_bps = float(sa.get("last_edge_bps", 0.0))
                        arm.cum_realized_usd = float(sa.get("cum_realized_usd", 0.0))
                        arm.cum_notional_usd = float(sa.get("cum_notional_usd", 0.0))
                        arm.last_used_ts = float(sa.get("last_used_ts", 0.0))
                        break
            saved_idx = int(data.get("current_arm_idx", self.current_arm_idx))
            if 0 <= saved_idx < len(self.arms):
                self.current_arm_idx = saved_idx
            self.pulls_in_current_arm = int(data.get("pulls_in_current_arm", 0))
            self.total_observations = int(data.get("total_observations", 0))
            self.last_switch_ts = float(data.get("last_switch_ts", 0.0))
            log.info(
                "[skew-adapter] loaded state: arm=%.2fx total_obs=%d (best so far: %.2fx mean=%.2fbp)",
                self.current_arm().multiplier,
                self.total_observations,
                self.arms[self.best_arm_idx()].multiplier,
                self.arms[self.best_arm_idx()].mean_edge_bps,
            )
        except Exception as exc:
            log.warning("[skew-adapter] load failed: %s", exc)


def disabled_snapshot(base_skew_bps: float) -> Dict[str, Any]:
    """Snapshot returned when the adapter is disabled — keeps the dashboard
    schema stable so the UI can render a 'disabled' state."""
    return {
        "enabled": False,
        "base_skew_bps": round(float(base_skew_bps), 4),
        "effective_skew_bps": round(float(base_skew_bps), 4),
        "current_multiplier": 1.0,
        "arms": [],
    }
