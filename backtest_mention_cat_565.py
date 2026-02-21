#!/usr/bin/env python3
"""
Per-category breakdown for NO 5-65c range, last 21 days, $3/bet
"""

import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

TRADE_CACHE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE = Path(__file__).parent / 'mention_markets_cache.json'

BET_DOLLARS = 3.00
MAX_HOURS_BEFORE_CLOSE = 4
LOOKBACK_DAYS = 21
MIN_NO = 0.05
MAX_NO = 0.65


def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def categorize(ticker):
    t = ticker.upper()
    if 'EARNINGS' in t: return 'Earnings'
    if 'TRUMP' in t and 'MENTION' in t: return 'Trump'
    if 'MAMDANI' in t: return 'Mamdani'
    if 'SECPRESS' in t or 'LEAVITT' in t: return 'Press_Briefing'
    if 'MADDOW' in t: return 'Maddow'
    if 'SNL' in t: return 'SNL'
    if 'ROGAN' in t or 'COOPERMENTION' in t or 'COLBERT' in t or 'KIMMEL' in t: return 'Talk_Show'
    if 'SNFMENTION' in t: return 'SNF'
    if 'TNFMENTION' in t: return 'TNF'
    if 'NBAMENTION' in t or 'NBAFINALS' in t: return 'NBA'
    if 'NFLMENTION' in t or 'SBMENTION' in t: return 'NFL'
    if 'NCAAMENTION' in t or 'NCAABMENTION' in t or 'MMMENTION' in t: return 'NCAA'
    if 'CFBMENTION' in t or 'GAMEDAYMENTION' in t: return 'CFB'
    if 'MLBMENTION' in t: return 'MLB'
    if 'FIGHTMENTION' in t: return 'Fight'
    if any(kw in t for kw in ['FEDMENTION', 'POWELL', 'WALLER', 'ECBMENTION', 'JPOW']): return 'Fed'
    if 'GOVERNOR' in t or 'HOCHUL' in t: return 'Governor'
    return 'Other'


def main():
    print("=" * 80)
    print(f"PER-CATEGORY BREAKDOWN — NO {MIN_NO*100:.0f}-{MAX_NO*100:.0f}c, last {LOOKBACK_DAYS}d, ${BET_DOLLARS}/bet")
    print("=" * 80)

    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()

    # Build one entry per ticker (first eligible trade)
    trades = []
    for ticker, m in markets_all.items():
        if ticker not in trade_cache or not trade_cache[ticker]:
            continue
        close_ts = parse_ts(m.get('close_time'))
        if not close_ts or close_ts < cutoff_ts:
            continue
        result = m.get('result', '')
        if result not in ('yes', 'no'):
            continue

        resolved_yes = result == 'yes'
        cat = categorize(ticker)
        raw_trades = sorted(trade_cache[ticker], key=lambda t: t[0])

        for ts, price_raw, count, side in raw_trades:
            yes_price = price_raw / 100.0 if price_raw > 1 else price_raw
            if yes_price < 0.01 or yes_price > 0.99:
                continue
            mins_before = (close_ts - ts) / 60
            if mins_before < 0 or mins_before > MAX_HOURS_BEFORE_CLOSE * 60:
                continue
            no_price = 1 - yes_price
            if no_price < MIN_NO or no_price > MAX_NO:
                continue
            contracts = int(BET_DOLLARS / no_price)
            if contracts < 1:
                continue

            cost = contracts * no_price
            no_win = not resolved_yes
            pnl = contracts * (1 - no_price) if no_win else -cost
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)

            trades.append({
                'ticker': ticker,
                'cat': cat,
                'no_price': no_price,
                'contracts': contracts,
                'cost': cost,
                'no_win': no_win,
                'pnl': pnl,
                'date': close_dt.strftime('%Y-%m-%d'),
                'close_ts': close_ts,
            })
            break

    print(f"Total trades: {len(trades)}")

    # Per-category stats
    cat_stats = defaultdict(lambda: {'n': 0, 'wins': 0, 'cost': 0, 'pnl': 0,
                                      'prices': [], 'daily_pnl': defaultdict(float)})
    for t in trades:
        c = t['cat']
        cat_stats[c]['n'] += 1
        cat_stats[c]['cost'] += t['cost']
        cat_stats[c]['pnl'] += t['pnl']
        cat_stats[c]['prices'].append(t['no_price'])
        cat_stats[c]['daily_pnl'][t['date']] += t['pnl']
        if t['no_win']:
            cat_stats[c]['wins'] += 1

    print(f"\n{'Category':>15s}  {'Trades':>7s}  {'Wins':>5s}  {'WR%':>6s}  {'AvgNO':>6s}  {'Cost':>8s}  {'PnL':>9s}  {'ROI%':>7s}  {'$/day':>7s}  {'WinDays':>8s}")
    print(f"{'-'*95}")

    sorted_cats = sorted(cat_stats.keys(),
                         key=lambda c: cat_stats[c]['pnl'] / cat_stats[c]['cost'] * 100
                         if cat_stats[c]['cost'] > 0 else 0,
                         reverse=True)

    total = {'n': 0, 'wins': 0, 'cost': 0, 'pnl': 0, 'daily_pnl': defaultdict(float)}
    for cat in sorted_cats:
        d = cat_stats[cat]
        if d['n'] < 3:
            continue
        wr = d['wins'] / d['n'] * 100
        roi = d['pnl'] / d['cost'] * 100 if d['cost'] > 0 else 0
        avg_no = sum(d['prices']) / len(d['prices'])
        pnl_day = d['pnl'] / LOOKBACK_DAYS
        win_days = sum(1 for v in d['daily_pnl'].values() if v > 0)
        active_days = len(d['daily_pnl'])
        print(f"{cat:>15s}  {d['n']:>7d}  {d['wins']:>5d}  {wr:5.1f}%  {avg_no*100:5.1f}c  ${d['cost']:>7.2f}  ${d['pnl']:>+8.2f}  {roi:>+6.1f}%  ${pnl_day:>+6.2f}  {win_days}/{active_days}")

        total['n'] += d['n']
        total['wins'] += d['wins']
        total['cost'] += d['cost']
        total['pnl'] += d['pnl']
        for date, p in d['daily_pnl'].items():
            total['daily_pnl'][date] += p

    wr = total['wins'] / total['n'] * 100
    roi = total['pnl'] / total['cost'] * 100
    pnl_day = total['pnl'] / LOOKBACK_DAYS
    win_days = sum(1 for v in total['daily_pnl'].values() if v > 0)
    active_days = len(total['daily_pnl'])
    print(f"{'-'*95}")
    print(f"{'TOTAL':>15s}  {total['n']:>7d}  {total['wins']:>5d}  {wr:5.1f}%  {'':>6s}  ${total['cost']:>7.2f}  ${total['pnl']:>+8.2f}  {roi:>+6.1f}%  ${pnl_day:>+6.2f}  {win_days}/{active_days}")

    # Now show what happens if we DROP the worst categories
    print(f"\n{'='*80}")
    print("CATEGORY EXCLUSION TEST — what if we drop the worst categories?")
    print(f"{'='*80}")

    # Sort by ROI ascending (worst first)
    worst_first = sorted(cat_stats.keys(),
                         key=lambda c: cat_stats[c]['pnl'] / cat_stats[c]['cost'] * 100
                         if cat_stats[c]['cost'] > 0 else -999)

    excluded = set()
    print(f"\n{'Excluding':>25s}  {'Trades':>7s}  {'Cost':>8s}  {'PnL':>9s}  {'ROI%':>7s}  {'$/day':>7s}")
    print(f"{'-'*70}")

    # Baseline
    print(f"{'(none)':>25s}  {total['n']:>7d}  ${total['cost']:>7.2f}  ${total['pnl']:>+8.2f}  {roi:>+6.1f}%  ${pnl_day:>+6.2f}")

    for cat in worst_first:
        d = cat_stats[cat]
        if d['n'] < 3:
            continue
        excluded.add(cat)
        rem_n = sum(cat_stats[c]['n'] for c in cat_stats if c not in excluded and cat_stats[c]['n'] >= 3)
        rem_cost = sum(cat_stats[c]['cost'] for c in cat_stats if c not in excluded and cat_stats[c]['n'] >= 3)
        rem_pnl = sum(cat_stats[c]['pnl'] for c in cat_stats if c not in excluded and cat_stats[c]['n'] >= 3)
        if rem_cost > 0 and rem_n > 0:
            rem_roi = rem_pnl / rem_cost * 100
            rem_day = rem_pnl / LOOKBACK_DAYS
            cat_roi = d['pnl'] / d['cost'] * 100 if d['cost'] > 0 else 0
            label = f"- {cat} ({cat_roi:+.0f}%)"
            print(f"{label:>25s}  {rem_n:>7d}  ${rem_cost:>7.2f}  ${rem_pnl:>+8.2f}  {rem_roi:>+6.1f}%  ${rem_day:>+6.2f}")

    print(f"\nDone!")


if __name__ == '__main__':
    main()
