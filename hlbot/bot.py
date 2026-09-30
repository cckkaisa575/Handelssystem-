"""Der vollautomatische Handels-Loop."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from .broker import Broker
from .config import Config
from .data import closed_candles, fetch_candles, interval_ms
from .risk import RiskGuard, RiskState, position_size
from .strategy import (
    Position,
    compute_features,
    entry_signal,
    exit_signal,
    initial_stop,
    update_trailing,
    warmup_bars,
)

log = logging.getLogger("hlbot.bot")

CandleFetcher = Callable[[str, int], pd.DataFrame]


class TradingBot:
    def __init__(self, cfg: Config, broker: Broker, fetcher: CandleFetcher | None = None):
        self.cfg = cfg
        self.broker = broker
        self.fetcher = fetcher or self._fetch
        self.state_path = Path(cfg.state_dir) / f"{cfg.mode}_bot_state.json"
        self.positions: dict[str, Position] = {}
        self.stop_oids: dict[str, int] = {}
        self.last_signal_bar: dict[str, int] = {}
        self.leverage_set: set[str] = set()
        risk_state = None
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            self.positions = {c: Position.from_dict(p) for c, p in raw.get("positions", {}).items()}
            self.stop_oids = {c: int(o) for c, o in raw.get("stop_oids", {}).items()}
            self.last_signal_bar = raw.get("last_signal_bar", {})
            risk_state = RiskState.from_dict(raw["risk"]) if "risk" in raw else None
        self.guard = RiskGuard(cfg.risk, risk_state)
        need = warmup_bars(cfg.strategy) + 50
        if cfg.history_bars < need:
            log.warning("history_bars=%d zu klein, verwende %d", cfg.history_bars, need)
            cfg.history_bars = need

    # --- Persistenz ---
    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "positions": {c: p.to_dict() for c, p in self.positions.items()},
            "stop_oids": self.stop_oids,
            "last_signal_bar": self.last_signal_bar,
            "risk": self.guard.state.to_dict(),
        }, indent=2))
        tmp.replace(self.state_path)

    def _fetch(self, coin: str, now_ms: int) -> pd.DataFrame:
        step = interval_ms(self.cfg.interval)
        return fetch_candles(self.cfg.base_url, coin, self.cfg.interval, now_ms - step * self.cfg.history_bars, now_ms)

    # --- Hilfen ---
    def _cancel_stops(self, coin: str) -> None:
        for so in self.broker.stop_orders(coin):
            self.broker.cancel(coin, so.oid)
        self.stop_oids.pop(coin, None)

    def _close(self, coin: str, reason: str) -> None:
        pos = self.positions.get(coin)
        ex = self.broker.positions().get(coin)
        if ex is not None:
            filled, px = self.broker.market_order(coin, ex.size < 0, abs(ex.size), reduce_only=True)
            log.info("SCHLIESSEN %s %s: %.6g @ %.6g (%s)", coin, pos.side if pos else "?", filled, px, reason)
        self._cancel_stops(coin)
        self.positions.pop(coin, None)

    def _ensure_stop(self, coin: str, pos: Position) -> None:
        """Genau eine Stop-Order passend zu Menge und Stop-Kurs auf der Börse halten."""
        want_px = self.broker.round_price(coin, pos.stop)
        want_sz = self.broker.round_size(coin, pos.qty)
        existing = self.broker.stop_orders(coin)
        ok = [s for s in existing if abs(s.trigger_px - want_px) <= want_px * 1e-6 and abs(s.size - want_sz) <= want_sz * 1e-6
              and s.is_buy == (pos.side == "short")]
        if len(ok) == 1 and len(existing) == 1:
            self.stop_oids[coin] = ok[0].oid
            return
        # erst neuen Stop setzen, dann alte löschen -> Position ist nie ungeschützt
        oid = self.broker.place_stop(coin, pos.side == "short", pos.qty, pos.stop)
        for s in existing:
            self.broker.cancel(coin, s.oid)
        if oid:
            self.stop_oids[coin] = oid
        log.info("Stop %s %s gesetzt auf %.6g", coin, pos.side, want_px)

    # --- ein Durchlauf pro geschlossener Kerze ---
    def step(self, now_ms: int | None = None) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        p, r = self.cfg.strategy, self.cfg.risk
        step_ms = interval_ms(self.cfg.interval)

        candles: dict[str, pd.DataFrame] = {}
        for coin in self.cfg.coins:
            try:
                candles[coin] = closed_candles(self.fetcher(coin, now_ms), self.cfg.interval, now_ms)
            except Exception as e:  # noqa: BLE001
                log.error("Kerzen für %s konnten nicht geladen werden: %s", coin, e)
        self.broker.process_closed_bars(candles)
        if hasattr(self.broker, "refresh_mids"):
            self.broker.refresh_mids()

        feats = {c: compute_features(df, p) for c, df in candles.items() if len(df) >= warmup_bars(p)}

        # 1. Abgleich lokaler Zustand <-> Börse
        ex_positions = self.broker.positions()
        for coin in list(self.positions):
            if coin not in ex_positions:
                log.info("Position %s ist geschlossen (Stop ausgelöst oder manuell).", coin)
                self._cancel_stops(coin)
                self.positions.pop(coin)
        for coin, ex in ex_positions.items():
            side = "long" if ex.size > 0 else "short"
            pos = self.positions.get(coin)
            if pos is None or pos.side != side:
                if coin not in feats:
                    log.warning("Fremde Position %s ohne Kursdaten – wird nicht verwaltet.", coin)
                    continue
                atr_val = float(feats[coin]["atr"].iloc[-1])
                pos = Position(coin, side, abs(ex.size), ex.entry_price,
                               initial_stop(side, ex.entry_price, atr_val, p), ex.entry_price, now_ms)
                log.warning("Übernehme bestehende Position %s %s %.6g @ %.6g", coin, side, pos.qty, pos.entry_price)
                self.positions[coin] = pos
            pos.qty = abs(ex.size)

        equity = self.broker.equity()
        self.guard.update(equity, now_ms)
        log.info("Equity %.2f USDC | Positionen: %s", equity,
                 ", ".join(f"{c} {q.side} stop {q.stop:.6g}" for c, q in self.positions.items()) or "keine")

        if self.guard.state.halted:
            log.critical("NOT-AUS aktiv: %s – alle Positionen werden geschlossen, kein Handel.", self.guard.state.halt_reason)
            for coin in list(self.positions):
                self._close(coin, "Not-Aus")
            self.save()
            return

        # 2. Offene Positionen verwalten
        for coin, pos in list(self.positions.items()):
            if coin not in feats:
                self._ensure_stop(coin, pos)
                continue
            bar = feats[coin].iloc[-1]
            if exit_signal(pos, bar):
                self._close(coin, "Trendbruch")
                continue
            update_trailing(pos, bar, p)
            mid = self.broker.mid(coin)
            if (pos.side == "long" and mid <= pos.stop) or (pos.side == "short" and mid >= pos.stop):
                self._close(coin, "Kurs bereits jenseits des Stops")
                continue
            self._ensure_stop(coin, pos)

        # 3. Neue Einstiege
        if not self.guard.can_open(equity):
            log.warning("Tagesverlust-Limit erreicht – keine neuen Positionen bis morgen (UTC).")
            self.save()
            return
        for coin, df in feats.items():
            if coin in self.positions or len(self.positions) >= r.max_positions:
                continue
            bar = df.iloc[-1]
            bar_time = int(bar["time"])
            if bar_time + 2 * step_ms < now_ms:           # Daten veraltet
                continue
            if self.last_signal_bar.get(coin) == bar_time:  # Signal dieser Kerze schon verarbeitet
                continue
            side = entry_signal(bar, p)
            if not side:
                continue
            self.last_signal_bar[coin] = bar_time
            atr_val = float(bar["atr"])
            price = self.broker.mid(coin)
            stop = initial_stop(side, price, atr_val, p)
            open_notional = sum(q.qty * self.broker.mid(c) for c, q in self.positions.items())
            qty = self.broker.round_size(coin, position_size(equity, price, stop, open_notional, len(self.positions), r))
            if qty <= 0 or qty * price < r.min_order_notional:
                log.info("Signal %s %s übersprungen (Größe zu klein / Limits).", coin, side)
                continue
            if coin not in self.leverage_set:
                self.broker.set_leverage(coin, r.exchange_leverage)
                self.leverage_set.add(coin)
            try:
                filled, avg_px = self.broker.market_order(coin, side == "long", qty)
            except Exception as e:  # noqa: BLE001
                log.error("Einstieg %s fehlgeschlagen: %s", coin, e)
                continue
            if filled <= 0:
                log.warning("Einstieg %s nicht ausgeführt.", coin)
                continue
            pos = Position(coin, side, filled, avg_px, initial_stop(side, avg_px, atr_val, p), avg_px, now_ms)
            self.positions[coin] = pos
            log.info("EINSTIEG %s %s %.6g @ %.6g, Stop %.6g, Nominal %.2f USDC",
                     coin, side, filled, avg_px, pos.stop, filled * avg_px)
            try:
                self._ensure_stop(coin, pos)
            except Exception as e:  # noqa: BLE001
                log.error("Stop für %s konnte nicht gesetzt werden (%s) – Position wird sofort geschlossen!", coin, e)
                self._close(coin, "kein Stop möglich")
        self.save()

    def run_forever(self, delay_s: float = 8.0) -> None:
        step_ms = interval_ms(self.cfg.interval)
        log.info("Bot gestartet im Modus %s, Coins %s, Intervall %s", self.cfg.mode, self.cfg.coins, self.cfg.interval)
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Durchlauf – neuer Versuch in 60 Sekunden.")
                time.sleep(60)
                continue
            now = time.time() * 1000
            next_close = (now // step_ms + 1) * step_ms
            time.sleep(max(1.0, (next_close - now) / 1000 + delay_s))
