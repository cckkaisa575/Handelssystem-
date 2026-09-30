"""Kommandozeile:  python -m hlbot <befehl>

  backtest    Strategie auf historischen Hyperliquid-Daten testen
  sweep       Parameter-Robustheit prüfen (Training vs. unberührter Testzeitraum)
  run         Bot starten (Modus aus config.toml: paper / testnet / live)
  status      Konto, Positionen und Risiko-Zustand anzeigen
  reset-halt  Not-Aus nach Max-Drawdown manuell zurücksetzen
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .config import Config, load_config


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(Path(cfg.log_dir) / f"{cfg.mode}.log")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", handlers=handlers)


def _load_candles(cfg: Config, args) -> dict[str, pd.DataFrame]:
    from .data import load_csv, load_history

    coins = args.coins.split(",") if args.coins else cfg.coins
    out = {}
    for coin in coins:
        if args.csv_dir:
            out[coin] = load_csv(Path(args.csv_dir) / f"{coin}_{cfg.interval}.csv")
        else:
            print(f"Lade {coin} {cfg.interval} ({args.days} Tage) ...", flush=True)
            out[coin] = load_history(cfg.base_url.replace("-testnet", ""), coin, cfg.interval, args.days,
                                     refresh=args.refresh)
        print(f"  {coin}: {len(out[coin])} Kerzen")
    return out


def cmd_backtest(cfg: Config, args) -> None:
    from .backtest import run_backtest

    candles = _load_candles(cfg, args)
    res = run_backtest(candles, cfg)
    stats = res.stats(cfg.interval)
    print("\n=== Backtest-Ergebnis ===")
    for k, v in stats.items():
        print(f"  {k:24s} {v}")
    print("\nMonatsrenditen:")
    for ts, v in res.monthly_returns().items():
        print(f"  {ts:%Y-%m}  {v * 100:+7.2f} %")
    by_coin = pd.DataFrame([t.__dict__ for t in res.trades])
    if not by_coin.empty:
        print("\nGewinn je Coin (USDC):")
        print(by_coin.groupby("coin")["pnl"].agg(["count", "sum"]).round(2).to_string())
        Path("logs").mkdir(exist_ok=True)
        by_coin.to_csv("logs/backtest_trades.csv", index=False)
        res.equity.to_csv("logs/backtest_equity.csv", header=["equity"])
        print("\nTrades: logs/backtest_trades.csv   Equity-Kurve: logs/backtest_equity.csv")


def cmd_sweep(cfg: Config, args) -> None:
    from .backtest import parameter_sweep

    candles = _load_candles(cfg, args)
    grid = {
        "breakout_len": [24, 48, 96],
        "stop_atr_mult": [2.0, 2.5, 3.0],
        "trail_atr_mult": [3.0, 3.5, 4.5],
        "adx_min": [15.0, 20.0, 25.0],
    }
    df = parameter_sweep(candles, cfg, grid)
    pd.set_option("display.width", 200)
    print(df.head(25).to_string())
    df.to_csv("logs/sweep.csv", index=False)
    print("\nAlle Ergebnisse: logs/sweep.csv. Aussagekräftig sind nur die test_*-Spalten.")
    print("Wähle Parameter, die in einer ganzen REGION des Gitters gut sind – nicht den einen Spitzenwert.")


def _make_broker(cfg: Config):
    from .broker import HyperliquidBroker, PaperBroker

    if cfg.mode == "paper":
        return PaperBroker(cfg, Path(cfg.state_dir) / "paper_account.json")
    return HyperliquidBroker(cfg)


def cmd_run(cfg: Config, args) -> None:
    from .bot import TradingBot

    if cfg.mode == "live" and not args.yes:
        print("ACHTUNG: Modus 'live' handelt mit echtem Geld. Verluste bis zum Totalverlust sind möglich.")
        if input("Zum Fortfahren 'JA' eingeben: ").strip() != "JA":
            print("Abgebrochen.")
            return
    bot = TradingBot(cfg, _make_broker(cfg))
    if args.once:
        bot.step()
    else:
        bot.run_forever()


def cmd_status(cfg: Config, args) -> None:
    broker = _make_broker(cfg)
    print(f"Modus: {cfg.mode}")
    print(f"Equity: {broker.equity():.2f} USDC")
    for coin, p in broker.positions().items():
        print(f"  {coin}: Größe {p.size:+.6g} @ {p.entry_price:.6g}, Stops: "
              f"{[s.trigger_px for s in broker.stop_orders(coin)]}")
    state = Path(cfg.state_dir) / f"{cfg.mode}_bot_state.json"
    if state.exists():
        print("Risiko-Zustand:", json.dumps(json.loads(state.read_text()).get("risk", {}), indent=2))


def cmd_reset_halt(cfg: Config, args) -> None:
    state = Path(cfg.state_dir) / f"{cfg.mode}_bot_state.json"
    if not state.exists():
        print("Kein Zustand vorhanden.")
        return
    raw = json.loads(state.read_text())
    raw.setdefault("risk", {}).update({"halted": False, "halt_reason": "", "peak_equity": 0.0})
    state.write_text(json.dumps(raw, indent=2))
    print("Not-Aus zurückgesetzt (Höchststand wird neu gemessen).")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="hlbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backtest", "sweep"):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=365)
        sp.add_argument("--coins", help="z. B. BTC,ETH,SOL (Standard: aus config)")
        sp.add_argument("--csv-dir", help="eigene CSVs statt Download: <dir>/<COIN>_<interval>.csv")
        sp.add_argument("--refresh", action="store_true", help="Cache ignorieren und neu laden")
    rp = sub.add_parser("run")
    rp.add_argument("--once", action="store_true", help="nur einen Durchlauf ausführen")
    rp.add_argument("--yes", action="store_true", help="Live-Sicherheitsabfrage überspringen (für Server/Docker)")
    sub.add_parser("status")
    sub.add_parser("reset-halt")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)
    {"backtest": cmd_backtest, "sweep": cmd_sweep, "run": cmd_run, "status": cmd_status,
     "reset-halt": cmd_reset_halt}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
