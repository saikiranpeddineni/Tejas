"""
backtest.py — Live-trade performance report + ML holdout eval
=============================================================
⚠️  NOT a true backtest. This does NOT replay the live entry/exit/risk gates over
    historical bars — it reports statistics on trades the bot ALREADY took
    (rows in `trades`), plus a temporal ML holdout eval. Because it inherits the
    stored (cost-adjusted) P&L of real decisions, it CANNOT validate a config or
    gate change; tuning against it is circular. For that you need a replay harness
    that feeds historical bars through _prevalidate_entry / risk_mgr.approve /
    ExitEngine with slippage. Treat the numbers here as "how did what we did do?",
    not "how would this strategy do?".

Usage:
    python backtest.py              # full report on all resolved trades
    python backtest.py --days 30   # last 30 days only
    python backtest.py --ml-only   # just ML metrics
"""
import argparse
import json
import logging
import math
import sys

import numpy as np

import db
from config import Config
from feature_builder_gold import FEATURE_NAMES
from ml_engine_gold import MLEngineGold
from performance import PerformanceAnalytics

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backtest")


def eval_ml_holdout(lookback_days: int = 90) -> dict:
    from datetime import datetime, timezone, timedelta
    try:
        from sklearn.metrics import roc_auc_score, accuracy_score, log_loss
        import lightgbm as lgb
        from ml_engine_gold import _LGB_PARAMS
    except ImportError:
        log.error("lightgbm/sklearn not installed")
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.fetchall(
        "SELECT features_json, outcome, entry_time FROM training_data "
        "WHERE outcome IS NOT NULL AND entry_time IS NOT NULL "
        "AND julianday(entry_time) IS NOT NULL "
        "AND julianday(entry_time) >= julianday(?) ORDER BY julianday(entry_time) ASC",
        (cutoff,),
    )

    X, y = [], []
    for row in rows:
        try:
            feat = json.loads(row["features_json"])
            if not isinstance(feat, dict):
                continue
            values = [float(feat.get(n, 0.0)) for n in FEATURE_NAMES]
            outcome = int(row["outcome"])
            if outcome not in (0, 1) or not all(math.isfinite(v) for v in values):
                continue
            X.append(values)
            y.append(outcome)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    if len(X) < 30:
        log.warning(f"Only {len(X)} valid resolved trades — need 30+ for meaningful eval")
        return {"error": "insufficient_data", "n": len(X)}

    X = np.asarray(X, dtype=np.float32)
    y = np.asarray(y, dtype=np.int32)

    split = int(len(X) * 0.80)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    if len(np.unique(y_tr)) < 2 or len(np.unique(y_te)) < 2:
        return {"error": "single_class_split"}

    dtr = lgb.Dataset(X_tr, y_tr, feature_name=FEATURE_NAMES)
    dte = lgb.Dataset(X_te, y_te, reference=dtr)
    # The test fold is for scoring only.  Early stopping against it tunes the
    # round count on the labels being reported and leaks holdout information.
    b = lgb.train(
        _LGB_PARAMS, dtr, num_boost_round=300,
        callbacks=[lgb.log_evaluation(0)],
    )

    preds = b.predict(X_te)
    auc   = float(roc_auc_score(y_te, preds))
    acc   = float(accuracy_score(y_te, (preds >= 0.5).astype(int)))
    ll    = float(log_loss(y_te, preds))
    calib = abs(float(preds.mean()) - float(y_te.mean()))

    imp = b.feature_importance("gain")
    top = sorted(zip(FEATURE_NAMES, imp), key=lambda x: -x[1])[:8]

    result = dict(
        n_train=len(X_tr), n_test=len(X_te),
        temporal_auc=round(auc, 4), accuracy=round(acc, 4),
        log_loss=round(ll, 4), calibration_error=round(calib, 4),
        avg_pred_prob=round(float(preds.mean()), 4),
        actual_win_rate=round(float(y_te.mean()), 4),
        top_features=[(n, round(v, 1)) for n, v in top],
    )
    log.info(f"Hold-out ML: AUC={auc:.3f} Acc={acc:.3f} LL={ll:.3f} Calib={calib:.4f}")
    return result


def simulate_trades(lookback_days: int = 90) -> dict:
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    rows = db.fetchall(
        "SELECT * FROM trades WHERE status='closed' AND entry_time IS NOT NULL "
        "AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL "
        "AND julianday(entry_time) IS NOT NULL "
        "AND julianday(entry_time) >= julianday(?) ORDER BY julianday(entry_time) ASC",
        (cutoff,),
    )
    valid_rows = []
    for row in rows:
        try:
            if (math.isfinite(float(row["pnl_pct"]))
                    and math.isfinite(float(row["pnl_usd"]))):
                valid_rows.append(row)
        except (TypeError, ValueError):
            continue
    rows = valid_rows
    if not rows:
        log.warning("No valid resolved trades in window")
        return {}

    all_pnl_pct = [float(r["pnl_pct"]) for r in rows]
    all_pnl_usd = [float(r["pnl_usd"]) for r in rows]

    filt = []
    for row in rows:
        try:
            quality = float(row["entry_quality"] or 0)
            if math.isfinite(quality) and quality >= Config.MIN_ENTRY_QUALITY:
                filt.append(row)
        except (TypeError, ValueError):
            continue
    f_pct = [float(r["pnl_pct"]) for r in filt]
    f_usd = [float(r["pnl_usd"]) for r in filt]

    def stats(pnl_pct, pnl_usd, label):
        if not pnl_pct:
            return None
        arr = np.array(pnl_pct)
        wins = sum(p > 0 for p in pnl_usd)
        wr   = wins / len(pnl_usd)
        mean = float(np.mean(arr)); std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
        sharpe = mean / std * np.sqrt(252) if std > 0 else 0.0
        total  = sum(pnl_usd)
        cumul  = np.cumsum(pnl_usd)
        max_dd = float(np.max(np.maximum.accumulate(cumul) - cumul))
        log.info(
            f"  [{label:30s}] n={len(pnl_pct):4d} | WR={wr:.1%} | "
            f"Sharpe={sharpe:5.2f} | PnL=${total:+,.2f} | MaxDD=${max_dd:.2f}"
        )
        return dict(n=len(pnl_pct), win_rate=round(wr,4), sharpe=round(sharpe,3),
                    total_pnl=round(total,2), max_dd=round(max_dd,2))

    log.info(f"\n── Backtest {lookback_days}d ({len(rows)} trades) ─────────────────")
    r_all  = stats(all_pnl_pct, all_pnl_usd, "All trades (unfiltered)")
    r_qual = stats(f_pct, f_usd, f"Quality >= {Config.MIN_ENTRY_QUALITY}")

    exit_reasons: dict = {}
    for r in rows:
        k = str(r["exit_reason"] or "RESOLVED").split("_")[0]
        if k not in exit_reasons:
            exit_reasons[k] = {"count": 0, "pnl": []}
        exit_reasons[k]["count"] += 1
        exit_reasons[k]["pnl"].append(float(r["pnl_pct"]))

    log.info("\n── Exit Reason Analysis ─────────────────────────────────")
    for reason, data in sorted(exit_reasons.items(), key=lambda x: -x[1]["count"]):
        avg = float(np.mean(data["pnl"])) if data["pnl"] else 0.0
        log.info(f"  {reason:20s}: {data['count']:4d} exits | avg pnl {avg:+.3f}")
    log.info("─────────────────────────────────────────────────────────\n")

    return dict(all=r_all, quality_filtered=r_qual)


def main():
    parser = argparse.ArgumentParser(description="StockBot Backtester")
    parser.add_argument("--days",     type=int, default=90)
    parser.add_argument("--ml-only",  action="store_true")
    args = parser.parse_args()

    db.init_db()

    print(f"\n{'='*60}")
    print(f"  StockBot Backtester — {args.days}d lookback")
    print(f"{'='*60}\n")

    print("── ML Hold-Out Evaluation ──────────────────────────────")
    ml_result = eval_ml_holdout(args.days)
    if "temporal_auc" in ml_result:
        auc = ml_result["temporal_auc"]
        print(f"  Temporal AUC:     {auc:.4f}  {'✓ GOOD' if auc > 0.57 else '⚠ WEAK'}")
        print(f"  Accuracy:         {ml_result['accuracy']:.4f}")
        print(f"  Log Loss:         {ml_result['log_loss']:.4f}")
        print(f"  Calib Error:      {ml_result['calibration_error']:.4f}  "
              f"{'✓ OK' if ml_result['calibration_error'] < 0.05 else '⚠ CHECK'}")
        print(f"  Train/Test:       {ml_result['n_train']}/{ml_result['n_test']}")
        if ml_result.get("top_features"):
            print("\n  Top ML Features:")
            for name, imp in ml_result["top_features"][:6]:
                max_imp = max((v for _, v in ml_result["top_features"]), default=0.0)
                bar = "█" * int(imp / max_imp * 20) if max_imp > 0 else ""
                print(f"    {name:25s} {bar}")
    print()

    if args.ml_only:
        return

    print("── Trade Simulation ────────────────────────────────────")
    sim = simulate_trades(args.days)

    if sim.get("quality_filtered"):
        qf = sim["quality_filtered"]
        print("\n── Readiness Check ──────────────────────────────────")
        print(f"  Sharpe   {qf.get('sharpe',0):.3f}   {'✓ PASS' if qf.get('sharpe',0) > 1.0 else '✗ FAIL (target > 1.0)'}")
        print(f"  Win Rate {qf.get('win_rate',0):.1%}   {'✓ PASS' if qf.get('win_rate',0) > 0.52 else '✗ FAIL (target > 52%)'}")
        if ml_result.get("temporal_auc"):
            auc = ml_result["temporal_auc"]
            print(f"  AUC      {auc:.3f}   {'✓ PASS' if auc > 0.57 else '✗ FAIL (target > 0.57)'}")

    perf = PerformanceAnalytics()
    perf.print_summary(args.days)


if __name__ == "__main__":
    main()
