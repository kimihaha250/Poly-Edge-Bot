from __future__ import annotations

# 多线程并发调用多个 LLM，对同一个市场事件分别打概率分，最终加权平均
# 这是整个策略的"AI大脑"模块，对应 PDF 里的步骤2：让3个AI做ensemble分析
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
from typing import Any

import requests

from bot.data_sources import MarketContext


@dataclass
class ModelOutput:
    """单个 LLM 模型的输出结果"""
    model: str          # 模型名称，如 "openai/gpt-4o-mini"
    probability: float  # 0–100 的概率（Yes 发生的百分比）
    reason: str         # 模型给出的简短理由
    is_fallback: bool = False  # True = 调用失败，回退到 50%，不可用于下单


def _extract_probability(text: str) -> float:
    """
    从 LLM 的原始回复文本里提取概率数字。
    模型可能回复 "68 | 因为新闻显示支持率上升" 或 "0.68"，都能处理。
    """
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        # 完全无法解析时返回 50（中性，不下注）
        return 50.0
    value = float(match.group(1))
    # 如果模型返回的是 0–1 小数（如 0.68），转成百分比
    if value <= 1:
        value = value * 100.0
    # 强制钳位到 [0, 100]，防止异常值影响加权
    return max(0.0, min(100.0, value))


def _call_openrouter(base_url: str, api_key: str, model: str, prompt: str) -> str:
    """
    通过 OpenRouter 统一 API 调用任意 LLM。
    OpenRouter 是一个 API 聚合平台，用同一个 key 可以调 GPT / Claude / Gemini 等。
    temperature=0.2 让输出更稳定，减少随机波动。
    trust_env=False：禁止 requests 读取系统代理，避免被随机端口劫持。

    重试策略：OpenRouter 对并发请求限速，会返回 403（权限/限速）或 429（频率超限）。
    遇到这两个状态码时最多重试 3 次，指数退避（2s → 4s → 8s），通常第一次重试就能成功。
    """
    import time as _time

    if not api_key:
        return "50 neutral fallback (missing OPENROUTER_API_KEY)"
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a probability forecaster."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    last_exc: Exception | None = None
    for attempt in range(4):   # 最多尝试 4 次（1次正常 + 3次重试）
        try:
            with requests.Session() as s:
                s.trust_env = False
                response = s.post(url, json=payload, headers=headers, timeout=35)
            if response.status_code in (403, 429):
                # 限速或瞬间并发超限，等待后重试
                wait = 2 ** attempt          # 1s, 2s, 4s, 8s
                last_exc = Exception(
                    f"{response.status_code} {response.reason} (attempt {attempt + 1}/4, retry in {wait}s)"
                )
                _time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as exc:
            # 非限速类 HTTP 错误（如 400 Bad Request）直接抛出，不重试
            raise exc
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                _time.sleep(2 ** attempt)
            continue

    raise last_exc or RuntimeError("_call_openrouter: all retries exhausted")


def _single_model_forecast(
    base_url: str,
    api_key: str,
    model: str,
    question: str,
    context: MarketContext,  # 包含新闻摘要、社交情绪、历史胜率三类数据
) -> ModelOutput:
    """
    让单个 LLM 对一个市场事件打分。
    Prompt 结构严格按照 PDF 里的示例模板：事件 + 新闻 + 社交情绪 + 历史胜率。
    出错时降级返回 50（不影响整体 ensemble）。
    """
    prompt = (
        f"Event: {question}\n"
        f"News summary: {context.news_summary}\n"
        f"Social sentiment score: {context.social_sentiment_score:.2f}\n"
        f"Historical similar-event win rate: {context.historical_win_rate * 100:.1f}%\n\n"
        "Return probability of YES in 0-100 and one short reason. "
        "Format: <number> | <reason>"
    )
    try:
        raw = _call_openrouter(base_url, api_key, model, prompt)
        probability = _extract_probability(raw)
        reason = raw.strip()[:240]  # 截断超长回复，只保留前240字符用于日志
        return ModelOutput(model=model, probability=probability, reason=reason)
    except Exception as exc:
        # 单个模型失败不影响其他模型，返回中性 50 并标记 is_fallback=True
        return ModelOutput(model=model, probability=50.0, reason=f"fallback due to error: {exc}", is_fallback=True)


def ensemble_probability(
    base_url: str,
    api_key: str,
    question: str,
    context: MarketContext,
    models_with_weights: list[tuple[str, float]],  # [(模型名, 权重), ...]
) -> tuple[float, list[ModelOutput]]:
    """
    核心 Ensemble 函数：并发调用所有模型，按权重加权平均得到最终 p_AI。

    返回：
        (加权平均概率 0–100, 每个模型的详细输出列表)

    并发逻辑：所有模型同时发请求（ThreadPoolExecutor），
    哪个先回来就先收集，最后统一加权，比串行快 3x。
    """
    outputs: list[ModelOutput] = []

    # 并发向所有 LLM 发请求，max_workers 等于模型数量
    with ThreadPoolExecutor(max_workers=len(models_with_weights)) as executor:
        futures = [
            executor.submit(_single_model_forecast, base_url, api_key, model, question, context)
            for model, _ in models_with_weights
        ]
        # as_completed：哪个 future 先完成就先处理，不按顺序等待
        for future in as_completed(futures):
            outputs.append(future.result())

    # 按模型名快速查权重
    weight_map = {model: weight for model, weight in models_with_weights}

    # 加权平均：p_AI = Σ(概率_i × 权重_i) / Σ权重_i
    weighted_sum = 0.0
    total_weight = 0.0
    for out in outputs:
        w = weight_map.get(out.model, 0.0)
        weighted_sum += out.probability * w
        total_weight += w

    if total_weight <= 0:
        # 所有模型都失败了，返回中性 50，不下注
        return 50.0, outputs

    return weighted_sum / total_weight, outputs
