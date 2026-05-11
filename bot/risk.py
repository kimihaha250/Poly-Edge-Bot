from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionState:
    entry_price: float
    side: str
    size_usdc: float


def should_take_profit(
    current_market_price: float,
    entry_price: float,
    current_edge: float,
    take_profit_delta: float,
) -> bool:
    return (current_market_price - entry_price) > take_profit_delta and current_edge < 0.08


def should_stop_loss(
    current_market_price: float,
    entry_price: float,
    stop_loss_multiplier: float,
) -> bool:
    return current_market_price < (entry_price * stop_loss_multiplier)
