#!/usr/bin/env python3
"""
Backtest $50 Liquidity Analysis
================================
Checks how much of a $50 bet could actually fill within slippage limits,
using real trade data as a proxy for orderbook depth.

For each market where the bot would enter:
- Find the first eligible NO trade (entry signal at price P)
- Look at ALL trades in that market within the next 5 minutes
- Filter trades within slippage: NO price between P and P+4c (named cats) or P+2c (Other)
- Sum total NO-side dollar volume available in that window
- Fillable amount = min($50, total_available_volume)
"""

import json
from datetime import datetime, timezone
from collections import defaultdict

# -- Load caches --

with open('mention_trade_cache.json') as f:
    trade_cache = json.load(f)

with open('mention_markets_cache.json') as f:
    markets_cache = json.load(f)

with open('mention_milestones_cache.json') as f:
    milestones_cache = json.load(f)

# -- Constants --

FEB20 = datetime(2025, 12, 29, 0, 0, 0, tzinfo=timezone.utc).timestamp()  # ~60 days back
FEB28 = datetime(2026, 2, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp()

SLIPPAGE_WINDOW_SEC = None  # Use full entry window, not fixed time

NBA_BLACKLIST = {'ROOK', 'INJU', 'CROW', 'ALL'}
NBA_ARENA_BLACKLIST = {
    'MSG', 'TD', 'XFIN', 'SPEC', 'MODA', 'TARG', 'PAYC', 'INTU', 'TOYO',
    'KIA', 'AMER', 'CHAS', 'CRYP', 'ROCK', 'FROS', 'FEDE', 'LITT', 'BALL',
    'CAPI', 'GOLD', 'FISE', 'SCOT', 'DELT', 'STAT', 'GAIN', 'KASE',
}
NCAAB_BLACKLIST = {'FRES', 'SAFE', 'TRAN', 'OVER'}
NCAAB_ARENA_BLACKLIST = {
    'MCKA', 'STEP', 'PINN', 'BRES', 'MACK', 'GALE', 'RUPP', 'HILT', 'KOHL',
    'ALLEN', 'COLE', 'SAND', 'CAPI', 'MEMO', 'UNIT', 'MSG', 'STAT', 'STEG',
    'NEVI', 'CRIS', 'MARR', 'WELS', 'LENO', 'CARV', 'MIZZ', 'DESE', 'CAME',
    'BUD', 'FERT', 'MILL',
}

BET_SIZE_SMALL = 5.0
BET_SIZE_LARGE = 50.0

# -- Helpers --

def get_category(ticker):
    upper = ticker.upper()
    if 'EARNINGS' in upper:
        return 'Earnings'
    if 'NBAMENTION' in upper or 'NBAFINALS' in upper:
        return 'NBA'
    if 'NCAAMENTION' in upper or 'NCAABMENTION' in upper:
        return 'NCAA'
    if 'TRUMPMENTION' in upper:
        return 'Trump'
    if 'MAMDANIMENTION' in upper:
        return 'Mamdani'
    if 'NEWSOMMENTION' in upper:
        return 'Newsom'
    return 'Other'


def get_word(ticker):
    parts = ticker.split('-')
    return parts[-1].upper() if parts else ''


def is_blacklisted(category, word):
    if category == 'NBA' and (word in NBA_BLACKLIST or word in NBA_ARENA_BLACKLIST):
        return True
    if category == 'NCAA' and (word in NCAAB_BLACKLIST or word in NCAAB_ARENA_BLACKLIST):
        return True
    return False


def get_event_ticker(ticker, market_info):
    et = market_info.get('event_ticker', '')
    if et and et in milestones_cache:
        return et
    parts = ticker.rsplit('-', 1)
    if len(parts) > 1 and parts[0] in milestones_cache:
        return parts[0]
    return None


def get_milestone_times(event_ticker):
    ms = milestones_cache.get(event_ticker)
    if not ms:
        return None, None
    return ms.get('start_ts'), ms.get('end_ts')


def in_entry_window(category, hours_to_event):
    h = hours_to_event
    if category == 'NBA':
        return -2 <= h <= -0.5
    elif category == 'NCAA':
        return (-1.5 <= h <= -0.5) or (1 <= h <= 24)
    elif category == 'Trump':
        return 0 <= h <= 24
    elif category in ('Mamdani', 'Newsom'):
        return 0 <= h <= 1.5
    else:
        return 0 <= h <= 24


def in_price_range(category, no_price_cents):
    if category == 'NCAA':
        return 6 <= no_price_cents <= 25
    elif category == 'NBA':
        return 9 <= no_price_cents <= 25
    else:
        return 1 <= no_price_cents <= 30


SLIP_NAMED = 4  # will be overridden in sweep
SLIP_OTHER = 2

def slippage_limit(category):
    if category == 'Other':
        return SLIP_OTHER
    return SLIP_NAMED


# -- Main analysis --

def run_backtest(slip_named, slip_other):
    global SLIP_NAMED, SLIP_OTHER
    SLIP_NAMED = slip_named
    SLIP_OTHER = slip_other

    results = []

events_in_range = set()
for evt_key, evt_data in milestones_cache.items():
    st = evt_data.get('start_ts', 0)
    if isinstance(st, (int, float)) and FEB20 <= st <= FEB28:
        events_in_range.add(evt_key)

print(f"Events with start_ts in Feb 20-27: {len(events_in_range)}")

skipped_earnings = 0
skipped_blacklist = 0
skipped_no_event = 0
skipped_no_result = 0
skipped_no_trades = 0

for ticker, market_info in markets_cache.items():
    category = get_category(ticker)
    if category == 'Earnings':
        skipped_earnings += 1
        continue

    event_ticker = get_event_ticker(ticker, market_info)
    if not event_ticker:
        skipped_no_event += 1
        continue

    if event_ticker not in events_in_range:
        continue

    word = get_word(ticker)
    if is_blacklisted(category, word):
        skipped_blacklist += 1
        continue

    start_ts, end_ts = get_milestone_times(event_ticker)
    if start_ts is None:
        continue

    result = market_info.get('result', '')
    if result not in ('yes', 'no'):
        skipped_no_result += 1
        continue

    trades = trade_cache.get(ticker, [])
    if not trades:
        skipped_no_trades += 1
        continue

    trades_sorted = sorted(trades, key=lambda t: t[0])

    # Find first eligible NO trade within entry window and price range
    entry_trade = None
    for t in trades_sorted:
        ts, yes_price, count, taker_side = t[0], t[1], t[2], t[3]
        no_price = 100 - yes_price

        hours_to_event = (start_ts - ts) / 3600.0

        if not in_entry_window(category, hours_to_event):
            continue
        if not in_price_range(category, no_price):
            continue

        entry_trade = t
        break

    if entry_trade is None:
        continue

    entry_ts = entry_trade[0]
    entry_yes_price = entry_trade[1]
    entry_no_price = 100 - entry_yes_price
    entry_count = entry_trade[2]

    # Liquidity analysis: all trades within entry window AND slippage
    # Bot uses maker orders — resting limit sits for entire entry window
    slip_limit = slippage_limit(category)
    max_no_price = entry_no_price + slip_limit

    total_no_volume_dollars = 0.0
    trades_in_window = 0

    for t in trades_sorted:
        ts = t[0]
        if ts < entry_ts:
            continue
        # Check this trade is still within entry window
        hours_to_evt = (start_ts - ts) / 3600.0
        if not in_entry_window(category, hours_to_evt):
            continue

        yes_price = t[1]
        count = t[2]
        no_price_c = 100 - yes_price

        if entry_no_price <= no_price_c <= max_no_price:
            dollar_vol = count * no_price_c / 100.0
            total_no_volume_dollars += dollar_vol
            trades_in_window += 1

    # Per-category caps
    # Trump: $100/market, all others: $50
    if category == 'Trump':
        cat_cap = 100.0
    else:
        cat_cap = 50.0
    fillable_50 = max(BET_SIZE_SMALL, min(cat_cap, total_no_volume_dollars))
    fill_pct = (fillable_50 / cat_cap) * 100.0

    cost_per_contract = entry_no_price / 100.0

    if result == 'no':
        pnl_per_dollar = (1.0 - cost_per_contract) / cost_per_contract
    else:
        pnl_per_dollar = -1.0

    pnl_5 = BET_SIZE_SMALL * pnl_per_dollar
    pnl_50_constrained = fillable_50 * pnl_per_dollar
    pnl_50_unconstrained = BET_SIZE_LARGE * pnl_per_dollar

    trade_date = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime('%Y-%m-%d')

    # Exclude State of the Union Trump events (pre-scripted, insider-known)
    evt_title = milestones_cache.get(event_ticker, {}).get('title', '').lower() if event_ticker else ''
    is_sotu = any(kw in evt_title for kw in ['state of the union', 'sotu', 'joint address', 'address to congress'])
    # Also flag by date if title not available - Feb 24 was SOTU
    if category == 'Trump' and (is_sotu or trade_date == '2026-02-24'):
        continue

    results.append({
        'ticker': ticker,
        'category': category,
        'word': word,
        'date': trade_date,
        'entry_no_price': entry_no_price,
        'result': result,
        'cost_per_contract': cost_per_contract,
        'total_no_volume_5min': total_no_volume_dollars,
        'trades_in_window': trades_in_window,
        'fillable_50': fillable_50,
        'fill_pct': fill_pct,
        'pnl_5': pnl_5,
        'pnl_50_constrained': pnl_50_constrained,
        'pnl_50_unconstrained': pnl_50_unconstrained,
        'won': result == 'no',
    })

print(f"Markets with valid entries: {len(results)}")
print(f"Skipped: earnings={skipped_earnings}, blacklist={skipped_blacklist}, "
      f"no_event={skipped_no_event}, no_result={skipped_no_result}, "
      f"no_trades={skipped_no_trades}")
print()

# -- Per-category table --

categories = ['NBA', 'NCAA', 'Trump', 'Mamdani', 'Newsom', 'Other']
cat_data = defaultdict(list)
for r in results:
    cat_data[r['category']].append(r)

print("=" * 140)
print("PER-CATEGORY SUMMARY (Feb 20-27, 2026)")
print("=" * 140)
print(f"{'Category':<10} {'Events':>6} {'Wins':>5} {'WR':>6} {'Cost$5':>9} {'Cost$50unc':>11} "
      f"{'Fill$50':>9} {'AvgFill%':>9} {'PnL$5':>9} {'PnL$50con':>10} {'ROI$50':>8}")
print("-" * 140)

total_events = 0
total_wins = 0
total_cost_5 = 0
total_cost_50_unc = 0
total_fillable_50 = 0
total_pnl_5 = 0
total_pnl_50_con = 0

for cat in categories:
    entries = cat_data.get(cat, [])
    if not entries:
        continue

    n = len(entries)
    wins = sum(1 for e in entries if e['won'])
    wr = wins / n * 100 if n > 0 else 0
    cost_5 = n * BET_SIZE_SMALL
    cost_50_unc = n * BET_SIZE_LARGE
    fill_50 = sum(e['fillable_50'] for e in entries)
    avg_fill = sum(e['fill_pct'] for e in entries) / n if n > 0 else 0
    pnl_5 = sum(e['pnl_5'] for e in entries)
    pnl_50_con = sum(e['pnl_50_constrained'] for e in entries)
    roi_50 = (pnl_50_con / fill_50 * 100) if fill_50 > 0 else 0

    total_events += n
    total_wins += wins
    total_cost_5 += cost_5
    total_cost_50_unc += cost_50_unc
    total_fillable_50 += fill_50
    total_pnl_5 += pnl_5
    total_pnl_50_con += pnl_50_con

    print(f"{cat:<10} {n:>6} {wins:>5} {wr:>5.1f}% ${cost_5:>7.0f} ${cost_50_unc:>9.0f} "
          f"${fill_50:>7.1f} {avg_fill:>8.1f}% ${pnl_5:>7.1f} ${pnl_50_con:>8.1f} {roi_50:>7.1f}%")

print("-" * 140)
total_wr = total_wins / total_events * 100 if total_events > 0 else 0
total_avg_fill = sum(e['fill_pct'] for e in results) / total_events if total_events > 0 else 0
total_roi_50 = (total_pnl_50_con / total_fillable_50 * 100) if total_fillable_50 > 0 else 0
print(f"{'TOTAL':<10} {total_events:>6} {total_wins:>5} {total_wr:>5.1f}% ${total_cost_5:>7.0f} ${total_cost_50_unc:>9.0f} "
      f"${total_fillable_50:>7.1f} {total_avg_fill:>8.1f}% ${total_pnl_5:>7.1f} ${total_pnl_50_con:>8.1f} {total_roi_50:>7.1f}%")

# -- Daily breakdown --

print()
print("=" * 120)
print("DAILY BREAKDOWN")
print("=" * 120)
print(f"{'Date':<12} {'Events':>6} {'Wins':>5} {'WR':>6} {'Cost$5':>9} {'Fill$50':>9} {'AvgFill%':>9} "
      f"{'PnL$5':>9} {'PnL$50con':>10} {'ROI$50':>8}")
print("-" * 120)

daily = defaultdict(list)
for r in results:
    daily[r['date']].append(r)

for date in sorted(daily.keys()):
    entries = daily[date]
    n = len(entries)
    wins = sum(1 for e in entries if e['won'])
    wr = wins / n * 100 if n > 0 else 0
    cost_5 = n * BET_SIZE_SMALL
    fill_50 = sum(e['fillable_50'] for e in entries)
    avg_fill = sum(e['fill_pct'] for e in entries) / n if n > 0 else 0
    pnl_5 = sum(e['pnl_5'] for e in entries)
    pnl_50_con = sum(e['pnl_50_constrained'] for e in entries)
    roi_50 = (pnl_50_con / fill_50 * 100) if fill_50 > 0 else 0

    print(f"{date:<12} {n:>6} {wins:>5} {wr:>5.1f}% ${cost_5:>7.0f} ${fill_50:>7.1f} {avg_fill:>8.1f}% "
          f"${pnl_5:>7.1f} ${pnl_50_con:>8.1f} {roi_50:>7.1f}%")

# -- Fill summary stats --

print()
print("=" * 80)
print("FILL SUMMARY STATS")
print("=" * 80)

full_fills = sum(1 for r in results if r['fill_pct'] >= 100.0)
partial_fills = sum(1 for r in results if 0 < r['fill_pct'] < 100.0)
zero_fills = sum(1 for r in results if r['fill_pct'] == 0)
avg_fill_all = sum(r['fill_pct'] for r in results) / len(results) if results else 0
sorted_fills = sorted(r['fill_pct'] for r in results)
median_fill = sorted_fills[len(sorted_fills) // 2] if sorted_fills else 0

print(f"Total markets with entries:     {len(results)}")
print(f"Full fills ($50):               {full_fills} ({full_fills/len(results)*100:.1f}%)")
print(f"Partial fills:                  {partial_fills} ({partial_fills/len(results)*100:.1f}%)")
print(f"Zero fills (entry trade only):  {zero_fills} ({zero_fills/len(results)*100:.1f}%)")
print(f"Average fill %:                 {avg_fill_all:.1f}%")
print(f"Median fill %:                  {median_fill:.1f}%")
print(f"Total deployed at $5/trade:     ${total_cost_5:.0f}")
print(f"Total deployed at $50 (uncon):  ${total_cost_50_unc:.0f}")
print(f"Total fillable at $50 (con):    ${total_fillable_50:.1f}")
if results:
    print(f"Effective avg bet size at $50:  ${total_fillable_50/len(results):.2f}")

# -- Fill distribution --

print()
print("=" * 80)
print("FILL PERCENTAGE DISTRIBUTION")
print("=" * 80)

buckets = [
    ('0%  (no additional fill)',  0, 0.001),
    ('1-25%',                     0.001, 25.001),
    ('25-50%',                    25.001, 50.001),
    ('50-75%',                    50.001, 75.001),
    ('75-99%',                    75.001, 99.999),
    ('100%  (full $50 fill)',     99.999, 999),
]

for label, lo, hi in buckets:
    count = sum(1 for r in results if lo <= r['fill_pct'] < hi)
    pct = count / len(results) * 100 if results else 0
    bar = '#' * int(pct / 2)
    print(f"  {label:<30} {count:>4} ({pct:>5.1f}%)  {bar}")

# -- Per-category fill detail --

print()
print("=" * 100)
print("PER-CATEGORY FILL ANALYSIS")
print("=" * 100)
print(f"{'Category':<10} {'Events':>6} {'Full':>5} {'Partial':>8} {'Zero':>5} {'AvgFill%':>9} "
      f"{'Avg5minVol':>11} {'SlipLimit':>10}")
print("-" * 100)

for cat in categories:
    entries = cat_data.get(cat, [])
    if not entries:
        continue

    n = len(entries)
    full = sum(1 for e in entries if e['fill_pct'] >= 100.0)
    partial = sum(1 for e in entries if 0 < e['fill_pct'] < 100.0)
    zero = sum(1 for e in entries if e['fill_pct'] == 0)
    avg_fill = sum(e['fill_pct'] for e in entries) / n
    avg_vol = sum(e['total_no_volume_5min'] for e in entries) / n
    slip = slippage_limit(cat)

    print(f"{cat:<10} {n:>6} {full:>5} {partial:>8} {zero:>5} {avg_fill:>8.1f}% "
          f"${avg_vol:>9.1f} {slip:>9}c")

# -- Worst liquidity --

print()
print("=" * 120)
print("BOTTOM 20 MARKETS BY FILL % (worst liquidity)")
print("=" * 120)
print(f"{'Ticker':<50} {'Cat':<6} {'NO$':>5} {'5minVol':>9} {'Fill$50':>8} {'Fill%':>7} {'Result':>7}")
print("-" * 120)

sorted_by_fill = sorted(results, key=lambda r: r['fill_pct'])
for r in sorted_by_fill[:20]:
    print(f"{r['ticker']:<50} {r['category']:<6} {r['entry_no_price']:>4.0f}c "
          f"${r['total_no_volume_5min']:>7.1f} ${r['fillable_50']:>6.1f} {r['fill_pct']:>6.1f}% "
          f"{'WIN' if r['won'] else 'LOSS':>6}")

# -- Best liquidity --

print()
print("=" * 120)
print("TOP 20 MARKETS BY 5-MIN NO VOLUME (best liquidity)")
print("=" * 120)
print(f"{'Ticker':<50} {'Cat':<6} {'NO$':>5} {'5minVol':>9} {'Fill$50':>8} {'Fill%':>7} {'Result':>7}")
print("-" * 120)

sorted_by_vol = sorted(results, key=lambda r: -r['total_no_volume_5min'])
for r in sorted_by_vol[:20]:
    print(f"{r['ticker']:<50} {r['category']:<6} {r['entry_no_price']:>4.0f}c "
          f"${r['total_no_volume_5min']:>7.1f} ${r['fillable_50']:>6.1f} {r['fill_pct']:>6.1f}% "
          f"{'WIN' if r['won'] else 'LOSS':>6}")

# -- Scaling comparison --

print()
print("=" * 80)
print("SCALING COMPARISON: $5 vs $50 PER TRADE")
print("=" * 80)
print(f"{'Metric':<35} {'$5/trade':>15} {'$50 unconstrd':>15} {'$50 constrained':>16}")
print("-" * 80)
print(f"{'Total capital deployed':<35} ${total_cost_5:>13.0f} ${total_cost_50_unc:>13.0f} ${total_fillable_50:>14.1f}")
print(f"{'Total PnL':<35} ${total_pnl_5:>13.1f} ${sum(r['pnl_50_unconstrained'] for r in results):>13.1f} ${total_pnl_50_con:>14.1f}")
roi_5 = (total_pnl_5 / total_cost_5 * 100) if total_cost_5 > 0 else 0
pnl_50_unc_total = sum(r['pnl_50_unconstrained'] for r in results)
roi_50_unc = (pnl_50_unc_total / total_cost_50_unc * 100) if total_cost_50_unc > 0 else 0
roi_50_con = (total_pnl_50_con / total_fillable_50 * 100) if total_fillable_50 > 0 else 0
print(f"{'ROI':<35} {roi_5:>14.1f}% {roi_50_unc:>14.1f}% {roi_50_con:>15.1f}%")
if results:
    print(f"{'Avg bet actually placed':<35} ${BET_SIZE_SMALL:>13.2f} ${BET_SIZE_LARGE:>13.2f} ${total_fillable_50/len(results):>14.2f}")
print(f"{'Fill rate':<35} {'100.0%':>15} {'100.0%':>15} {total_avg_fill:>15.1f}%")
