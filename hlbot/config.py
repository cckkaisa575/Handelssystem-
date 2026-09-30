"""Konfiguration laden (TOML-Datei + Umgebungsvariablen für Schlüssel)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class StrategyParams:
    trend_ema: int = 200
    breakout_len: int = 48
    atr_len: int = 14
    adx_len: int = 14
    adx_min: float = 20.0
    stop_atr_mult: float = 2.5
    trail_atr_mult: float = 3.5
    allow_shorts: bool = True


@dataclass
class RiskParams:
    risk_per_trade: float = 0.02
    max_positions: int = 3
    max_leverage_per_position: float = 2.0
    max_total_leverage: float = 4.0
    exchange_leverage: int = 5
    min_order_notional: float = 11.0
    daily_loss_limit: float = 0.08
    max_drawdown: float = 0.35


@dataclass
class CostParams:
    taker_fee: float = 0.00045
    slippage: float = 0.0005
    funding_per_hour: float = 0.0000125


@dataclass
class Config:
    mode: str = "paper"
    start_capital: float = 100.0
    state_dir: str = "state"
    log_dir: str = "logs"
    coins: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL", "HYPE"])
    interval: str = "1h"
    history_bars: int = 600
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    costs: CostParams = field(default_factory=CostParams)
    account_address: str = ""
    secret_key: str = ""

    @property
    def base_url(self) -> str:
        from hyperliquid.utils import constants

        return constants.TESTNET_API_URL if self.mode == "testnet" else constants.MAINNET_API_URL


def _fill(dc, values: dict):
    known = {f.name for f in fields(dc)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unbekannte Konfigurationsschlüssel: {sorted(unknown)}")
    for k, v in values.items():
        setattr(dc, k, v)
    return dc


def load_env_file(path: str | Path = ".env") -> None:
    """Minimaler .env-Parser (KEY=VALUE), überschreibt keine gesetzten Variablen."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path | None = "config.toml") -> Config:
    cfg = Config()
    if path and Path(path).exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        _fill(cfg, raw.get("general", {}))
        _fill(cfg, raw.get("market", {}))
        _fill(cfg.strategy, raw.get("strategy", {}))
        _fill(cfg.risk, raw.get("risk", {}))
        _fill(cfg.costs, raw.get("costs", {}))
    load_env_file()
    cfg.account_address = os.environ.get("HL_ACCOUNT_ADDRESS", "")
    cfg.secret_key = os.environ.get("HL_SECRET_KEY", "")
    if cfg.mode not in ("paper", "testnet", "live"):
        raise ValueError(f"mode muss paper, testnet oder live sein, nicht {cfg.mode!r}")
    return cfg
