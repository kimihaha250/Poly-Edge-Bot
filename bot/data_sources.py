from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from bot.polymarket_gamma import GammaClient


@dataclass
class MarketContext:
    news_summary: str
    social_sentiment_score: float
    historical_win_rate: float


def _make_session() -> requests.Session:
    """创建禁用系统代理自动嗅探的 Session，防止被随机系统代理端口劫持"""
    s = requests.Session()
    s.trust_env = False
    return s


def fetch_news_summary(question: str, news_api_key: str, tavily_api_key: str) -> str:
    if news_api_key:
        try:
            url = "https://newsapi.org/v2/everything"
            params = {"q": question, "sortBy": "publishedAt", "language": "en", "pageSize": 5}
            headers = {"X-Api-Key": news_api_key}
            with _make_session() as s:
                res = s.get(url, params=params, headers=headers, timeout=12)
            res.raise_for_status()
            articles = res.json().get("articles", [])
            titles = [a.get("title", "") for a in articles if a.get("title")]
            if titles:
                return " | ".join(titles[:5])
        except Exception:
            pass

    if tavily_api_key:
        try:
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": tavily_api_key,
                "query": question,
                "search_depth": "basic",
                "max_results": 5,
            }
            with _make_session() as s:
                res = s.post(url, json=payload, timeout=12)
            res.raise_for_status()
            results = res.json().get("results", [])
            snippets = [r.get("content", "") for r in results if r.get("content")]
            if snippets:
                return " | ".join(snippets[:3])
        except Exception:
            pass

    return "No external news API configured; using placeholder summary."


def fetch_social_sentiment(question: str, x_bearer_token: str) -> float:
    # Placeholder: if X API key is unavailable, return neutral score.
    if not x_bearer_token:
        return 0.0
    # The simplified implementation keeps a deterministic fallback for now.
    lowered = question.lower()
    positive_words = ("approved", "passes", "win", "growth", "bullish")
    negative_words = ("ban", "lawsuit", "recession", "hack", "bearish")
    score = 0.0
    score += 0.2 if any(w in lowered for w in positive_words) else 0.0
    score -= 0.2 if any(w in lowered for w in negative_words) else 0.0
    return max(-1.0, min(1.0, score))


def estimate_historical_win_rate(question: str, gamma_client: GammaClient) -> float:
    resolved = gamma_client.get_resolved_markets(limit=200)
    if not resolved:
        return 0.5
    words = {w.strip(" ?!.,").lower() for w in question.split() if len(w) >= 4}
    candidates: list[dict[str, Any]] = []
    for m in resolved:
        title = str(m.get("question", m.get("title", ""))).lower()
        overlap = sum(1 for w in words if w in title)
        if overlap >= 2:
            candidates.append(m)
    if not candidates:
        return 0.5

    yes_count = 0
    total = 0
    for m in candidates[:50]:
        outcome = str(m.get("outcome", m.get("resolution", m.get("result", "")))).lower()
        # Fallback heuristics for different historical schema shapes.
        if "yes" in outcome or outcome == "1":
            yes_count += 1
            total += 1
        elif "no" in outcome or outcome == "0":
            total += 1

    if total == 0:
        return 0.5
    return yes_count / total


def build_market_context(
    question: str,
    news_api_key: str,
    tavily_api_key: str,
    x_bearer_token: str,
    gamma_client: GammaClient,
) -> MarketContext:
    return MarketContext(
        news_summary=fetch_news_summary(question, news_api_key, tavily_api_key),
        social_sentiment_score=fetch_social_sentiment(question, x_bearer_token),
        historical_win_rate=estimate_historical_win_rate(question, gamma_client),
    )
