"""
sentiment_engine.py — ★ NEW: News & Social Sentiment Scoring
=============================================================
Fetches news headlines for each symbol and computes a sentiment
score using keyword analysis + Alpaca's built-in news feed.

Why sentiment matters:
  A stock with strong technical setup BUT negative news flow is
  a losing trade waiting to happen. A stock with bullish momentum
  AND positive news has compounding tailwinds.

  Professional quant funds estimate ~15-25% of alpha comes from
  integrating news sentiment with technical signals.

TWO DATA SOURCES:
  1. Alpaca News API (included with your data subscription)
     → Real-time news for US stocks, no extra key needed
  2. AlphaVantage NEWS_SENTIMENT (free tier: 500 calls/day)
     → More structured sentiment scores

SENTIMENT SCORE: -1.0 (very negative) to +1.0 (very positive)

USAGE:
  A score > +0.3  → boost entry edge (good news = tailwind)
  A score < -0.3  → suppress or block entry (bad news = headwind)
  A score in [-0.3, +0.3] → neutral, no adjustment
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from brokers.data_provider import _yahoo_symbol, search_news
from config import Config

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD DICTIONARIES  (no NLP library needed)
# ─────────────────────────────────────────────────────────────────────────────
_BULLISH_KEYWORDS = {
    # Earnings/Revenue
    "beats", "beat", "exceeded", "exceeds", "record", "surge", "surged",
    "blowout", "raised", "raises", "guidance", "upgrade", "upgraded",
    # Business
    "acquisition", "partnership", "deal", "contract", "wins", "win",
    "expansion", "growth", "profit", "revenue", "earnings", "dividend",
    # Market
    "breakout", "rally", "buy", "bullish", "strong", "outperform",
    "positive", "optimistic", "momentum", "upside",
}

_BEARISH_KEYWORDS = {
    # Earnings/Revenue
    "miss", "missed", "below", "disappoints", "disappointing", "cut",
    "cuts", "lowered", "lowers", "warning", "downgrade", "downgraded",
    # Business
    "layoffs", "layoff", "restructuring", "bankruptcy", "lawsuit",
    "investigation", "probe", "fine", "penalty", "recall", "fraud",
    # Market
    "sell", "bearish", "weak", "underperform", "negative", "pessimistic",
    "decline", "crash", "slump", "downside", "risk", "concern",
}

_EXTREME_NEGATIVE = {
    "bankruptcy", "fraud", "sec", "investigation", "delisted", "default",
    "lawsuit", "criminal", "scandal", "collapse", "bankrupt",
}


class SentimentEngine:
    def __init__(self):
        # Cache: symbol → ((score, n_relevant), fetched_at)
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = Config.SENTIMENT_CACHE_MINS * 60

    # ── PUBLIC API ────────────────────────────────────────────────────────────
    def _score_and_count(self, symbol: str) -> tuple:
        """Returns (score [-1,1], n_relevant_articles), cached."""
        now = time.time()
        if symbol in self._cache:
            val, fetched_at = self._cache[symbol]
            if now - fetched_at < self._cache_ttl:
                return val

        val = self._fetch_yahoo_news(symbol)   # (score, n_relevant)

        # Optional AlphaVantage supplement (only if a real key is configured)
        if abs(val[0]) < 0.1 and Config.ALPHA_VANTAGE_KEY not in ("", "demo"):
            av_score = self._fetch_alphavantage_sentiment(symbol)
            if abs(av_score) > 0.1:
                val = ((val[0] + av_score) / 2, max(val[1], 1))

        self._cache[symbol] = (val, now)

        score, n = val
        if n and abs(score) > 0.2:
            direction = "📈 BULLISH" if score > 0 else "📉 BEARISH"
            log.info(f"[Sentiment] {symbol}: {score:+.3f} {direction} ({n} articles)")
        return val

    def get_score(self, symbol: str) -> float:
        """
        Returns sentiment score [-1.0, +1.0].
        Positive = bullish news, Negative = bearish news.
        0.0 if no relevant news.
        """
        return self._score_and_count(symbol)[0]

    def get_scores_bulk(self, symbols: List[str]) -> Dict[str, float]:
        """Get sentiment for multiple symbols efficiently."""
        return {sym: self.get_score(sym) for sym in symbols}

    def edge_adjustment(self, symbol: str) -> float:
        """
        Returns edge adjustment based on sentiment.
        Positive = add to edge (bullish news), Negative = subtract from edge.
        """
        score = self.get_score(symbol)
        if score > Config.SENTIMENT_BOOST_SCORE:
            return Config.SENTIMENT_EDGE_BOOST
        elif score < Config.SENTIMENT_MIN_SCORE:
            return -Config.SENTIMENT_EDGE_PENALTY
        return 0.0

    def is_blocked(self, symbol: str) -> bool:
        """Hard block only on strongly negative news backed by >1 relevant article
        (one noisy headline shouldn't veto a good technical setup)."""
        score, n = self._score_and_count(symbol)
        return score <= -0.5 and n >= 2

    # ── YAHOO NEWS ─────────────────────────────────────────────────────────────
    def _fetch_yahoo_news(self, symbol: str) -> tuple:
        """
        Fetch recent headlines from Yahoo's news search and score them by keyword.
        Relevance filter: keep only articles Yahoo tagged with this stock's ticker
        (relatedTickers) or whose title contains the symbol — Yahoo search is noisy
        and untagged market-wide stories must not move a single stock's score.

        Returns (score [-1,1], n_relevant_articles).
        """
        ysym = _yahoo_symbol(symbol)                 # e.g. RELIANCE -> RELIANCE.NS
        root = symbol.upper().split("-")[0]
        try:
            import re
            articles = search_news(ysym, count=10)
            scores = []
            for a in articles:
                tickers = {str(t).upper() for t in (a.get("relatedTickers") or [])}
                title = (a.get("title") or "")
                # ★ Whole-word matching everywhere. Raw substring matching blocked
                # stocks on BULLISH headlines: "sec" ⊂ "sector", "cut" ⊂ "execute",
                # and short roots misfired relevance ("LT" ⊂ "DEFAULT").
                relevant = (ysym.upper() in tickers
                            or root in tickers
                            or re.search(rf"\b{re.escape(root)}\b", title.upper()))
                if not relevant:
                    continue
                words = set(re.findall(r"[a-z']+", title.lower()))
                bull = sum(1 for kw in _BULLISH_KEYWORDS if kw in words)
                bear = sum(1 for kw in _BEARISH_KEYWORDS if kw in words)
                if any(kw in words for kw in _EXTREME_NEGATIVE):
                    scores.append(-0.90)
                    continue
                total = bull + bear
                scores.append((bull - bear) / total if total else 0.0)
            if not scores:
                return (0.0, 0)
            return (float(sum(scores) / len(scores)), len(scores))
        except Exception as e:
            # Fail-open is the right call (a news outage must not halt trading),
            # but at WARNING so the operator can see the gate has gone dark.
            log.warning(f"[Sentiment] Yahoo news failed {symbol} — gate inactive this cycle: {e}")
            return (0.0, 0)

    # ── ALPHAVANTAGE SENTIMENT ────────────────────────────────────────────────
    def _fetch_alphavantage_sentiment(self, symbol: str) -> float:
        """
        AlphaVantage NEWS_SENTIMENT endpoint.
        Returns overall_sentiment_score (already computed by AV).
        Free: 500 calls/day.
        """
        try:
            resp = requests.get(
                "https://www.alphavantage.co/query",
                params={
                    "function":   "NEWS_SENTIMENT",
                    "tickers":    symbol,
                    "limit":      10,
                    "apikey":     Config.ALPHA_VANTAGE_KEY,
                },
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()

            if "feed" not in data:
                return 0.0

            scores = []
            for article in data["feed"][:10]:
                for ticker_info in article.get("ticker_sentiment", []):
                    if ticker_info.get("ticker", "").upper() == symbol.upper():
                        try:
                            score = float(ticker_info.get("ticker_sentiment_score", 0))
                            scores.append(score)
                        except (ValueError, TypeError):
                            pass

            return float(sum(scores) / len(scores)) if scores else 0.0

        except Exception as e:
            log.debug(f"[Sentiment] AlphaVantage failed {symbol}: {e}")
            return 0.0
