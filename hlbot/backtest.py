"""Portfolio-Backtest über mehrere Coins mit Gebühren, Slippage und Funding.

Ablauf je Kerze t (entspricht dem Live-Bot):
  1. Offene Einstiegs-/Ausstiegsaufträge aus Kerze t-1 werden zum Eröffnungskurs von t ausgeführt.
  2. Stops werden gegen Hoch/Tief von t geprüft (bei Lücken: Ausführung zum Eröffnungskurs).
  3. Funding wird belastet.
  4. Nach Schluss von t: Trailing-Stop nachziehen, Ausstiegs- und Einstiegssignale -> Ausführung in t+1.
"""
from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .data import interval_ms
from .risk import RiskGuard, position_size
from .strategy import (
    Position,
    compute_features,
    entry_signal,
    exit_signal,
    initial_stop,
    stop_hit,
    update_trailing,
    warmup_bars,
)


@dataclass
class Trade:
    coin: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float        # netto nach Gebühren und Funding
    fees: float
    reason: str


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade] = field(default_factory=list)
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    halted_at: int | None = None

    def monthly_returns(self) -> pd.Series:
        eq = self.equity.copy()
        eq.index = pd.to_datetime(eq.index, unit="ms", utc=True)
        month_end = eq.resample("ME").last()
        prev = month_end.shift(1)
        prev.iloc[0] = eq.iloc[0]
        return (month_end / prev - 1).dropna()

    def stats(self, interval: str = "1h") -> dict:
        eq = self.equity
        if eq.empty:
            return {}
        start, end = float(eq.iloc[0]), float(eq.iloc[-1])
        years = max((eq.index[-1] - eq.index[0]) / (365.25 * 86_400_000), 1e-9)
        rets = eq.pct_change().dropna()
        bars_per_year = 365.25 * 86_400_000 / interval_ms(interval)
        sharpe = float(rets.mean() / rets.std() * math.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
        dd = (eq / eq.cummax() - 1).min()
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        monthly = self.monthly_returns()
        months = max(years * 12, 1e-9)
        return {
            "start_equity": round(start, 2),
            "end_equity": round(end, 2),
            "total_return_pct": round((end / start - 1) * 100, 2),
            "avg_monthly_return_pct": round(((end / start) ** (1 / months) - 1) * 100, 2) if end > 0 else -100.0,
            "median_month_pct": round(float(monthly.median()) * 100, 2) if len(monthly) else 0.0,
            "worst_month_pct": round(float(monthly.min()) * 100, 2) if len(monthly) else 0.0,
            "best_month_pct": round(float(monthly.max()) * 100, 2) if len(monthly) else 0.0,
            "positive_months": f"{int((monthly > 0).sum())}/{len(monthly)}",
            "max_drawdown_pct": round(float(dd) * 100, 2),
            "sharpe": round(sharpe, 2),
            "trades": len(self.trades),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 1) if self.trades else 0.0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else float("inf"),
            "fees_paid": round(self.fees_paid, 2),
            "funding_paid": round(self.funding_paid, 2),
            "halted": self.halted_at is not None,
        }


def run_backtest(
    candles: dict[str, pd.DataFrame],
    cfg: Config,
    start_time: int | None = None,
    end_time: int | None = None,
) -> BacktestResult:
    p, r, c = cfg.strategy, cfg.risk, cfg.costs
    bar_hours = interval_ms(cfg.interval) / 3_600_000
    warm = warmup_bars(p)

    rows: dict[str, dict[int, dict]] = {}
    for coin, df in candles.items():
        feats = compute_features(df, p)
        feats["bar_no"] = np.arange(len(feats))
        rows[coin] = {int(rec["time"]): rec for rec in feats.to_dict("records")}

    times = sorted(set().union(*[set(v) for v in rows.values()])) if rows else []
    if start_time is not None:
        times = [t for t in times if t >= start_time]
    if end_time is not None:
        times = [t for t in times if t < end_time]

    cash = cfg.start_capital
    positions: dict[str, Position] = {}
    pending_entry: dict[str, tuple[str, float]] = {}   # coin -> (side, atr)
    pending_exit: set[str] = set()
    last_close: dict[str, float] = {}
    guard = RiskGuard(r)
    res = BacktestResult(equity=pd.Series(dtype=float))
    fees_by_pos: dict[str, float] = {}
    eq_times, eq_vals = [], []

    def equity() -> float:
        return cash + sum(pos.sign * pos.qty * (last_close[k] - pos.entry_price) for k, pos in positions.items())

    def close_position(coin: str, raw_price: float, t: int, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(coin)
        px = raw_price * (1 - c.slippage * pos.sign)
        fee = pos.qty * px * c.taker_fee
        gross = pos.sign * pos.qty * (px - pos.entry_price)
        cash += gross - fee
        res.fees_paid += fee
        total_fees = fees_by_pos.pop(coin, 0.0) + fee
        res.trades.append(
            Trade(coin, pos.side, pos.entry_time, t, pos.entry_price, px, pos.qty,
                  gross - total_fees, total_fees, reason)
        )

    for t in times:
        # 1. ausstehende Aufträge zum Eröffnungskurs
        for coin in list(pending_exit):
            bar = rows[coin].get(t)
            if bar is not None and coin in positions:
                close_position(coin, bar["open"], t, "signal")
            pending_exit.discard(coin)
        for coin, (side, atr_val) in list(pending_entry.items()):
            bar = rows[coin].get(t)
            pending_entry.pop(coin)
            if bar is None or coin in positions:
                continue
            sign = 1 if side == "long" else -1
            px = bar["open"] * (1 + c.slippage * sign)
            stop = initial_stop(side, px, atr_val, p)
            open_notional = sum(pp.qty * last_close[k] for k, pp in positions.items())
            qty = position_size(equity(), px, stop, open_notional, len(positions), r)
            if qty <= 0:
                continue
            fee = qty * px * c.taker_fee
            cash -= fee
            res.fees_paid += fee
            fees_by_pos[coin] = fee
            positions[coin] = Position(coin, side, qty, px, stop, px, t)

        # 2. Stops innerhalb der Kerze
        for coin in list(positions):
            bar = rows[coin].get(t)
            if bar is None:
                continue
            hit = stop_hit(positions[coin], bar)
            if hit is not None:
                close_position(coin, hit, t, "stop")

        # 3. Funding + Schlusskurse
        for coin, rec in rows.items():
            if t in rec:
                last_close[coin] = rec[t]["close"]
        for coin, pos in positions.items():
            if t in rows[coin]:
                f = pos.qty * last_close[coin] * c.funding_per_hour * bar_hours
                cash -= f
                res.funding_paid += f
                fees_by_pos[coin] = fees_by_pos.get(coin, 0.0) + f

        eq = equity()
        guard.update(eq, t)
        eq_times.append(t)
        eq_vals.append(eq)
        if guard.state.halted and res.halted_at is None:
            res.halted_at = t

        # 4. Signale nach Kerzenschluss
        for coin, pos in positions.items():
            bar = rows[coin].get(t)
            if bar is None:
                continue
            update_trailing(pos, bar, p)
            if exit_signal(pos, bar):
                pending_exit.add(coin)
        if guard.can_open(eq):
            slots = r.max_positions - len(positions) + len(pending_exit)
            for coin, rec in rows.items():
                if slots <= 0:
                    break
                bar = rec.get(t)
                if bar is None or coin in positions or bar["bar_no"] < warm:
                    continue
                side = entry_signal(bar, p)
                if side:
                    pending_entry[coin] = (side, bar["atr"])
                    slots -= 1

    # offene Positionen am Ende zum letzten Schlusskurs schließen
    if times:
        for coin in list(positions):
            close_position(coin, last_close[coin], times[-1], "end")
        if eq_vals:
            eq_vals[-1] = cash
    res.equity = pd.Series(eq_vals, index=eq_times, dtype=float)
    return res


def parameter_sweep(
    candles: dict[str, pd.DataFrame],
    cfg: Config,
    grid: dict[str, list],
    train_frac: float = 0.7,
) -> pd.DataFrame:
    """Parameter auf dem Trainingszeitraum testen und auf dem unberührten Testzeitraum prüfen.

    Nur die Test-Spalten (test_*) sagen etwas über die Zukunft – Trainingswerte sind überoptimistisch.
    """
    all_times = sorted(set().union(*[set(df["time"].astype(int)) for df in candles.values()]))
    split = all_times[int(len(all_times) * train_frac)]
    keys = list(grid)
    out = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        c2 = copy.deepcopy(cfg)
        for k, v in zip(keys, combo):
            setattr(c2.strategy, k, v)
        train = run_backtest(candles, c2, end_time=split).stats(cfg.interval)
        test = run_backtest(candles, c2, start_time=split).stats(cfg.interval)
        row = dict(zip(keys, combo))
        for prefix, s in (("train", train), ("test", test)):
            for m in ("avg_monthly_return_pct", "max_drawdown_pct", "sharpe", "trades"):
                row[f"{prefix}_{m}"] = s.get(m)
        out.append(row)
    return pd.DataFrame(out).sort_values("train_sharpe", ascending=False).reset_index(drop=True)
