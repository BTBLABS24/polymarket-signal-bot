#!/usr/bin/env python3
"""NBA mention market backtest — price bucket analysis.
Uses mention_markets_cache.json + mention_trade_cache.json.
Goal: find if there's a profitable NO price range for NBA mentions.
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

MARKETS_CACHE = Path("mention_markets_cache.json")
TRADE_CACHE   = Path("mention_trade_cache.json")

BET_DOLLARS = 1.0       # flat $1/bet for clean ROI math
LOOKBACK_DAYS = 90       # last 3 months
MAX_HOURS_BEFORE_CLOSE = 4  # entry window: last 4h before close

def parse_ts(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return float(s)
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.timestamp()
    except:
        return None

def main():
    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()

    # Collect all NBA mention entries
    entries = []
    for ticker, m in markets_all.items():
        t = ticker.upper()
        if 'NBAMENTION' not in t and 'NBAFINALS' not in t:
            continue

        if ticker not in trade_cache or not trade_cache[ticker]:
            continue

        close_ts = parse_ts(m.get('close_time'))
        if not close_ts or close_ts < cutoff_ts:
            continue

        result = m.get('result', '')
        if result not in ('yes', 'no'):
            continue

        raw_trades = sorted(trade_cache[ticker], key=lambda t: t[0])

        for ts, price_raw, count, side in raw_trades:
            yes_price = price_raw / 100.0 if price_raw > 1 else price_raw
            if yes_price < 0.01 or yes_price > 0.99:
                continue

            mins_before = (close_ts - ts) / 60
            if mins_before < 0 or mins_before > MAX_HOURS_BEFORE_CLOSE * 60:
                continue

            no_price = 1 - yes_price
            if no_price < 0.03 or no_price > 0.70:
                continue

            no_win = (result == 'no')
            contracts = int(BET_DOLLARS / no_price)
            if contracts < 1:
                continue

            cost = contracts * no_price
            pnl = contracts * (1 - no_price) if no_win else -cost

            entries.append({
                'ticker': ticker,
                'no_price': no_price,
                'contracts': contracts,
                'cost': cost,
                'no_win': no_win,
                'pnl': pnl,
                'close_ts': close_ts,
                'date': datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%Y-%m-%d'),
                'title': m.get('title', '')[:60],
            })
            break  # first eligible trade only

    print(f"NBA mention markets found: {len(entries)}")
    if not entries:
        print("No NBA mention entries found in cache.")
        return

    # ---- Overall stats ----
    total_n = len(entries)
    total_wins = sum(1 for e in entries if e['no_win'])
    total_cost = sum(e['cost'] for e in entries)
    total_pnl = sum(e['pnl'] for e in entries)
    wr = total_wins / total_n * 100
    roi = total_pnl / total_cost * 100 if total_cost > 0 else 0

    print(f"\n{'='*90}")
    print(f"NBA MENTION MARKETS — Last {LOOKBACK_DAYS} days, ${BET_DOLLARS}/bet, BUY NO")
    print(f"{'='*90}")
    print(f"Total: {total_n} trades, WR={wr:.1f}%, ROI={roi:+.1f}%, PnL=${total_pnl:+.2f}, Cost=${total_cost:.2f}")

    # ---- Price bucket breakdown ----
    buckets = [
        (0.03, 0.05, "3-4c"),
        (0.05, 0.10, "5-9c"),
        (0.10, 0.15, "10-14c"),
        (0.15, 0.20, "15-19c"),
        (0.20, 0.25, "20-24c"),
        (0.25, 0.30, "25-29c"),
        (0.30, 0.40, "30-39c"),
        (0.40, 0.50, "40-49c"),
        (0.50, 0.65, "50-64c"),
    ]

    print(f"\n{'Bucket':>10s}  {'N':>5s}  {'Wins':>5s}  {'WR%':>6s}  {'Cost':>8s}  {'PnL':>9s}  {'ROI%':>7s}")
    print("-" * 60)

    for lo, hi, label in buckets:
        b_entries = [e for e in entries if lo <= e['no_price'] < hi]
        if not b_entries:
            continue
        n = len(b_entries)
        wins = sum(1 for e in b_entries if e['no_win'])
        cost = sum(e['cost'] for e in b_entries)
        pnl = sum(e['pnl'] for e in b_entries)
        b_wr = wins / n * 100
        b_roi = pnl / cost * 100 if cost > 0 else 0
        print(f"{label:>10s}  {n:>5d}  {wins:>5d}  {b_wr:5.1f}%  ${cost:>7.2f}  ${pnl:>+8.2f}  {b_roi:>+6.1f}%")

    # ---- Wider range combos for sweet spot ----
    print(f"\n{'='*90}")
    print("RANGE SEARCH — Best NO price ranges (min 20 trades)")
    print(f"{'='*90}")
    price_points = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.65]
    results = []
    for i, lo in enumerate(price_points):
        for hi in price_points[i+1:]:
            b = [e for e in entries if lo <= e['no_price'] < hi]
            if len(b) < 20:
                continue
            n = len(b)
            wins = sum(1 for e in b if e['no_win'])
            cost = sum(e['cost'] for e in b)
            pnl = sum(e['pnl'] for e in b)
            b_wr = wins / n * 100
            b_roi = pnl / cost * 100 if cost > 0 else 0
            results.append({'lo': lo, 'hi': hi, 'n': n, 'wins': wins,
                            'wr': b_wr, 'roi': b_roi, 'pnl': pnl, 'cost': cost})

    results.sort(key=lambda r: r['roi'], reverse=True)
    print(f"{'Range':>12s}  {'N':>5s}  {'Wins':>5s}  {'WR%':>6s}  {'Cost':>8s}  {'PnL':>9s}  {'ROI%':>7s}")
    print("-" * 65)
    for r in results[:20]:
        label = f"{int(r['lo']*100)}-{int(r['hi']*100)}c"
        print(f"{label:>12s}  {r['n']:>5d}  {r['wins']:>5d}  {r['wr']:5.1f}%  ${r['cost']:>7.2f}  ${r['pnl']:>+8.2f}  {r['roi']:>+6.1f}%")

    # ---- Daily P&L ----
    daily = defaultdict(lambda: {'n': 0, 'wins': 0, 'pnl': 0.0})
    for e in entries:
        d = e['date']
        daily[d]['n'] += 1
        daily[d]['pnl'] += e['pnl']
        if e['no_win']:
            daily[d]['wins'] += 1

    print(f"\n{'='*60}")
    print("DAILY P&L (last 30 days)")
    print(f"{'='*60}")
    recent_cutoff = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    for d in sorted(daily.keys()):
        if d < recent_cutoff:
            continue
        dd = daily[d]
        print(f"  {d}  {dd['n']:>3d} bets  {dd['wins']:>2d}W  ${dd['pnl']:>+7.2f}")

    # ---- Sample trades (recent) ----
    print(f"\n{'='*90}")
    print("RECENT INDIVIDUAL TRADES (last 30 days)")
    print(f"{'='*90}")
    recent = sorted([e for e in entries if e['date'] >= recent_cutoff], key=lambda e: e['close_ts'])
    for e in recent[-50:]:
        outcome = "WIN" if e['no_win'] else "LOSS"
        print(f"  {e['date']}  NO {e['no_price']*100:4.0f}c  {outcome:4s}  ${e['pnl']:>+7.2f}  {e['title']}")

if __name__ == "__main__":
    main()
