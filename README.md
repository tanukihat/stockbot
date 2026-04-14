# 🤖 StockBot — Autonomous AI Trading Bot

A fully autonomous, sentiment-driven trading bot that uses Claude AI to analyze Reddit/StockTwits chatter and execute real (paper) trades on Alpaca. Runs 24/7, manages its own positions, and messages you on Telegram when something happens.

**Currently paper trading. Use real money at your own risk.**

> ⚠️ **DISCLAIMER:** This project is for educational and entertainment purposes only. It is **NOT financial advice**. By using this software, you acknowledge that you assume **full liability** for any and all outcomes, including financial losses. The authors and contributors make no representations or warranties of any kind regarding accuracy, performance, or fitness for any particular purpose. Past performance of the bot does not guarantee future results. **Do your own research. Never trade with money you can't afford to lose.**

---

## How It Works

1. **Scrapes sentiment** from Reddit (WSB, r/stocks, r/CryptoCurrency, etc.) and StockTwits every 30 minutes
2. **Sends batches to Claude** (Haiku) for signal analysis — BUY / SELL / HOLD with confidence scores
3. **Executes trades** on Alpaca with bracket orders (take-profit + stop-loss baked in)
4. **Manages positions** autonomously — trailing stops, sentiment exits, overnight holds, stale position cleanup
5. **Notifies you** on Telegram for every open, close, and daily summary

---

## Strategy

| Parameter | Value |
|-----------|-------|
| Take-profit | +8% |
| Stop-loss | -8% |
| Trailing stop | activates at +5%, floors at that level |
| Max positions | 5 simultaneous |
| Scan interval | 30 min (stocks), 60 min (crypto) |
| Allocation | ~60% stocks / ~25% crypto / ~15% options |
| Momentum gate | skips buy if stock already moved >8% from open |
| Min confidence | 0.65 (raises bar when fully deployed) |

**Target sectors:** AI/ML, Quantum Computing, Strategic Minerals, Crypto

---

## Architecture

```
main.py                    — scheduler + main loop
├── sentiment/
│   ├── reddit.py          — Reddit JSON API scraper (no auth needed)
│   ├── stocktwits.py      — StockTwits sentiment
│   ├── finnhub.py         — Finnhub news sentiment
│   ├── discovery.py       — Auto-discover trending tickers
│   └── aggregator.py      — Merge + normalize sources
├── analysis/
│   └── claude_analyzer.py — Claude Haiku signal generation (batched)
├── trading/
│   ├── portfolio.py       — Position tracking, allocation, volatility scaling
│   ├── executor.py        — Alpaca order execution + momentum gate
│   ├── overnight.py       — Overnight hold eligibility logic
│   └── sentiment_exit.py  — Exit based on sentiment reversal
├── notifications/
│   └── telegram.py        — Trade alerts, daily summary, startup notify
├── events_writer.py       — Milestone/event logging for dashboard
└── data/
    └── db.py              — SQLite trade log
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in:

```env
ANTHROPIC_API_KEY=your_claude_api_key
ALPACA_API_KEY=your_alpaca_key
ALPACA_SECRET_KEY=your_alpaca_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

- **Alpaca:** Sign up at [alpaca.markets](https://alpaca.markets) — use the paper trading URL to start
- **Telegram bot:** Create one at [@BotFather](https://t.me/BotFather), get your chat ID from `getUpdates`
- **Anthropic:** Get a key at [console.anthropic.com](https://console.anthropic.com)

### 3. Run

```bash
bash run.sh
```

Or directly:
```bash
python main.py
```

---

## Status Dashboard

There's a companion Flask app (`/var/www/stockbot-status/`) that serves a live dashboard with equity curve, open positions, recent trades, and benchmark comparisons (SPY/QQQ). Deploy it separately on a VPS with nginx.

---

## Notifications

The bot sends Telegram messages for:
- 📈 **OPENED** — new position with shares, entry price, TP/SL levels, confidence, reasoning
- 💰 **CLOSED** — P&L, exit price, close reason (take-profit / stop-loss / trailing / sentiment)
- 📊 **Daily summary** — EOD P&L, trade count, portfolio value
- 🤖 **Startup** — current positions inherited on restart
- ⚠️ **Errors** — critical failures

---

## Tuning

Key levers in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `TAKE_PROFIT_PCT` | 0.08 | Exit at +8% |
| `STOP_LOSS_PCT` | 0.08 | Exit at -8% |
| `MAX_POSITIONS` | 5 | Max simultaneous positions |
| `MIN_SENTIMENT_SCORE` | 0.65 | Min confidence to open |
| `MAX_INTRADAY_MOVE_PCT` | 0.08 | Skip if already moved >8% today |
| `TRAILING_ACTIVATE_PCT` | 0.05 | Start trailing stop at +5% |
| `SCAN_INTERVAL_MINUTES` | 30 | Stock scan frequency |

---

## License

MIT — do whatever you want with it. Not financial advice.
