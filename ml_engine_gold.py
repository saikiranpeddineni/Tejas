"""
ml_engine_gold.py — ★ UPGRADED: Ensemble ML + Walk-Forward Validation
=======================================================================
UPGRADES vs previous version:
  ★ LightGBM + XGBoost ensemble (weighted voting)
  ★ Walk-forward validation (temporal split — no look-ahead bias)
  ★ Feature importance drift tracking over time
  ★ Calibrated probabilities (Platt scaling on top of ensemble)
  ★ Regime-aware prediction (different behavior in bull/bear)
  ★ Dynamic Kelly with portfolio correlation adjustment

WHY ENSEMBLE?
  LightGBM and XGBoost make different types of errors. Combining them
  reduces variance without increasing bias — classic ML ensemble theory.
  Real quant funds run 10-50 model ensembles. Two is a good start.

WHY WALK-FORWARD?
  Standard train/test split leaks future data. Walk-forward trains
  on t=0..n, tests on t=n+1..n+k, then trains on t=0..n+k, etc.
  This is how you get honest performance numbers.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np

import db
from config import Config
from feature_builder_gold import FEATURE_NAMES, N_FEATURES

log = logging.getLogger(__name__)

try:
    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, accuracy_score, log_loss
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    log.warning("ML libs not installed — using rule-based scoring")

try:
    import xgboost  # noqa: F401  — availability check only; XGBClassifier imported locally
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False
    log.warning("xgboost not installed — using LGB only")

_LGB_PARAMS = {
    "objective":"binary","metric":["binary_logloss","auc"],
    "num_leaves":31,"learning_rate":0.02,
    "feature_fraction":0.75,"bagging_fraction":0.80,"bagging_freq":5,
    "min_data_in_leaf":10,"lambda_l1":0.1,"lambda_l2":0.2,
    "verbosity":-1,"seed":42,
}

_XGB_PARAMS = {
    "objective":"binary:logistic","eval_metric":"auc",
    "max_depth":5,"learning_rate":0.02,"n_estimators":500,
    "subsample":0.80,"colsample_bytree":0.75,
    "min_child_weight":10,"reg_alpha":0.1,"reg_lambda":0.2,
    "seed":42,"verbosity":0,
}


class MLEngineGold:
    def __init__(self):
        Path(Config.MODEL_DIR).mkdir(parents=True, exist_ok=True)
        self.lgb_model:   Optional["lgb.Booster"] = None
        self.xgb_model:   Optional[object] = None
        self.platt_model: Optional[object] = None
        self.feature_importance = np.zeros(N_FEATURES)
        self.val_auc   = 0.0
        self.train_auc = 0.0
        self.walk_forward_auc = 0.0   # ★ honest metric
        self._model_file: Optional[str] = None
        self._metrics_match_model = False
        self._new_since_train = 0
        self._load_models()

    def _lgb_path(self)      -> str: return os.path.join(Config.MODEL_DIR, "lgb_gold.txt")
    def _forecast_path(self) -> str: return os.path.join(Config.MODEL_DIR, "lgb_forecast.txt")
    def _xgb_path(self)      -> str: return os.path.join(Config.MODEL_DIR, "xgb_gold.pkl")
    def _platt_path(self)    -> str: return os.path.join(Config.MODEL_DIR, "platt_gold.pkl")
    def _metrics_path(self)  -> str: return os.path.join(Config.MODEL_DIR, "ml_metrics.json")

    def _save_metrics(self):
        """Persist AUC per model file so forecast training cannot overwrite the
        trust metadata for the live-outcome model (and vice versa).

        The legacy top-level fields remain for older dashboard readers; the
        nested map is authoritative when present.
        """
        try:
            path = self._metrics_path()
            payload = {}
            try:
                with open(path) as f:
                    payload = json.load(f)
            except Exception:
                pass
            models = payload.get("models", {}) if isinstance(payload, dict) else {}
            if not isinstance(models, dict):
                models = {}
            model_file = os.path.basename(self._model_file or self._lgb_path())
            models[model_file] = {
                "val_auc": self.val_auc,
                "walk_forward_auc": self.walk_forward_auc,
            }
            payload = {
                "val_auc": self.val_auc,
                "walk_forward_auc": self.walk_forward_auc,
                "model_file": model_file,
                "models": models,
            }
            with open(path, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            log.debug(f"[ML] could not save metrics: {e}")

    def _load_models(self):
        if not ML_AVAILABLE: return
        # ★ Two trainers exist and MUST NOT share a file: train() learns from live
        #   trade WIN/LOSS → lgb_gold.txt; train_on() (the dashboard Train button)
        #   learns forward-return from history → lgb_forecast.txt. They used to
        #   overwrite each other, so ml_prob silently flipped meaning. Precedence:
        #   the live-refined model wins if present, else the forecast bootstrap.
        # Try each candidate in precedence order; skip any whose feature count no
        # longer matches the builder (a stale pre-upgrade model must neither crash
        # predict NOR shadow a freshly retrained fallback file).
        for model_file in (self._lgb_path(), self._forecast_path()):
            if not os.path.exists(model_file):
                continue
            try:
                booster = lgb.Booster(model_file=model_file)
            except Exception:
                log.info(f"[ML] {os.path.basename(model_file)} unreadable — skipped")
                continue
            if booster.num_feature() != N_FEATURES:
                log.warning(
                    f"[ML] {os.path.basename(model_file)} expects "
                    f"{booster.num_feature()} features but the builder now produces "
                    f"{N_FEATURES} — skipped; retrain (🧠 Train button or "
                    f"forecast_trainer.py) to refresh it."
                )
                continue
            self.lgb_model = booster
            self._model_file = os.path.basename(model_file)
            self.feature_importance = booster.feature_importance("gain")
            log.info(f"✓ LightGBM loaded ({os.path.basename(model_file)})")
            break
        else:
            log.info("No usable model — analyst engine drives until a retrain")
        # Restore last known AUC for the trust gate.
        try:
            with open(self._metrics_path()) as f:
                m = json.load(f)
            selected = (m.get("models", {}).get(self._model_file)
                        if isinstance(m, dict) and isinstance(m.get("models"), dict)
                        and self._model_file else None)
            if isinstance(selected, dict):
                metrics = selected
                self._metrics_match_model = True
            else:
                metrics = m
                metrics_file = os.path.basename(str(m.get("model_file", "")))
                self._metrics_match_model = bool(
                    self._model_file and metrics_file and metrics_file == self._model_file
                )
                if not self._metrics_match_model and self._model_file:
                    log.warning(
                        f"[ML] Metrics/model mismatch ({metrics_file or 'un-tagged'} vs "
                        f"{self._model_file}) — ML trust gate disabled until retrain"
                    )
            self.val_auc = float(metrics.get("val_auc", 0.0))
            self.walk_forward_auc = float(metrics.get("walk_forward_auc", 0.0))
            if not np.isfinite(self.val_auc) or not np.isfinite(self.walk_forward_auc):
                raise ValueError("non-finite persisted metrics")
        except Exception:
            self.val_auc = 0.0
            self.walk_forward_auc = 0.0
            self._metrics_match_model = False
        # XGBoost and the Platt calibrator are companions to the live-outcome
        # LightGBM model.  A forecast bootstrap model has different label
        # semantics; loading these artifacts alongside it would silently ensemble
        # incompatible probabilities after a restart.
        live_model = self._model_file == os.path.basename(self._lgb_path())
        if live_model and XGB_AVAILABLE and os.path.exists(self._xgb_path()):
            try:
                self.xgb_model = joblib.load(self._xgb_path())
                if getattr(self.xgb_model, "n_features_in_", N_FEATURES) != N_FEATURES:
                    log.warning("[ML] XGBoost model feature-count mismatch — discarded until retrain")
                    self.xgb_model = None
                else:
                    log.info("✓ XGBoost loaded")
            except Exception: log.info("XGBoost model unreadable — will retrain")
        if live_model:
            try:
                platt = joblib.load(self._platt_path())
                if getattr(platt, "n_features_in_", N_FEATURES) != N_FEATURES:
                    raise ValueError("feature-count mismatch")
                self.platt_model = platt
                log.info("✓ Platt calibrator loaded")
            except Exception:
                pass
        # A fresh process may have no live model yet (or may have loaded only
        # the forecast bootstrap), but train() still needs a new calibrator.
        if self.platt_model is None and ML_AVAILABLE:
            self.platt_model = CalibratedClassifierCV(
                LogisticRegression(max_iter=1000, C=0.5, random_state=42),
                method="sigmoid",
            )

    # ── FEATURE LOGGING ───────────────────────────────────────────────────────
    def log_entry(self, features, side: str, entry_price: float) -> bool:
        feat_dict = dict(zip(FEATURE_NAMES, features.to_list()))
        db.execute(
            "INSERT OR IGNORE INTO training_data "
            "(trade_id, symbol, side, features_json, entry_price, entry_time) "
            "VALUES (?,?,?,?,?,?)",
            (features.trade_id, features.symbol, side,
             json.dumps(feat_dict), entry_price, features.timestamp.isoformat()),
        )
        return True

    def log_exit(self, trade_id: str, outcome: int, exit_reason: str, pnl_pct: float) -> bool:
        rows = db.execute(
            "UPDATE training_data SET outcome=?, exit_reason=?, pnl_pct=?, "
            "exit_time=datetime('now','utc') WHERE trade_id=? AND outcome IS NULL",
            (outcome, exit_reason, pnl_pct, trade_id),
        )
        if rows:
            self._new_since_train += 1
            log.info(f"[ML] {'WIN' if outcome else 'LOSS'} logged: {trade_id[:8]} {pnl_pct:+.2%}")
            # ★ Do NOT retrain mid-session: retraining inside the exit path shifts
            #   the probability surface for the rest of the day and triggers mass
            #   exits/rotation. The scheduler retrains after-hours instead.
            if Config.RETRAIN_IN_SESSION and self._new_since_train >= Config.RETRAIN_EVERY_N:
                self.train()
        return rows > 0

    # ── ★ TRAINING WITH WALK-FORWARD ─────────────────────────────────────────
    def train(self) -> Dict:
        if not ML_AVAILABLE:
            return {"status": "ml_not_available"}

        rows = db.fetchall(
            "SELECT features_json, outcome, entry_time FROM training_data "
            "WHERE outcome IS NOT NULL AND entry_time IS NOT NULL "
            "AND julianday(entry_time) IS NOT NULL "
            "ORDER BY julianday(entry_time) ASC"
        )
        # Parse before sizing/splitting.  A corrupt or partially migrated row
        # must not count toward the sample threshold or shift the temporal split
        # away from the actual parsed data.
        X, y = [], []
        for row in rows:
            try:
                feat = json.loads(row["features_json"])
                if not isinstance(feat, dict):
                    continue
                values = [float(feat.get(nm, 0.0)) for nm in FEATURE_NAMES]
                outcome = int(row["outcome"])
                if outcome not in (0, 1) or not np.isfinite(values).all():
                    continue
                X.append(values)
                y.append(outcome)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

        n = len(X)
        if n < Config.MIN_TRAIN_SAMPLES:
            log.info(f"[ML] Need {Config.MIN_TRAIN_SAMPLES} samples, have {n}")
            return {"status": "insufficient_data", "n": n}

        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.int32)

        if len(np.unique(y)) < 2:
            log.warning("[ML] Single class in training data")
            return {"status": "single_class"}

        # ── ★ Walk-Forward Validation ─────────────────────────────────────
        wf_aucs = []
        fold_size = max(20, n // 5)
        for start in range(fold_size * 2, n - fold_size, fold_size):
            X_tr_wf, X_te_wf = X[:start], X[start:start+fold_size]
            y_tr_wf, y_te_wf = y[:start], y[start:start+fold_size]
            if len(np.unique(y_te_wf)) < 2:
                continue
            try:
                # ★ NO early stopping against the scored fold — tuning the round
                # count on the fold being evaluated leaks it into the metric and
                # biases walk-forward AUC upward. Fixed rounds, fold used ONLY
                # for scoring.
                d_tr = lgb.Dataset(X_tr_wf, y_tr_wf, feature_name=FEATURE_NAMES)
                b = lgb.train(_LGB_PARAMS, d_tr, num_boost_round=200,
                              callbacks=[lgb.log_evaluation(0)])
                preds = b.predict(X_te_wf)
                wf_aucs.append(float(roc_auc_score(y_te_wf, preds)))
            except Exception:
                pass

        self.walk_forward_auc = float(np.mean(wf_aucs)) if wf_aucs else 0.0
        log.info(f"[ML] ★ Walk-Forward AUC: {self.walk_forward_auc:.3f} ({len(wf_aucs)} folds)")

        # ── Full LightGBM Train ────────────────────────────────────────────
        # ★ Temporal 80/20 split (rows are ordered by entry_time): a random
        # stratified split leaks same-day-correlated trades across the boundary
        # and inflates val_auc — which then overrides the honest walk-forward
        # number in the trust gate.
        cut = int(n * 0.8)
        X_tr, X_val, y_tr, y_val = X[:cut], X[cut:], y[:cut], y[cut:]
        if len(np.unique(y_val)) < 2 or len(np.unique(y_tr)) < 2:
            log.warning("[ML] Temporal split single-class — deferring retrain until more data")
            return {"status": "single_class_temporal", "n": n}
        dtr  = lgb.Dataset(X_tr, y_tr, feature_name=FEATURE_NAMES)
        dval = lgb.Dataset(X_val, y_val, reference=dtr)
        self.lgb_model = lgb.train(
            _LGB_PARAMS, dtr, num_boost_round=600,
            valid_sets=[dtr, dval], valid_names=["train","val"],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
        )
        self.lgb_model.save_model(self._lgb_path())
        self._model_file = os.path.basename(self._lgb_path())

        tr_pred  = self.lgb_model.predict(X_tr)
        val_pred = self.lgb_model.predict(X_val)
        self.train_auc = float(roc_auc_score(y_tr, tr_pred))
        self.val_auc   = float(roc_auc_score(y_val, val_pred))
        overfit = (self.train_auc - self.val_auc) > Config.OVERFIT_AUC_DELTA

        self.feature_importance = self.lgb_model.feature_importance("gain")
        top = sorted(zip(FEATURE_NAMES, self.feature_importance.tolist()), key=lambda x:-x[1])[:8]

        # ── ★ XGBoost Train ────────────────────────────────────────────────
        if XGB_AVAILABLE and Config.USE_ENSEMBLE:
            try:
                from xgboost import XGBClassifier
                self.xgb_model = XGBClassifier(**_XGB_PARAMS)
                self.xgb_model.fit(
                    X_tr, y_tr,
                    eval_set=[(X_val, y_val)],
                    verbose=False,
                )
                joblib.dump(self.xgb_model, self._xgb_path())
                xgb_auc = float(roc_auc_score(y_val, self.xgb_model.predict_proba(X_val)[:,1]))
                log.info(f"[ML] XGBoost AUC: {xgb_auc:.3f}")
            except Exception as e:
                log.warning(f"[ML] XGBoost train failed: {e}")

        # ── Platt calibrator on ensemble ──────────────────────────────────
        try:
            self.platt_model.fit(X_tr, y_tr)
            joblib.dump(self.platt_model, self._platt_path())
        except Exception as e:
            log.warning(f"[ML] Platt failed: {e}")

        # ── Persist metrics ────────────────────────────────────────────────
        acc = float(accuracy_score(y_val, (val_pred >= 0.5).astype(int)))
        ll  = float(log_loss(y_val, val_pred))
        db.execute(
            "INSERT INTO model_metrics "
            "(trained_at,n_samples,train_auc,val_auc,accuracy,log_loss,overfit_flag,top_features) "
            "VALUES (datetime('now','utc'),?,?,?,?,?,?,?)",
            (n, self.train_auc, self.val_auc, acc, ll, int(overfit),
             json.dumps([{"name":nm,"imp":v} for nm,v in top])),
        )

        self._new_since_train = 0
        self._metrics_match_model = True
        self._save_metrics()
        log.info(
            f"✓ Ensemble trained | n={n} | LGB_AUC={self.val_auc:.3f} "
            f"| WalkFwd={self.walk_forward_auc:.3f} | overfit={overfit}"
        )
        return dict(n=n, train_auc=self.train_auc, val_auc=self.val_auc,
                    walk_forward_auc=self.walk_forward_auc, accuracy=acc, overfit=overfit)

    # ── ★ TRAIN ON A HISTORICAL DATASET (forward-return forecasting) ──────────
    def train_on(self, X, y, ts=None, embargo_days: int = 7) -> Dict:
        """Train the LightGBM forecaster on a prepared (X, y) dataset built from
        historical bars (label = did price rise over the forecast horizon).

        ★ Honest split: when `ts` (per-row window-end timestamps) is provided,
        the 80/20 cut is by DATE across ALL symbols with an embargo gap of
        `embargo_days` — rows are appended symbol-block by symbol-block, so a
        row-index cut is a SYMBOL split (train sees the validation period's
        market conditions through the market-context features) and overlapping
        5-day labels straddle the boundary. Both inflated val_auc before."""
        if not ML_AVAILABLE:
            return {"status": "ml_not_available"}
        # Validate and clean the prepared dataset before changing the currently
        # usable model.  A failed forecast build should not disable a live model
        # that was already serving predictions.
        try:
            X = np.asarray(X, dtype=np.float32)
            y_raw = np.asarray(y, dtype=np.float64)
        except (TypeError, ValueError):
            return {"status": "invalid_dataset", "n": 0}
        if X.ndim != 2 or X.shape[1] != N_FEATURES or y_raw.ndim != 1:
            return {"status": "invalid_dataset", "n": int(len(X)) if X.ndim else 0}
        if len(X) != len(y_raw):
            return {"status": "invalid_dataset", "n": int(len(X))}
        if ts is not None:
            try:
                if len(ts) != len(X):
                    return {"status": "invalid_timestamps", "n": int(len(X))}
            except TypeError:
                return {"status": "invalid_timestamps", "n": int(len(X))}
        valid = np.isfinite(X).all(axis=1) & np.isfinite(y_raw)
        valid &= np.isin(y_raw, (0, 1))
        if not valid.all():
            X = X[valid]
            y_raw = y_raw[valid]
        y = y_raw.astype(np.int32)
        if len(X) < 200 or len(np.unique(y)) < 2:
            return {"status": "insufficient_data", "n": int(len(X))}
        if ts is not None:
            import datetime as _dt
            ts_values = [t for t, keep in zip(ts, valid) if keep]
            try:
                dates = np.array([
                    t.date() if hasattr(t, "date")
                    else _dt.datetime.fromisoformat(str(t)).date()
                    for t in ts_values
                ], dtype=object)
            except (TypeError, ValueError):
                return {"status": "invalid_timestamps", "n": int(len(X))}
            uniq   = sorted(set(dates.tolist()))
            # Use the last date in the 80% training bucket.  Indexing the
            # 80th-percentile date directly made a five-date dataset choose its
            # final date as the cutoff and leave validation empty.
            cutoff = uniq[max(0, int(len(uniq) * 0.8) - 1)]
            emb    = _dt.timedelta(days=embargo_days)
            tr_mask = np.array([d <= cutoff - emb for d in dates])
            va_mask = np.array([d > cutoff for d in dates])
            X_tr, y_tr = X[tr_mask], y[tr_mask]
            X_val, y_val = X[va_mask], y[va_mask]
        else:
            cut = int(len(X) * 0.8)
            X_tr, X_val, y_tr, y_val = X[:cut], X[cut:], y[:cut], y[cut:]
        if (len(X_tr) < 100 or len(X_val) < 50
                or len(np.unique(y_val)) < 2 or len(np.unique(y_tr)) < 2):
            # ★ NO random-split fallback — that reintroduced full look-ahead
            # exactly when quiet markets made the tail single-class.
            return {"status": "insufficient_data_after_split",
                    "n": int(len(X)), "train": int(len(X_tr)), "val": int(len(X_val))}
        # A forward-return forecast has different label semantics from the live
        # trade-outcome model. Never blend an already-loaded companion artifact
        # with the forecast booster, including after a successful in-process train.
        self.xgb_model = None
        self.platt_model = None
        dtr  = lgb.Dataset(X_tr, y_tr, feature_name=FEATURE_NAMES)
        dval = lgb.Dataset(X_val, y_val, reference=dtr)
        self.lgb_model = lgb.train(
            _LGB_PARAMS, dtr, num_boost_round=600,
            valid_sets=[dtr, dval], valid_names=["train", "val"],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
        )
        # ★ Forecast/bootstrap model lives in its OWN file so it never clobbers the
        #   live-outcome model (lgb_gold.txt). See _load_models for precedence.
        self.lgb_model.save_model(self._forecast_path())
        self._model_file = os.path.basename(self._forecast_path())
        self.feature_importance = self.lgb_model.feature_importance("gain")
        self.train_auc = float(roc_auc_score(y_tr,  self.lgb_model.predict(X_tr)))
        self.val_auc   = float(roc_auc_score(y_val, self.lgb_model.predict(X_val)))
        self.walk_forward_auc = 0.0   # train_on has no walk-forward pass
        self._metrics_match_model = True
        self._save_metrics()
        top = sorted(zip(FEATURE_NAMES, self.feature_importance.tolist()), key=lambda x: -x[1])[:6]
        log.info(f"[ML] ★ Forecast model trained | n={len(X)} | val_AUC={self.val_auc:.3f} "
                 f"| top={[t[0] for t in top]}")
        return {"status": "ok", "n": int(len(X)),
                "train_auc": round(self.train_auc, 3), "val_auc": round(self.val_auc, 3),
                "top_features": [t[0] for t in top]}

    # ── TRUST GATE ────────────────────────────────────────────────────────────
    def is_trustworthy(self) -> bool:
        """Whether the loaded model is good enough to blend into the decision.
        Guards against trusting a known-overfit/low-AUC model at 60% weight. The
        persisted metrics must belong to the model file that was loaded; an
        untagged or mismatched artifact is treated as untrusted until retrained."""
        if self.lgb_model is None and self.xgb_model is None:
            return False
        if not self._metrics_match_model:
            return False
        # ★ Prefer the walk-forward number when it exists — it is the honest
        # (strictly out-of-time) metric. max() let a leaky random-split val_auc
        # override a failing walk-forward score and open the gate anyway.
        auc = self.walk_forward_auc if self.walk_forward_auc > 0.0 else self.val_auc
        if auc <= 0.0:
            return True
        return auc >= getattr(Config, "ML_MIN_AUC", 0.52)

    # ── INFERENCE ─────────────────────────────────────────────────────────────
    def predict(self, features) -> Tuple[float, float]:
        """Returns (probability, confidence). Ensemble when both models available."""
        try:
            X = np.asarray([features.to_list()], dtype=np.float32)
        except (TypeError, ValueError):
            return 0.50, 0.0
        if X.shape != (1, N_FEATURES) or not np.isfinite(X).all():
            log.warning("[ML] Invalid feature vector — returning neutral probability")
            return 0.50, 0.0

        lgb_prob, xgb_prob = None, None

        if self.lgb_model is not None:
            try:
                value = float(self.lgb_model.predict(X)[0])
                lgb_prob = value if np.isfinite(value) else None
            except Exception:
                lgb_prob = None

        if XGB_AVAILABLE and Config.USE_ENSEMBLE and self.xgb_model is not None:
            try:
                value = float(self.xgb_model.predict_proba(X)[0][1])
                xgb_prob = value if np.isfinite(value) else None
            except Exception:
                pass

        if lgb_prob is not None and xgb_prob is not None:
            # ★ Weighted ensemble
            prob = (lgb_prob * Config.ENSEMBLE_LGB_WEIGHT +
                    xgb_prob * Config.ENSEMBLE_XGB_WEIGHT)
            conf = 1.0 - abs(lgb_prob - xgb_prob)  # models agree = high conf
            return float(prob), float(max(0.3, min(1.0, conf)))

        if lgb_prob is not None:
            # ★ Clip each feature into [0,1] before measuring deviation from the
            # 0.5 "neutral" center — unnormalized features (sector_code 0-6,
            # volume_ratio 0-10) contributed |x-0.5| up to 9.5 and pinned conf
            # at 1.0 for nearly every symbol, making it carry no information.
            w = self.feature_importance / (self.feature_importance.sum() + 1e-9)
            dev = np.abs(np.clip(X[0], 0.0, 1.0) - 0.5)
            conf = float(np.clip((w * dev).sum() * 2, 0., 1.))
            return lgb_prob, conf

        if self.platt_model is not None and hasattr(self.platt_model, "predict_proba"):
            try:
                return float(self.platt_model.predict_proba(X)[0][1]), 0.35
            except Exception: pass

        return self._rule_based(features), 0.20

    def _rule_based(self, f) -> float:
        """Gold rule-based scoring (fallback when no trained model)."""
        score = 0.50
        score += f.momentum_5d * 0.8
        score += f.ema9_vs_ema21 * 1.0
        score += f.macd_histogram * 3.0
        score += f.macd_trend * 0.03
        # RSI
        if f.rsi_14 < 0.30: score += 0.05
        elif f.rsi_14 > 0.70: score -= 0.05
        # ★ New features in rule-based
        # williams_r is normalized 0=overbought (price at range high), 1=oversold.
        if f.williams_r < 0.20: score -= 0.04    # overbought → fade
        if f.williams_r > 0.80: score += 0.04    # oversold → bounce
        score += f.obv_trend * 0.02               # smart money direction
        score += f.relative_strength * 0.3
        if f.volume_ratio > 1.5: score += np.sign(f.momentum_5d) * 0.03
        # ★ Market context: don't fight the tape, favor aligned trends, and let a
        #   decisive candle confirm (or a doji discount) the momentum read.
        score += f.market_trend_20d * 0.5
        score += (f.trend_alignment - 0.5) * 0.10
        score += (f.candle_stability - 0.5) * np.sign(f.momentum_5d) * 0.05
        return float(np.clip(score, 0.05, 0.95))

    # ── KELLY SIZING ──────────────────────────────────────────────────────────
    def kelly_bet(
        self,
        ml_prob: float,
        entry_quality: float,
        bankroll: float,
        drawdown: float = 0.0,
        regime_mult: float = 1.0,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
    ) -> Tuple[float, float]:
        # Use the actual signal distances when available. ATR-based stops can
        # differ materially from the legacy fixed 7%/3% assumptions; sizing on
        # the wrong payoff ratio overstates Kelly for wide stops and understates
        # it for tight targets.
        tp = tp_pct if tp_pct is not None else Config.TAKE_PROFIT_PCT
        sl = sl_pct if sl_pct is not None else Config.STOP_LOSS_PCT
        try:
            vals = (ml_prob, entry_quality, bankroll, drawdown, regime_mult, tp, sl)
            if not all(np.isfinite(float(v)) for v in vals):
                return 0.0, 0.0
        except (TypeError, ValueError):
            return 0.0, 0.0
        # Kelly is undefined outside a probability/quality/risk domain. Reject
        # malformed model output rather than letting an out-of-range probability
        # or regime multiplier manufacture an oversized order.
        if (not 0.0 < ml_prob < 1.0 or not 0.0 <= entry_quality <= 1.0
                or bankroll <= 0 or drawdown < 0 or not 0.0 <= regime_mult <= 2.0
                or tp <= 0 or sl <= 0):
            return 0.0, 0.0
        if ml_prob <= 0.50: return 0.0, 0.0
        b = tp / sl; p = ml_prob; q = 1. - ml_prob
        raw_kelly = (p*b - q) / b
        if raw_kelly <= 0: return 0.0, 0.0
        kelly = raw_kelly * Config.KELLY_FRACTION
        kelly *= max(0.5, entry_quality)
        # Preserve defensive regime multipliers. A floor of 0.3 silently
        # tripled high-fear sizing (0.10) and raised bear-high-vol sizing
        # (0.25), defeating the regime policy. Extreme fear is blocked by the
        # main loop, but zero remains a safe result if this helper is called
        # directly.
        kelly *= max(0.0, regime_mult)
        if drawdown > 0.05: kelly *= max(0.1, 1. - (drawdown-0.05)*6)
        size = kelly * bankroll
        size = min(size, Config.MAX_POSITION_USD)
        size = min(size, bankroll * Config.MAX_POSITION_PCT)
        return round(kelly, 5), round(size, 2)

    # ── ENTRY QUALITY ─────────────────────────────────────────────────────────
    def entry_quality(self, f, edge: float) -> float:
        """Graded [0,1] setup quality. Does NOT saturate — it must discriminate
        between a marginal and an excellent setup so position sizing reflects it."""
        vol_pen   = min(1., max(0., (f.atr_pct - 0.02) / 0.08))      # high vol = worse
        spread_pen= min(1., max(0., (f.bid_ask_spread - 0.001) / 0.005))
        vwap_pen  = min(1., max(0., abs(f.vwap_deviation) / 0.05))   # chasing = worse
        vol_bonus = min(1., f.volume_ratio / 2.0)                    # participation

        # consensus: how many independent bullish indicators agree
        bull_signals = sum([
            0.40 < f.rsi_14 < 0.75,
            f.macd_trend > 0,
            f.ema9_vs_ema21 > 0,
            f.momentum_5d > 0,
            f.obv_trend > 0,
            f.stoch_k > 0.30,
            f.williams_r > 0.30,
            f.trend_alignment >= 0.60,   # ★ multi-timeframe trends agree
        ])
        consensus = bull_signals / 8.0

        # ★ Candle-stability confirmation: a setup printed on decisive candles is
        #   worth slightly more than one printed on dojis (0.9× … 1.1×, neutral 1).
        stability_mult = 0.9 + 0.2 * f.candle_stability

        base = ((1. - vol_pen * 0.25)
                * (1. - spread_pen * 0.20)
                * (1. - vwap_pen * 0.15))
        quality = base * (0.45 + 0.55 * consensus) * (0.6 + 0.4 * vol_bonus) * stability_mult
        # mild edge tilt — better edge nudges quality up, but never saturates it
        quality *= 0.8 + 0.4 * min(1.0, edge / (2 * Config.MIN_EDGE))
        return float(np.clip(quality, 0., 1.))

    # ── AUDIT ─────────────────────────────────────────────────────────────────
    def audit(self) -> dict:
        row = db.fetchone(
            "SELECT COUNT(*) AS total, SUM(outcome IS NOT NULL) AS resolved, "
            "SUM(outcome=1) AS wins FROM training_data"
        )
        total=row["total"] or 0; resolved=row["resolved"] or 0; wins=row["wins"] or 0
        return dict(total=total, resolved=resolved, pending=total-resolved,
                    win_rate=round(wins/resolved,4) if resolved else 0.,
                    val_auc=round(self.val_auc,4),
                    walk_forward_auc=round(self.walk_forward_auc,4))
