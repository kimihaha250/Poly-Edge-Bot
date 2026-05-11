from __future__ import annotations

# 实盘下单模块，封装 py-clob-client 的订单提交逻辑
# 对应 PDF 步骤4：自动下单（Polymarket API）和步骤7：自动下单+风控模块
#
# 关键概念：
#   CLOB（Central Limit Order Book）= 中心化限价订单簿，Polymarket 交易层
#   单位换算：CLOB 以"份数（contracts）"下单，不是 USDC
#             size_contracts = size_usdc / price
#             例：买 $10 的 Yes（当前价 0.40）→ 10/0.40 = 25 contracts

import logging
from typing import Any


class Trader:
    """
    实盘交易客户端，基于 py-clob-client。

    两种 API 版本都兼容：
    - v2+（推荐）：create_order(OrderArgs) → post_order(signed_order)
    - legacy：create_and_post_order(**kwargs)

    DRY_RUN 模式下只打日志，不发真实订单，是默认的安全模式。
    """

    def __init__(
        self,
        dry_run: bool,              # True = 模拟模式，不发真实订单
        clob_host: str,             # CLOB API 地址，默认 https://clob.polymarket.com
        chain_id: int,              # 链 ID，Polygon 主网 = 137
        private_key: str,           # 钱包私钥（用于签名订单），留空则自动降级 DRY_RUN
        proxy_address: str,         # Polymarket 代理钱包地址（可选）
        signature_type: int = 0,    # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE（网页账户）
    ) -> None:
        self.dry_run = dry_run
        self._client = None
        self._api_version: int = 2
        self._signature_type = signature_type
        self.logger = logging.getLogger(self.__class__.__name__)

        if dry_run:
            self.logger.info("Trader initialized in DRY_RUN mode (no real orders)")
            return

        # 没有私钥就无法签名，自动降级为安全的模拟模式
        if not private_key:
            self.logger.warning("POLYMARKET_PRIVATE_KEY not set → DRY_RUN fallback")
            self.dry_run = True
            return

        try:
            from py_clob_client.client import ClobClient

            # py_clob_client 内部用 httpx 发请求，默认不读系统代理。
            # 如果系统设置了 HTTP_PROXY / HTTPS_PROXY，这里手动注入到 httpx 客户端，
            # 否则在 GFW 环境下 clob.polymarket.com 的 TLS 握手会被 Reset。
            self._patch_clob_httpx_proxy()
            self.logger.info("------Trading client is initial... (LIVE, api_version=%d)", self._api_version)

            # 初始化 CLOB 客户端
            # signature_type（0/1/2）必须和 funder 一起传，否则签名方式和服务器期望不匹配：
            #   - EOA (0)             → 直接用私钥签，maker=signer
            #   - POLY_PROXY (1)      → 老式 Polymarket 代理钱包
            #   - POLY_GNOSIS_SAFE (2)→ 网页端账户（有 proxy_address 时必须传 2）
            # 缺少 signature_type 或数值错误都会导致 "invalid signature"
            try:
                client = ClobClient(
                    host=clob_host,
                    chain_id=chain_id,
                    key=private_key,
                    signature_type=self._signature_type,
                    funder=proxy_address if proxy_address else None,
                )
            except TypeError:
                client = ClobClient(host=clob_host, chain_id=chain_id, key=private_key)
            self.logger.info(
                "ClobClient init: sig_type=%d funder=%s",
                self._signature_type, proxy_address or "(none)",
            )

            # 生成/恢复 API 凭证（不传 nonce，使用默认值 0）
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            self._client = client
            self._api_version = self._detect_api_version()
            self.logger.info("Trading client ready (LIVE, api_version=%d)", self._api_version)
        except Exception as exc:
            # 任何初始化失败都安全降级，不崩溃；打印完整堆栈方便排查
            import traceback as _tb
            self.logger.warning(
                "Trading client init failed → DRY_RUN fallback.\n"
                "Error: %s\nTraceback:\n%s",
                exc, _tb.format_exc(),
            )
            self.dry_run = True

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _patch_clob_httpx_proxy(self) -> None:
        """
        py_clob_client 内部用 httpx 发请求，httpx 的 TLS 指纹会被 Cloudflare Reset。
        解决方案：用 curl_cffi 替换 py_clob_client 模块级的 _http_client，
        curl_cffi 模拟 Chrome TLS 指纹，可以绕过 Cloudflare Bot 检测。
        curl_cffi 不可用时降级到带代理的 httpx。
        """
        try:
            import py_clob_client.http_helpers.helpers as _helpers
            from curl_cffi import requests as _cffi

            # curl_cffi.Response 和 httpx.Response 接口兼容（都有 .status_code / .json()）
            # 用包装类让 py_clob_client 无感知地切换到 curl_cffi
            # 关键：必须转发 content= 参数（POST 下单时 py_clob_client 用它传预序列化的 JSON 字节）
            # 自动重试：Cloudflare / GFW 偶发 Connection reset，retry 2 次可挡住 90%+
            _logger = self.logger
            class _CurlCffiClient:
                def request(self, method, url, headers=None, json=None, content=None, data=None, **kw):
                    import time as _time
                    kwargs = {
                        "headers": headers,
                        "impersonate": "chrome",
                        "timeout": 30,
                    }
                    # 按优先级转发 body：content（bytes 预序列化）→ data → json
                    if content is not None:
                        kwargs["data"] = content
                    elif data is not None:
                        kwargs["data"] = data
                    elif json is not None:
                        kwargs["json"] = json

                    last_exc = None
                    for attempt in range(3):
                        try:
                            return _cffi.request(method, url, **kwargs)
                        except Exception as exc:
                            # 只对连接类错误重试（35=SSL/TLS reset, 56=recv fail, 28=timeout）
                            msg = str(exc).lower()
                            if any(k in msg for k in ("connection reset", "recv failure", "timeout", "curl: (35)", "curl: (56)", "curl: (28)")):
                                last_exc = exc
                                wait = 0.5 * (2 ** attempt)  # 0.5s → 1s → 2s
                                _logger.warning("CLOB request retry %d/3 after %.1fs: %s", attempt + 1, wait, exc)
                                _time.sleep(wait)
                                continue
                            raise
                    # 三次都失败，抛最后一次
                    raise last_exc

            _helpers._http_client = _CurlCffiClient()
            self.logger.info("CLOB client: curl_cffi (Chrome TLS) injected ✓")
            return
        except ImportError:
            self.logger.warning("curl_cffi not found, falling back to httpx + proxy")
        except Exception as e:
            self.logger.warning("curl_cffi injection failed: %s", e)

        # 降级方案：httpx + 显式代理
        import os, httpx
        proxy_url = (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        )
        if not proxy_url:
            self.logger.warning("CLOB: no proxy configured and curl_cffi unavailable — may fail behind GFW")
            return
        try:
            import py_clob_client.http_helpers.helpers as _helpers
            try:
                _helpers._http_client = httpx.Client(proxy=proxy_url, timeout=30)
            except TypeError:
                _helpers._http_client = httpx.Client(proxies={"all://": proxy_url}, timeout=30)
            self.logger.info("CLOB httpx client: proxy injected → %s", proxy_url)
        except Exception as e:
            self.logger.warning("CLOB httpx proxy injection failed: %s", e)

    def _detect_api_version(self) -> int:
        """
        检测已安装的 py-clob-client 是 v2（有 OrderArgs）还是旧版。
        v2 用 create_order() + post_order()，旧版用 create_and_post_order()。
        """
        try:
            from py_clob_client.clob_types import OrderArgs  # noqa: F401
            return 2
        except ImportError:
            return 1

    def _usdc_to_contracts(self, price: float, size_usdc: float) -> float:
        """
        USDC 金额换算成 CLOB 的份数（contracts）。
        CLOB 订单簿以份数计量，1 份 = $1 如果结果为 YES（概率=1）。
        换算：contracts = usdc / price
        例：花 $10 买价格为 0.40 的 Yes → 10/0.40 = 25 contracts
        """
        if price <= 0:
            raise ValueError(f"Cannot convert size — invalid price: {price}")
        return round(size_usdc / price, 4)

    def _resolve_side_const(self, buy_or_sell: str):
        """
        获取 py-clob-client 里代表方向的常量。
        v2 用枚举类型（BUY/SELL），旧版直接用字符串，这里做统一兼容。
        """
        try:
            from py_clob_client.clob_types import BUY, SELL
            return BUY if buy_or_sell == "BUY" else SELL
        except ImportError:
            # 旧版库没有枚举，直接传字符串
            return buy_or_sell

    def _build_order_args(self, token_id: str, buy_or_sell: str, price: float, size_contracts: float):
        """
        构建 OrderArgs 对象（v2 API 所需的订单参数结构）。
        token_id ：Yes 或 No 那一侧的代币 ID（不是市场 ID）。
        注意：neg_risk 和 tick_size 必须放在 create_order 的 options= 里，不在 OrderArgs 里。
        """
        from py_clob_client.clob_types import OrderArgs
        side = self._resolve_side_const(buy_or_sell)
        return OrderArgs(token_id=token_id, price=price, size=size_contracts, side=side)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def place_limit_order(
        self,
        token_id: str,          # Yes 或 No 代币的 token_id（来自 Gamma API）
        side: str,              # "BUY_YES" | "BUY_NO" | "SELL"
        price: float,           # 最差价保护（滑点上限），范围 [0, 1]
        size_usdc: float,       # 想花多少 USDC
        neg_risk: bool = False, # negRisk 多结果市场必须为 True
        tick_size: str = "0.001",  # 市场最小价格单位，必须传给 options 才能正确签名
    ) -> dict[str, Any]:
        """
        以市价单（FOK）立即入场。

        FOK = Fill-Or-Kill：立即吃对手盘，全部成交或全部取消，不挂单等待。
        price 作为最差价保护（滑点上限），amount 直接传 USDC 金额，SDK 自动换算份数。

        返回 dict 包含 status 字段：
            "simulated"  → DRY_RUN 模式，未真实下单
            "submitted"  → 成功提交到 CLOB
            "error"      → 下单失败，见 error 字段
        """
        # 统一 side 方向为 BUY/SELL（CLOB 只认识这两个）
        buy_or_sell = "SELL" if side == "SELL" else "BUY"

        # DRY_RUN：只打印日志，不真正发单，安全测试用
        if self.dry_run or not self._client:
            self.logger.info(
                "[DRY_RUN] FOK %s token=%s price=%.4f amount=$%.2f USDC",
                buy_or_sell, token_id, price, size_usdc,
            )
            return {
                "status": "simulated",
                "token_id": token_id,
                "side": buy_or_sell,
                "price": price,
                "size_usdc": size_usdc,
            }

        try:
            from py_clob_client.clob_types import (
                OrderType,
                MarketOrderArgs,
                PartialCreateOrderOptions,
            )

            # options 必须是 PartialCreateOrderOptions 对象（dataclass），不是 dict
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

            # 市价单（FOK）：立即吃对手盘，全部成交或全部取消
            # price 作为最差价保护（滑点上限），amount 直接传 USDC 金额
            order_args = MarketOrderArgs(
                token_id=token_id,
                side=self._resolve_side_const(buy_or_sell),
                amount=size_usdc,
                price=price,
            )
            market_order = self._client.create_market_order(order_args, options=options)
            result = self._client.post_order(market_order, OrderType.FOK)
            # 兼容 result 是对象或 dict 的两种情况
            order_id = getattr(result, "orderID", None) or (result.get("orderID") if isinstance(result, dict) else None)
            self.logger.info("Order submitted: id=%s side=%s price=%.4f", order_id, buy_or_sell, price)
            return {"status": "submitted", "order_id": order_id, "raw": str(result)}
        except Exception as exc:
            self.logger.exception("Order placement failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def place_market_order_v2(
        self,
        token_id: str,
        side: str,                  # "BUY" | "SELL"
        amount_usdc: float,         # BUY: 花的 USDC；SELL: 卖出的 shares
        price: float,               # 最差价保护（滑点上限）
        tick_size: str = "0.001",   # 市场最小价格单位
        neg_risk: bool = False,     # 多结果 negRisk 市场必须为 True
    ) -> dict[str, Any]:
        """
        按 Polymarket 官方 Python SDK 文档 1:1 写的市价单（FOK）下单方法。
        独立实现，保留给需要直接调用的场景（不经过旧的 place_limit_order）。

        参考：https://docs.polymarket.com/trading/orders/create
        """
        buy_or_sell = "SELL" if side.upper() == "SELL" else "BUY"
        if self.dry_run or not self._client:
            self.logger.info(
                "[DRY_RUN v2] FOK %s token=%s price=%.4f amount=%.4f",
                buy_or_sell, token_id, price, amount_usdc,
            )
            return {"status": "simulated", "token_id": token_id, "side": buy_or_sell}

        try:
            from py_clob_client.clob_types import (
                OrderType, MarketOrderArgs, PartialCreateOrderOptions,
            )
            side_const = self._resolve_side_const(buy_or_sell)
            order_args = MarketOrderArgs(
                token_id=token_id, amount=amount_usdc, side=side_const, price=price,
            )
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            signed = self._client.create_market_order(order_args, options=options)
            result = self._client.post_order(signed, OrderType.FOK)
            order_id = getattr(result, "orderID", None) or (
                result.get("orderID") if isinstance(result, dict) else None
            )
            self.logger.info(
                "v2 Order submitted: id=%s side=%s price=%.4f", order_id, buy_or_sell, price,
            )
            return {"status": "submitted", "order_id": order_id, "raw": str(result)}
        except Exception as exc:
            self.logger.exception("v2 Order placement failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    def close_position(
        self,
        token_id: str,
        price: float,      # 当前市场价格，用于计算平仓 contracts 数量
        size_usdc: float,  # 持仓的原始 USDC 成本（用于反算 contracts）
    ) -> dict[str, Any]:
        """
        平仓：以限价 SELL 卖出当前持有的 Yes 仓位。
        止盈或止损时调用此方法，direction 固定为 SELL。
        """
        return self.place_limit_order(
            token_id=token_id,
            side="SELL",
            price=price,
            size_usdc=size_usdc,
        )
