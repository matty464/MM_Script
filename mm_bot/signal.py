"""Online ML fair-value signal.

Model persistence: the RLS weight vector and covariance matrix are saved to
`state/ml_model.json` every `AUTOSAVE_INTERVAL_UPDATES` updates, and loaded
automatically on startup. This means learning accumulates across restarts.

Algorithm: Recursive Least Squares (RLS) with exponential forgetting.

RLS is the online equivalent of weighted linear regression. It processes
one observation at a time, updates weight vector and covariance in O(n²),
and forgets old data at rate (1 - forgetting_factor) per step.

Why RLS over gradient descent (SGD)?
- Converges much faster (no learning rate to tune).
- Numerically exact for linear models.
- Ideal for streaming financial data where the relationship between book
  features and short-term price changes shifts slowly over time.

Target variable:
  y = (mid[t + horizon_s] - mid[t]) / mid[t]  in basis points

Prediction pipeline each tick:
  1. Extract feature vector x (via FeatureExtractor)
  2. Predict: signal_bps = x @ w
  3. If we have a lagged observation (t-horizon_s), compute actual y and
     update the RLS weight vector with that (x_lagged, y_actual) pair.
  4. Return signal_bps for the quoter to use as a fair-value adjustment.

The quoter then does:
  adjusted_fair = micro_price * (1 + signal_bps / 1e4)

Positive signal -> model expects price to rise -> shift quotes up.
Negative signal -> model expects price to fall -> shift quotes down.
This means we quote better prices on the side we're already likely to get
filled on (reducing adverse selection) and worse prices on the other side.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from .features import FeatureExtractor

AUTOSAVE_INTERVAL_UPDATES = 50   # save every N model updates (~50s at 1s loop)
DEFAULT_STATE_DIR = "state"

log = logging.getLogger("signal")


@dataclass
class PredictionRecord:
    ts: float
    features: List[float]
    mid_at_prediction: float


@dataclass
class SignalStats:
    n_updates: int = 0
    mae_bps: float = 0.0          # mean abs error of recent predictions
    correlation: float = 0.0      # recent predicted vs actual correlation
    weights: List[float] = field(default_factory=list)
    # Recent (predicted_bps, actual_bps) pairs — exposed so the dashboard
    # can render a scatter showing how well predictions track reality.
    recent_pred: List[float] = field(default_factory=list)
    recent_actual: List[float] = field(default_factory=list)


class RLSPredictor:
    """Recursive Least Squares with exponential forgetting.

    Parameters
    ----------
    n_features : int
        Length of the feature vector.
    forgetting_factor : float
        λ in (0, 1]. 1.0 = no forgetting (OLS over all data).
        0.99 ≈ half-life of ~70 samples. 0.98 ≈ half-life of ~35 samples.
    delta : float
        Initial diagonal of P (inverse Hessian). Large delta = uninformed prior.
    max_signal_bps : float
        Hard clip on the predicted signal to prevent runaway predictions.
    horizon_s : float
        How many seconds ahead we are predicting.
    min_updates : int
        Don't use predictions until the model has seen this many samples.
    """

    def __init__(
        self,
        n_features: int,
        forgetting_factor: float = 0.990,
        delta: float = 1_000.0,
        max_signal_bps: float = 10.0,
        horizon_s: float = 10.0,
        min_updates: int = 30,
    ):
        self.n = n_features
        self.lam = forgetting_factor
        self.max_signal = max_signal_bps
        self.horizon_s = horizon_s
        self.min_updates = min_updates

        # Weight vector (initialized to zero = no prediction bias)
        self.w: List[float] = [0.0] * n_features
        # Inverse covariance matrix (initialized to delta * I)
        self.P: List[List[float]] = [[delta if i == j else 0.0 for j in range(n_features)] for i in range(n_features)]

        # Prediction queue for labeling (stores x and mid at time t,
        # so at time t+horizon we can compute the actual return)
        self._pending: Deque[PredictionRecord] = deque()

        # Stats tracking
        self._n_updates: int = 0
        self._recent_errors: Deque[float] = deque(maxlen=100)
        self._recent_pred: Deque[float] = deque(maxlen=100)
        self._recent_actual: Deque[float] = deque(maxlen=100)

    def predict_and_update(
        self, features: List[float], mid: float, ts: float
    ) -> Optional[float]:
        """Main entry point. Call once per tick.

        Returns the signal in bps (or None if not enough data yet).
        """
        x = features

        # --- Update: check if any pending predictions are ready to label ---
        while self._pending and ts >= self._pending[0].ts + self.horizon_s:
            record = self._pending.popleft()
            if record.mid_at_prediction > 0 and mid > 0:
                actual_bps = math.log(mid / record.mid_at_prediction) * 1e4
                self._rls_update(record.features, actual_bps)
                # Track stats
                pred_bps = self._dot(record.features, self.w)
                self._recent_errors.append(abs(pred_bps - actual_bps))
                self._recent_pred.append(pred_bps)
                self._recent_actual.append(actual_bps)

        # --- Predict ---
        self._pending.append(PredictionRecord(ts=ts, features=list(x), mid_at_prediction=mid))

        if self._n_updates < self.min_updates:
            return None  # model not warm yet

        raw_pred = self._dot(x, self.w)
        clipped = max(min(raw_pred, self.max_signal), -self.max_signal)
        return clipped

    def stats(self) -> SignalStats:
        mae_raw = (sum(self._recent_errors) / len(self._recent_errors)) if self._recent_errors else 0.0
        # Tiny MAE from a quiet window reads as 0.00 bps in the UI — apply a soft floor
        # only once we have enough samples.
        n_err = len(self._recent_errors)
        mae = max(mae_raw, 0.03) if n_err >= 12 and mae_raw < 0.03 else mae_raw
        corr = _pearson(list(self._recent_pred), list(self._recent_actual))
        return SignalStats(
            n_updates=self._n_updates,
            mae_bps=mae,
            correlation=corr,
            weights=list(self.w),
            recent_pred=list(self._recent_pred),
            recent_actual=list(self._recent_actual),
        )

    def _rls_update(self, x: List[float], y: float) -> None:
        """One step of RLS update."""
        n = self.n
        lam = self.lam

        # P x
        Px = [sum(self.P[i][j] * x[j] for j in range(n)) for i in range(n)]

        # x^T P x
        xTPx = sum(x[i] * Px[i] for i in range(n))

        # Gain vector k = P x / (λ + x^T P x)
        denom = lam + xTPx
        if abs(denom) < 1e-12:
            return
        k = [Px[i] / denom for i in range(n)]

        # Innovation: e = y - x^T w
        e = y - self._dot(x, self.w)

        # Update weights: w = w + k * e
        for i in range(n):
            self.w[i] += k[i] * e

        # Update P: P = (P - k (P x)^T) / λ  ... note (P x) = Px
        for i in range(n):
            for j in range(n):
                self.P[i][j] = (self.P[i][j] - k[i] * Px[j]) / lam

        self._n_updates += 1

    def to_dict(self) -> dict:
        """Serialize model state to a JSON-safe dict."""
        return {
            "n": self.n,
            "n_updates": self._n_updates,
            "w": self.w,
            "P": self.P,
            "forgetting_factor": self.lam,
            "max_signal": self.max_signal,
            "horizon_s": self.horizon_s,
            "min_updates": self.min_updates,
        }

    def load_dict(self, d: dict) -> None:
        """Restore model state from a previously saved dict."""
        if d.get("n") != self.n:
            raise ValueError(
                f"Saved model has n={d.get('n')} features but current model has n={self.n}. "
                f"Delete state/ml_model.json to start fresh."
            )
        self.w = list(d["w"])
        self.P = [list(row) for row in d["P"]]
        self._n_updates = int(d.get("n_updates", 0))
        log.info(
            "RLS model loaded: n_updates=%d w=%s",
            self._n_updates,
            [round(x, 4) for x in self.w],
        )

    @staticmethod
    def _dot(a: List[float], b: List[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


def _pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 8:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    ssx = sum((x - mx) ** 2 for x in xs)
    ssy = sum((y - my) ** 2 for y in ys)
    # If either series barely moved, correlation is ill-defined (would blow up to ±1).
    if ssx < 1e-6 or ssy < 1e-6:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    r = num / (math.sqrt(ssx * ssy) + 1e-15)
    return max(-1.0, min(1.0, r))


class FairValueSignal:
    """Combines feature extraction and RLS prediction into one object
    that the strategy can call with a single method.

    Model is automatically saved to `state_dir/ml_model.json` every
    AUTOSAVE_INTERVAL_UPDATES updates and loaded from there on startup,
    so learning persists across restarts.
    """

    def __init__(
        self,
        horizon_s: float = 10.0,
        forgetting_factor: float = 0.990,
        max_signal_bps: float = 10.0,
        min_updates: int = 30,
        state_dir: str = DEFAULT_STATE_DIR,
    ):
        self.extractor = FeatureExtractor()
        self.model = RLSPredictor(
            n_features=FeatureExtractor.N_FEATURES,
            forgetting_factor=forgetting_factor,
            max_signal_bps=max_signal_bps,
            horizon_s=horizon_s,
            min_updates=min_updates,
        )
        self._last_signal_bps: float = 0.0
        self._enabled: bool = True
        self._state_dir = Path(state_dir)
        self._model_path = self._state_dir / "ml_model.json"
        self._updates_since_save: int = 0

        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._model_path.exists():
            log.info("no saved ML model found at %s — starting fresh", self._model_path)
            return
        try:
            with open(self._model_path, "r") as f:
                d = json.load(f)
            self.model.load_dict(d)
            log.info(
                "ML model loaded from %s (%d previous updates)",
                self._model_path,
                self.model._n_updates,
            )
        except Exception as exc:
            log.warning(
                "failed to load ML model from %s (%s) — starting fresh", self._model_path, exc
            )

    def save(self) -> None:
        """Save model weights to disk. Called automatically + on shutdown."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._model_path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(self.model.to_dict(), f)
            os.replace(tmp, self._model_path)  # atomic rename
            log.debug(
                "ML model saved (%d updates) -> %s", self.model._n_updates, self._model_path
            )
            self._updates_since_save = 0
        except Exception as exc:
            log.warning("failed to save ML model: %s", exc)

    def _maybe_autosave(self) -> None:
        prev = self.model._n_updates
        # Check if an update just happened by comparing update counter
        new_updates = self.model._n_updates - (prev - self._updates_since_save)
        self._updates_since_save += 1
        if self._updates_since_save >= AUTOSAVE_INTERVAL_UPDATES:
            self.save()

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def on_trade(self, ts: float, is_buy: bool, sz: float, px: float) -> None:
        self.extractor.on_trade(ts, is_buy, sz, px)

    def on_book(self, ts: float, mid: float, levels=None) -> None:
        self.extractor.on_book(ts, mid, levels)

    def on_funding(self, premium: float) -> None:
        self.extractor.on_funding(premium)

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, bid_px: float, bid_sz: float, ask_px: float, ask_sz: float) -> float:
        """Return fair-value adjustment in bps (positive = price going up).

        Returns 0.0 if the model is not warm yet (min_updates not reached).
        """
        if not self._enabled:
            return 0.0

        features = self.extractor.extract(bid_px, bid_sz, ask_px, ask_sz)
        if features is None:
            return 0.0

        prev_updates = self.model._n_updates
        mid = 0.5 * (bid_px + ask_px)
        signal = self.model.predict_and_update(features, mid, time.time())

        # Autosave whenever the model receives a new training update
        if self.model._n_updates > prev_updates:
            self._updates_since_save += 1
            if self._updates_since_save >= AUTOSAVE_INTERVAL_UPDATES:
                self.save()

        if signal is None:
            return 0.0

        self._last_signal_bps = signal
        return signal

    def stats(self) -> SignalStats:
        return self.model.stats()

    def last_signal_bps(self) -> float:
        return self._last_signal_bps
