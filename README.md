# Hyperliquid-Trendfolge-Bot

Ein Bot, der auf der dezentralen Börse **Hyperliquid** vollautomatisch Krypto-Perpetuals handelt
(Standard: BTC, ETH, SOL, HYPE). Er ist für ein Startkapital von **100 USDC** ausgelegt.

## ⚠️ Bitte zuerst lesen: realistische Erwartungen

Das Ziel war **10–20 % pro Monat nach Gebühren**. Kein ehrliches System kann das zusagen:

* 10 %/Monat ergibt aufgezinst **~214 % im Jahr**, 20 %/Monat ergibt **~790 % im Jahr**. Das schafft auf Dauer
  kein bekannter Fonds und auch kein öffentlich bekannter Algorithmus. Wer solche Renditen verspricht, hat
  entweder zu stark auf die Vergangenheit optimiert oder geht ein Risiko ein, das früher oder später zum
  Totalverlust führt.
* Trendfolge verdient ihr Geld in **wenigen starken Monaten**. Dazwischen liegen oft **viele kleine Verlustmonate**.
  In einem starken Trendmonat sind +10–20 % möglich, als Durchschnitt über alle Monate ist das nicht realistisch.
* Mit den Standardeinstellungen (2 % Risiko je Trade, max. 4x Gesamthebel) sind **Rückgänge von 20–35 %**
  normal. Der Not-Aus greift bei −35 %.
* **Die Strategie ist noch nicht an echten Hyperliquid-Daten getestet.** In der Umgebung, in der der Code
  entstanden ist, war die Hyperliquid-API gesperrt. Der Code ist nur mit künstlichen Kursdaten auf Korrektheit
  geprüft. **Du musst den Backtest selbst ausführen**, bevor echtes Geld eingesetzt wird (Schritt 2).

Nur Geld einsetzen, dessen Verlust du verkraften kannst. Das hier ist keine Anlageberatung.

## Strategie in Kürze

Der Bot bewertet jede geschlossene Stundenkerze (`hlbot/strategy.py`):

| Baustein | Regel |
|---|---|
| Trendfilter | Long nur über der EMA(200), Short nur darunter |
| Einstieg | Ausbruch über das Hoch bzw. unter das Tief der letzten 48 Kerzen (Donchian) |
| Trendstärke | ADX(14) ≥ 20, damit Seitwärtsphasen gemieden werden |
| Anfangs-Stop | 2,5 × ATR vom Einstieg, **als Stop-Order direkt auf der Börse** |
| Trailing-Stop | Chandelier: 3,5 × ATR vom besten Kurs seit Einstieg, wird nur nachgezogen, nie gelockert |
| Ausstieg | Stop getroffen oder Schlusskurs auf der falschen Seite der EMA(200) |
| Positionsgröße | Ein Stop-Treffer kostet ≈ 2 % des Kontos; max. 2x Hebel je Position, 4x gesamt, 3 Positionen |
| Schutz | Tagesverlust −8 %: keine neuen Trades bis zum nächsten UTC-Tag. Drawdown −35 %: alles schließen, Bot stoppt |

Gebühren (0,045 % Taker), Slippage (0,05 %) und Funding (pessimistisch angesetzt) sind im Backtest enthalten.
Hyperliquid verlangt mindestens 10 USD je Order. Mit 100 USDC ist das ausreichend.

## Installation

```bash
git clone <dieses-repo> && cd Handelssystem-
python3 -m venv .venv && source .venv/bin/activate      # Python ≥ 3.11
pip install -r requirements.txt
cp config.example.toml config.toml
```

## Schritt 1 – Tests

```bash
python -m pytest -q
```

## Schritt 2 – Backtest mit echten Hyperliquid-Daten (Pflicht!)

```bash
python -m hlbot backtest --days 365      # lädt 1h-Kerzen von Hyperliquid, Cache in data/
python -m hlbot sweep --days 365         # Robustheit: 81 Parameter-Kombinationen, Training vs. Test
```

`backtest` zeigt Gesamt- und Monatsrenditen, Max-Drawdown, Sharpe, Trefferquote, Gebühren und den Gewinn je Coin.
Außerdem schreibt es `logs/backtest_trades.csv` und `logs/backtest_equity.csv`.

So liest du die Ergebnisse:
* **`test_*`-Spalten im Sweep sind die wichtigen**, denn diesen Zeitraum hat die Optimierung nie gesehen.
* Nimm Parameter, die in einer **ganzen Region** gut sind, nicht den einen Spitzenwert.
* Ist `test_avg_monthly_return_pct` negativ oder der Drawdown zu hoch, dann **nicht live gehen**.
* Hyperliquid liefert nur ca. 5000 Kerzen je Intervall. Für einen längeren Test kannst du eigene CSVs
  (`time,open,high,low,close,volume`, Zeit in ms) mit `--csv-dir` übergeben.

Mehr Rendite heißt mehr Risiko: `risk_per_trade` und die Hebel-Grenzen in `config.toml` erhöhen Gewinn **und**
Drawdown ungefähr im gleichen Verhältnis. Im Backtest siehst du, was das für den Drawdown bedeutet.

## Schritt 3 – Paper-Trading (mind. 2–4 Wochen)

```bash
python -m hlbot run          # mode = "paper" in config.toml, kein Schlüssel nötig
python -m hlbot status
```

Der Bot nutzt echte Live-Kurse und simuliert die Ausführung. Der Kontostand liegt in `state/paper_account.json`,
das Protokoll in `logs/paper.log`.

## Schritt 4 – Testnet, dann Live

1. Auf <https://app.hyperliquid.xyz> USDC einzahlen (über Arbitrum). Die USDC müssen im **Perps**-Konto liegen.
2. Unter **More → API** ein **API-Wallet** anlegen und autorisieren. Ein API-Wallet darf handeln, aber **keine
   Auszahlungen** machen. Den Private Key deines Haupt-Wallets **niemals** in den Bot eintragen.
3. `.env` anlegen:
   ```bash
   cp .env.example .env    # HL_ACCOUNT_ADDRESS = Haupt-Wallet, HL_SECRET_KEY = Key des API-Wallets
   chmod 600 .env
   ```
4. Zuerst `mode = "testnet"` (Test-USDC über die Testnet-Faucet), danach `mode = "live"`:
   ```bash
   python -m hlbot run      # fragt im Live-Modus einmal nach Bestätigung
   ```

### Dauerbetrieb auf einem Server (VPS)

Mit Docker:
```bash
docker build -t hlbot .
docker run -d --name hlbot --restart unless-stopped \
  -v $PWD/config.toml:/app/config.toml:ro -v $PWD/.env:/app/.env:ro \
  -v $PWD/state:/app/state -v $PWD/logs:/app/logs hlbot
docker logs -f hlbot
```
Alternativ als systemd-Dienst, siehe `deploy/hlbot.service`.

## Wie der Bot sich absichert

* Jede Position hat sofort eine **Stop-Order auf der Börse**. Auch wenn Bot oder Server ausfallen, ist der
  Verlust begrenzt. Kann kein Stop gesetzt werden, wird die Position sofort wieder geschlossen.
* Beim Nachziehen wird **erst der neue Stop gesetzt und dann der alte gelöscht**. Die Position ist also nie ungeschützt.
* Nach einem Neustart gleicht der Bot seinen Zustand mit der Börse ab. Fremde oder manuell eröffnete Positionen
  in den konfigurierten Coins übernimmt er mit einem ATR-Stop.
* Nach dem Not-Aus bleibt der Bot gestoppt, bis du ihn bewusst mit `python -m hlbot reset-halt` freigibst.

## Projektstruktur

```
hlbot/
  config.py      Konfiguration (config.toml + .env)
  indicators.py  EMA, ATR, ADX, Donchian
  strategy.py    Einstiegs-/Ausstiegsregeln (gemeinsam für Backtest und Live)
  risk.py        Positionsgröße, Tagesverlust-Limit, Not-Aus
  data.py        Kerzen von Hyperliquid laden und cachen
  backtest.py    Portfolio-Backtest und Parameter-Sweep
  broker.py      Hyperliquid-Anbindung (SDK) und Paper-Broker
  bot.py         Handels-Loop (einmal je geschlossener Kerze)
  __main__.py    Kommandozeile
tests/           Tests mit künstlichen Kursdaten
```
