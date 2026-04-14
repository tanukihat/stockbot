from flask import Flask, jsonify, request
import requests, time, json, os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars can be set directly
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

ALPACA_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
BASE_URL      = "https://paper-api.alpaca.markets/v2"
DATA_URL      = "https://data.alpaca.markets/v2"
HEADERS       = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
EVENTS_FILE   = os.path.join(os.path.dirname(__file__), "events.json")
EVENT_SECRET  = os.environ["STATUS_PAGE_SECRET"]

_cache = {}
def _get(url, params=None, ttl=60):
    key = url + str(params)
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < ttl:
        return _cache[key]["data"]
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    _cache[key] = {"data": data, "ts": now}
    return data

def load_events():
    try:
        with open(EVENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def get_benchmarks(base_value, portfolio_timestamps):
    start = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
    raw = _get(f"{DATA_URL}/stocks/bars",
               {"symbols": "SPY,QQQ", "timeframe": "1Day",
                "start": start, "limit": 50, "feed": "iex"}, ttl=300)
    bars = raw.get("bars", {})
    result = {}
    for sym, sym_bars in bars.items():
        if not sym_bars:
            continue
        first = sym_bars[0]["c"]
        by_date = {b["t"][:10]: b["c"] for b in sym_bars}
        prices = []
        for ts in portfolio_timestamps:
            d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            p = by_date.get(d)
            if p is None:
                prices.append(prices[-1] if prices else None)
            else:
                prices.append(p)
        first_p = next((p for p in prices if p is not None), None)
        if first_p:
            prices = [round(p / first_p * base_value, 2) if p is not None else None for p in prices]
        result[sym] = prices
    return result

def match_events_to_chart(events, timestamps):
    """Find the chart label index closest to each event's date."""
    dates = [datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") for ts in timestamps]
    matched = []
    for ev in events:
        target = ev["date"]
        # Find exact match first, then nearest
        idx = None
        if target in dates:
            idx = dates.index(target)
        else:
            # Find nearest future date in range
            for i, d in enumerate(dates):
                if d >= target:
                    idx = i
                    break
        if idx is not None:
            matched.append({**ev, "index": idx})
    return matched

@app.route("/stockbot/api/status")
def api_status():
    try:
        account   = _get(f"{BASE_URL}/account")
        clock     = _get(f"{BASE_URL}/clock", ttl=30)
        positions = _get(f"{BASE_URL}/positions")
        history   = _get(f"{BASE_URL}/account/portfolio/history", {"timeframe": "1D", "period": "1A"})
        all_orders = _get(f"{BASE_URL}/orders", {"status": "all", "limit": 50, "direction": "desc"})
        orders = all_orders

        # Build buy-price lookup (oldest→newest so latest buy wins per symbol)
        buy_prices = {}
        for o in reversed(all_orders):
            if o["status"] == "filled" and o["side"] == "buy" and o.get("filled_avg_price"):
                buy_prices[o["symbol"]] = float(o["filled_avg_price"])

        # Annotate sell orders with P/L
        def order_pnl(o):
            if o["side"] != "sell" or not o.get("filled_avg_price"):
                return None, None
            sell_p = float(o["filled_avg_price"])
            buy_p  = buy_prices.get(o["symbol"])
            if not buy_p:
                return None, None
            qty    = float(o.get("filled_qty") or o.get("qty") or 0)
            pct    = (sell_p / buy_p) - 1
            abs_pl = (sell_p - buy_p) * qty
            return round(pct, 6), round(abs_pl, 2)
        pv   = float(account["portfolio_value"])
        base = float(history.get("base_value") or 100000)
        pl   = pv - base
        timestamps = history.get("timestamp", [])
        equity     = history.get("equity", [])
        benchmarks = {}
        try:
            benchmarks = get_benchmarks(base, timestamps)
        except Exception:
            pass
        # Build close-reason lookup from events: { symbol -> reason string }
        # Events logged by stockbot on every close look like "NVDA closed +4.1% — Take-profit triggered"
        all_events_raw = load_events()
        close_reason_lookup = {}
        for ev in reversed(all_events_raw):  # newest first
            lbl = ev.get("label", "")
            if " closed " in lbl and " — " in lbl:
                parts = lbl.split(" closed ", 1)
                sym = parts[0].strip()
                reason = parts[1].split(" — ", 1)[-1].strip() if " — " in parts[1] else ""
                if sym and reason and sym not in close_reason_lookup:
                    close_reason_lookup[sym] = reason

        return jsonify({
            "account": {"portfolio_value":pv,"cash":float(account["cash"]),"base_value":base,"total_pl":pl,"total_pl_pct":pl/base if base else 0},
            "positions": [{"symbol":p["symbol"],"qty":float(p["qty"]),"avg_entry_price":float(p["avg_entry_price"]),"current_price":float(p["current_price"]),"market_value":float(p["market_value"]),"unrealized_pl":float(p["unrealized_pl"]),"unrealized_plpc":float(p["unrealized_plpc"]),"side":p["side"],"asset_class":p.get("asset_class","us_equity")} for p in positions],
            "history": {"timestamp": timestamps, "equity": equity},
            "benchmarks": benchmarks,

            "recent_orders": [{
                "symbol": o["symbol"],
                "side": o["side"],
                "qty": o.get("filled_qty") or o.get("qty") or "—",
                "filled_avg_price": o.get("filled_avg_price"),
                "status": o["status"],
                "created_at": o["created_at"],
                "asset_class": o.get("asset_class","us_equity"),
                "pnl_pct": order_pnl(o)[0],
                "pnl_abs": order_pnl(o)[1],
                "close_reason": close_reason_lookup.get(o["symbol"].replace("/USD","").rstrip("USD") if len(o["symbol"]) > 4 else o["symbol"]) if o["side"] == "sell" else None,
            } for o in orders if o["status"] in ("filled","canceled","expired") and not (o.get("order_class","") == "" and o.get("type") in ("limit","stop") and float(o.get("filled_qty") or 0) == 0)][:12],
            "market": {
                "is_open": clock.get("is_open", False),
                "next_open": clock.get("next_open", ""),
                "next_close": clock.get("next_close", ""),
            },
            "updated_at": int(time.time()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0a0c10">
<title>StockBot — Live Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📈</text></svg>">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
:root{color-scheme:dark;--bg:#0a0c10;--surface:#111418;--border:#1e2328;--text:#e2e8f0;--muted:#64748b;--green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--accent:#3b82f6;--mono:'JetBrains Mono','Fira Code',ui-monospace,monospace;}
*{box-sizing:border-box;margin:0;padding:0;}
html{overflow-x:hidden;}body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;min-height:100vh;padding:16px;overflow-x:hidden;overscroll-behavior-y:contain;-webkit-font-smoothing:antialiased;}.wrap{max-width:1100px;margin:0 auto;}
header{display:flex;align-items:center;margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
.logo{font-size:20px;font-weight:700;letter-spacing:-.5px;margin-right:10px;}.logo span{color:var(--accent);}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite;flex-shrink:0;margin-right:10px;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.hright{margin-left:auto;text-align:right;color:var(--muted);font-size:11px;line-height:1.6;}
.mkt-pill{display:inline-flex;align-items:center;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;letter-spacing:.04em;}
.mkt-pill .mkt-dot{width:6px;height:6px;border-radius:50%;margin-right:5px;}
.mkt-open{background:rgba(34,197,94,.15);color:var(--green);}
.mkt-open .mkt-dot{background:var(--green);box-shadow:0 0 5px var(--green);}
.mkt-closed{background:rgba(100,116,139,.15);color:var(--muted);}
.mkt-closed .mkt-dot{background:var(--muted);}
.hright b{color:var(--text);font-family:var(--mono);font-weight:400;}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;}
@media(min-width:640px){.stats{grid-template-columns:repeat(4,1fr);}}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;transition:border-color .15s,transform .15s,box-shadow .15s;cursor:default;user-select:none;-webkit-user-select:none;}
.stat:hover{border-color:#2e3a4a;transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.4);}
.stat-label{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px;}
.stat-value{font-family:var(--mono);font-size:20px;font-weight:700;line-height:1;}
@media(max-width:380px){.stat-value{font-size:17px;}}
.stat-sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:5px;}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:14px;transition:border-color .15s;position:relative;}
@media(min-width:600px){.chart-card:hover{border-color:#2e3a4a;}}
.chart-card canvas{max-height:220px;}
.chart-legend{display:flex;flex-wrap:wrap;margin-top:12px;}
.legend-item{display:flex;align-items:center;font-size:11px;color:var(--muted);margin-right:16px;margin-bottom:4px;}.legend-item>*+*{margin-left:6px;}
.legend-dot{width:12px;height:3px;border-radius:2px;flex-shrink:0;}

.section{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:18px 0 10px;}
.section-primary{font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text);margin:22px 0 12px;display:flex;align-items:center;gap:8px;}
.section-primary .live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite;flex-shrink:0;}
.pos-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;transition:border-color .15s,transform .15s;}
.pos-card.pos-green{border-left:3px solid var(--green);}
.pos-card.pos-red{border-left:3px solid var(--red);}
@media(min-width:600px){.pos-card:hover{border-color:#2e3a4a;transform:translateY(-1px);}}
.pos-pl-big{font-family:var(--mono);font-size:36px;font-weight:700;}
.tbl-wrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow-x:auto;overflow-y:hidden;}
.tbl-wrap table{width:100%;border-collapse:collapse;}
.tbl-wrap th{text-align:left;font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding:10px 14px;border-bottom:1px solid var(--border);white-space:nowrap;}
.tbl-wrap td{padding:10px 14px;font-family:var(--mono);font-size:12px;border-bottom:1px solid var(--border);white-space:nowrap;}.pos-tbl td{padding:12px 16px;}
.tbl-wrap tr:last-child td{border-bottom:none;}
.tbl-wrap tbody tr:hover td{background:rgba(59,130,246,.06);}
a.sym{color:var(--text);text-decoration:none;font-weight:700;border-bottom:1px dashed transparent;transition:color .15s,border-color .15s;}
a.sym:hover{color:var(--accent);border-bottom-color:var(--accent);}

.mobile-cards{display:flex;flex-direction:column;gap:8px;}
.mob-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;transition:border-color .15s,transform .15s;}
.mob-card:hover{border-color:#2e3a4a;transform:translateY(-1px);}
.mob-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:1px;}
.mob-row:last-child{margin-bottom:0;}
.mob-sym{font-size:16px;font-weight:700;}
.mob-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;margin-top:10px;}.pos-card .mob-grid{gap:8px 16px;margin-top:1px;}
.mob-cell-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
.mob-cell-val{font-family:var(--mono);font-size:12px;margin-top:1px;}.pos-card .mob-cell-val{font-size:20px;}.pos-card .mob-cell-label{font-size:13px;letter-spacing:.04em;}
@media(max-width:599px){.desktop-table{display:none;}}
@media(min-width:600px){.mobile-cards{display:none;}}
.badge{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:4px;text-transform:uppercase;touch-action:manipulation;}
.b-buy{background:rgba(34,197,94,.15);color:var(--green);}.b-sell{background:rgba(239,68,68,.15);color:var(--red);}
.b-crypto{background:rgba(245,158,11,.15);color:var(--yellow);}.b-stock{background:rgba(59,130,246,.15);color:var(--accent);}
.b-filled{background:rgba(34,197,94,.12);color:var(--green);}.b-canceled{background:rgba(100,116,139,.2);color:var(--muted);}
.tip{position:relative;display:inline-block;-webkit-tap-highlight-color:transparent;}
.tip .tiptext{visibility:hidden;opacity:0;background:#1e2328;color:var(--text);border:1px solid var(--border);font-family:var(--mono);font-size:11px;line-height:1.5;border-radius:6px;padding:7px 10px;position:absolute;z-index:99;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);white-space:nowrap;pointer-events:none;transition:opacity .15s;box-shadow:0 4px 16px rgba(0,0,0,.5);}
.tip:hover .tiptext,.tip.tip-active .tiptext{visibility:visible;opacity:1;}
.green{color:var(--green);}.red{color:var(--red);}.muted{color:var(--muted);}
.empty{color:var(--muted);text-align:center;padding:20px;font-style:italic;}
.loading{display:flex;align-items:center;justify-content:center;height:180px;color:var(--muted);}
.footer{text-align:center;color:#1e2328;font-size:11px;font-family:monospace;margin-top:24px;}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="dot" id="dot"></div>
  <div class="logo">Stock<span>Bot</span> 🦞</div>
  <div id="mkt-pill"></div>
  <div class="hright">Paper trading · auto-refresh 60s<br><b id="ts">—</b></div>
</header>
<div id="app"><div class="loading">Loading…</div></div>

<script>
const $=s=>document.getElementById(s);
const fmt=(n,d=2)=>n==null?'—':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtM=n=>n==null?'—':(n<0?'-$'+fmt(-n):'$'+fmt(n));
const fmtP=n=>(n>=0?'+':'')+fmt(n*100,2)+'%';
const fmtPL=n=>(n>=0?'+$':'-$')+fmt(Math.abs(n));
const cls=n=>n>=0?'green':'red';
const iso=s=>{if(!s)return'—';const d=new Date(s);return d.toLocaleDateString('en-US',{month:'short',day:'numeric'})+' '+d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit'});};
function symUrl(sym,ac){return ac==='crypto'?`https://finance.yahoo.com/quote/${sym}-USD`:`https://finance.yahoo.com/quote/${sym}`;}
function symLink(sym,ac){return `<a class="sym" href="${symUrl(sym,ac)}" target="_blank" rel="noopener">${sym}</a>`;}


// Touch support for .tip tooltips
document.addEventListener('click', function(e) {
  var tip = e.target.closest ? e.target.closest('.tip') : null;
  document.querySelectorAll('.tip.tip-active').forEach(function(el) {
    if(el !== tip) el.classList.remove('tip-active');
  });
  if(tip) tip.classList.toggle('tip-active');
});


let chart=null;
function mkChart(ts, eq, bench) {
  const days=ts.length>1?Math.round((ts[ts.length-1]-ts[0])/86400):null;
  const titleEl=document.getElementById('chart-title');
  if(titleEl)titleEl.textContent=days!=null?'Equity Curve — '+days+' Days':'Equity Curve — 1 Month';
  const labels = ts.map(t => {
    const d = new Date(t*1000);
    return d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
  });
  const botColor = (eq[eq.length-1]||0)>=(eq[0]||0) ? '#22c55e' : '#ef4444';



  const datasets = [{
    label:'StockBot', data:eq, borderColor:botColor, borderWidth:2.5,
    backgroundColor:botColor+'14', fill:true, tension:.3,
    pointRadius:0, pointHoverRadius:5,
    pointHoverBackgroundColor:botColor, pointHoverBorderColor:'#0a0c10', pointHoverBorderWidth:2,
    order:1
  }];
  if(bench.SPY && bench.SPY.length) datasets.push({label:'S&P 500 (SPY)',data:bench.SPY,borderColor:'#f59e0b',borderWidth:1.5,backgroundColor:'transparent',fill:false,tension:.3,pointRadius:0,pointHoverRadius:4,pointHoverBackgroundColor:'#f59e0b',order:2});
  if(bench.QQQ && bench.QQQ.length) datasets.push({label:'Nasdaq (QQQ)',data:bench.QQQ,borderColor:'#a78bfa',borderWidth:1.5,backgroundColor:'transparent',fill:false,tension:.3,pointRadius:0,pointHoverRadius:4,pointHoverBackgroundColor:'#a78bfa',borderDash:[4,3],order:3});

  if(chart) chart.destroy();
  const ctx = document.getElementById('ec').getContext('2d');
  chart = new Chart(ctx, {type:'line', data:{labels,datasets}, options:{
    responsive:true, maintainAspectRatio:true,
    interaction:{intersect:false,mode:'index'},
    plugins:{
      legend:{display:false},

      tooltip:{
        backgroundColor:'#1e2328',borderColor:'#2e3338',borderWidth:1,
        titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:10,
        callbacks:{label:c=>{
          const base=datasets[0].data.find(v=>v!=null);
          const val=c.parsed.y, pct=base?((val/base)-1)*100:0;
          return ` ${c.dataset.label}: ${fmtM(val)}  (${pct>=0?'+':''}${pct.toFixed(2)}%)`;
        }}
      }
    },
    scales:{
      x:{grid:{color:'#1e2328'},ticks:{color:'#64748b',maxTicksLimit:7,font:{family:'monospace',size:10}}},
      y:{grid:{color:'#1e2328'},ticks:{color:'#64748b',font:{family:'monospace',size:10},callback:v=>'$'+(v/1000).toFixed(0)+'k'}}
    }
  }});
}

function posRow(p){const c=p.asset_class==='crypto',pc=cls(p.unrealized_plpc);return `<tr style="border-left:3px solid ${p.unrealized_plpc>=0?'var(--green)':'var(--red)'}"><td style="font-size:22px;font-weight:700">${symLink(p.symbol,p.asset_class)}</td><td><span class="badge ${c?'b-crypto':'b-stock'}">${c?'crypto':'stock'}</span></td><td style="font-size:18px">${fmt(p.qty,c?6:4)}</td><td class="tip" style="font-size:18px">${fmtM(p.avg_entry_price)}<span class="tiptext">Entry price</span></td><td class="tip" style="font-size:18px">${fmtM(p.current_price)}<span class="tiptext">Last price</span></td><td style="font-size:18px">${fmtM(p.market_value)}</td><td class="${pc} tip" style="font-size:22px;font-weight:700">${fmtPL(p.unrealized_pl)} <span class="muted" style="font-size:14px;font-weight:400">(${fmtP(p.unrealized_plpc)})</span><span class="tiptext">Unrealized P/L</span></td></tr>`;}
function posCard(p){const c=p.asset_class==='crypto',pc=cls(p.unrealized_plpc),accent=p.unrealized_plpc>=0?'pos-green':'pos-red';return `<div class="pos-card ${accent}"><div class="mob-row"><div style="font-size:18px">${symLink(p.symbol,p.asset_class)} <span class="badge ${c?'b-crypto':'b-stock'}">${c?'crypto':'stock'}</span></div><div class="${pc} pos-pl-big">${fmtP(p.unrealized_plpc)}</div></div><div class="mob-grid"><div><div class="mob-cell-label">Value</div><div class="mob-cell-val">${fmtM(p.market_value)}</div></div><div><div class="mob-cell-label">P/L</div><div class="mob-cell-val ${pc}">${fmtPL(p.unrealized_pl)}</div></div><div><div class="mob-cell-label">Entry</div><div class="mob-cell-val">${fmtM(p.avg_entry_price)}</div></div><div><div class="mob-cell-label">Qty</div><div class="mob-cell-val">${fmt(p.qty,c?6:4)}</div></div></div></div>`;}
function closeReasonBadge(reason){
  if(!reason) return '';
  const r=reason.toLowerCase();
  let color='#64748b';
  if(r.includes('stop')) color='var(--red)';
  else if(r.includes('take-profit')||r.includes('take_profit')) color='var(--green)';
  else if(r.includes('floor')||r.includes('trailing')) color='var(--yellow)';
  return `<span style="font-size:10px;color:${color};margin-left:4px;opacity:0.85">${reason}</span>`;
}
function ordRow(o){
  const ac=o.asset_class||'us_equity',hasPnl=o.pnl_pct!=null,pc=hasPnl?cls(o.pnl_pct):'muted';
  const plCell=hasPnl?fmtPL(o.pnl_abs)+' <span class="muted">('+fmtP(o.pnl_pct)+')</span>':'';
  const reasonCell=o.close_reason?closeReasonBadge(o.close_reason):'';
  return `<tr><td>${symLink(o.symbol,ac)}</td><td><span class="badge b-${o.side}">${o.side.toUpperCase()}</span></td><td>${fmt(o.qty,4)}</td><td>${o.filled_avg_price?fmtM(o.filled_avg_price):'—'}</td><td class="${pc}">${plCell}</td><td><span class="badge b-${o.status}">${o.status}</span>${reasonCell}</td><td class="muted">${iso(o.created_at)}</td></tr>`;
}
function ordCard(o){
  const ac=o.asset_class||'us_equity',hasPnl=o.pnl_pct!=null,pc=hasPnl?cls(o.pnl_pct):'muted';
  const plVal=hasPnl?fmtPL(o.pnl_abs)+' ('+fmtP(o.pnl_pct)+')':'';
  const reasonHtml=o.close_reason?`<div style="margin-top:6px">${closeReasonBadge(o.close_reason)}</div>`:'';
  return `<div class="mob-card"><div class="mob-row"><div>${symLink(o.symbol,ac)} <span class="badge b-${o.side}">${o.side.toUpperCase()}</span></div><div style="display:flex;align-items:center"><span class="badge b-${o.status}" style="margin-right:6px">${o.status}</span><span class="muted" style="font-size:10px;font-family:var(--mono)">${iso(o.created_at)}</span></div></div><div class="mob-grid"><div><div class="mob-cell-label">Price</div><div class="mob-cell-val">${o.filled_avg_price?fmtM(o.filled_avg_price):'—'}</div></div><div><div class="mob-cell-label">P/L</div><div class="mob-cell-val ${pc}">${plVal||'—'}</div></div></div>${reasonHtml}</div>`;
}

let _chartData=null;
function setChartRange(days){
  if(!_chartData)return;
  days=parseInt(days)||0;
  let {ts,eq,bench}=_chartData;
  if(days>0){
    const cutoff=ts[ts.length-1]-days*86400;
    const si=ts.findIndex(t=>t>=cutoff);
    if(si>0){
      ts=ts.slice(si);
      eq=eq.slice(si);
      const nb={};for(const k in bench)nb[k]=(bench[k]||[]).slice(si);bench=nb;
    }
  }
  mkChart(ts,eq,bench);
}

function render(d){
  const mkt=d.market||{};
  const mktOpen=mkt.is_open;
  const mktNext=mkt.is_open?mkt.next_close:mkt.next_open;
  const mktLabel=mktOpen?'Market Open':'Market Closed';
  const mktNextStr=mktNext?(()=>{const nd=new Date(mktNext);return (mktOpen?'Closes ':'Opens ')+nd.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',timeZoneName:'short'});})():'';
  const pillEl=$('mkt-pill');
  if(pillEl)pillEl.innerHTML=`<span class="mkt-pill ${mktOpen?'mkt-open':'mkt-closed'}"><span class="mkt-dot"></span>${mktLabel}${mktNextStr?' · '+mktNextStr:''}</span>`;

  if(d.error){$('app').innerHTML=`<div class="mob-card"><p class="red">Error: ${d.error}</p></div>`;return;}
  const a=d.account, plc=cls(a.total_pl), bench=d.benchmarks||{};
  function benchRet(arr){const f=arr&&arr.find(v=>v!=null),l=arr&&[...arr].reverse().find(v=>v!=null);return(f&&l)?((l/f)-1)*100:null;}
  const spyRet=benchRet(bench.SPY),qqqRet=benchRet(bench.QQQ),botRet=a.total_pl_pct*100;
  function retSpan(v){if(v==null)return'';return `<span class="${v>=0?'green':'red'}" style="font-family:var(--mono)">${v>=0?'+':''}${v.toFixed(2)}%</span>`;}

  $('app').innerHTML=`
    <div class="stats">
      <div class="stat tip"><span class="tiptext">Total portfolio value including open positions</span>
        <div class="stat-label">Portfolio</div><div class="stat-value">${fmtM(a.portfolio_value)}</div><div class="stat-sub">Cash: ${fmtM(a.cash)}</div>
      </div>
      <div class="stat tip"><span class="tiptext">P/L vs starting balance</span>
        <div class="stat-label">Total P/L</div><div class="stat-value ${plc}">${fmtPL(a.total_pl)}</div><div class="stat-sub ${plc}">${fmtP(a.total_pl_pct)}</div>
      </div>
      <div class="stat tip"><span class="tiptext">Starting balance for the charted period</span>
        <div class="stat-label">Starting</div><div class="stat-value">${fmtM(a.base_value)}</div><div class="stat-sub muted">Paper trading</div>
      </div>
      <div class="stat tip"><span class="tiptext">Bot allows max 5 simultaneous positions</span>
        <div class="stat-label">Positions</div><div class="stat-value">${d.positions.length}</div><div class="stat-sub muted">of 5 slots</div>
      </div>
    </div>
    <div class="chart-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
        <div class="stat-label" id="chart-title" style="margin-bottom:0">Equity Curve</div>
        <select id="chart-range" onchange="setChartRange(this.value)" style="-webkit-appearance:none;-moz-appearance:none;appearance:none;background:var(--bg);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:11px;font-family:var(--mono);cursor:pointer;outline:none;touch-action:manipulation;">
          <option value="0">All Time</option>
          <option value="180">6 Months</option>
          <option value="30" selected>30 Days</option>
          <option value="7">1 Week</option>
        </select>
      </div>
      <canvas id="ec"></canvas>
      <div class="chart-legend">
        <div class="legend-item"><div class="legend-dot" style="background:${botRet>=0?'#22c55e':'#ef4444'}"></div><span>StockBot ${retSpan(botRet)}</span></div>
        ${(spyRet!=null)?`<div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div><span>S&P 500 (SPY) ${retSpan(spyRet)}</span></div>`:''}
        ${(qqqRet!=null)?`<div class="legend-item"><div class="legend-dot" style="background:#a78bfa"></div><span>Nasdaq (QQQ) ${retSpan(qqqRet)}</span></div>`:''}

      </div>
    </div>
    <div class="section-primary"><span class="live-dot"></span>Open Positions</div>
    ${d.positions.length===0?'<div class="mob-card"><p class="empty">No open positions</p></div>':`<div class="tbl-wrap pos-tbl desktop-table" style="border-color:#1e3a2a"><table><thead><tr><th>Symbol</th><th>Type</th><th>Qty</th><th>Entry</th><th>Current</th><th>Value</th><th>P/L</th></tr></thead><tbody>${d.positions.map(posRow).join('')}</tbody></table></div><div class="mobile-cards">${d.positions.map(posCard).join('')}</div>`}
    <div class="section">Recent Trades</div>
    ${d.recent_orders.length===0?'<div class="mob-card"><p class="empty">No recent trades</p></div>':`<div class="tbl-wrap desktop-table"><table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg Price</th><th>P/L</th><th>Status</th><th>Date</th></tr></thead><tbody>${d.recent_orders.map(ordRow).join('')}</tbody></table></div><div class="mobile-cards">${d.recent_orders.map(ordCard).join('')}</div>`}
    <div class="section">Inverse Cramer Score — Correlation Tracker</div>
    <div class="chart-card" id="ics-section">
      <div style="margin-bottom:12px">
        <div class="stat-label" style="margin-bottom:4px">ICS vs P/L — Each dot = one closed trade</div>
        <div style="font-size:11px;color:var(--muted);line-height:1.5">≥ 0.65 = Cramer bearish → IC buy · ≤ 0.35 = Cramer bullish → IC avoid</div>
      </div>
      <canvas id="ics-chart" style="max-height:240px"></canvas>
      <div class="chart-legend" style="margin-top:12px">
        <div class="legend-item"><div style="width:10px;height:10px;border-radius:50%;background:var(--green);margin-right:6px"></div><span>Win (P/L &gt; 0)</span></div>
        <div class="legend-item"><div style="width:10px;height:10px;border-radius:50%;background:var(--red);margin-right:6px"></div><span>Loss (P/L ≤ 0)</span></div>
        <div class="legend-item" style="margin-left:auto;font-size:11px;color:var(--muted)" id="ics-corr"></div>
      </div>
      <div id="ics-empty" class="empty" style="display:none">No ICS data yet — closes after this deploy will populate it.</div>
    </div>
    <div class="footer">paper trading only · not financial advice</div>
</div>`;

  if(d.history.timestamp&&d.history.timestamp.length>1){
    _chartData={
      ts:d.history.timestamp,
      eq:d.history.equity,
      bench:(()=>{const nb={};for(const k in bench)nb[k]=(bench[k]||[]);return nb;})(),
    };
    const sel=document.getElementById('chart-range');
    if(sel)sel.value='30';
    setChartRange('30');
  }
  renderICSChart();
}

let _icsChart=null;
async function renderICSChart(){
  try{
    const r=await fetch('/stockbot/api/ics');
    const data=await r.json();
    const pts=data.trades||[];
    const sec=document.getElementById('ics-section');
    if(!sec)return;
    if(!pts.length){document.getElementById('ics-empty').style.display='';document.getElementById('ics-chart').style.display='none';return;}
    document.getElementById('ics-empty').style.display='none';
    document.getElementById('ics-chart').style.display='';
    const wins=pts.filter(p=>p.pnl_pct>0);
    const losses=pts.filter(p=>p.pnl_pct<=0);
    const toPoint=p=>({x:p.ics,y:parseFloat((p.pnl_pct*100).toFixed(2)),label:p.symbol,close_time:p.close_time});
    if(_icsChart)_icsChart.destroy();
    const ctx=document.getElementById('ics-chart').getContext('2d');
    _icsChart=new Chart(ctx,{
      type:'scatter',
      data:{datasets:[
        {label:'Win',data:wins.map(toPoint),backgroundColor:'rgba(34,197,94,0.7)',pointRadius:6,pointHoverRadius:8},
        {label:'Loss',data:losses.map(toPoint),backgroundColor:'rgba(239,68,68,0.7)',pointRadius:6,pointHoverRadius:8},
      ]},
      options:{
        responsive:true,maintainAspectRatio:true,
        plugins:{
          legend:{display:false},
          tooltip:{backgroundColor:'#1e2328',borderColor:'#2e3a4a',borderWidth:1,titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:10,
            callbacks:{
              title:items=>{const d=items[0].raw;return d.label+' · '+new Date(d.close_time).toLocaleDateString('en-US',{month:'short',day:'numeric'});},
              label:item=>{const d=item.raw;return['ICS: '+d.x.toFixed(2),'P/L: '+(d.y>=0?'+':'')+d.y.toFixed(2)+'%'];}
            }
          },
          annotation:{annotations:{
            zeroLine:{type:'line',yMin:0,yMax:0,borderColor:'#2e3a4a',borderWidth:1},
            buyZone:{type:'box',xMin:0.65,xMax:1.0,backgroundColor:'rgba(34,197,94,0.05)',borderColor:'rgba(34,197,94,0.2)',borderWidth:1,label:{content:'IC Buy Zone',display:true,color:'rgba(34,197,94,0.4)',font:{size:10}}},
            avoidZone:{type:'box',xMin:0,xMax:0.35,backgroundColor:'rgba(239,68,68,0.05)',borderColor:'rgba(239,68,68,0.2)',borderWidth:1,label:{content:'IC Avoid Zone',display:true,color:'rgba(239,68,68,0.4)',font:{size:10}}},
          }}
        },
        scales:{
          x:{min:0,max:1,grid:{color:'#1e2328'},title:{display:true,text:'Inverse Cramer Score (ICS)',color:'#64748b',font:{size:11}},ticks:{color:'#64748b',font:{family:'monospace',size:10}}},
          y:{grid:{color:'#1e2328'},title:{display:true,text:'Trade P/L %',color:'#64748b',font:{size:11}},ticks:{color:'#64748b',font:{family:'monospace',size:10},callback:v=>v+'%'}},
        }
      }
    });
    if(pts.length>=3){
      const n=pts.length,sx=pts.reduce((a,p)=>a+p.ics,0)/n,sy=pts.reduce((a,p)=>a+p.pnl_pct,0)/n;
      const num=pts.reduce((a,p)=>a+(p.ics-sx)*(p.pnl_pct-sy),0);
      const den=Math.sqrt(pts.reduce((a,p)=>a+(p.ics-sx)**2,0)*pts.reduce((a,p)=>a+(p.pnl_pct-sy)**2,0));
      const corr=den?num/den:0;
      document.getElementById('ics-corr').textContent='Correlation: '+(corr>=0?'+':'')+corr.toFixed(2)+' (n='+n+')';
    }
  }catch(e){console.warn('ICS chart error:',e);}
}

async function refresh(){
  try{const r=await fetch('/stockbot/api/status');render(await r.json());$('dot').style.cssText='background:var(--green);box-shadow:0 0 8px var(--green)';}
  catch(e){$('dot').style.cssText='background:var(--red);box-shadow:0 0 8px var(--red)';}
  $('ts').textContent=new Date().toLocaleTimeString();
}
refresh();
setInterval(refresh,60000);
</script>
</body>
</html>"""

@app.route("/stockbot/api/ics-record", methods=["POST"])
def api_ics_record():
    if request.headers.get("X-Event-Secret","") != EVENT_SECRET:
        return jsonify({"error":"unauthorized"}), 401
    data = request.json
    if not data or "symbol" not in data:
        return jsonify({"error":"missing symbol"}), 400
    import sqlite3, os
    db_path = os.path.join(os.path.dirname(__file__), "stockbot.db")
    try:
        conn = sqlite3.connect(db_path)
        for col,typ in [("ics","REAL"),("cramer_action","TEXT"),("cramer_sentiment","TEXT"),("pnl_pct","REAL"),("close_time","TEXT")]:
            try: conn.execute(f"ALTER TABLE ics_log ADD COLUMN {col} {typ}")
            except: pass
        conn.execute("""CREATE TABLE IF NOT EXISTS ics_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, close_time TEXT, pnl_pct REAL,
            ics REAL, cramer_action TEXT, cramer_sentiment TEXT
        )""")
        conn.commit()
        conn.execute(
            "INSERT INTO ics_log (symbol,close_time,pnl_pct,ics,cramer_action,cramer_sentiment) VALUES (?,?,?,?,?,?)",
            (data["symbol"], data.get("close_time"), data.get("pnl_pct"), data.get("ics"), data.get("cramer_action"), data.get("cramer_sentiment"))
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stockbot/api/ics")
def api_ics():
    import sqlite3, os
    db_path = os.path.join(os.path.dirname(__file__), "stockbot.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        for col,typ in [("ics","REAL"),("cramer_action","TEXT"),("cramer_sentiment","TEXT")]:
            try: conn.execute(f"ALTER TABLE position_log ADD COLUMN {col} {typ}")
            except: pass
        conn.commit()
        c = conn.cursor()
        # Try ics_log first (pushed from local bot), fall back to position_log
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS ics_log (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, close_time TEXT, pnl_pct REAL, ics REAL, cramer_action TEXT, cramer_sentiment TEXT)")
            conn.commit()
        except: pass
        c.execute("""
            SELECT symbol, close_time, pnl_pct, ics, cramer_action, cramer_sentiment
            FROM ics_log
            ORDER BY close_time DESC LIMIT 200
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"trades": rows})
    except Exception as e:
        return jsonify({"trades":[], "error": str(e)})

@app.route("/stockbot/api/event", methods=["POST"])
def api_add_event():
    if request.headers.get("X-Event-Secret","") != EVENT_SECRET:
        return jsonify({"error":"unauthorized"}), 401
    ev = request.json
    if not ev or not ev.get("label"):
        return jsonify({"error":"missing label"}), 400
    try:
        try:
            with open(EVENTS_FILE) as f:
                events = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            events = []
        d = ev.get("date", str(__import__("datetime").date.today()))
        for e in events:
            if e.get("label") == ev["label"] and e.get("date") == d:
                return jsonify({"ok":True,"skipped":True})
        events.append({"date":d,"emoji":ev.get("emoji","📌"),"label":ev["label"],"detail":ev.get("detail",""),"color":ev.get("color","#94a3b8")})
        events.sort(key=lambda e: e["date"])
        with open(EVENTS_FILE,"w") as f:
            json.dump(events, f, indent=2)
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/stockbot/")
@app.route("/stockbot")
def index(): return HTML

if __name__=="__main__":
    app.run(host="127.0.0.1", port=8081, debug=False)
