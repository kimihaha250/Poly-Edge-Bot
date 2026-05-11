from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv


def _slugs_from_raw(raw: str) -> str:
    """
    把用户填写的原始值（完整 URL 或 slug，支持逗号分隔多个）统一转成 slug 字符串。

    支持以下格式（任意混合）：
        https://polymarket.com/event/will-trump-be-indicted
        https://polymarket.com/zh/event/will-trump-be-indicted
        https://polymarket.com/event/some-market?tid=123
        will-trump-be-indicted          ← 直接填 slug 也支持
    """
    slugs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # 如果是 URL，提取 /event/ 后面的路径部分作为 slug
        match = re.search(r"/event/([^/?#\s]+)", item)
        if match:
            slugs.append(match.group(1))
        else:
            # 不像 URL，直接当 slug 用
            slugs.append(item)
    return ",".join(slugs)


def _topic_tags_from_raw(raw: str) -> str:
    """
    把话题页 URL 解析成 tag slug 列表（逗号分隔）。

    支持以下格式：
        https://polymarket.com/iran/trump-iran   → trump-iran
        https://polymarket.com/politics          → politics
        https://polymarket.com/iran              → iran
        trump-iran                               → trump-iran（直接填也支持）
    规则：取 polymarket.com 之后最后一个非空路径段，忽略 ?query 和 #anchor。
    """
    tags = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("http"):
            # 去掉 query 和 fragment，取最后一段路径
            path = re.sub(r"[?#].*$", "", item)
            # 移除 /zh/ 语言前缀
            path = re.sub(r"/(zh|en|ko|pt|fr|es)/", "/", path)
            segments = [s for s in path.rstrip("/").split("/") if s and s not in ("https:", "http:", "polymarket.com")]
            if segments:
                tags.append(segments[-1])
        else:
            tags.append(item)
    return ",".join(tags)


@dataclass(frozen=True)
class BotConfig:
    dry_run: bool
    scan_interval_seconds: int
    edge_threshold: float
    take_profit_delta: float
    stop_loss_multiplier: float
    max_order_usdc: float
    max_total_exposure_usdc: float
    kelly_fraction: float
    target_market_slug: str   # 已解析好的市场 slug 集合（逗号分隔）
    target_market_urls: str   # 用户填写的原始市场 URL/slug（仅用于日志）
    target_topic_tags: str    # 已解析好的话题 tag 集合（逗号分隔）
    target_topic_urls: str    # 用户填写的原始话题 URL/tag（仅用于日志）
    openrouter_api_key: str
    openrouter_base_url: str
    model_1: str
    model_2: str
    model_3: str
    model_1_weight: float
    model_2_weight: float
    model_3_weight: float
    news_api_key: str
    tavily_api_key: str
    x_bearer_token: str
    clob_host: str
    chain_id: int
    polymarket_private_key: str
    polymarket_proxy_address: str
    polymarket_signature_type: int   # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
    http_proxy: str
    https_proxy: str
    gamma_relay_url: str
    im_webhook_url: str              # IM 通知 webhook（飞书/企业微信/钉钉/Discord，留空禁用）


def _to_bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_sig_type(raw: str, has_proxy: bool) -> int:
    """
    解析 Polymarket 签名类型。
    留空时按 has_proxy 自动推断：填了代理地址 → Gnosis Safe (2)，否则 → EOA (0)。
    """
    if raw:
        try:
            v = int(raw)
            if v in (0, 1, 2):
                return v
        except ValueError:
            pass
    return 2 if has_proxy else 0


def load_config() -> BotConfig:
    load_dotenv()
    return BotConfig(
        dry_run=_to_bool(os.getenv("DRY_RUN", "true"), default=True),
        scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "600")),
        edge_threshold=float(os.getenv("EDGE_THRESHOLD", "0.15")),
        take_profit_delta=float(os.getenv("TAKE_PROFIT_DELTA", "0.10")),
        stop_loss_multiplier=float(os.getenv("STOP_LOSS_MULTIPLIER", "0.85")),
        max_order_usdc=float(os.getenv("MAX_ORDER_USDC", "50")),
        max_total_exposure_usdc=float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "300")),
        kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
        target_market_urls=os.getenv("TARGET_MARKET_URLS", os.getenv("TARGET_MARKET_SLUG", "")).strip(),
        target_market_slug=_slugs_from_raw(
            os.getenv("TARGET_MARKET_URLS", os.getenv("TARGET_MARKET_SLUG", "")).strip()
        ),
        target_topic_urls=os.getenv("TARGET_TOPIC_URLS", "").strip(),
        target_topic_tags=_topic_tags_from_raw(os.getenv("TARGET_TOPIC_URLS", "").strip()),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        model_1=os.getenv("MODEL_1", "openai/gpt-4o-mini").strip(),
        model_2=os.getenv("MODEL_2", "anthropic/claude-3.5-sonnet").strip(),
        model_3=os.getenv("MODEL_3", "google/gemini-2.0-flash-001").strip(),
        model_1_weight=float(os.getenv("MODEL_1_WEIGHT", "0.34")),
        model_2_weight=float(os.getenv("MODEL_2_WEIGHT", "0.33")),
        model_3_weight=float(os.getenv("MODEL_3_WEIGHT", "0.33")),
        news_api_key=os.getenv("NEWS_API_KEY", "").strip(),
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
        x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com").strip(),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", "").strip(),
        polymarket_proxy_address=os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip(),
        polymarket_signature_type=_parse_sig_type(
            os.getenv("POLYMARKET_SIGNATURE_TYPE", "").strip(),
            has_proxy=bool(os.getenv("POLYMARKET_PROXY_ADDRESS", "").strip()),
        ),
        http_proxy=os.getenv("HTTP_PROXY", os.getenv("http_proxy", "")).strip(),
        https_proxy=os.getenv("HTTPS_PROXY", os.getenv("https_proxy", "")).strip(),
        gamma_relay_url=os.getenv("GAMMA_RELAY_URL", "").strip().rstrip("/"),
        im_webhook_url=os.getenv("IM_WEBHOOK_URL", "").strip(),
    )
