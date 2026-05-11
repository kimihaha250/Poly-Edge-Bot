#!/usr/bin/env python3
"""
Backtest: Probability-Arbitrage Strategy
=========================================
Fetches real historical prices for a *resolved* Polymarket market, then
runs a parameter sweep over (edge_threshold × kelly_fraction) to find the
combination that would have produced the best risk-adjusted return.

Usage
-----
    python backtest.py --slug <market-slug> [options]

Required
    --slug          Market slug from Polymarket (e.g. "will-trump-be-indicted-by-june")

Optional
    --oracle-accuracy  Float 0-1 (default 0.70). Simulates how much better your AI
                       is vs the market.  0.5 = no edge, 1.0 = perfect oracle.
    --capital          Starting capital in USDC (default 100)
    --max-order        Max single order in USDC (default 20)
    --take-profit      Take-profit delta (default 0.10)
    --stop-loss        Stop-loss multiplier (default 0.85)
    --out              Optional CSV path to save full sweep results
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from typing import Any

import requests


# ──────────────────────────────────────────────────────────────────────────────
# Data fetching
# ──────────────────────────────────────────────────────────────────────────────

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


def fetch_market_info(slug: str) -> dict[str, Any]:
    resp = requests.get(f"{GAMMA_URL}/markets", params={"slug": slug, "limit": 1}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        sys.exit(f"ERROR: No market found for slug '{slug}'")
    return data[0]


def _extract_yes_token_id(market: dict[str, Any]) -> str:
    for key in ("token_id", "yesTokenId", "clobTokenId"):
        if market.get(key):
            return str(market[key])
    tokens = market.get("tokens")
    if isinstance(tokens, list) and tokens:
        for t in tokens:
            if str(t.get("outcome", "")).lower() == "yes" and t.get("token_id"):
                return str(t["token_id"])
        if tokens[0].get("token_id"):
            return str(tokens[0]["token_id"])
    return ""


def fetch_price_history(token_id: str, fidelity: int = 60) -> list[dict[str, Any]]:
    """Pull price history from CLOB timeseries endpoint.

    fidelity=60 → 1-hour candles.  Reduce (e.g. 1) for minute-level data.
    """
    try:
        resp = requests.get(
            f"{CLOB_URL}/prices-history",
            params={"market": token_id, "fidelity": fidelity},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"WARN: CLOB price history unavailable ({exc}), trying Gamma fallback.")
        return []

    if isinstance(data, dict):
        return data.get("history", data.get("prices", []))
    if isinstance(data, list):
        return data
    return []


def parse_true_outcome(market: dict[str, Any]) -> float | None:
    """Return 1.0 for YES resolution, 0.0 for NO, None if unknown."""
    for key in ("outcome", "resolution", "result"):
        raw = str(market.get(key, "")).lower().strip()
        if raw in {"yes", "1", "true"}:
            return 1.0
        if raw in {"no", "0", "false"}:
            return 0.0
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Strategy helpers (mirror bot/strategy.py to avoid circular deps)
# ──────────────────────────────────────────────────────────────────────────────

def _kelly_fraction(p: float, price: float) -> float:
    p = max(0.0001, min(0.9999, p))
    q = 1 - p
    b = max(0.0001, (1 / max(price, 0.0001)) - 1)
    return max(0.0, (b * p - q) / b)


def _simulate_p_ai(p_market: float, true_outcome: float, oracle_accuracy: float) -> float:
    """Blend oracle knowledge with market noise to simulate an AI with partial edge."""
    blended = oracle_accuracy * true_outcome + (1 - oracle_accuracy) * p_market
    return max(0.01, min(0.99, blended))


# ──────────────────────────────────────────────────────────────────────────────
# Backtest engine
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    edge_threshold: float
    kelly_fraction: float
    total_pnl_usdc: float
    num_trades: int
    win_rate: float
    max_drawdown_pct: float
    sharpe: float
    final_portfolio: float
    ticks: int = 0


def run_backtest(
    price_history: list[dict[str, Any]],
    true_outcome: float,
    oracle_accuracy: float,
    edge_threshold: float,
    kelly_fraction: float,
    initial_capital: float = 100.0,
    max_order_usdc: float = 20.0,
    take_profit_delta: float = 0.10,
    stop_loss_multiplier: float = 0.85,
    max_open_positions: int = 5,
) -> BacktestResult:
    portfolio = initial_capital
    portfolio_curve: list[float] = [initial_capital]

    @dataclass
    class Position:
        entry_price: float
        cost_usdc: float
        size_contracts: float
        side: str  # "YES" or "NO"

    open_positions: list[Position] = []
    trades: list[float] = []  # realised PnL per trade

    for tick in price_history:
        raw = tick.get("p") or tick.get("price") or tick.get("y")
        if raw is None:
            continue
        p_market = float(raw)
        if p_market > 1:
            p_market /= 100.0
        p_market = max(0.01, min(0.99, p_market))

        p_ai = _simulate_p_ai(p_market, true_outcome, oracle_accuracy)
        edge = p_ai - p_market

        # ---- Risk management for existing positions ----
        surviving: list[Position] = []
        for pos in open_positions:
            price_for_side = p_market if pos.side == "YES" else (1 - p_market)
            price_delta = price_for_side - pos.entry_price
            take_profit_hit = price_delta > take_profit_delta and abs(edge) < 0.08
            stop_loss_hit = price_for_side < pos.entry_price * stop_loss_multiplier

            if take_profit_hit or stop_loss_hit:
                pnl = price_delta * pos.size_contracts
                portfolio += pos.cost_usdc + pnl
                trades.append(pnl)
            else:
                surviving.append(pos)
        open_positions = surviving

        # ---- Entry ----
        if abs(edge) >= edge_threshold and len(open_positions) < max_open_positions:
            entry_side = "YES" if edge > 0 else "NO"
            p_for_side = p_ai if edge > 0 else (1 - p_ai)
            market_p_for_side = p_market if edge > 0 else (1 - p_market)
            raw_k = _kelly_fraction(p_for_side, market_p_for_side)
            frac = min(1.0, raw_k * kelly_fraction)
            cost = min(max_order_usdc, portfolio * frac)
            if cost > 0.5 and portfolio >= cost:
                portfolio -= cost
                open_positions.append(Position(
                    entry_price=market_p_for_side,
                    cost_usdc=cost,
                    size_contracts=cost / max(market_p_for_side, 0.01),
                    side=entry_side,
                ))

        portfolio_curve.append(portfolio)

    # ---- Resolve remaining positions at true outcome ----
    for pos in open_positions:
        exit_price = true_outcome if pos.side == "YES" else (1 - true_outcome)
        pnl = (exit_price - pos.entry_price) * pos.size_contracts
        portfolio += pos.cost_usdc + pnl
        trades.append(pnl)
    portfolio_curve.append(portfolio)

    # ---- Metrics ----
    num_trades = len(trades)
    win_rate = sum(1 for t in trades if t > 0) / max(1, num_trades)
    total_pnl = portfolio - initial_capital

    peak = initial_capital
    max_dd = 0.0
    for val in portfolio_curve:
        peak = max(peak, val)
        dd = (peak - val) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    returns = [
        portfolio_curve[i] / portfolio_curve[i - 1] - 1
        for i in range(1, len(portfolio_curve))
        if portfolio_curve[i - 1] > 0
    ]
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
        sharpe = (mean_r / std_r * math.sqrt(8760)) if std_r > 1e-9 else 0.0
    else:
        sharpe = 0.0

    return BacktestResult(
        edge_threshold=edge_threshold,
        kelly_fraction=kelly_fraction,
        total_pnl_usdc=round(total_pnl, 2),
        num_trades=num_trades,
        win_rate=round(win_rate, 3),
        max_drawdown_pct=round(max_dd * 100, 2),
        sharpe=round(sharpe, 3),
        final_portfolio=round(portfolio, 2),
        ticks=len(price_history),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Parameter sweep
# ──────────────────────────────────────────────────────────────────────────────

EDGE_THRESHOLDS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]
KELLY_FRACTIONS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]


def sweep(
    price_history: list[dict[str, Any]],
    true_outcome: float,
    oracle_accuracy: float,
    initial_capital: float,
    max_order_usdc: float,
    take_profit_delta: float,
    stop_loss_multiplier: float,
) -> list[BacktestResult]:
    results: list[BacktestResult] = []
    for et in EDGE_THRESHOLDS:
        for kf in KELLY_FRACTIONS:
            r = run_backtest(
                price_history=price_history,
                true_outcome=true_outcome,
                oracle_accuracy=oracle_accuracy,
                edge_threshold=et,
                kelly_fraction=kf,
                initial_capital=initial_capital,
                max_order_usdc=max_order_usdc,
                take_profit_delta=take_profit_delta,
                stop_loss_multiplier=stop_loss_multiplier,
            )
            results.append(r)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 12) -> str:
    if max_val <= 0:
        return ""
    filled = int(round(value / max_val * width))
    return "█" * filled + "░" * (width - filled)


def print_results_table(results: list[BacktestResult], top_n: int = 10) -> None:
    by_sharpe = sorted(results, key=lambda r: r.sharpe, reverse=True)
    top = by_sharpe[:top_n]
    max_pnl = max(abs(r.total_pnl_usdc) for r in top) or 1.0

    header = (
        f"{'edge_thr':>9} {'kelly':>7} {'PnL($)':>8} {'trades':>7} "
        f"{'win%':>6} {'maxDD%':>7} {'sharpe':>7}  {'PnL bar':<14}"
    )
    sep = "─" * len(header)
    print(f"\n{'Top results (sorted by Sharpe)':^{len(header)}}")
    print(sep)
    print(header)
    print(sep)

    for r in top:
        bar = _bar(abs(r.total_pnl_usdc), max_pnl)
        prefix = "+" if r.total_pnl_usdc >= 0 else " "
        print(
            f"{r.edge_threshold:>9.2f} {r.kelly_fraction:>7.2f} "
            f"{prefix}{r.total_pnl_usdc:>7.2f} {r.num_trades:>7d} "
            f"{r.win_rate*100:>5.1f}% {r.max_drawdown_pct:>6.1f}% "
            f"{r.sharpe:>7.3f}  {bar}"
        )
    print(sep)
    best = top[0]
    print(
        f"\n★ Best params: edge_threshold={best.edge_threshold:.2f}  "
        f"kelly_fraction={best.kelly_fraction:.2f}  "
        f"(PnL=${best.total_pnl_usdc:+.2f}, Sharpe={best.sharpe:.3f})\n"
    )


def save_csv(results: list[BacktestResult], path: str) -> None:
    fields = [
        "edge_threshold", "kelly_fraction", "total_pnl_usdc", "num_trades",
        "win_rate", "max_drawdown_pct", "sharpe", "final_portfolio", "ticks",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow({f: getattr(r, f) for f in fields})
    print(f"Full sweep saved to {path}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest the probability-arbitrage strategy on a resolved Polymarket market."
    )
    parser.add_argument("--slug", required=True, help="Polymarket market slug")
    parser.add_argument("--oracle-accuracy", type=float, default=0.70,
                        help="Simulated AI accuracy vs market (0.5–1.0, default 0.70)")
    parser.add_argument("--capital", type=float, default=100.0, help="Starting USDC capital")
    parser.add_argument("--max-order", type=float, default=20.0, help="Max single order USDC")
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--stop-loss", type=float, default=0.85)
    parser.add_argument("--fidelity", type=int, default=60, help="Price history fidelity in minutes")
    parser.add_argument("--out", default="", help="Optional CSV path for full sweep results")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    print(f"\nFetching market info for slug: {args.slug}")
    market = fetch_market_info(args.slug)
    question = market.get("question") or market.get("title") or args.slug
    token_id = _extract_yes_token_id(market)
    true_outcome = parse_true_outcome(market)

    print(f"  Question : {question}")
    print(f"  Token ID : {token_id}")
    print(f"  Outcome  : {true_outcome}")

    if true_outcome is None:
        sys.exit("ERROR: Market is not yet resolved or outcome is unknown. Backtest requires a resolved market.")

    if not token_id:
        sys.exit("ERROR: Could not find YES token_id for this market.")

    print(f"\nFetching price history (fidelity={args.fidelity}min)…")
    history = fetch_price_history(token_id, fidelity=args.fidelity)

    if not history:
        print("WARN: No price history returned from CLOB. Generating synthetic price path from resolution.")
        import random
        random.seed(42)
        steps = 100
        history = []
        p = 0.5
        for _ in range(steps):
            p = max(0.01, min(0.99, p + random.gauss(0, 0.03)))
            history.append({"p": p})
        print(f"  Using {steps}-tick synthetic history.")
    else:
        print(f"  {len(history)} price ticks loaded.")

    print(f"\nRunning parameter sweep "
          f"({len(EDGE_THRESHOLDS)}×{len(KELLY_FRACTIONS)}={len(EDGE_THRESHOLDS)*len(KELLY_FRACTIONS)} combos)…")

    results = sweep(
        price_history=history,
        true_outcome=true_outcome,
        oracle_accuracy=args.oracle_accuracy,
        initial_capital=args.capital,
        max_order_usdc=args.max_order,
        take_profit_delta=args.take_profit,
        stop_loss_multiplier=args.stop_loss,
    )

    print_results_table(results, top_n=10)

    if args.out:
        save_csv(results, args.out)


if __name__ == "__main__":
    main()
