from __future__ import annotations

# 策略核心：Edge 计算 + Kelly 公式仓位 + 下注方向决策
# 对应 PDF 步骤3（对比+决策）和步骤6（Edge计算+交易决策逻辑）
from dataclasses import dataclass


@dataclass
class TradeDecision:
    """make_trade_decision 的返回结果，描述【该怎么做】"""
    action: str      # "ENTER"（开仓）或 "HOLD"（不动）
    side: str        # "BUY_YES" / "BUY_NO" / "NONE"
    edge: float      # p_AI - p_market，正数=AI认为Yes被低估，负数=被高估
    size_usdc: float # 建议下注金额（USDC）
    reason: str      # 决策原因，用于日志


def kelly_position_fraction(probability_yes: float, market_price_yes: float) -> float:
    """
    Kelly 公式：计算理论上最优的仓位占比。

    Kelly 公式原型：  f* = (b*p - q) / b
        p  = AI 计算的胜率（Yes 发生的概率）
        q  = 1 - p（失败概率）
        b  = 赔率（赢了能赚多少倍）
               在预测市场里：买 0.40 的 Yes，赢了变 1.00，净盈 0.60
               所以 b = (1/price) - 1 = (1/0.40) - 1 = 1.5

    例：p=0.68, market_price=0.42
        b = (1/0.42) - 1 ≈ 1.38
        f* = (1.38 × 0.68 - 0.32) / 1.38 ≈ 0.448 → 最多用总资金的 44.8%

    注意：实际使用时会乘以 KELLY_FRACTION（通常 0.25）做"分数 Kelly"，
          更保守，防止极端情况破产。
    """
    # 钳位防止除零或数学越界
    p = max(0.0001, min(0.9999, probability_yes))
    q = 1 - p
    # 赔率：在预测市场中 price 是成本，1.0 是最大回报，b 是净利润倍数
    b = max(0.0001, (1 / max(market_price_yes, 0.0001)) - 1)
    fraction = (b * p - q) / b
    # 负数表示没有正期望值，不开仓
    return max(0.0, fraction)


def make_trade_decision(
    p_ai: float,           # AI ensemble 给出的 Yes 概率（0–1）
    p_market: float,       # 市场当前 Yes 价格（0–1，即市场隐含概率）
    edge_threshold: float, # 最小 edge 门槛（如 0.15 = 15%），低于此不下注
    balance_cap_usdc: float, # 可用余额上限（已扣除已持仓）
    max_order_usdc: float,   # 单笔最大下注额
    kelly_fraction: float,   # Kelly 缩减系数（0.25 = 只下满 Kelly 的 25%）
) -> TradeDecision:
    """
    综合 edge 和 Kelly 公式，输出一个具体的交易决策。

    决策流程：
    1. 计算 edge = p_AI - p_market
    2. |edge| 低于门槛 → HOLD，不下注
    3. edge > 0 → AI 认为 Yes 被低估 → BUY_YES
       edge < 0 → AI 认为 No 被低估  → BUY_NO（买 No = 买"不发生"那一侧）
    4. 用 Kelly 公式算最优仓位，再乘缩减系数，最后取 min(max_order, 可用余额×比例)
    """
    # PDF 核心公式：Edge = p_AI - p_market
    edge = p_ai - p_market

    # edge 绝对值不够大，信号不够强，等待更好机会
    if abs(edge) < edge_threshold:
        return TradeDecision(
            action="HOLD",
            side="NONE",
            edge=edge,
            size_usdc=0.0,
            reason=f"edge {edge:.3f} below threshold {edge_threshold:.3f}",
        )

    # 确定下注方向
    side = "BUY_YES" if edge > 0 else "BUY_NO"

    # 对 BUY_NO 方向，需要用 No 那侧的概率和价格来计算 Kelly
    # p_market 的 No 价格 = 1 - p_market（市场总是 Yes+No=1）
    p_for_side = p_ai if edge > 0 else (1 - p_ai)
    market_price_for_side = p_market if edge > 0 else (1 - p_market)

    # 计算原始 Kelly 最优比例，然后缩减到保守水平
    raw_kelly = kelly_position_fraction(p_for_side, market_price_for_side)
    sized_fraction = min(1.0, raw_kelly * max(0.0, kelly_fraction))

    # 最终下注额 = min(单笔上限, 可用余额 × Kelly比例)
    size_usdc = min(max_order_usdc, balance_cap_usdc * sized_fraction)
    # 极端情况（Kelly 结果近零）兜底：至少下 1% 资金，避免完全不入场
    if size_usdc <= 0:
        size_usdc = min(max_order_usdc, balance_cap_usdc * 0.01)

    return TradeDecision(
        action="ENTER",
        side=side,
        edge=edge,
        size_usdc=round(size_usdc, 2),
        reason=f"edge {edge:.3f} passed threshold; kelly_fraction={sized_fraction:.3f}",
    )
