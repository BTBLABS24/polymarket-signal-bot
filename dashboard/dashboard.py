#!/usr/bin/env python3
"""Kalshi Bot Dashboard — live view of trades, positions, and P&L."""

import os
import time
import base64
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from flask import Flask, render_template_string

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ──────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID = os.environ.get('KALSHI_API_KEY_ID', '')
KALSHI_PRIVATE_KEY_ENV = os.environ.get('KALSHI_PRIVATE_KEY', '')
KALSHI_PRIVATE_KEY_B64 = os.environ.get('KALSHI_PRIVATE_KEY_B64', '')
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

app = Flask(__name__)

# ── Auth ────────────────────────────────────────────────────────────────────
_private_key = None

def _load_key():
    global _private_key
    if _private_key:
        return
    if KALSHI_PRIVATE_KEY_ENV:
        pem = KALSHI_PRIVATE_KEY_ENV.replace('\\n', '\n').encode()
        _private_key = serialization.load_pem_private_key(pem, password=None)
    elif KALSHI_PRIVATE_KEY_B64:
        pem = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
        _private_key = serialization.load_pem_private_key(pem, password=None)

def auth_get(path, params=None):
    _load_key()
    if not _private_key or not KALSHI_API_KEY_ID:
        return None
    ts = str(int(time.time() * 1000))
    msg = f"{ts}GET{path}".encode()
    sig = _private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    headers = {
        'KALSHI-ACCESS-KEY': KALSHI_API_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
        'KALSHI-ACCESS-TIMESTAMP': ts,
    }
    r = requests.get(f"https://api.elections.kalshi.com{path}", headers=headers, params=params)
    if r.status_code == 200:
        return r.json()
    return None

def pub_get(path, params=None):
    r = requests.get(f"https://api.elections.kalshi.com{path}", params=params)
    return r.json() if r.status_code == 200 else None


# ── Helpers ─────────────────────────────────────────────────────────────────
def classify_ticker(ticker):
    t = ticker.upper()
    if 'TRUMPMENTION' in t:
        return 'trump'
    if 'NCAAMENTION' in t or 'NCAABMENTION' in t or 'CFBMENTION' in t:
        return 'ncaab'
    if 'NBAMENTION' in t:
        return 'nba'
    return 'default'

def fmt_dollars(v):
    if v >= 0:
        return f"+${v:,.2f}"
    return f"-${abs(v):,.2f}"

STRATEGY_RULES = {
    'trump':   {'timing': '0-24h before event', 'price': '5-30c NO'},
    'ncaab':   {'timing': '0.5-1.5h after start', 'price': '6-25c NO'},
    'nba':     {'timing': '0.5-2h after start', 'price': '15-30c NO'},
    'default': {'timing': '0-1.5h before event', 'price': '5-30c NO'},
}


# ── Data fetching ───────────────────────────────────────────────────────────
def fetch_paginated(path, key, params=None):
    """Fetch all pages from a paginated Kalshi endpoint."""
    all_items = []
    cursor = None
    for _ in range(20):  # safety limit
        p = dict(params or {})
        p['limit'] = 100
        if cursor:
            p['cursor'] = cursor
        data = auth_get(path, p)
        if not data:
            break
        batch = data.get(key, [])
        if not batch:
            break
        all_items.extend(batch)
        cursor = data.get('cursor')
        if not cursor:
            break
    return all_items


def get_dashboard_data():
    """Fetch all data needed for the dashboard."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_str = today_start.strftime('%Y-%m-%dT%H:%M:%SZ')
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Balance
    bal_data = auth_get('/trade-api/v2/portfolio/balance') or {}
    balance = bal_data.get('balance', 0) / 100
    payout = bal_data.get('payout', 0) / 100

    # Today's fills
    fills_today = fetch_paginated(
        '/trade-api/v2/portfolio/fills', 'fills',
        {'min_ts': today_str}
    )

    # Today's settlements
    settlements_today = fetch_paginated(
        '/trade-api/v2/portfolio/settlements', 'settlements',
        {'min_ts': today_str}
    )

    # 7-day settlements (for history)
    settlements_7d = fetch_paginated(
        '/trade-api/v2/portfolio/settlements', 'settlements',
        {'min_ts': week_ago}
    )

    # Open positions
    pos_data = auth_get('/trade-api/v2/portfolio/positions', {'limit': 100}) or {}
    positions = pos_data.get('market_positions', [])
    open_positions = [p for p in positions if (p.get('market_exposure') or 0) != 0]

    # Process fills into entries
    entries_today = []
    fill_map = defaultdict(list)  # ticker -> fills
    for f in fills_today:
        ticker = f.get('ticker', '')
        fill_map[ticker].append(f)
        if f.get('action') == 'buy':
            nc = f.get('no_count', 0)
            yc = f.get('yes_count', 0)
            np_cents = f.get('no_price', 0)
            yp_cents = f.get('yes_price', 0)
            cost = nc * np_cents / 100 + yc * yp_cents / 100
            side = f"NO@{np_cents}c" if nc > 0 else f"YES@{yp_cents}c"
            qty = nc if nc > 0 else yc
            ts = f.get('created_time', '')[:19].replace('T', ' ')
            cat = classify_ticker(ticker)
            entries_today.append({
                'time': ts,
                'ticker': ticker,
                'cat': cat,
                'side': side,
                'qty': qty,
                'cost': round(cost, 2),
            })

    # Process settlements
    settled_today = []
    settled_pnl = 0
    settled_wins = 0
    settled_losses = 0
    for s in settlements_today:
        ticker = s.get('ticker', '')
        revenue = s.get('revenue', 0) / 100
        nc = s.get('no_count', 0)
        yc = s.get('yes_count', 0)
        result = s.get('market_result', '')
        cat = classify_ticker(ticker)
        settled_pnl += revenue
        if revenue > 0:
            settled_wins += 1
        elif revenue < 0:
            settled_losses += 1
        settled_today.append({
            'ticker': ticker,
            'cat': cat,
            'result': result,
            'side': f"{nc}NO" if nc > 0 else f"{yc}YES",
            'revenue': round(revenue, 2),
        })
    settled_today.sort(key=lambda x: -x['revenue'])

    # Process open positions
    open_pos = []
    total_exposure = 0
    for p in open_positions:
        ticker = p.get('ticker', '')
        nc = p.get('no_count', 0) or 0
        yc = p.get('yes_count', 0) or 0
        exp = (p.get('market_exposure') or 0) / 100
        total_exposure += exp
        cat = classify_ticker(ticker)
        resting = p.get('resting_orders_count', 0) or 0
        open_pos.append({
            'ticker': ticker,
            'cat': cat,
            'qty': nc if nc > 0 else yc,
            'side': 'NO' if nc > 0 else 'YES',
            'exposure': round(exp, 2),
            'resting': resting,
        })
    open_pos.sort(key=lambda x: x['ticker'])

    # 7-day P&L by day and category
    daily_pnl = defaultdict(float)
    cat_pnl_7d = defaultdict(lambda: {'revenue': 0, 'wins': 0, 'losses': 0, 'count': 0})
    for s in settlements_7d:
        ticker = s.get('ticker', '')
        revenue = s.get('revenue', 0) / 100
        ts = s.get('settled_time', '') or s.get('created_time', '')
        cat = classify_ticker(ticker)
        if ts:
            day = ts[:10]
            daily_pnl[day] += revenue
        cat_pnl_7d[cat]['revenue'] += revenue
        cat_pnl_7d[cat]['count'] += 1
        if revenue > 0:
            cat_pnl_7d[cat]['wins'] += 1
        elif revenue < 0:
            cat_pnl_7d[cat]['losses'] += 1

    daily_sorted = sorted(daily_pnl.items())

    # Category breakdown for today
    cat_today = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0})
    for s in settled_today:
        cat = s['cat']
        cat_today[cat]['count'] += 1
        cat_today[cat]['pnl'] += s['revenue']
        if s['revenue'] > 0:
            cat_today[cat]['wins'] += 1
        elif s['revenue'] < 0:
            cat_today[cat]['losses'] += 1

    return {
        'balance': balance,
        'payout': payout,
        'total_exposure': round(total_exposure, 2),
        'entries_today': entries_today,
        'settled_today': settled_today,
        'settled_pnl': round(settled_pnl, 2),
        'settled_wins': settled_wins,
        'settled_losses': settled_losses,
        'open_positions': open_pos,
        'n_open': len(open_pos),
        'daily_pnl': daily_sorted,
        'cat_pnl_7d': dict(cat_pnl_7d),
        'cat_today': dict(cat_today),
        'now': now.strftime('%Y-%m-%d %H:%M UTC'),
    }


# ── HTML Template ───────────────────────────────────────────────────────────
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Kalshi Bot Dashboard</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --green: #3fb950; --red: #f85149; --blue: #58a6ff; --dim: #8b949e; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'SF Mono', Consolas, monospace; background: var(--bg);
         color: var(--text); padding: 20px; max-width: 1200px; margin: 0 auto; }
  h1 { color: var(--blue); font-size: 18px; margin-bottom: 4px; }
  .subtitle { color: var(--dim); font-size: 12px; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card-label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .card-value { font-size: 24px; font-weight: bold; margin-top: 4px; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--text); }
  h2 { color: var(--blue); font-size: 14px; margin: 20px 0 8px; text-transform: uppercase;
       letter-spacing: 1px; }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
          font-size: 13px; margin-bottom: 16px; }
  th { background: #1c2128; color: var(--dim); font-size: 11px; text-transform: uppercase;
       letter-spacing: 1px; padding: 8px 12px; text-align: left; }
  td { padding: 6px 12px; border-top: 1px solid var(--border); }
  tr:hover { background: #1c2128; }
  .cat { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px;
         font-weight: bold; text-transform: uppercase; }
  .cat-trump { background: #f8514922; color: #f85149; }
  .cat-ncaab { background: #3fb95022; color: #3fb950; }
  .cat-nba { background: #58a6ff22; color: #58a6ff; }
  .cat-default { background: #8b949e22; color: #8b949e; }
  .strat-box { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
               padding: 12px 16px; margin-bottom: 16px; display: grid;
               grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }
  .strat-item { font-size: 12px; }
  .strat-item .cat { margin-right: 6px; }
  .bar { display: inline-block; height: 14px; border-radius: 2px; min-width: 2px; }
  .bar-pos { background: var(--green); }
  .bar-neg { background: var(--red); }
  .empty { color: var(--dim); text-align: center; padding: 24px; font-size: 13px; }
  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav { margin-bottom: 16px; }
  .nav a { margin-right: 16px; font-size: 13px; }
</style>
</head>
<body>
<h1>Kalshi Mention Bot</h1>
<div class="subtitle">{{ data.now }} &middot; auto-refreshes every 60s</div>

<div class="nav">
  <a href="/">Dashboard</a>
  <a href="/history">7-Day History</a>
</div>

<!-- Summary Cards -->
<div class="grid">
  <div class="card">
    <div class="card-label">Balance</div>
    <div class="card-value neutral">${{ "%.2f"|format(data.balance) }}</div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value neutral">{{ data.n_open }}</div>
  </div>
  <div class="card">
    <div class="card-label">Exposure</div>
    <div class="card-value neutral">${{ "%.2f"|format(data.total_exposure) }}</div>
  </div>
  <div class="card">
    <div class="card-label">Today's P&L (settled)</div>
    <div class="card-value {{ 'positive' if data.settled_pnl >= 0 else 'negative' }}">
      {{ "$%.2f"|format(data.settled_pnl) if data.settled_pnl >= 0 else "-$%.2f"|format(data.settled_pnl|abs) }}
    </div>
  </div>
  <div class="card">
    <div class="card-label">Today W/L</div>
    <div class="card-value">
      <span class="positive">{{ data.settled_wins }}W</span> /
      <span class="negative">{{ data.settled_losses }}L</span>
    </div>
  </div>
</div>

<!-- Strategy Rules -->
<h2>Strategy Rules</h2>
<div class="strat-box">
  {% for cat, rule in rules.items() %}
  <div class="strat-item">
    <span class="cat cat-{{ cat }}">{{ cat }}</span>
    {{ rule.timing }} &middot; {{ rule.price }}
  </div>
  {% endfor %}
</div>

<!-- Today's Category Breakdown -->
{% if data.cat_today %}
<h2>Today by Category</h2>
<table>
<tr><th>Category</th><th>Settled</th><th>W/L</th><th>P&L</th></tr>
{% for cat in ['trump','ncaab','nba','default'] %}
{% if cat in data.cat_today %}
{% set c = data.cat_today[cat] %}
<tr>
  <td><span class="cat cat-{{ cat }}">{{ cat }}</span></td>
  <td>{{ c.count }}</td>
  <td><span class="positive">{{ c.wins }}W</span> / <span class="negative">{{ c.losses }}L</span></td>
  <td class="{{ 'positive' if c.pnl >= 0 else 'negative' }}">
    {{ "$%.2f"|format(c.pnl) if c.pnl >= 0 else "-$%.2f"|format(c.pnl|abs) }}
  </td>
</tr>
{% endif %}
{% endfor %}
</table>
{% endif %}

<!-- Open Positions -->
<h2>Open Positions ({{ data.n_open }})</h2>
{% if data.open_positions %}
<table>
<tr><th>Ticker</th><th>Cat</th><th>Side</th><th>Qty</th><th>Exposure</th></tr>
{% for p in data.open_positions %}
<tr>
  <td style="font-size:12px">{{ p.ticker }}</td>
  <td><span class="cat cat-{{ p.cat }}">{{ p.cat }}</span></td>
  <td>{{ p.side }}</td>
  <td>{{ p.qty }}</td>
  <td>${{ "%.2f"|format(p.exposure) }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<div class="empty">No open positions</div>
{% endif %}

<!-- Today's Entries -->
<h2>Today's Entries ({{ data.entries_today|length }})</h2>
{% if data.entries_today %}
<table>
<tr><th>Time (UTC)</th><th>Ticker</th><th>Cat</th><th>Side</th><th>Qty</th><th>Cost</th></tr>
{% for e in data.entries_today %}
<tr>
  <td style="font-size:11px">{{ e.time }}</td>
  <td style="font-size:12px">{{ e.ticker }}</td>
  <td><span class="cat cat-{{ e.cat }}">{{ e.cat }}</span></td>
  <td>{{ e.side }}</td>
  <td>{{ e.qty }}</td>
  <td>${{ "%.2f"|format(e.cost) }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<div class="empty">No entries today</div>
{% endif %}

<!-- Settled Today -->
<h2>Settled Today ({{ data.settled_today|length }})</h2>
{% if data.settled_today %}
<table>
<tr><th>Ticker</th><th>Cat</th><th>Side</th><th>Result</th><th>Revenue</th></tr>
{% for s in data.settled_today %}
<tr>
  <td style="font-size:12px">{{ s.ticker }}</td>
  <td><span class="cat cat-{{ s.cat }}">{{ s.cat }}</span></td>
  <td>{{ s.side }}</td>
  <td>{{ s.result }}</td>
  <td class="{{ 'positive' if s.revenue >= 0 else 'negative' }}">
    {{ "$%.2f"|format(s.revenue) if s.revenue >= 0 else "-$%.2f"|format(s.revenue|abs) }}
  </td>
</tr>
{% endfor %}
</table>
{% else %}
<div class="empty">No settlements today</div>
{% endif %}

</body>
</html>
"""

HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>Kalshi Bot — 7-Day History</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9;
          --green: #3fb950; --red: #f85149; --blue: #58a6ff; --dim: #8b949e; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'SF Mono', Consolas, monospace; background: var(--bg);
         color: var(--text); padding: 20px; max-width: 1200px; margin: 0 auto; }
  h1 { color: var(--blue); font-size: 18px; margin-bottom: 4px; }
  .subtitle { color: var(--dim); font-size: 12px; margin-bottom: 20px; }
  h2 { color: var(--blue); font-size: 14px; margin: 20px 0 8px; text-transform: uppercase;
       letter-spacing: 1px; }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
          font-size: 13px; margin-bottom: 16px; }
  th { background: #1c2128; color: var(--dim); font-size: 11px; text-transform: uppercase;
       letter-spacing: 1px; padding: 8px 12px; text-align: left; }
  td { padding: 6px 12px; border-top: 1px solid var(--border); }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--text); }
  .cat { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px;
         font-weight: bold; text-transform: uppercase; }
  .cat-trump { background: #f8514922; color: #f85149; }
  .cat-ncaab { background: #3fb95022; color: #3fb950; }
  .cat-nba { background: #58a6ff22; color: #58a6ff; }
  .cat-default { background: #8b949e22; color: #8b949e; }
  .bar { display: inline-block; height: 16px; border-radius: 2px; min-width: 2px; vertical-align: middle; }
  .bar-pos { background: var(--green); }
  .bar-neg { background: var(--red); }
  .nav { margin-bottom: 16px; }
  .nav a { color: var(--blue); text-decoration: none; margin-right: 16px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card-label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .card-value { font-size: 24px; font-weight: bold; margin-top: 4px; }
</style>
</head>
<body>
<h1>7-Day History</h1>
<div class="subtitle">{{ data.now }}</div>

<div class="nav">
  <a href="/">Dashboard</a>
  <a href="/history">7-Day History</a>
</div>

<!-- 7d totals -->
{% set total_7d = data.cat_pnl_7d.values()|map(attribute='revenue')|sum %}
{% set total_wins = data.cat_pnl_7d.values()|map(attribute='wins')|sum %}
{% set total_losses = data.cat_pnl_7d.values()|map(attribute='losses')|sum %}
<div class="grid">
  <div class="card">
    <div class="card-label">7-Day P&L</div>
    <div class="card-value {{ 'positive' if total_7d >= 0 else 'negative' }}">
      {{ "$%.2f"|format(total_7d) if total_7d >= 0 else "-$%.2f"|format(total_7d|abs) }}
    </div>
  </div>
  <div class="card">
    <div class="card-label">7-Day W/L</div>
    <div class="card-value">
      <span class="positive">{{ total_wins }}W</span> /
      <span class="negative">{{ total_losses }}L</span>
    </div>
  </div>
</div>

<!-- By Category -->
<h2>7-Day by Category</h2>
<table>
<tr><th>Category</th><th>Settled</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>P&L</th></tr>
{% for cat in ['trump','ncaab','nba','default'] %}
{% if cat in data.cat_pnl_7d %}
{% set c = data.cat_pnl_7d[cat] %}
{% set wr = (c.wins / c.count * 100) if c.count > 0 else 0 %}
<tr>
  <td><span class="cat cat-{{ cat }}">{{ cat }}</span></td>
  <td>{{ c.count }}</td>
  <td class="positive">{{ c.wins }}</td>
  <td class="negative">{{ c.losses }}</td>
  <td>{{ "%.1f"|format(wr) }}%</td>
  <td class="{{ 'positive' if c.revenue >= 0 else 'negative' }}">
    {{ "$%.2f"|format(c.revenue) if c.revenue >= 0 else "-$%.2f"|format(c.revenue|abs) }}
  </td>
</tr>
{% endif %}
{% endfor %}
</table>

<!-- Daily P&L -->
<h2>Daily P&L</h2>
<table>
<tr><th>Date</th><th>P&L</th><th></th></tr>
{% for day, pnl in data.daily_pnl %}
{% set bar_w = (pnl|abs / 20)|int %}
{% if bar_w > 200 %}{% set bar_w = 200 %}{% endif %}
{% if bar_w < 2 %}{% set bar_w = 2 %}{% endif %}
<tr>
  <td>{{ day }}</td>
  <td class="{{ 'positive' if pnl >= 0 else 'negative' }}" style="min-width:80px">
    {{ "$%.2f"|format(pnl) if pnl >= 0 else "-$%.2f"|format(pnl|abs) }}
  </td>
  <td><span class="bar {{ 'bar-pos' if pnl >= 0 else 'bar-neg' }}" style="width:{{ bar_w }}px"></span></td>
</tr>
{% endfor %}
</table>

</body>
</html>
"""


# ── Routes ──────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    data = get_dashboard_data()
    return render_template_string(TEMPLATE, data=data, rules=STRATEGY_RULES)

@app.route('/history')
def history():
    data = get_dashboard_data()
    return render_template_string(HISTORY_TEMPLATE, data=data)

@app.route('/health')
def health():
    return 'ok'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
