#!/usr/bin/env python3
"""Sweep slippage levels to find PnL-maximizing point.
Trump $100 cap, others $50 cap, 60-day window, ex-SOTU."""

import json
from datetime import datetime, timezone
from collections import defaultdict

with open('mention_trade_cache.json') as f:
    trade_cache = json.load(f)
with open('mention_markets_cache.json') as f:
    markets_cache = json.load(f)
with open('mention_milestones_cache.json') as f:
    milestones_cache = json.load(f)

START = datetime(2025, 12, 29, 0, 0, 0, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 2, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp()

NBA_BLACKLIST = {'ROOK', 'INJU', 'CROW', 'ALL'}
NBA_ARENA_BLACKLIST = {
    'MSG', 'TD', 'XFIN', 'SPEC', 'MODA', 'TARG', 'PAYC', 'INTU', 'TOYO',
    'KIA', 'AMER', 'CHAS', 'CRYP', 'ROCK', 'FROS', 'FEDE', 'LITT', 'BALL',
    'CAPI', 'GOLD', 'FISE', 'SCOT', 'DELT', 'STAT', 'GAIN', 'KASE',
}
NCAAB_BLACKLIST = {'FRES', 'SAFE', 'TRAN'}
NCAAB_ARENA_BLACKLIST = {
    'MCKA', 'STEP', 'PINN', 'BRES', 'MACK', 'GALE', 'RUPP', 'HILT', 'KOHL',
    'ALLEN', 'COLE', 'SAND', 'CAPI', 'MEMO', 'UNIT', 'MSG', 'STAT', 'STEG',
    'NEVI', 'CRIS', 'MARR', 'WELS', 'LENO', 'CARV', 'MIZZ', 'DESE', 'CAME',
    'BUD', 'FERT', 'MILL',
}

def get_category(ticker):
    upper = ticker.upper()
    if 'EARNINGS' in upper: return 'Earnings'
    if 'NBAMENTION' in upper or 'NBAFINALS' in upper: return 'NBA'
    if 'NCAAMENTION' in upper or 'NCAABMENTION' in upper: return 'NCAA'
    if 'TRUMPMENTION' in upper: return 'Trump'
    if 'MAMDANIMENTION' in upper: return 'Mamdani'
    if 'NEWSOMMENTION' in upper: return 'Newsom'
    return 'Other'

def in_entry_window(category, h):
    if category == 'NBA': return -2 <= h <= -0.5
    elif category == 'NCAA': return (-1.5 <= h <= -0.5) or (1 <= h <= 24)
    elif category == 'Trump': return 0 <= h <= 24
    elif category in ('Mamdani', 'Newsom'): return 0 <= h <= 1.5
    else: return 0 <= h <= 24

def in_price_range(category, no_price):
    if category == 'NCAA': return 6 <= no_price <= 25
    elif category == 'NBA': return 9 <= no_price <= 25
    else: return 1 <= no_price <= 30

# Pre-compute events in range
events_in_range = set()
for evt_key, evt_data in milestones_cache.items():
    st = evt_data.get('start_ts', 0)
    if isinstance(st, (int, float)) and START <= st <= END:
        events_in_range.add(evt_key)

# Pre-compute market metadata (category, event_ticker, start_ts, result, sorted trades)
# so we don't redo this for every slippage level
market_meta = {}
for ticker, market_info in markets_cache.items():
    category = get_category(ticker)
    if category == 'Earnings':
        continue

    word = ticker.split('-')[-1].upper()
    if category == 'NBA' and (word in NBA_BLACKLIST or word in NBA_ARENA_BLACKLIST):
        continue
    if category == 'NCAA' and (word in NCAAB_BLACKLIST or word in NCAAB_ARENA_BLACKLIST):
        continue

    et = market_info.get('event_ticker', '')
    if et and et in milestones_cache:
        event_ticker = et
    else:
        parts = ticker.rsplit('-', 1)
        if len(parts) > 1 and parts[0] in milestones_cache:
            event_ticker = parts[0]
        else:
            continue

    if event_ticker not in events_in_range:
        continue

    ms = milestones_cache.get(event_ticker, {})
    start_ts = ms.get('start_ts')
    if start_ts is None:
        continue

    result = market_info.get('result', '')
    if result not in ('yes', 'no'):
        continue

    trades = trade_cache.get(ticker, [])
    if not trades:
        continue

    trades_sorted = sorted(trades, key=lambda t: t[0])

    # Find first eligible trade
    entry_trade = None
    for t in trades_sorted:
        ts, yes_price = t[0], t[1]
        no_price = 100 - yes_price
        h = (start_ts - ts) / 3600.0
        if not in_entry_window(category, h):
            continue
        if not in_price_range(category, no_price):
            continue
        entry_trade = t
        break

    if entry_trade is None:
        continue

    entry_ts = entry_trade[0]
    entry_no = 100 - entry_trade[1]
    trade_date = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime('%Y-%m-%d')

    # Exclude SOTU
    evt_title = ms.get('title', '').lower()
    is_sotu = any(kw in evt_title for kw in ['state of the union', 'sotu', 'joint address', 'address to congress'])
    if category == 'Trump' and (is_sotu or trade_date == '2026-02-24'):
        continue

    # Pre-collect all trades from entry onward within entry window
    window_trades = []
    for t in trades_sorted:
        ts = t[0]
        if ts < entry_ts:
            continue
        h = (start_ts - ts) / 3600.0
        if not in_entry_window(category, h):
            continue
        no_price = 100 - t[1]
        window_trades.append((no_price, t[2]))  # (no_price_cents, count)

    cap = 100.0 if category == 'Trump' else 50.0

    market_meta[ticker] = {
        'category': category,
        'entry_no': entry_no,
        'result': result,
        'cap': cap,
        'window_trades': window_trades,
    }

print(f"Markets pre-computed: {len(market_meta)}")
print()

# Sweep slippage levels
# Test: named_slip from 2 to 15, other_slip = named_slip - 2 (or at least 1)
slippage_levels = [
    (2, 1),
    (3, 2),
    (4, 2),
    (4, 4),
    (5, 3),
    (6, 4),
    (8, 4),
    (8, 6),
    (10, 6),
    (10, 8),
    (12, 8),
    (12, 10),
    (15, 10),
    (15, 12),
    (20, 15),
]

print("=" * 130)
print("SLIPPAGE SWEEP: 60-DAY BACKTEST (Trump $100, Others $50, ex-SOTU)")
print("=" * 130)
print(f"{'Named':>6} {'Other':>6} {'Mkts':>6} {'Deployed':>10} {'PnL':>10} {'ROI':>8} {'$/day':>8} {'AvgBet':>8} {'AvgFill%':>9}")
print("-" * 130)

best_pnl = 0
best_config = None

for slip_named, slip_other in slippage_levels:
    total_deployed = 0
    total_pnl = 0
    n_markets = 0

    for ticker, meta in market_meta.items():
        entry_no = meta['entry_no']
        cat = meta['category']
        result = meta['result']
        cap = meta['cap']
        slip = slip_other if cat == 'Other' else slip_named
        max_no = entry_no + slip

        vol = 0.0
        for no_price, count in meta['window_trades']:
            if entry_no <= no_price <= max_no:
                vol += count * no_price / 100.0

        fillable = max(5.0, min(cap, vol))
        cost_per = entry_no / 100.0

        if result == 'no':
            pnl = fillable * ((1.0 - cost_per) / cost_per)
        else:
            pnl = -fillable

        total_deployed += fillable
        total_pnl += pnl
        n_markets += 1

    roi = (total_pnl / total_deployed * 100) if total_deployed > 0 else 0
    avg_bet = total_deployed / n_markets if n_markets > 0 else 0
    avg_fill = (total_deployed / (n_markets * 50.0)) * 100 if n_markets > 0 else 0  # rough
    daily_pnl = total_pnl / 60.0

    if total_pnl > best_pnl:
        best_pnl = total_pnl
        best_config = (slip_named, slip_other)

    print(f"{slip_named:>5}c {slip_other:>5}c {n_markets:>6} ${total_deployed:>8.0f} ${total_pnl:>8.0f} {roi:>7.1f}% ${daily_pnl:>6.0f} ${avg_bet:>6.1f} {avg_fill:>8.1f}%")

print("-" * 130)
print(f"BEST PnL: named={best_config[0]}c, other={best_config[1]}c -> ${best_pnl:,.0f}")
print()

# Now show per-category breakdown for best config
print("=" * 130)
print(f"PER-CATEGORY BREAKDOWN AT BEST SLIPPAGE: named={best_config[0]}c, other={best_config[1]}c")
print("=" * 130)
print(f"{'Category':<10} {'Mkts':>6} {'WR':>6} {'Deployed':>10} {'PnL':>10} {'ROI':>8} {'AvgBet':>8}")
print("-" * 130)

slip_named, slip_other = best_config
cat_stats = defaultdict(lambda: {'n': 0, 'wins': 0, 'deployed': 0, 'pnl': 0})

for ticker, meta in market_meta.items():
    entry_no = meta['entry_no']
    cat = meta['category']
    result = meta['result']
    cap = meta['cap']
    slip = slip_other if cat == 'Other' else slip_named
    max_no = entry_no + slip

    vol = 0.0
    for no_price, count in meta['window_trades']:
        if entry_no <= no_price <= max_no:
            vol += count * no_price / 100.0

    fillable = max(5.0, min(cap, vol))
    cost_per = entry_no / 100.0

    if result == 'no':
        pnl = fillable * ((1.0 - cost_per) / cost_per)
        cat_stats[cat]['wins'] += 1
    else:
        pnl = -fillable

    cat_stats[cat]['n'] += 1
    cat_stats[cat]['deployed'] += fillable
    cat_stats[cat]['pnl'] += pnl

for cat in ['NBA', 'NCAA', 'Trump', 'Mamdani', 'Newsom', 'Other']:
    s = cat_stats.get(cat)
    if not s or s['n'] == 0:
        continue
    wr = s['wins'] / s['n'] * 100
    roi = (s['pnl'] / s['deployed'] * 100) if s['deployed'] > 0 else 0
    avg = s['deployed'] / s['n']
    print(f"{cat:<10} {s['n']:>6} {wr:>5.1f}% ${s['deployed']:>8.0f} ${s['pnl']:>8.0f} {roi:>7.1f}% ${avg:>6.1f}")

totals = {'n': 0, 'wins': 0, 'deployed': 0, 'pnl': 0}
for s in cat_stats.values():
    for k in totals:
        totals[k] += s[k]
wr = totals['wins'] / totals['n'] * 100 if totals['n'] > 0 else 0
roi = (totals['pnl'] / totals['deployed'] * 100) if totals['deployed'] > 0 else 0
avg = totals['deployed'] / totals['n'] if totals['n'] > 0 else 0
print("-" * 130)
print(f"{'TOTAL':<10} {totals['n']:>6} {wr:>5.1f}% ${totals['deployed']:>8.0f} ${totals['pnl']:>8.0f} {roi:>7.1f}% ${avg:>6.1f}")
print(f"\nDaily PnL: ${totals['pnl']/60:.0f}/day")

# Also show the current (4c/2c) for comparison
print()
print("=" * 130)
print("COMPARISON: CURRENT (4c/2c) vs BEST")
print("=" * 130)
for label, sn, so in [("Current (4c/2c)", 4, 2), (f"Best ({best_config[0]}c/{best_config[1]}c)", best_config[0], best_config[1])]:
    td = 0; tp = 0
    for ticker, meta in market_meta.items():
        entry_no = meta['entry_no']
        cat = meta['category']
        result = meta['result']
        cap = meta['cap']
        slip = so if cat == 'Other' else sn
        max_no = entry_no + slip

        vol = 0.0
        for no_price, count in meta['window_trades']:
            if entry_no <= no_price <= max_no:
                vol += count * no_price / 100.0

        fillable = max(5.0, min(cap, vol))
        cost_per = entry_no / 100.0
        if result == 'no':
            pnl = fillable * ((1.0 - cost_per) / cost_per)
        else:
            pnl = -fillable
        td += fillable
        tp += pnl

    roi = (tp / td * 100) if td > 0 else 0
    print(f"  {label:<25} Deployed: ${td:>9,.0f}  PnL: ${tp:>9,.0f}  ROI: {roi:>6.1f}%  $/day: ${tp/60:>6,.0f}")
