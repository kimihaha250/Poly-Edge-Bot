from __future__ import annotations

import logging
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP 后端选择
# ---------------------------------------------------------------------------
# Polymarket 的 Gamma API 部署在 Cloudflare 后面，Cloudflare Bot Management
# 会检测 TLS ClientHello 指纹——Python requests 的 ssl 指纹与浏览器差异明显，
# 会被直接 Reset（即 ConnectionResetError 54）。
#
# curl_cffi 使用 libcurl + BoringSSL，能完整模拟 Chrome 的 TLS 握手指纹，
# 可以绕过这种检测。优先使用；安装失败时降级到标准 requests（仅供参考）。
# ---------------------------------------------------------------------------

try:
    from curl_cffi import requests as _cffi_requests
    _USE_CFFI = True
    logger.debug("GammaClient: using curl_cffi (Chrome TLS impersonation)")
except ImportError:
    import requests as _std_requests  # type: ignore[no-redef]
    _USE_CFFI = False
    logger.warning(
        "curl_cffi not installed — falling back to requests. "
        "Install with: pip install curl-cffi  (needed to bypass Cloudflare)"
    )


def _get_url(url: str, params: dict[str, Any], timeout: int) -> Any:
    """
    发一个 GET 请求并返回解析好的 JSON。
    curl_cffi 模式下 impersonate='chrome' 让 TLS 指纹与 Chrome 124 一致。
    """
    if _USE_CFFI:
        resp = _cffi_requests.get(
            url,
            params=params,
            timeout=timeout,
            impersonate="chrome",   # 模拟 Chrome TLS 指纹，关键参数
        )
    else:
        # 降级模式：关闭自动代理嗅探，避免被系统中残留的无效代理端口干扰
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })
        resp = s.get(url, params=params, timeout=(10, timeout))

    resp.raise_for_status()
    return resp.json()


class GammaClient:
    def __init__(
        self,
        timeout: int = 20,
        proxies: dict[str, str] | None = None,
        relay_url: str = "",
    ) -> None:
        self._timeout = timeout
        self._proxies = proxies or {}
        # relay_url：Cloudflare Worker 中继地址，留空则直连 Gamma API
        # 设置后所有请求路径变为 relay_url + path，绕过 Cloudflare Bot 封锁
        self._base_url = relay_url.rstrip("/") if relay_url else GAMMA_BASE_URL
        if relay_url:
            logger.info("GammaClient: using relay %s", relay_url)
        elif proxies:
            logger.info("GammaClient: proxy configured %s", proxies)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        reraise=True,
    )
    def _get_with_retry(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        if _USE_CFFI and self._proxies:
            # curl_cffi 用 proxy= 参数（单个字符串，取 https 优先）
            proxy = self._proxies.get("https") or self._proxies.get("http")
            resp = _cffi_requests.get(
                url,
                params=params,
                timeout=self._timeout,
                impersonate="chrome",
                proxy=proxy,
            )
            resp.raise_for_status()
            return resp.json()
        return _get_url(url, params, self._timeout)

    def _safe_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """网络全部失败时只打 warning，主循环不崩溃"""
        try:
            return self._get_with_retry(path, params or {})
        except Exception as exc:
            logger.warning("GammaClient: request failed for %s — %s", path, exc)
            return None

    def list_active_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        data = self._safe_get("/markets", {"active": "true", "limit": limit})
        return data if isinstance(data, list) else []

    def get_market_by_slug(self, slug: str) -> dict[str, Any] | None:
        data = self._safe_get("/markets", {"slug": slug, "limit": 1})
        if isinstance(data, list) and data:
            return data[0]
        return None

    def get_resolved_markets(self, limit: int = 200) -> list[dict[str, Any]]:
        data = self._safe_get("/markets", {"closed": "true", "limit": limit})
        return data if isinstance(data, list) else []

    def list_markets_by_tag(self, tag: str, limit: int = 100) -> list[dict[str, Any]]:
        """
        通过话题 tag 获取该话题下的所有活跃市场。

        Polymarket 话题页 URL 形如：
            https://polymarket.com/iran/trump-iran  → tag = "trump-iran"
            https://polymarket.com/politics         → tag = "politics"

        实现策略（双重保险）：
        1. 优先查询 /events 端点，返回 Event 对象，每个 Event 里包含 markets 列表
        2. 如果 /events 没数据，降级用 /markets?tag=... 直接查
        """
        # 尝试 /events 端点（slug 对应话题子分类）
        events = self._safe_get("/events", {
            "slug": tag,
            "active": "true",
            "limit": 50,
        })
        if isinstance(events, list) and events:
            # 从每个 Event 里提取 markets 字段
            markets: list[dict[str, Any]] = []
            for event in events:
                event_markets = event.get("markets", [])
                if isinstance(event_markets, list):
                    markets.extend(event_markets)
            if markets:
                logger.info("GammaClient: tag=%s via /events → %d markets", tag, len(markets))
                return markets[:limit]

        # 降级：直接用 tag 参数查 /markets
        data = self._safe_get("/markets", {
            "tag": tag,
            "active": "true",
            "limit": limit,
        })
        result = data if isinstance(data, list) else []
        logger.info("GammaClient: tag=%s via /markets?tag → %d markets", tag, len(result))
        return result
