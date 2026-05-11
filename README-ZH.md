# Poly-EDGE 中文说明

这是一个基于 Polymarket 的 AI 概率套利机器人。  
核心流程是：抓取市场 -> 多模型预测 -> 计算 edge -> 自动下单 -> 风控止盈止损 -> 记录与通知。

## 主要功能

- 支持按单个/多个市场 URL（或 slug）定向交易：`TARGET_MARKET_URLS`
- 支持按话题页 URL 批量抓取市场：`TARGET_TOPIC_URLS`
- 三模型 Ensemble（OpenRouter）加权输出概率
- 严格下单前校验：任一模型失败或返回极端值（0%/100%）即跳过
- 自动交易（默认 FOK 市价单，含滑点保护价格）
- 止盈/止损自动平仓
- 防重复开仓（持仓快照 + 会话级去重）
- Markdown 交易日志：`trader_log.md`
- IM webhook 通知（飞书/企微/钉钉/Discord）

## 环境要求

- Python 3.10+
- 可访问 Polymarket API（国内通常需要代理或中继）

安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 快速开始

1. 复制配置文件：

```bash
cp .env.example .env
```

2. 按需修改 `.env`（至少配置 OpenRouter 和目标市场）
3. 先用模拟模式验证：`DRY_RUN=true`
4. 启动：

```bash
python main.py
```

## 关键配置说明

### 运行参数

- `DRY_RUN`：是否模拟下单（建议先 `true`）
- `SCAN_INTERVAL_SECONDS`：扫描间隔秒数
- `EDGE_THRESHOLD`：最低 edge 阈值（低于阈值不进场）
- `MAX_ORDER_USDC`：单笔最大下单金额
- `MAX_TOTAL_EXPOSURE_USDC`：总风险敞口上限
- `KELLY_FRACTION`：Kelly 仓位系数
- `TAKE_PROFIT_DELTA`、`STOP_LOSS_MULTIPLIER`：止盈止损参数

### 市场筛选

- `TARGET_MARKET_URLS`：精确市场列表（支持 URL 或 slug，逗号分隔）
- `TARGET_TOPIC_URLS`：话题页列表（支持 URL 或 tag，逗号分隔）

示例：

```dotenv
TARGET_MARKET_URLS=https://polymarket.com/event/will-trump-be-indicted
TARGET_TOPIC_URLS=https://polymarket.com/iran/trump-iran
```

### LLM 配置

- `OPENROUTER_API_KEY`
- `MODEL_1/2/3` 与对应权重 `MODEL_1_WEIGHT/2/3_WEIGHT`

### Polymarket/CLOB 配置

- `CLOB_HOST`：CLOB 地址（可填中继地址）
- `CHAIN_ID=137`
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_PROXY_ADDRESS`：网页端账户的代理地址（可选）
- `POLYMARKET_SIGNATURE_TYPE`：
  - `0` = EOA
  - `1` = POLY_PROXY
  - `2` = POLY_GNOSIS_SAFE（网页账户常用）

> 若你使用网页端代理账户，通常应配置 `POLYMARKET_SIGNATURE_TYPE=2`，否则可能出现 `invalid signature`。

### 代理与中继

- `HTTP_PROXY` / `HTTPS_PROXY`：本地代理
- `GAMMA_RELAY_URL`：Gamma 中继（可选）

## IM Webhook 通知

新增参数：

```dotenv
IM_WEBHOOK_URL=
```

填入 webhook 后，每次写入 `trader_log.md` 的开仓/平仓事件都会异步推送到 IM 渠道。

支持：

- 飞书机器人 webhook（优先卡片消息）
- 企业微信机器人 webhook
- 钉钉机器人 webhook
- Discord webhook

说明：

- webhook 留空则不推送
- 推送失败只记 warning，不影响交易主流程

## 日志与持仓文件

- `trader_log.md`：每笔开平仓记录（含模型概率明细）
- `positions.json`：持仓快照，重启后自动恢复

## 交易逻辑简述

1. 拉取目标市场（精确市场 + 话题市场）
2. 读取市场价格（Gamma + CLOB）
3. 构建上下文并调用 3 个 LLM
4. 汇总概率 `p_ai`，计算 `edge = p_ai - p_market`
5. 通过阈值与 Kelly 计算下单金额
6. 满足条件则下单（FOK），并写入持仓与日志
7. 每轮检查止盈/止损，触发则平仓并记录

## 常见问题

### 1) `invalid signature`

常见原因是签名类型与账户模式不一致。  
网页端代理账户通常需要：

- `POLYMARKET_PROXY_ADDRESS` 已配置
- `POLYMARKET_SIGNATURE_TYPE=2`

### 2) `not enough balance / allowance`

代表余额或授权不足。需要在 Polymarket 网页端先完成资金与授权初始化（通常手动下过一笔小单后即可）。

### 3) `connection reset by peer` / curl 35

属于网络抖动或线路问题。当前代码已在 CLOB 请求层做连接类错误重试（指数退避），可降低瞬时失败率。

## 安全建议

- 默认先 `DRY_RUN=true` 验证逻辑
- 实盘先小金额测试
- 不要把 `.env` 提交到仓库
- 为私钥使用独立小资金钱包

