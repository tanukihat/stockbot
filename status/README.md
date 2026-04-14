# StockBot Status Dashboard

A live web dashboard for your StockBot instance. Shows portfolio equity curve vs SPY/QQQ benchmarks, open positions, recent trades, and the Inverse Cramer Score correlation tracker.

## Setup

### Option A — Plain Python (any server)

**Requirements:** Python 3.9+, pip

```bash
cd status/
pip install flask requests python-dotenv
cp .env.example .env
# Edit .env with your credentials
python app.py
```

The dashboard will be available at `http://localhost:8081/stockbot/`.

To run it publicly, put nginx in front:

```nginx
location /stockbot/ {
    proxy_pass http://127.0.0.1:8081;
    proxy_set_header Host $host;
}
```

### Option B — Docker

```bash
cd status/
cp .env.example .env
# Edit .env with your credentials
docker compose up -d
```

Dashboard at `http://localhost:8081/stockbot/`.

---

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API key (same as stockbot) |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key (same as stockbot) |
| `STATUS_PAGE_SECRET` | A random secret shared with your stockbot instance |

Generate a secret with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Wiring up StockBot

To enable event logging and the Inverse Cramer Score tracker, add these to your **stockbot** `.env`:

```env
STATUS_PAGE_URL=https://your-domain.com/stockbot/api/event
STATUS_PAGE_SECRET=your-shared-secret-here
```

Both values must match what you set in the status page `.env`. If you skip this, the dashboard still works — you just won't get trade event markers or ICS data.

---

## ICS Correlation Tracker

The Inverse Cramer Score (ICS) chart appears at the bottom of the dashboard and fills in automatically as StockBot closes positions. Each dot represents one closed trade:

- **X axis** — ICS (0 = Cramer was bullish, 1 = Cramer was bearish)
- **Y axis** — Trade P/L %
- **Green dots** — profitable trades, **red dots** — losses
- **IC Buy Zone** (ICS ≥ 0.65) — Cramer was bearish, Inverse Cramer theory says buy
- **IC Avoid Zone** (ICS ≤ 0.35) — Cramer was bullish, Inverse Cramer theory says avoid

The correlation coefficient in the legend corner tells you numerically whether ICS predicted outcomes. If it's positive and growing, the theory is holding up.
