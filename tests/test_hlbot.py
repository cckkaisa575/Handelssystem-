import copy

import numpy as np
import pandas as pd
import pytest

from hlbot.backtest import run_backtest
from hlbot.bot import TradingBot
from hlbot.broker import PaperBroker, round_price, round_size
from hlbot.config import Config, load_config
from hlbot.indicators import donchian
from hlbot.risk import RiskGuard, position_size
from hlbot.strategy import Position, compute_features, entry_signal, stop_hit, update_trailing

H = 3_600_000


def synthetic(n=3000, seed=0, start_px=100.0, drift_regimes=True):
    """Kurse mit abwechselnden Trend- und Seitwärtsphasen (nur für Funktionstests, keine Performance-Aussage)."""
    rng = np.random.default_rng(seed)
    drift = np.zeros(n)
    if drift_regimes:
        for i in range(0, n, 300):
            drift[i:i + 300] = rng.choice([0.002, -0.002, 0.0])
    rets = drift + rng.normal(0, 0.008, n)
    close = start_px * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_px], close[:-1]])
    wiggle = np.abs(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    t0 = 1_700_000_000_000 // H * H
    return pd.DataFrame({"time": t0 + np.arange(n) * H, "open": open_, "high": high, "low": low,
                         "close": close, "volume": 1.0})


def test_donchian_has_no_lookahead():
    df = synthetic(100)
    up, lo = donchian(df, 10)
    assert up.iloc[20] == df["high"].iloc[10:20].max()
    assert lo.iloc[20] == df["low"].iloc[10:20].min()


def test_position_size_respects_risk_and_caps():
    cfg = Config()
    r = cfg.risk
    qty = position_size(100, 100, 98, 0, 0, r)            # 2 USDC Risiko / 2 Abstand = 1 Stück = 100 Nominal
    assert qty == pytest.approx(1.0)
    qty = position_size(100, 100, 99.9, 0, 0, r)          # sehr enger Stop -> Hebelgrenze 2x
    assert qty * 100 == pytest.approx(200)
    assert position_size(100, 100, 99.9, 390, 1, r) == 0  # Rest-Spielraum 10 < Mindestgröße
    assert position_size(100, 100, 98, 0, r.max_positions, r) == 0


def test_rounding():
    assert round_size(0.123456, 3) == 0.123
    assert round_price(65432.123, 5) == 65432.0
    assert round_price(1.234567, 1) == 1.2346
    assert round_price(123456.7, 5) == 123457.0


def test_trailing_only_tightens_and_stop_hit():
    cfg = Config()
    pos = Position("X", "long", 1, 100, 95, 100)
    update_trailing(pos, {"high": 110, "low": 105, "atr": 1.0}, cfg.strategy)
    assert pos.stop == pytest.approx(110 - 3.5)
    update_trailing(pos, {"high": 101, "low": 90, "atr": 5.0}, cfg.strategy)
    assert pos.stop == pytest.approx(106.5)
    assert stop_hit(pos, {"open": 107, "high": 108, "low": 106, "close": 107}) == 106.5
    assert stop_hit(pos, {"open": 100, "high": 101, "low": 99, "close": 100}) == 100  # Lücke


def test_backtest_runs_and_accounts_costs():
    cfg = Config()
    candles = {"AAA": synthetic(seed=1), "BBB": synthetic(seed=2, start_px=5.0)}
    res = run_backtest(candles, cfg)
    s = res.stats()
    assert s["trades"] > 0
    assert res.fees_paid > 0
    assert res.equity.iloc[-1] == pytest.approx(cfg.start_capital + sum(t.pnl for t in res.trades), rel=1e-6)

    free = copy.deepcopy(cfg)
    free.costs.taker_fee = free.costs.slippage = free.costs.funding_per_hour = 0
    assert run_backtest(candles, free).stats()["end_equity"] > s["end_equity"]


def test_backtest_no_trades_in_random_walk_without_signal_edge_is_bounded():
    cfg = Config()
    res = run_backtest({"AAA": synthetic(seed=3, drift_regimes=False)}, cfg)
    # Keine Performance-Aussage – nur: Risikolimits halten (kein Totalverlust in einem Schritt)
    assert res.equity.min() > 0


def test_risk_guard_halts_on_drawdown():
    g = RiskGuard(Config().risk)
    g.update(100, 0)
    g.update(64, H)
    assert g.state.halted
    assert not g.can_open(64)


def test_config_example_loads(tmp_path):
    cfg = load_config("config.example.toml")
    assert cfg.mode == "paper" and cfg.start_capital == 100


class FakeFeed:
    def __init__(self, df):
        self.df = df

    def __call__(self, coin, now_ms):
        return self.df[self.df["time"] < now_ms]


def test_bot_paper_end_to_end(tmp_path):
    cfg = Config(coins=["AAA"], state_dir=str(tmp_path))
    df = synthetic(2000, seed=5)
    feed = FakeFeed(df)
    now = {"t": 0}
    mids = lambda: {"AAA": float(df[df["time"] < now["t"]]["close"].iloc[-1])}  # noqa: E731
    broker = PaperBroker(cfg, tmp_path / "paper.json", mid_source=mids, decimals={"AAA": 2})
    bot = TradingBot(cfg, broker, fetcher=feed)

    feats = compute_features(df, cfg.strategy)
    entries = 0
    for i in range(400, 2000):
        now["t"] = int(df["time"].iloc[i]) + H + 5000      # kurz nach Schluss von Kerze i
        had = "AAA" in bot.positions
        bot.step(now["t"])
        if "AAA" in bot.positions:
            pos = bot.positions["AAA"]
            stops = broker.stop_orders("AAA")
            assert len(stops) == 1, "jede Position braucht genau einen Stop"
            assert stops[0].is_buy == (pos.side == "short")
            if not had:
                entries += 1
                assert entry_signal(feats.iloc[i], cfg.strategy) == pos.side
        else:
            assert broker.stop_orders("AAA") == []
    assert entries > 0
    assert broker.equity() > 0

    # Neustart übernimmt den gespeicherten Zustand
    bot2 = TradingBot(cfg, PaperBroker(cfg, tmp_path / "paper.json", mid_source=mids, decimals={"AAA": 2}), fetcher=feed)
    assert bot2.positions.keys() == bot.positions.keys()
