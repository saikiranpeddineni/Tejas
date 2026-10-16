"""
forecast_trainer.py — Train Tejas's forward-return forecaster on history.
=========================================================================
For each stock in the live universe we replay history: at many past dates we
build the 34-feature snapshot from data available *up to that day*, and label it
1 if the stock was higher `horizon` trading days later (by more than a small
threshold), else 0. LightGBM learns which patterns preceded gains — i.e. a
probabilistic forward forecast, learned from the market's own past.

This is an EDGE ESTIMATE, not a guarantee. Markets are noisy; a good model is
right perhaps 52–58% of the time — enough to matter with disciplined sizing,
not a crystal ball.

Run standalone:   python forecast_trainer.py
Or from the dashboard's  🧠 Train  button.
"""
import logging
import math

from config import Config
from models import Quote

log = logging.getLogger(__name__)


def _synthetic_quote(sym: str, close: float, ts) -> Quote:
    """A quote matching what the LIVE Yahoo feed produces (mid≈close, constant
    0.05% synthetic spread → spread_pct 0.001). Building training features through
    the SAME quote-aware path the live scan uses removes train/serve skew in the
    price-basis and bid_ask_spread features."""
    half = close * 0.0005
    return Quote(symbol=sym, bid=close - half, ask=close + half,
                 bid_size=1, ask_size=1, timestamp=ts)


def build_dataset(broker, builder, symbols, horizon=5, max_symbols=200,
                  step=2, hist=420, up_threshold=0.005):
    if horizon < 1 or step < 1 or hist < 1:
        log.warning("[Forecast] invalid horizon/step/history — no dataset built")
        return [], [], []
    # ts = each row's window-end timestamp, so the trainer can split by DATE.
    # Rows are appended symbol-block by symbol-block, so a row-index split is a
    # symbol split (all dates on both sides) — see MLEngineGold.train_on.
    X, y, ts = [], [], []
    used = 0
    for sym in symbols[:max_symbols]:
        try:
            bars = broker.get_bars(sym, "1Day", limit=hist)
        except Exception:
            continue
        if not bars or len(bars) < 150:
            continue
        used += 1
        for t in range(120, len(bars) - horizon, step):
            window = bars[:t + 1]
            q = _synthetic_quote(sym, window[-1].close, window[-1].timestamp)
            feats = builder.build(sym, window, q)
            if feats is None:
                continue
            base_close = float(bars[t].close)
            future_close = float(bars[t + horizon].close)
            if (not math.isfinite(base_close) or not math.isfinite(future_close)
                    or base_close <= 0 or future_close <= 0):
                continue
            fwd = (future_close - base_close) / base_close
            X.append(feats.to_list())
            y.append(1 if fwd > up_threshold else 0)
            ts.append(window[-1].timestamp)
    log.info(f"[Forecast] built {len(X)} samples from {used} stocks (horizon={horizon}d)")
    return X, y, ts


def train_forecast_model(broker, builder, ml, horizon=None, max_symbols=200):
    from universe import get_universe
    horizon = horizon or int(getattr(Config, "FORECAST_HORIZON", 5))
    symbols, _ = get_universe()
    X, y, ts = build_dataset(broker, builder, symbols, horizon=horizon, max_symbols=max_symbols)
    # Embargo = horizon + 2: rows whose 5-day forward label reaches past the
    # cutoff date must not sit in the training set.
    result = ml.train_on(X, y, ts=ts, embargo_days=horizon + 2)
    result["horizon"] = horizon
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    import db
    db.init_db()
    from brokers import get_broker
    from ml_engine_gold import MLEngineGold
    from feature_builder_gold import FeatureBuilderGold
    broker = get_broker()
    ml = MLEngineGold()
    builder = FeatureBuilderGold(broker)
    print("Training forward-return forecaster on historical NSE data…")
    res = train_forecast_model(broker, builder, ml)
    print("Result:", res)
