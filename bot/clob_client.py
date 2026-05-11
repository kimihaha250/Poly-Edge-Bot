from __future__ import annotations

# CLOB（Central Limit Order Book）只读客户端
# 负责读取实时订单簿价格和历史价格数据，不涉及下单
# 对应 PDF 步骤3：读取实时价格和历史价格
#
# py-clob-client 返回的是强类型对象（OrderBookSummary、PricePoint），
# 这个包装类把它们全部转成普通 Python dict，其他模块不需要直接依赖 py-clob-client。

from typing import Any


class ClobReadOnlyClient:
    """
    只读 CLOB 客户端，无需私钥，不能下单。
    如果 py-clob-client 未安装或连接失败，所有方法返回空数据（不崩溃）。
    """

    def __init__(self, host: str, chain_id: int) -> None:
        self.host = host
        self.chain_id = chain_id
        self._client = None  # 内部 ClobClient 实例，初始化失败时为 None

        try:
            from py_clob_client.client import ClobClient
            # 只读模式不传私钥（key），ClobClient 允许无 key 初始化
            self._client = ClobClient(host=host, chain_id=chain_id)
        except Exception:
            # 库未安装或网络不通时静默失败，后续方法统一返回空数据
            self._client = None

    @property
    def available(self) -> bool:
        """客户端是否成功初始化（可用于健康检查）"""
        return self._client is not None

    # ------------------------------------------------------------------
    # 订单簿相关
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_entries(entries: Any) -> list[dict[str, Any]]:
        """
        把 py-clob-client 返回的 OrderSummary 对象列表统一转成 dict 列表。
        新版库返回对象（有 .price .size 属性），旧版可能直接返回 dict。
        统一格式：[{"price": "0.42", "size": "100"}, ...]
        """
        if not entries:
            return []
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                result.append(entry)
            else:
                # 对象格式：用 getattr 安全读取属性
                result.append({
                    "price": str(getattr(entry, "price", 0)),
                    "size": str(getattr(entry, "size", 0)),
                })
        return result

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """
        获取某个代币的完整订单簿（买单 bids + 卖单 asks）。

        返回格式：
        {
            "bids": [{"price": "0.41", "size": "500"}, ...],  # 按价格降序
            "asks": [{"price": "0.42", "size": "300"}, ...],  # 按价格升序
        }
        bids = 市场上愿意买的人出的价
        asks = 市场上愿意卖的人要的价
        成交价通常在 bids[0] 和 asks[0] 之间（买一/卖一）
        """
        if not self._client:
            return {"bids": [], "asks": []}
        try:
            ob = self._client.get_order_book(token_id=token_id)
            # ob 是 OrderBookSummary 对象，需要用 getattr 读取属性
            bids = getattr(ob, "bids", None) if not isinstance(ob, dict) else ob.get("bids", [])
            asks = getattr(ob, "asks", None) if not isinstance(ob, dict) else ob.get("asks", [])
            return {
                "bids": self._normalise_entries(bids),
                "asks": self._normalise_entries(asks),
            }
        except Exception:
            return {"bids": [], "asks": []}

    def get_best_bid_ask(self, token_id: str) -> tuple[float | None, float | None]:
        """
        返回当前最优买价（bid）和最优卖价（ask）。

        bid  = 买一价：当前市场上最高的买单价格（立刻卖出能拿到的价格）
        ask  = 卖一价：当前市场上最低的卖单价格（立刻买入需要付的价格）
        spread = ask - bid（价差，越小越好）

        bot 下单时通常用 ask 价格作为入场价，这样限价单能立刻成交。
        """
        ob = self.get_orderbook(token_id=token_id)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        try:
            best_bid = float(bids[0]["price"]) if bids else None
        except Exception:
            best_bid = None
        try:
            best_ask = float(asks[0]["price"]) if asks else None
        except Exception:
            best_ask = None
        return best_bid, best_ask

    # ------------------------------------------------------------------
    # 历史价格
    # ------------------------------------------------------------------

    def get_prices_history(self, token_id: str, fidelity: int = 60) -> list[dict[str, Any]]:
        """
        获取某个代币的历史价格序列，供回测和趋势分析使用。

        参数：
            fidelity：K 线粒度（分钟），1=1分钟K线，60=1小时K线（默认）

        返回格式：
            [{"t": 1710000000, "p": 0.42}, ...]
            t = Unix 时间戳（秒）
            p = 该时间点的价格（0–1）

        版本兼容：
            - 新版库：get_prices_history(token_id, fidelity=60)
            - 旧版库：get_prices_history(token_id)（不接受 fidelity 参数）
            用 try/except 自动适配
        """
        if not self._client:
            return []
        try:
            raw = self._client.get_prices_history(token_id=token_id, fidelity=fidelity)
        except TypeError:
            # 旧版 API 不接受 fidelity 参数，降级调用无参版本
            try:
                raw = self._client.get_prices_history(token_id=token_id)
            except Exception:
                return []
        except Exception:
            return []

        if not raw:
            return []

        # 统一转成 dict 格式：PricePoint 对象有 .t（时间戳）和 .p（价格）属性
        normalised: list[dict[str, Any]] = []
        for point in raw:
            if isinstance(point, dict):
                normalised.append(point)
            else:
                normalised.append({
                    "t": getattr(point, "t", None),          # Unix 时间戳
                    "p": float(getattr(point, "p", 0) or 0), # 价格 0–1
                })
        return normalised
