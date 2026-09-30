"""Trendfolge-Strategie: Donchian-Ausbruch + EMA-Trendfilter + ADX + ATR-Stops.

Die Logik ist bewusst als reine Funktionen geschrieben, damit Backtest, Paper-
und Live-Handel exakt dieselben Entscheidungen treffen.

Regeln (ausgewertet auf jeder GESCHLOSSENEN Kerze):
  Long-Einstieg : close > höchstes Hoch der letzten N Kerzen, close > EMA(trend), ADX >= adx_min
  Short-Einstieg: close < tiefstes Tief der letzten N Kerzen, close < EMA(trend), ADX >= adx_min
  Anfangs-Stop  : Einstieg -/+ stop_atr_mult * ATR
  Trailing-Stop : Chandelier – bestes Hoch/Tief seit Einstieg -/+ trail_atr_mult * ATR (zieht nur nach)
  Ausstieg      : Stop getroffen ODER Schlusskurs kreuzt die Trend-EMA gegen die Position
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

from .config import StrategyParams
from .indicators import adx, atr, donchian, ema


def compute_features(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["ema_trend"] = ema(out["close"], p.trend_ema)
    out["atr"] = atr(out, p.atr_len)
    out["adx"] = adx(out, p.adx_len)
    out["dc_high"], out["dc_low"] = donchian(out, p.breakout_len)
    return out


def warmup_bars(p: StrategyParams) -> int:
    return max(p.trend_ema, p.breakout_len + 1, p.atr_len * 3, p.adx_len * 3) + 1


@dataclass
class Position:
    coin: str
    side: str            # "long" oder "short"
    qty: float           # immer positiv
    entry_price: float
    stop: float
    extreme: float       # höchstes Hoch (long) / tiefstes Tief (short) seit Einstieg
    entry_time: int = 0  # ms

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Position":
        return Position(**d)


def _valid(row: pd.Series, cols: tuple[str, ...]) -> bool:
    return all(pd.notna(row.get(c)) for c in cols)


def entry_signal(row: pd.Series, p: StrategyParams) -> Optional[str]:
    if not _valid(row, ("ema_trend", "atr", "adx", "dc_high", "dc_low")) or row["atr"] <= 0:
        return None
    if row["adx"] < p.adx_min:
        return None
    close = row["close"]
    if close > row["dc_high"] and close > row["ema_trend"]:
        return "long"
    if p.allow_shorts and close < row["dc_low"] and close < row["ema_trend"]:
        return "short"
    return None


def initial_stop(side: str, price: float, atr_value: float, p: StrategyParams) -> float:
    d = p.stop_atr_mult * atr_value
    return price - d if side == "long" else price + d


def update_trailing(pos: Position, row: pd.Series, p: StrategyParams) -> Position:
    """Extremwert und Chandelier-Stop nach Schluss einer Kerze nachziehen (nie lockern)."""
    if not _valid(row, ("atr",)):
        return pos
    if pos.side == "long":
        pos.extreme = max(pos.extreme, row["high"])
        pos.stop = max(pos.stop, pos.extreme - p.trail_atr_mult * row["atr"])
    else:
        pos.extreme = min(pos.extreme, row["low"])
        pos.stop = min(pos.stop, pos.extreme + p.trail_atr_mult * row["atr"])
    return pos


def exit_signal(pos: Position, row: pd.Series) -> bool:
    """Trendbruch: Schlusskurs auf der falschen Seite der Trend-EMA."""
    if not _valid(row, ("ema_trend",)):
        return False
    if pos.side == "long":
        return row["close"] < row["ema_trend"]
    return row["close"] > row["ema_trend"]


def stop_hit(pos: Position, bar: pd.Series) -> Optional[float]:
    """Prüft, ob der Stop innerhalb einer Kerze ausgelöst wurde; gibt den Ausführungskurs (vor Slippage) zurück.

    Bei einer Kurslücke über den Stop hinweg wird zum Eröffnungskurs ausgeführt.
    """
    if pos.side == "long" and bar["low"] <= pos.stop:
        return min(bar["open"], pos.stop)
    if pos.side == "short" and bar["high"] >= pos.stop:
        return max(bar["open"], pos.stop)
    return None
