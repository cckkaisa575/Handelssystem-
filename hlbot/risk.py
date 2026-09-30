"""Positionsgrößen und Konto-Schutzmechanismen."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .config import RiskParams


def position_size(
    equity: float,
    price: float,
    stop: float,
    open_notional: float,
    open_positions: int,
    r: RiskParams,
) -> float:
    """Menge so wählen, dass ein Stop-Treffer ca. `risk_per_trade` des Kontos kostet.

    Begrenzt durch Hebel je Position, Gesamthebel und Anzahl Positionen.
    Gibt 0 zurück, wenn der Trade das Börsen-Minimum nicht erreicht.
    """
    if equity <= 0 or price <= 0 or open_positions >= r.max_positions:
        return 0.0
    stop_dist = abs(price - stop)
    if stop_dist <= 0:
        return 0.0
    qty = equity * r.risk_per_trade / stop_dist
    notional = qty * price
    notional = min(
        notional,
        equity * r.max_leverage_per_position,
        max(0.0, equity * r.max_total_leverage - open_notional),
    )
    if notional < r.min_order_notional:
        return 0.0
    return notional / price


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    halted: bool = False          # Not-Aus (Max-Drawdown), bleibt bis zum manuellen Reset
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RiskState":
        return RiskState(**d)


class RiskGuard:
    def __init__(self, r: RiskParams, state: RiskState | None = None):
        self.r = r
        self.state = state or RiskState()

    def update(self, equity: float, now_ms: int) -> None:
        s = self.state
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if s.day != day:
            s.day = day
            s.day_start_equity = equity
        s.peak_equity = max(s.peak_equity, equity)
        if s.peak_equity > 0 and equity < s.peak_equity * (1 - self.r.max_drawdown) and not s.halted:
            s.halted = True
            s.halt_reason = (
                f"Max-Drawdown überschritten: Equity {equity:.2f} < {1 - self.r.max_drawdown:.0%} "
                f"vom Höchststand {s.peak_equity:.2f}"
            )

    def daily_limit_hit(self, equity: float) -> bool:
        s = self.state
        return s.day_start_equity > 0 and equity < s.day_start_equity * (1 - self.r.daily_loss_limit)

    def can_open(self, equity: float) -> bool:
        return not self.state.halted and not self.daily_limit_hit(equity)
