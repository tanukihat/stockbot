# 🤖 StockBot — Autonomous AI Trading Bot

Sentiment-driven scalping bot targeting 5-10% gains.  
Sectors: AI/ML, Quantum Computing, Strategic Minerals, Crypto.

## Setup

### 1. Install dependencies
```bash
cd ~/workspace/stockbot
pip install -r requirements.txt
```

### 2. Configure .env
Fill in your `.env` file:
- `ANTHROPIC_API_KEY` — your Claude API key
- `TELEGRAM_BOT_TOKEN` — create a bot at https://t.me/BotFather
- `TELEGRAM_CHAT_ID` — message your bot, then visit:
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat ID

Alpaca keys are already filled in.

### 3. Run
```bash
python main.py
```

Or as a background service:
```bash
nohup python main.py > stockbot.log 2>&1 &
```

## Strategy

| Parameter | Value |
|-----------|-------|
| Target gain | +7% (exits at 7%, room to run to 10%) |
| Stop-loss | -8% |
| Max positions | 5 |
| Scan interval | 30 min (market hours), 60 min (crypto 24/7) |
| Allocation | 60% stocks / 25% crypto / 15% options |
| Hold limit | 48h (stale positions auto-closed) |

## Architecture

```
main.py (scheduler)
├── sentiment/
│   ├── reddit.py        → Reddit JSON API scraper
│   ├── stocktwits.py    → StockTwits sentiment
│   └── aggregator.py    → Merge + normalize
├── analysis/
│   └── claude_analyzer.py → Claude Sonnet signal generation
├── trading/
│   ├── portfolio.py     → Position tracking + allocation
│   └── executor.py      → Alpaca order execution
├── notifications/
│   └── telegram.py      → Telegram updates
└── data/
    └── db.py            → SQLite trade log
```

## Telegram Commands
The bot sends you updates on every trade. No commands supported yet —
it's a fire-and-forget autonomous system.

## TODO
- [ ] **Telegram setup** — need bot token + chat ID from Michael
  - Create bot: message @BotFather → `/newbot`
  - Get chat ID: message the bot, then hit `https://api.telegram.org/bot<TOKEN>/getUpdates`
  - Fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
  - Until then, notifications print to stdout/log only
- [ ] Add more sentiment sources (news API, Google Trends)
- [ ] StockTwits crypto symbol mapping refinement
- [ ] Options expiry tracking + auto-close on approach
