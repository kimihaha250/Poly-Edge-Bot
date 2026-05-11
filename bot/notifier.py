from __future__ import annotations

# IM 消息通知模块
# 支持飞书（Feishu/Lark）自定义机器人 webhook，以及通用 JSON webhook（企业微信、钉钉等）
# 发送失败只打 WARNING，不影响主流程。

import json
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 内部：按 URL 自动识别 webhook 类型并构造 payload
# ------------------------------------------------------------------

def _is_feishu(url: str) -> bool:
    return "feishu.cn" in url or "larksuite.com" in url


def _is_wecom(url: str) -> bool:
    return "qyapi.weixin.qq.com" in url


def _is_dingtalk(url: str) -> bool:
    return "oapi.dingtalk.com" in url


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url


def _feishu_card(title: str, color: str, fields: list[tuple[str, str]]) -> dict[str, Any]:
    """构造飞书卡片消息（interactive card）"""
    md_lines = "\n".join(f"**{k}**：{v}" for k, v in fields)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"content": title, "tag": "plain_text"},
                "template": color,   # green / red / orange / blue / grey
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"content": md_lines, "tag": "lark_md"},
                }
            ],
        },
    }


def _generic_payload(title: str, text: str) -> dict[str, Any]:
    """企业微信 / 钉钉 / Discord / 通用 JSON"""
    return {"msgtype": "text", "text": {"content": f"{title}\n{text}"}}


def _discord_payload(title: str, text: str, color: int) -> dict[str, Any]:
    return {"embeds": [{"title": title, "description": text, "color": color}]}


def _build_payload(
    url: str,
    title: str,
    color_feishu: str,
    color_discord: int,
    fields: list[tuple[str, str]],
) -> dict[str, Any]:
    text = "\n".join(f"{k}: {v}" for k, v in fields)
    if _is_feishu(url):
        return _feishu_card(title, color_feishu, fields)
    if _is_discord(url):
        return _discord_payload(title, text, color_discord)
    # 企业微信 / 钉钉 / 通用
    return _generic_payload(title, text)


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------

def _send(url: str, payload: dict[str, Any]) -> None:
    """同步发送，供线程调用"""
    try:
        import requests as _req
        resp = _req.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Webhook send failed: %s", exc)


def _send_async(url: str, payload: dict[str, Any]) -> None:
    """异步发送（daemon 线程），不阻塞主流程"""
    t = threading.Thread(target=_send, args=(url, payload), daemon=True)
    t.start()


def notify_entry(
    webhook_url: str,
    slug: str,
    question: str,
    side: str,
    size_usdc: float,
    p_ai: float,
    p_market: float,
    edge: float,
    order_status: str,
) -> None:
    """开仓通知"""
    if not webhook_url:
        return

    status_label = {"submitted": "✅ 已提交", "simulated": "🔵 模拟", "error": "❌ 失败"}.get(
        order_status, order_status
    )
    color_feishu = "green" if order_status == "submitted" else "orange" if order_status == "simulated" else "red"
    color_discord = 0x00C851 if order_status == "submitted" else 0xFF8800 if order_status == "simulated" else 0xFF4444

    fields = [
        ("事件", "🟢 开仓"),
        ("市场", question[:60] + ("…" if len(question) > 60 else "")),
        ("Slug", slug),
        ("方向", side),
        ("金额", f"${size_usdc:.2f} USDC"),
        ("AI 概率", f"{p_ai:.1%}"),
        ("市场概率", f"{p_market:.1%}"),
        ("Edge", f"{edge:+.1%}"),
        ("状态", status_label),
    ]
    payload = _build_payload(
        webhook_url,
        title=f"🟢 Polymarket 开仓 — {side}",
        color_feishu=color_feishu,
        color_discord=color_discord,
        fields=fields,
    )
    _send_async(webhook_url, payload)


def notify_exit(
    webhook_url: str,
    slug: str,
    question: str,
    exit_reason: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
) -> None:
    """平仓通知"""
    if not webhook_url:
        return

    reason_label = {"TAKE_PROFIT": "🟡 止盈", "STOP_LOSS": "🔴 止损"}.get(exit_reason, f"⚪ {exit_reason}")
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    color_feishu = "yellow" if exit_reason == "TAKE_PROFIT" else "red"
    color_discord = 0xFFCC00 if exit_reason == "TAKE_PROFIT" else 0xFF4444

    fields = [
        ("事件", reason_label),
        ("市场", question[:60] + ("…" if len(question) > 60 else "")),
        ("Slug", slug),
        ("入场价", f"{entry_price:.3f}"),
        ("出场价", f"{exit_price:.3f}"),
        ("盈亏", pnl_str),
    ]
    payload = _build_payload(
        webhook_url,
        title=f"{reason_label} Polymarket 平仓",
        color_feishu=color_feishu,
        color_discord=color_discord,
        fields=fields,
    )
    _send_async(webhook_url, payload)
