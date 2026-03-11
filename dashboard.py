"""
StockBot Equity Dashboard
Generates an interactive HTML chart showing:
  - Portfolio equity curve (always visible)
  - S&P 500 (SPY) benchmark — toggleable
  - QQQ benchmark — toggleable
  - Total capital (starting value reference line) — toggleable

All lines normalized to starting value = 1.0 so they're on the same scale.

Usage:
    python dashboard.py [--period 1M] [--out equity_chart.html]

Opens in default browser unless --no-open is passed.
"""
import argparse
import os
import sys
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import plotly.graph_objects as go

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_BASE_URL     = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _get(path, base=None, params=None):
    base = base or ALPACA_BASE_URL
    url = f"{base}/{path.lstrip('/')}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_portfolio_history(period="1M", timeframe="1D"):
    """
    Returns (timestamps_list, equity_list, base_value) from Alpaca portfolio history.
    Filters out leading zeros (flat pre-funding periods).
    """
    data = _get(
        "/account/portfolio/history",
        params={"period": period, "timeframe": timeframe, "extended_hours": "false"},
    )
    timestamps = data.get("timestamp", [])
    equity     = data.get("equity", [])
    base_value = data.get("base_value", None)

    # Convert epoch timestamps → datetime strings; drop zero-equity entries
    ts_clean, eq_clean = [], []
    for t, e in zip(timestamps, equity):
        if e and e > 0:
            ts_clean.append(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"))
            eq_clean.append(e)

    return ts_clean, eq_clean, base_value


def fetch_bars(symbols, start_date, timeframe="1Day"):
    """
    Fetches daily OHLCV bars for a list of stock symbols.
    Returns dict: { symbol: [(date_str, close), ...] }
    """
    params = {
        "symbols": ",".join(symbols),
        "timeframe": timeframe,
        "start": start_date,
        "limit": 1000,
        "feed": "sip",
        "adjustment": "all",
    }
    data = _get("/stocks/bars", base=DATA_BASE_URL, params=params)
    bars_by_sym = data.get("bars", {})
    result = {}
    for sym, bars in bars_by_sym.items():
        result[sym] = [(b["t"][:10], b["c"]) for b in bars]
    return result


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(values, base=None):
    """Normalize a list of floats so the first value (or base) = 1.0."""
    anchor = base if base else values[0]
    if not anchor:
        return values
    return [v / anchor for v in values]


# ---------------------------------------------------------------------------
# Chart builder
# ---------------------------------------------------------------------------

def build_chart(period="1M", out="equity_chart.html"):
    print("Fetching portfolio history...")
    ts, equity, base_value = fetch_portfolio_history(period)

    if not ts:
        print("No portfolio history data available yet.")
        sys.exit(1)

    start_date = ts[0]
    print(f"Portfolio history: {len(ts)} data points from {start_date} → {ts[-1]}")
    print(f"Base value (starting capital): ${base_value:,.2f}" if base_value else "No base_value from Alpaca")

    # Fetch benchmarks over same window
    print("Fetching SPY and QQQ bars...")
    bench = fetch_bars(["SPY", "QQQ"], start_date=start_date)

    spy_dates  = [d for d, _ in bench.get("SPY", [])]
    spy_closes = [c for _, c in bench.get("SPY", [])]
    qqq_dates  = [d for d, _ in bench.get("QQQ", [])]
    qqq_closes = [c for _, c in bench.get("QQQ", [])]

    # Normalize all series to start = 1.0
    equity_norm = normalize(equity)
    spy_norm    = normalize(spy_closes) if spy_closes else []
    qqq_norm    = normalize(qqq_closes) if qqq_closes else []

    # Total capital: flat reference line at 1.0 (starting capital)
    # Shown alongside equity so you can instantly see if you're up/down vs start
    cap_dates  = [ts[0], ts[-1]]
    cap_values = [1.0, 1.0]

    # -----------------------------------------------------------------------
    # Build Plotly figure
    # -----------------------------------------------------------------------
    fig = go.Figure()

    # Portfolio equity — always visible, primary line
    pnl_pct = (equity[-1] / equity[0] - 1) * 100 if equity else 0
    fig.add_trace(go.Scatter(
        x=ts,
        y=equity_norm,
        name=f"Portfolio  ({pnl_pct:+.1f}%)",
        line=dict(color="#00d4aa", width=2.5),
        hovertemplate="%{x}<br>Portfolio: %{customdata:.2f} (%{y:.3f}x)<extra></extra>",
        customdata=equity,
        visible=True,
    ))

    # S&P 500 (SPY) — toggleable (starts visible)
    spy_pnl = (spy_closes[-1] / spy_closes[0] - 1) * 100 if spy_closes else 0
    fig.add_trace(go.Scatter(
        x=spy_dates,
        y=spy_norm,
        name=f"S&P 500 / SPY  ({spy_pnl:+.1f}%)",
        line=dict(color="#f0a500", width=1.5, dash="dot"),
        hovertemplate="%{x}<br>SPY: $%{customdata:.2f} (%{y:.3f}x)<extra></extra>",
        customdata=spy_closes,
        visible=True,
    ))

    # QQQ — toggleable (starts visible)
    qqq_pnl = (qqq_closes[-1] / qqq_closes[0] - 1) * 100 if qqq_closes else 0
    fig.add_trace(go.Scatter(
        x=qqq_dates,
        y=qqq_norm,
        name=f"QQQ  ({qqq_pnl:+.1f}%)",
        line=dict(color="#9b59b6", width=1.5, dash="dot"),
        hovertemplate="%{x}<br>QQQ: $%{customdata:.2f} (%{y:.3f}x)<extra></extra>",
        customdata=qqq_closes,
        visible=True,
    ))

    # Total capital reference line — toggleable (starts visible)
    cap_label = f"Starting Capital  (${base_value:,.0f})" if base_value else "Starting Capital"
    fig.add_trace(go.Scatter(
        x=cap_dates,
        y=cap_values,
        name=cap_label,
        line=dict(color="#e74c3c", width=1.2, dash="dash"),
        hovertemplate="Starting capital reference<extra></extra>",
        visible=True,
    ))

    # -----------------------------------------------------------------------
    # Layout
    # -----------------------------------------------------------------------
    fig.update_layout(
        title=dict(
            text=f"StockBot Equity  —  {period} view  (click legend to toggle)",
            font=dict(size=18, color="#e0e0e0"),
        ),
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#e0e0e0", family="monospace"),
        legend=dict(
            bgcolor="rgba(30,30,50,0.85)",
            bordercolor="#444",
            borderwidth=1,
            itemclick="toggle",
            itemdoubleclick="toggleothers",
        ),
        xaxis=dict(
            title="Date",
            gridcolor="#2a2a4a",
            showgrid=True,
            zeroline=False,
        ),
        yaxis=dict(
            title="Normalized Value (1.0 = start)",
            gridcolor="#2a2a4a",
            showgrid=True,
            zeroline=True,
            zerolinecolor="#555",
            tickformat=".3f",
        ),
        hovermode="x unified",
        margin=dict(l=60, r=40, t=70, b=60),
    )

    # Horizontal line at y=1.0 as a subtle reference
    fig.add_hline(y=1.0, line_dash="solid", line_color="#333", line_width=1)

    # -----------------------------------------------------------------------
    # Export
    # -----------------------------------------------------------------------
    out_path = Path(__file__).parent / out
    fig.write_html(
        str(out_path),
        include_plotlyjs="cdn",
        full_html=True,
        config={"scrollZoom": True, "displayModeBar": True},
    )
    print(f"Chart saved → {out_path}")
    return str(out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="StockBot equity dashboard")
    parser.add_argument("--period", default="1M", help="History period: 1D, 1W, 1M, 3M, 1A, all (default: 1M)")
    parser.add_argument("--out", default="equity_chart.html", help="Output HTML filename")
    parser.add_argument("--no-open", action="store_true", help="Don't open in browser")
    args = parser.parse_args()

    out_path = build_chart(period=args.period, out=args.out)

    if not args.no_open:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
