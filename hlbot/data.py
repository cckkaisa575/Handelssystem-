"""Kerzendaten von Hyperliquid laden (öffentliche Info-API, kein Schlüssel nötig) und cachen."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as e:
        raise ValueError(f"Nicht unterstütztes Intervall: {interval}") from e


def _post_info(base_url: str, payload: dict, retries: int = 4) -> list | dict:
    delay = 1.0
    for attempt in range(retries):
        try:
            resp = requests.post(f"{base_url}/info", json=payload, timeout=20)
            if resp.status_code == 429:
                raise requests.HTTPError("rate limited")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def candles_to_frame(raw: list[dict]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(
        {
            "time": [int(c["t"]) for c in raw],
            "open": [float(c["o"]) for c in raw],
            "high": [float(c["h"]) for c in raw],
            "low": [float(c["l"]) for c in raw],
            "close": [float(c["c"]) for c in raw],
            "volume": [float(c["v"]) for c in raw],
        }
    )
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def fetch_candles(base_url: str, coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Lädt Kerzen im Zeitraum, in Blöcken (Hyperliquid liefert max. ~5000 pro Anfrage)."""
    step = interval_ms(interval)
    frames = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + step * 4999)
        raw = _post_info(
            base_url,
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": chunk_end}},
        )
        df = candles_to_frame(raw)
        if not df.empty:
            frames.append(df)
            cursor = int(df["time"].iloc[-1]) + step
        else:
            cursor = chunk_end + step
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)


def closed_candles(df: pd.DataFrame, interval: str, now_ms: int) -> pd.DataFrame:
    """Entfernt die noch laufende (nicht geschlossene) Kerze."""
    if df.empty:
        return df
    return df[df["time"] + interval_ms(interval) <= now_ms].reset_index(drop=True)


def load_history(
    base_url: str, coin: str, interval: str, days: int, cache_dir: str | Path = "data", refresh: bool = False
) -> pd.DataFrame:
    """Historische Kerzen mit CSV-Cache laden (für Backtests)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{coin}_{interval}.csv"
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    cached = pd.read_csv(path) if path.exists() and not refresh else pd.DataFrame(columns=COLUMNS)
    if not cached.empty and int(cached["time"].iloc[0]) <= start:
        fetch_from = int(cached["time"].iloc[-1]) + interval_ms(interval)
    else:
        cached = pd.DataFrame(columns=COLUMNS)
        fetch_from = start
    new = fetch_candles(base_url, coin, interval, fetch_from, now) if fetch_from < now else pd.DataFrame(columns=COLUMNS)
    df = pd.concat([cached, new]) if not new.empty else cached
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    df = closed_candles(df, interval, now)
    df.to_csv(path, index=False)
    return df[df["time"] >= start].reset_index(drop=True)


def load_csv(path: str | Path) -> pd.DataFrame:
    """Eigene CSV mit Spalten time(ms),open,high,low,close[,volume] laden."""
    df = pd.read_csv(path)
    missing = {"time", "open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"CSV {path} fehlen Spalten: {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df[COLUMNS].sort_values("time").reset_index(drop=True)
