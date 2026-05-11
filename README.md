# AI Probability Arbitrage Bot (Polymarket)

[中文文档（完整说明）](./readme-zh.md)

## English Overview

This bot runs an AI-driven probability arbitrage workflow on Polymarket:

1. Market discovery via Gamma API (`bot/polymarket_gamma.py`)
2. Price/orderbook reads via CLOB (`bot/clob_client.py`)
3. Context collection (news/social placeholders) (`bot/data_sources.py`)
4. 3-model OpenRouter ensemble forecasting (`bot/ensemble.py`)
5. Edge + Kelly sizing decision (`bot/strategy.py`)
6. Auto order execution + risk controls (`bot/trader.py`, `bot/risk.py`)
7. Main loop with retries, persistence, and logs (`bot/runner.py`)

Recent updates include:

- Targeting by market URLs/slugs (`TARGET_MARKET_URLS`)
- Targeting by topic URLs/tags (`TARGET_TOPIC_URLS`)
- Strict model validation before entering positions
- FOK market-order flow with signature-type support
- Markdown trade logs + IM webhook notifications (`IM_WEBHOOK_URL`)

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Safety

- `DRY_RUN=true` by default
- Keep dry-run until token mapping, signature type, and order path are validated
- Start with very small capital and narrow scope (`TARGET_MARKET_URLS`)

---

## 中文入口

- 完整中文说明请看：[`readme-zh.md`](./readme-zh.md)
- 推荐先读中文文档中的「关键配置说明」和「常见问题」


