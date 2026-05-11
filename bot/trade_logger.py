from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from bot.notifier import notify_entry, notify_exit

LOG_PATH = "trader_log.md"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_header() -> None:
    """如果日志文件不存在，先写入表头"""
    if os.path.exists(LOG_PATH):
        return
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("# 交易记录日志\n\n")
        f.write("> 由 bot 自动生成，每次开仓/平仓/止盈/止损时追加\n\n")
        f.write("---\n\n")
        f.write(
            "| 时间 | 事件 | 市场 | Slug | "
            "方向 | 买入金额 | AI 概率 | 市场概率 | 差值(Edge) | 订单状态 |\n"
        )
        f.write(
            "|------|------|------|------|"
            "------|----------|---------|----------|-----------|----------|\n"
        )


def log_entry(
    slug: str,
    question: str,
    side: str,
    size_usdc: float,
    p_ai: float,
    p_market: float,
    order_status: str,
    model_details: list[dict[str, Any]] | None = None,
    webhook_url: str = "",
) -> None:
    """
    记录一笔开仓到 trader_log.md。

    参数说明：
        slug         : 市场唯一标识，用于构造 Polymarket URL
        question     : 市场问题原文
        side         : "BUY_YES" / "BUY_NO"
        size_usdc    : 下注金额（USDC）
        p_ai         : AI ensemble 概率（0–1）
        p_market     : 市场当前概率（0–1）
        order_status : "submitted" / "simulated" / "error"
        model_details: 各模型打分详情（可选，写入折叠块）
    """
    _ensure_header()
    edge = p_ai - p_market
    short_q = question[:50] + "…" if len(question) > 50 else question
    edge_str = f"+{edge:.1%}" if edge >= 0 else f"{edge:.1%}"
    status_emoji = {"submitted": "✅", "simulated": "🔵", "error": "❌"}.get(order_status, "⚪")

    row = (
        f"| {_now()} "
        f"| 🟢 开仓 "
        f"| {short_q} "
        f"| `{slug}` "
        f"| {side} "
        f"| ${size_usdc:.2f} "
        f"| {p_ai:.1%} "
        f"| {p_market:.1%} "
        f"| {edge_str} "
        f"| {status_emoji} {order_status} |\n"
    )

    lines = [row]

    # 如果有各模型明细，用 HTML details 折叠块附在行后
    if model_details:
        lines.append("\n<details><summary>📊 模型打分明细</summary>\n\n")
        lines.append("| 模型 | 概率 | 理由 |\n|------|------|------|\n")
        for m in model_details:
            reason = str(m.get("reason", "")).replace("|", "｜")[:80]
            lines.append(f"| `{m['model']}` | {m['probability']:.1f}% | {reason} |\n")
        lines.append("\n</details>\n\n")

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.writelines(lines)

    notify_entry(
        webhook_url=webhook_url,
        slug=slug,
        question=question,
        side=side,
        size_usdc=size_usdc,
        p_ai=p_ai,
        p_market=p_market,
        edge=p_ai - p_market,
        order_status=order_status,
    )


def log_exit(
    slug: str,
    question: str,
    exit_reason: str,       # "TAKE_PROFIT" / "STOP_LOSS" / "MANUAL"
    entry_price: float,
    exit_price: float,
    size_usdc: float,
    webhook_url: str = "",
) -> None:
    """记录一笔平仓（止盈/止损）到 trader_log.md"""
    _ensure_header()
    short_q = question[:50] + "…" if len(question) > 50 else question
    pnl = (exit_price - entry_price) * (size_usdc / max(entry_price, 0.001))
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    reason_emoji = {"TAKE_PROFIT": "🟡 止盈", "STOP_LOSS": "🔴 止损"}.get(exit_reason, f"⚪ {exit_reason}")

    row = (
        f"| {_now()} "
        f"| {reason_emoji} "
        f"| {short_q} "
        f"| `{slug}` "
        f"| SELL "
        f"| {pnl_str} "
        f"| — "
        f"| {exit_price:.1%} "
        f"| 入场 {entry_price:.1%} "
        f"| — |\n"
    )
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(row)

    notify_exit(
        webhook_url=webhook_url,
        slug=slug,
        question=question,
        exit_reason=exit_reason,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl=pnl,
    )
