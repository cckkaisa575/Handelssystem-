"""Börsen-Anbindung: Hyperliquid (Testnet/Mainnet) und ein Paper-Broker mit Live-Kursen."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import Config

log = logging.getLogger("hlbot.broker")


@dataclass
class ExPosition:
    coin: str
    size: float          # vorzeichenbehaftet: + long, - short
    entry_price: float


@dataclass
class StopOrder:
    oid: int
    coin: str
    is_buy: bool
    size: float
    trigger_px: float


def round_size(sz: float, sz_decimals: int) -> float:
    """Abrunden auf die erlaubten Nachkommastellen der Menge."""
    f = 10 ** sz_decimals
    return math.floor(sz * f + 1e-9) / f


def round_price(px: float, sz_decimals: int) -> float:
    """Hyperliquid-Perp-Preise: max. 5 signifikante Stellen und max. (6 - szDecimals) Nachkommastellen."""
    if px <= 0:
        return px
    if px >= 100_000:
        return float(round(px))
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


class Broker:
    """Gemeinsame Schnittstelle für Paper- und Live-Broker."""

    def equity(self) -> float: ...
    def positions(self) -> dict[str, ExPosition]: ...
    def mid(self, coin: str) -> float: ...
    def sz_decimals(self, coin: str) -> int: ...
    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> tuple[float, float]: ...
    def place_stop(self, coin: str, is_buy: bool, size: float, trigger_px: float) -> Optional[int]: ...
    def cancel(self, coin: str, oid: int) -> None: ...
    def stop_orders(self, coin: str) -> list[StopOrder]: ...
    def set_leverage(self, coin: str, leverage: int) -> None: ...

    def process_closed_bars(self, candles: dict[str, pd.DataFrame]) -> None:
        """Nur Paper: Stops gegen geschlossene Kerzen prüfen. Live übernimmt das die Börse."""

    def round_size(self, coin: str, sz: float) -> float:
        return round_size(sz, self.sz_decimals(coin))

    def round_price(self, coin: str, px: float) -> float:
        return round_price(px, self.sz_decimals(coin))


class HyperliquidBroker(Broker):
    def __init__(self, cfg: Config):
        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        if not cfg.secret_key or not cfg.account_address:
            raise RuntimeError("HL_SECRET_KEY und HL_ACCOUNT_ADDRESS müssen gesetzt sein (siehe .env.example).")
        wallet = eth_account.Account.from_key(cfg.secret_key)
        self.address = cfg.account_address
        self.info = Info(cfg.base_url, skip_ws=True)
        self.exchange = Exchange(wallet, cfg.base_url, account_address=cfg.account_address)
        self.slippage = 0.01  # max. Abweichung für IOC-"Markt"-Orders
        self._decimals = {u["name"]: int(u["szDecimals"]) for u in self.info.meta()["universe"]}
        log.info("Verbunden mit %s, Konto %s (API-Wallet %s)", cfg.base_url, self.address, wallet.address)

    # --- Abfragen ---
    def _user_state(self) -> dict:
        return self.info.user_state(self.address)

    def equity(self) -> float:
        value = float(self._user_state()["marginSummary"]["accountValue"])
        if value < 1.0:
            try:
                spot = self.info.spot_user_state(self.address)
                usdc = sum(float(b["total"]) for b in spot.get("balances", []) if b["coin"] == "USDC")
                if usdc > 1.0:
                    log.warning(
                        "Perp-Konto fast leer, aber %.2f USDC im Spot-Konto. "
                        "Bitte in der Hyperliquid-Oberfläche USDC von Spot nach Perps übertragen.", usdc)
            except Exception:  # noqa: BLE001 – nur Hinweis
                pass
        return value

    def positions(self) -> dict[str, ExPosition]:
        out = {}
        for ap in self._user_state().get("assetPositions", []):
            p = ap["position"]
            size = float(p["szi"])
            if size != 0:
                out[p["coin"]] = ExPosition(p["coin"], size, float(p["entryPx"] or 0))
        return out

    def mid(self, coin: str) -> float:
        return float(self.info.all_mids()[coin])

    def sz_decimals(self, coin: str) -> int:
        return self._decimals[coin]

    def stop_orders(self, coin: str) -> list[StopOrder]:
        out = []
        for o in self.info.frontend_open_orders(self.address):
            if o["coin"] != coin or not o.get("isTrigger"):
                continue
            out.append(StopOrder(int(o["oid"]), coin, o["side"] == "B", float(o["sz"]), float(o["triggerPx"])))
        return out

    # --- Aktionen ---
    @staticmethod
    def _statuses(resp: dict) -> list[dict]:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"Order abgelehnt: {resp}")
        return resp["response"]["data"]["statuses"]

    def set_leverage(self, coin: str, leverage: int) -> None:
        resp = self.exchange.update_leverage(int(leverage), coin, is_cross=True)
        if resp.get("status") != "ok":
            log.warning("Hebel für %s konnte nicht gesetzt werden: %s", coin, resp)

    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> tuple[float, float]:
        size = self.round_size(coin, size)
        if size <= 0:
            return 0.0, 0.0
        px = self.round_price(coin, self.mid(coin) * (1 + self.slippage if is_buy else 1 - self.slippage))
        resp = self.exchange.order(coin, is_buy, size, px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only)
        st = self._statuses(resp)[0]
        if "filled" in st:
            return float(st["filled"]["totalSz"]), float(st["filled"]["avgPx"])
        if "error" in st:
            raise RuntimeError(f"Order-Fehler {coin}: {st['error']}")
        return 0.0, 0.0

    def place_stop(self, coin: str, is_buy: bool, size: float, trigger_px: float) -> Optional[int]:
        size = self.round_size(coin, size)
        trigger_px = self.round_price(coin, trigger_px)
        # Limit-Preis mit großzügigem Puffer, damit der Stop sicher ausgeführt wird
        limit_px = self.round_price(coin, trigger_px * (1.05 if is_buy else 0.95))
        order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}}
        resp = self.exchange.order(coin, is_buy, size, limit_px, order_type, reduce_only=True)
        st = self._statuses(resp)[0]
        if "error" in st:
            raise RuntimeError(f"Stop-Order-Fehler {coin}: {st['error']}")
        return int(st.get("resting", {}).get("oid", 0)) or None

    def cancel(self, coin: str, oid: int) -> None:
        resp = self.exchange.cancel(coin, oid)
        if resp.get("status") != "ok":
            log.warning("Stornierung %s/%s fehlgeschlagen: %s", coin, oid, resp)


class PaperBroker(Broker):
    """Simulation mit echten Hyperliquid-Kursen. Zustand wird in einer JSON-Datei gespeichert."""

    def __init__(self, cfg: Config, state_path: Path, mid_source=None, decimals: dict[str, int] | None = None):
        self.cfg = cfg
        self.path = state_path
        self.fee = cfg.costs.taker_fee
        self.slippage = cfg.costs.slippage
        self._mid_source = mid_source
        self._decimals = decimals
        self._mids_cache: dict[str, float] = {}
        if state_path.exists():
            self.s = json.loads(state_path.read_text())
        else:
            self.s = {"cash": cfg.start_capital, "positions": {}, "stops": {}, "next_oid": 1,
                      "last_bar": {}, "fees": 0.0, "fills": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.s, indent=2))

    def _load_meta(self) -> None:
        from .data import _post_info

        meta = _post_info(self.cfg.base_url, {"type": "meta"})
        self._decimals = {u["name"]: int(u["szDecimals"]) for u in meta["universe"]}

    def refresh_mids(self) -> None:
        if self._mid_source is not None:
            self._mids_cache = {k: float(v) for k, v in self._mid_source().items()}
            return
        from .data import _post_info

        self._mids_cache = {k: float(v) for k, v in _post_info(self.cfg.base_url, {"type": "allMids"}).items()}

    def sz_decimals(self, coin: str) -> int:
        if self._decimals is None:
            self._load_meta()
        return self._decimals.get(coin, 3)

    def mid(self, coin: str) -> float:
        if coin not in self._mids_cache:
            self.refresh_mids()
        return self._mids_cache[coin]

    def positions(self) -> dict[str, ExPosition]:
        return {c: ExPosition(c, p["size"], p["entry"]) for c, p in self.s["positions"].items()}

    def equity(self) -> float:
        upnl = sum(p["size"] * (self.mid(c) - p["entry"]) for c, p in self.s["positions"].items())
        return self.s["cash"] + upnl

    def _fill(self, coin: str, is_buy: bool, size: float, price: float, reason: str) -> None:
        pos = self.s["positions"].get(coin, {"size": 0.0, "entry": 0.0})
        signed = size if is_buy else -size
        fee = size * price * self.fee
        self.s["cash"] -= fee
        self.s["fees"] += fee
        old = pos["size"]
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):          # eröffnen / aufstocken
            pos["entry"] = (abs(old) * pos["entry"] + size * price) / abs(new)
        else:                                                # reduzieren / schließen
            closed = min(abs(old), size)
            self.s["cash"] += closed * (price - pos["entry"]) * (1 if old > 0 else -1)
            if abs(new) > 1e-12 and (new > 0) != (old > 0):  # gedreht
                pos["entry"] = price
        pos["size"] = round(new, 10)
        if abs(pos["size"]) < 1e-12:
            self.s["positions"].pop(coin, None)
            for oid in [o for o, st in self.s["stops"].items() if st["coin"] == coin]:
                self.s["stops"].pop(oid)
        else:
            self.s["positions"][coin] = pos
        self.s["fills"].append({"coin": coin, "buy": is_buy, "size": size, "price": price, "fee": fee, "reason": reason})
        self.s["fills"] = self.s["fills"][-500:]
        self._save()

    def market_order(self, coin: str, is_buy: bool, size: float, reduce_only: bool = False) -> tuple[float, float]:
        size = self.round_size(coin, size)
        if reduce_only:
            cur = self.s["positions"].get(coin, {"size": 0.0})["size"]
            if cur == 0 or (cur > 0) == is_buy:
                return 0.0, 0.0
            size = min(size, abs(cur))
        if size <= 0:
            return 0.0, 0.0
        px = self.mid(coin) * (1 + self.slippage if is_buy else 1 - self.slippage)
        self._fill(coin, is_buy, size, px, "market")
        return size, px

    def place_stop(self, coin: str, is_buy: bool, size: float, trigger_px: float) -> Optional[int]:
        oid = self.s["next_oid"]
        self.s["next_oid"] += 1
        self.s["stops"][str(oid)] = {"coin": coin, "is_buy": is_buy, "size": self.round_size(coin, size),
                                     "trigger": self.round_price(coin, trigger_px)}
        self._save()
        return oid

    def cancel(self, coin: str, oid: int) -> None:
        self.s["stops"].pop(str(oid), None)
        self._save()

    def stop_orders(self, coin: str) -> list[StopOrder]:
        return [StopOrder(int(o), st["coin"], st["is_buy"], st["size"], st["trigger"])
                for o, st in self.s["stops"].items() if st["coin"] == coin]

    def set_leverage(self, coin: str, leverage: int) -> None:
        pass

    def process_closed_bars(self, candles: dict[str, pd.DataFrame]) -> None:
        for coin, df in candles.items():
            if df.empty:
                continue
            last = self.s["last_bar"].get(coin)
            new_bars = df if last is None else df[df["time"] > last]
            if last is None:
                new_bars = df.iloc[0:0]  # beim ersten Start keine alten Kerzen nachspielen
            for _, bar in new_bars.iterrows():
                for oid, st in list(self.s["stops"].items()):
                    if st["coin"] != coin or coin not in self.s["positions"]:
                        continue
                    trig = st["trigger"]
                    if not st["is_buy"] and bar["low"] <= trig:      # Long-Stop (Verkauf)
                        px = min(bar["open"], trig) * (1 - self.slippage)
                    elif st["is_buy"] and bar["high"] >= trig:       # Short-Stop (Kauf)
                        px = max(bar["open"], trig) * (1 + self.slippage)
                    else:
                        continue
                    size = min(st["size"], abs(self.s["positions"][coin]["size"]))
                    self.s["stops"].pop(oid, None)
                    log.info("[PAPER] Stop ausgelöst %s @ %.6g", coin, px)
                    self._fill(coin, st["is_buy"], size, px, "stop")
            self.s["last_bar"][coin] = int(df["time"].iloc[-1])
        self._save()
