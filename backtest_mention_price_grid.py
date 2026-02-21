#!/usr/bin/env python3
"""
Price Range Grid Search — find optimal NO price window for BUY NO strategy.
Tests every combination of min/max NO price to find the sweet spot.
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


def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.timestamp()
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
    print("PRICE RANGE GRID SEARCH — BUY NO @ $3/bet, last 21 days")
    print("=" * 80)

    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()

    # Build all eligible first-trades per ticker (one entry per market)
    all_entries = []
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
            # Store ALL trades with no_price, filter later by range
            all_entries.append({
                'ticker': ticker,
                'no_price': no_price,
                'no_win': not resolved_yes,
                'cat': cat,
                'close_ts': close_ts,
                'trade_ts': ts,
            })
            break  # first eligible only

    print(f"Total eligible entries (no price filter): {len(all_entries)}")

    # ================================================================
    # Grid search: test every min/max NO price combo
    # ================================================================
    price_points = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
                    0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]

    print(f"\n{'='*80}")
    print("FIXED RANGES — ROI% and PnL for each NO price window")
    print(f"{'='*80}")
    print(f"{'Range':>15s}  {'Trades':>7s}  {'WR%':>6s}  {'ROI%':>7s}  {'PnL':>10s}  {'$/day':>8s}  {'AvgNO':>6s}")
    print(f"{'-'*70}")

    results = []
    for i, lo in enumerate(price_points):
        for hi in price_points[i+1:]:
            n = wins = cost = pnl = 0
            for e in all_entries:
                if e['no_price'] < lo or e['no_price'] > hi:
                    continue
                contracts = int(BET_DOLLARS / e['no_price'])
                if contracts < 1:
                    continue
                c = contracts * e['no_price']
                n += 1
                cost += c
                if e['no_win']:
                    wins += 1
                    pnl += contracts * (1 - e['no_price'])
                else:
                    pnl -= c

            if n >= 30:
                wr = wins / n * 100
                roi = pnl / cost * 100 if cost > 0 else 0
                avg_no = cost / n / (BET_DOLLARS / (cost / n)) if n > 0 else 0
                results.append({
                    'lo': lo, 'hi': hi, 'n': n, 'wins': wins,
                    'wr': wr, 'roi': roi, 'pnl': pnl, 'cost': cost,
                })

    # Sort by ROI
    results.sort(key=lambda r: r['roi'], reverse=True)

    for r in results[:40]:
        avg_no_price = r['cost'] / r['n'] / (int(BET_DOLLARS / (r['cost'] / r['n'])) or 1)
        pnl_day = r['pnl'] / LOOKBACK_DAYS
        label = f"NO {r['lo']*100:.0f}-{r['hi']*100:.0f}c"
        print(f"{label:>15s}  {r['n']:>7d}  {r['wr']:5.1f}%  {r['roi']:>+6.1f}%  ${r['pnl']:>+9.2f}  ${pnl_day:>+7.2f}  {r['cost']/r['n']:>5.2f}")

    # ================================================================
    # Heatmap: ROI by (min_no, max_no)
    # ================================================================
    print(f"\n{'='*80}")
    print("ROI% HEATMAP — rows=min NO price, cols=max NO price")
    print("(blank = <30 trades)")
    print(f"{'='*80}")

    # Build lookup
    roi_map = {}
    n_map = {}
    for r in results:
        roi_map[(r['lo'], r['hi'])] = r['roi']
        n_map[(r['lo'], r['hi'])] = r['n']

    col_points = [p for p in price_points if p >= 0.20]
    row_points = [p for p in price_points if p <= 0.60]

    hdr = "Min/Max"
    print(f"{hdr:>8s}", end='')
    for hi in col_points:
        print(f"  {hi*100:4.0f}c", end='')
    print()
    print("-" * (8 + len(col_points) * 7))

    for lo in row_points:
        print(f"{lo*100:>6.0f}c ", end='')
        for hi in col_points:
            if hi <= lo:
                print(f"{'':>6s} ", end='')
            elif (lo, hi) in roi_map:
                roi = roi_map[(lo, hi)]
                n = n_map[(lo, hi)]
                if n >= 30:
                    print(f"{roi:>+5.0f}% ", end='')
                else:
                    print(f"{'':>6s} ", end='')
            else:
                print(f"{'':>6s} ", end='')
        print()

    # ================================================================
    # PnL heatmap
    # ================================================================
    print(f"\n{'='*80}")
    print("DAILY PnL ($) HEATMAP — rows=min NO price, cols=max NO price")
    print(f"{'='*80}")

    pnl_map = {}
    for r in results:
        pnl_map[(r['lo'], r['hi'])] = r['pnl'] / LOOKBACK_DAYS

    hdr2 = "Min/Max"
    print(f"{hdr2:>8s}", end='')
    for hi in col_points:
        print(f"  {hi*100:5.0f}c", end='')
    print()
    print("-" * (8 + len(col_points) * 8))

    for lo in row_points:
        print(f"{lo*100:>6.0f}c ", end='')
        for hi in col_points:
            if hi <= lo:
                print(f"{'':>7s} ", end='')
            elif (lo, hi) in pnl_map and n_map.get((lo, hi), 0) >= 30:
                d = pnl_map[(lo, hi)]
                print(f"${d:>+5.0f}  ", end='')
            else:
                print(f"{'':>7s} ", end='')
        print()

    # ================================================================
    # Per 5c bucket: standalone performance
    # ================================================================
    print(f"\n{'='*80}")
    print("PER 5c BUCKET — standalone performance")
    print(f"{'='*80}")
    print(f"{'Bucket':>12s}  {'Trades':>7s}  {'WR%':>6s}  {'ROI%':>7s}  {'PnL':>10s}  {'$/day':>8s}")
    print(f"{'-'*60}")

    buckets = [(i/100, (i+5)/100) for i in range(5, 96, 5)]
    for lo, hi in buckets:
        n = wins = cost = pnl = 0
        for e in all_entries:
            if e['no_price'] < lo or e['no_price'] >= hi:
                continue
            contracts = int(BET_DOLLARS / e['no_price'])
            if contracts < 1:
                continue
            c = contracts * e['no_price']
            n += 1
            cost += c
            if e['no_win']:
                wins += 1
                pnl += contracts * (1 - e['no_price'])
            else:
                pnl -= c

        if n >= 5:
            wr = wins / n * 100
            roi = pnl / cost * 100 if cost > 0 else 0
            label = f"NO {lo*100:.0f}-{hi*100:.0f}c"
            print(f"{label:>12s}  {n:>7d}  {wr:5.1f}%  {roi:>+6.1f}%  ${pnl:>+9.2f}  ${pnl/LOOKBACK_DAYS:>+7.2f}")

    print(f"\nDone!")


if __name__ == '__main__':
    main()
