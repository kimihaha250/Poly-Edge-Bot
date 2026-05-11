from __future__ import annotations

import logging
import time
from typing import Any

from bot.clob_client import ClobReadOnlyClient
from bot.config import BotConfig
from bot.data_sources import build_market_context
from bot.ensemble import ensemble_probability
from bot.polymarket_gamma import GammaClient
from bot.positions import PositionStore
from bot.risk import should_stop_loss, should_take_profit
from bot.strategy import make_trade_decision
from bot.trade_logger import log_entry, log_exit
from bot.trader import Trader


# ------------------------------------------------------------------
# Market schema helpers
# ------------------------------------------------------------------

def _extract_question(market: dict[str, Any]) -> str:
    return str(market.get("question") or market.get("title") or "")


def _extract_slug(market: dict[str, Any]) -> str:
    return str(market.get("slug") or "")


def _extract_clob_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    """
    从市场数据提取 (yes_token_id, no_token_id)。

    clobTokenIds 是 JSON 字符串数组，顺序与 outcomes 对应：
        outcomes[0]="Yes" → clobTokenIds[0] = Yes token
        outcomes[1]="No"  → clobTokenIds[1] = No token
    返回 (yes_token_id, no_token_id)，任意一个找不到时返回空字符串。
    """
    import json as _json

    raw_ids = market.get("clobTokenIds")
    if raw_ids:
        try:
            ids = _json.loads(raw_ids) if isinstance(raw_ids, str) else list(raw_ids)
        except Exception:
            ids = []
        if len(ids) >= 2:
            raw_outcomes = market.get("outcomes", '["Yes","No"]')
            try:
                outcomes = _json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
            except Exception:
                outcomes = ["Yes", "No"]
            yes_id = no_id = ""
            for i, outcome in enumerate(outcomes):
                if i >= len(ids):
                    break
                if str(outcome).lower() == "yes":
                    yes_id = str(ids[i])
                elif str(outcome).lower() == "no":
                    no_id = str(ids[i])
            # 找不到明确标签，按位置兜底
            if not yes_id and ids:
                yes_id = str(ids[0])
            if not no_id and len(ids) > 1:
                no_id = str(ids[1])
            return yes_id, no_id
        elif len(ids) == 1:
            return str(ids[0]), ""

    # tokens 对象数组
    tokens = market.get("tokens")
    if isinstance(tokens, list) and len(tokens) >= 2:
        yes_id = no_id = ""
        for t in tokens:
            outcome = str(t.get("outcome", "")).lower()
            if outcome == "yes":
                yes_id = str(t.get("token_id", ""))
            elif outcome == "no":
                no_id = str(t.get("token_id", ""))
        return yes_id, no_id

    # 旧版单值字段
    for field in ("token_id", "yesTokenId", "clobTokenId"):
        if market.get(field):
            return str(market[field]), ""

    return "", ""


def _extract_yes_token_id(market: dict[str, Any]) -> str:
    """向后兼容：只取 Yes token_id"""
    yes_id, _ = _extract_clob_token_ids(market)
    return yes_id


def _extract_market_price_yes(market: dict[str, Any]) -> float | None:
    for key in ("lastTradePrice", "price", "yesPrice"):
        value = market.get(key)
        if value is not None:
            try:
                p = float(value)
                return p if p <= 1 else p / 100
            except Exception:
                pass
    return None


# ------------------------------------------------------------------
# Scan loop helpers
# ------------------------------------------------------------------

def _resolve_market_price(
    market: dict[str, Any],
    clob_ro: ClobReadOnlyClient,
    token_id: str,
) -> tuple[float | None, float | None, float | None]:
    """Returns (market_price_yes, best_bid, best_ask)."""
    market_price_yes = _extract_market_price_yes(market)
    best_bid, best_ask = clob_ro.get_best_bid_ask(token_id)
    if market_price_yes is None:
        if best_bid is not None and best_ask is not None:
            market_price_yes = (best_bid + best_ask) / 2
        elif best_ask is not None:
            market_price_yes = best_ask
        elif best_bid is not None:
            market_price_yes = best_bid
    return market_price_yes, best_bid, best_ask


def _process_risk(
    slug: str,
    token_id: str,
    market_price_yes: float,
    edge: float,
    store: PositionStore,
    trader: Trader,
    config: BotConfig,
    logger: logging.Logger,
) -> None:
    pos = store.get(slug)
    if pos is None:
        return

    entry_price = float(pos["entry_price"])
    size_usdc = float(pos["size_usdc"])

    if should_take_profit(
        current_market_price=market_price_yes,
        entry_price=entry_price,
        current_edge=edge,
        take_profit_delta=config.take_profit_delta,
    ):
        logger.info("[%s] TAKE PROFIT triggered (entry=%.4f now=%.4f)", slug, entry_price, market_price_yes)
        trader.close_position(token_id=token_id, price=market_price_yes, size_usdc=size_usdc)
        log_exit(
            slug=slug,
            question=pos.get("question", slug),
            exit_reason="TAKE_PROFIT",
            entry_price=entry_price,
            exit_price=market_price_yes,
            size_usdc=size_usdc,
            webhook_url=config.im_webhook_url,
        )
        store.remove(slug)

    elif should_stop_loss(
        current_market_price=market_price_yes,
        entry_price=entry_price,
        stop_loss_multiplier=config.stop_loss_multiplier,
    ):
        logger.info("[%s] STOP LOSS triggered (entry=%.4f now=%.4f)", slug, entry_price, market_price_yes)
        trader.close_position(token_id=token_id, price=market_price_yes, size_usdc=size_usdc)
        log_exit(
            slug=slug,
            question=pos.get("question", slug),
            exit_reason="STOP_LOSS",
            entry_price=entry_price,
            exit_price=market_price_yes,
            size_usdc=size_usdc,
            webhook_url=config.im_webhook_url,
        )
        store.remove(slug)


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def run_bot(config: BotConfig) -> None:
    logger = logging.getLogger("poly_edge_bot")
    proxies: dict[str, str] = {}
    if config.http_proxy:
        proxies["http"] = config.http_proxy
    if config.https_proxy:
        proxies["https"] = config.https_proxy
    gamma = GammaClient(proxies=proxies or None, relay_url=config.gamma_relay_url)
    clob_ro = ClobReadOnlyClient(host=config.clob_host, chain_id=config.chain_id)
    trader = Trader(
        dry_run=config.dry_run,
        clob_host=config.clob_host,
        chain_id=config.chain_id,
        private_key=config.polymarket_private_key,
        proxy_address=config.polymarket_proxy_address,
        signature_type=config.polymarket_signature_type,
    )
    store = PositionStore()  # auto-loads from positions.json on startup

    # 本次运行已成功下单的 slug 集合（只增不减）。
    # 用途：防止同一市场在 止盈/止损 平仓后被再次开仓（store 里平仓后会删掉该 slug）。
    # 重启后自动从 store 的持仓快照初始化，避免重启瞬间重复开仓。
    session_entered: set[str] = set(store.slugs())

    models_with_weights = [
        (config.model_1, config.model_1_weight),
        (config.model_2, config.model_2_weight),
        (config.model_3, config.model_3_weight),
    ]

    # 解析目标 slug 集合（来自 TARGET_MARKET_URLS，支持多个逗号分隔）
    target_slugs: set[str] = {
        s.strip() for s in config.target_market_slug.split(",") if s.strip()
    }
    # 解析话题 tag 集合（来自 TARGET_TOPIC_URLS，如 polymarket.com/iran/trump-iran）
    target_tags: list[str] = [
        t.strip() for t in config.target_topic_tags.split(",") if t.strip()
    ]

    _any_filter = bool(target_slugs or target_tags)
    if target_slugs:
        logger.info(
            "Bot started — dry_run=%s | interval=%ds | edge_threshold=%.2f | "
            "tracking %d market slug(s): %s",
            config.dry_run,
            config.scan_interval_seconds,
            config.edge_threshold,
            len(target_slugs),
            ", ".join(sorted(target_slugs)),
        )
    if target_tags:
        logger.info(
            "Bot started — also watching %d topic tag(s): %s",
            len(target_tags),
            ", ".join(target_tags),
        )
    if not _any_filter:
        logger.info(
            "Bot started — dry_run=%s | interval=%ds | edge_threshold=%.2f | "
            "scanning ALL active markets",
            config.dry_run,
            config.scan_interval_seconds,
            config.edge_threshold,
        )

    while True:
        try:
            # ── 1. 按精确 slug 筛选的市场 ────────────────────────────────
            if target_slugs:
                base_markets = gamma.list_active_markets(limit=200)
                slug_markets = [m for m in base_markets if _extract_slug(m) in target_slugs]
            else:
                slug_markets = gamma.list_active_markets(limit=100) if not target_tags else []

            # ── 2. 按话题 tag 拉取的市场 ─────────────────────────────────
            tag_markets: list[dict] = []
            for tag in target_tags:
                tag_markets.extend(gamma.list_markets_by_tag(tag, limit=100))

            # ── 3. 合并去重（以市场 slug 为 key）────────────────────────
            seen: set[str] = set()
            markets: list[dict] = []
            for m in slug_markets + tag_markets:
                key = _extract_slug(m) or id(m)
                if key not in seen:
                    seen.add(str(key))
                    markets.append(m)

            if not markets:
                logger.warning("No active markets found, sleeping...")
                time.sleep(config.scan_interval_seconds)
                continue

            for market in markets[:20]:
                logger.info("-------------market is starting ")

                #话题名字
                question = _extract_question(market)
                slug = _extract_slug(market)
                token_id = _extract_yes_token_id(market)

                logger.info("-------------slug is "+slug+",question:"+question+",token_id:"+token_id)
                if not question or not token_id:
                    continue

                market_price_yes, best_bid, best_ask = _resolve_market_price(market, clob_ro, token_id)
               
                if market_price_yes is None:
                    continue

                # --- risk check before fetching AI (fast path) ---
                _process_risk(
                    slug=slug,
                    token_id=token_id,
                    market_price_yes=market_price_yes,
                    edge=0.0,
                    store=store,
                    trader=trader,
                    config=config,
                    logger=logger,
                )
            
                context = build_market_context(
                    question=question,
                    news_api_key=config.news_api_key,
                    tavily_api_key=config.tavily_api_key,
                    x_bearer_token=config.x_bearer_token,
                    gamma_client=gamma,
                )
                p_ai_percent, model_outputs = ensemble_probability(
                    base_url=config.openrouter_base_url,
                    api_key=config.openrouter_api_key,
                    question=question,
                    context=context,
                    models_with_weights=models_with_weights,
                )
                p_ai = p_ai_percent / 100.0

                # Respect total exposure cap
                available_usdc = max(0.0, config.max_total_exposure_usdc - store.total_exposure_usdc())
                decision = make_trade_decision(
                    p_ai=p_ai,
                    p_market=market_price_yes,
                    edge_threshold=config.edge_threshold,
                    balance_cap_usdc=available_usdc,
                    max_order_usdc=config.max_order_usdc,
                    kelly_fraction=config.kelly_fraction,
                )

                logger.info(
                    "[%s] p_market=%.3f p_ai=%.3f edge=%+.3f action=%-5s side=%-8s size=$%.2f",
                    slug,
                    market_price_yes,
                    p_ai,
                    decision.edge,
                    decision.action,
                    decision.side,
                    decision.size_usdc,
                )
                for out in model_outputs:
                    logger.info("  %-45s p=%5.1f%%  %s", out.model, out.probability, out.reason[:120])

                # --- post-AI risk check with real edge ---
                _process_risk(
                    slug=slug,
                    token_id=token_id,
                    market_price_yes=market_price_yes,
                    edge=decision.edge,
                    store=store,
                    trader=trader,
                    config=config,
                    logger=logger,
                )

                # --- entry ---
                # 校验所有模型输出是否有效：
                #   1. 任何一个模型调用失败（is_fallback）→ 跳过
                #   2. 任何一个模型概率为极端值 0% 或 100% → 跳过
                #      （极端值通常意味着事件已发生/已结案，不适合套利）
                failed_models = [o.model for o in model_outputs if o.is_fallback]
                extreme_models = [o.model for o in model_outputs if not o.is_fallback and o.probability in (0.0, 100.0)]

                if failed_models:
                    logger.warning(
                        "[%s] 跳过下单 — %d 个模型未能返回有效概率: %s",
                        slug, len(failed_models), ", ".join(failed_models),
                    )
                if extreme_models:
                    logger.warning(
                        "[%s] 跳过下单 — %d 个模型返回极端概率(0%%/100%%): %s",
                        slug, len(extreme_models), ", ".join(extreme_models),
                    )

                _models_invalid = bool(failed_models or extreme_models)
                if decision.action == "ENTER" and decision.size_usdc > 0 \
                        and slug not in store \
                        and slug not in session_entered \
                        and not _models_invalid:
                    # 根据方向选择正确的 token_id 和价格
                    # BUY_YES → 买 Yes token，用 Yes 的 ask 价
                    # BUY_NO  → 买 No token，用 No 的 ask 价（≈ 1 - yes_bid）
                    yes_token_id, no_token_id = _extract_clob_token_ids(market)
                    neg_risk = bool(market.get("negRisk", False))
                    # orderPriceMinTickSize 来自 Gamma API，必须以字符串形式传给 create_order options
                    raw_tick = market.get("orderPriceMinTickSize", 0.001)
                    tick_size = str(raw_tick) if raw_tick else "0.001"

                    if decision.side == "BUY_NO" and no_token_id:
                        order_token_id = no_token_id
                        # No 价格 = 1 - Yes 最优买价（bid）；没有 bid 时用 1 - lastTradePrice
                        best_no_ask = round(1.0 - (best_bid if best_bid is not None else market_price_yes), 3)
                        buy_price = best_no_ask
                    else:
                        order_token_id = yes_token_id or token_id
                        buy_price = best_ask if best_ask is not None else market_price_yes

                    # 价格精度：Polymarket 要求 3 位小数（tick=0.001）
                    buy_price = round(buy_price, 3)

                    result = trader.place_limit_order(
                        token_id=order_token_id,
                        side=decision.side,
                        price=buy_price,
                        size_usdc=decision.size_usdc,
                        neg_risk=neg_risk,
                        tick_size=tick_size,
                    )
                    logger.info("------------- order is ok result: %s", result)
                    if result.get("status") in {"submitted", "simulated"}:
                        session_entered.add(slug)   # 记入本次运行已下单集合，防止重复开仓
                        store.add(slug, {
                            "entry_price": buy_price,
                            "side": decision.side,
                            "size_usdc": decision.size_usdc,
                            "token_id": token_id,
                            "question": question,
                        })
                        log_entry(
                            slug=slug,
                            question=question,
                            side=decision.side,
                            size_usdc=decision.size_usdc,
                            p_ai=p_ai,
                            p_market=market_price_yes,
                            order_status=result["status"],
                            model_details=[
                                {"model": o.model, "probability": o.probability, "reason": o.reason}
                                for o in model_outputs
                            ],
                            webhook_url=config.im_webhook_url,
                        )

            logger.info("Scan complete — open positions: %d | sleeping %ds", len(store), config.scan_interval_seconds)
            time.sleep(config.scan_interval_seconds)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user (Ctrl-C).")
            break
        except Exception:
            logger.exception("Main loop error — will retry after sleep.")
            time.sleep(config.scan_interval_seconds)
