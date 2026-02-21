#!/usr/bin/env python3
"""
ALL Mention Markets — BUY NO Strategy with Train/Test Split

Key insight: YES is systematically overpriced on mention markets.
If YES overdelivers at 8% when priced at 27c, then NO at 73c resolves 92% of the time.
Buying NO at 73c when it resolves 92% → edge.

Strategy: BUY NO on mention markets during events.
Equivalent framing: the NO price = (1 - YES price).
If resolved NO: profit = (1 - no_price) per contract
If resolved YES: loss = no_price per contract
"""

import json
import re
from datetime import datetime, timezone
from collections import defaultdict, Counter
from pathlib import Path

TRADE_CACHE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE = Path(__file__).parent / 'mention_markets_cache.json'


def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.timestamp()
    except Exception:
        return None


def categorize(ticker, title=''):
    t = ticker.upper()
    tl = (title or '').lower()
    if 'EARNINGS' in t:
        return 'Earnings'
    if 'TRUMP' in t and 'MENTION' in t:
        return 'Trump'
    if 'MAMDANI' in t:
        return 'Mamdani'
    if 'SECPRESS' in t or 'LEAVITT' in t:
        return 'Press_Briefing'
    if 'MADDOW' in t:
        return 'Maddow'
    if 'SNL' in t:
        return 'SNL'
    if 'ROGAN' in t or 'COOPERMENTION' in t or 'COLBERT' in t or 'KIMMEL' in t:
        return 'Talk_Show'
    # Sports subcategories
    if 'SNFMENTION' in t:
        return 'SNF'
    if 'TNFMENTION' in t:
        return 'TNF'
    if 'NBAMENTION' in t or 'NBAFINALS' in t:
        return 'NBA'
    if 'NFLMENTION' in t or 'SBMENTION' in t:
        return 'NFL'
    if 'NCAAMENTION' in t or 'NCAABMENTION' in t or 'MMMENTION' in t:
        return 'NCAA'
    if 'CFBMENTION' in t or 'GAMEDAYMENTION' in t:
        return 'CFB'
    if 'MLBMENTION' in t:
        return 'MLB'
    if 'FIGHTMENTION' in t:
        return 'Fight'
    if 'WOMENTION' in t or 'olympic' in tl:
        return 'Olympics'
    if any(kw in t for kw in ['FEDMENTION', 'POWELL', 'WALLER', 'ECBMENTION', 'JPOW']):
        return 'Fed'
    if 'GOVERNOR' in t or 'HOCHUL' in t:
        return 'Governor'
    if 'MVEMENTION' in t:
        tl2 = tl
        if any(kw in tl2 for kw in ['tirico', 'collinsworth', 'michaels', 'herbstreit',
                                      'nantz', 'romo', 'buck', 'aikman', 'football',
                                      'basketball', 'announcer', 'commentator']):
            return 'Sports_MVE'
        return 'Other'
    return 'Other'


def main():
    print("=" * 80)
    print("ALL MENTION MARKETS — BUY NO STRATEGY — TRAIN/TEST SPLIT")
    print("=" * 80)

    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    print(f"Total markets in cache: {len(markets_all)}")
    print(f"Total tickers with trade data: {len(trade_cache)}")

    # Build event-level dataset from ALL mention markets with trade data
    events = defaultdict(list)
    for ticker, m in markets_all.items():
        if ticker not in trade_cache:
            continue
        if not trade_cache[ticker]:
            continue
        et = m.get('event_ticker', ticker)
        events[et].append(m)

    # Build trade-level records grouped by event
    event_data = []
    for et, mkts in events.items():
        close_ts = None
        for m in mkts:
            ct = parse_ts(m.get('close_time'))
            if ct:
                close_ts = ct
                break
        if not close_ts:
            continue

        event_trades = []
        for m in mkts:
            ticker = m['ticker']
            result = m.get('result', '')
            resolved_yes = result == 'yes'
            cat = categorize(ticker, m.get('title', ''))
            raw_trades = trade_cache.get(ticker, [])

            for trade in raw_trades:
                ts, price_raw, count, side = trade
                yes_price = price_raw / 100.0 if price_raw > 1 else price_raw
                if yes_price < 0.01 or yes_price > 0.99:
                    continue
                mins_before = (close_ts - ts) / 60
                if mins_before < 0:
                    continue
                no_price = 1 - yes_price

                event_trades.append({
                    'ticker': ticker,
                    'event': et,
                    'cat': cat,
                    'resolved_yes': resolved_yes,
                    'yes_price': yes_price,
                    'no_price': no_price,
                    'count': count,
                    'mins_before': mins_before,
                    'close_ts': close_ts,
                })

        if event_trades:
            event_data.append({
                'event_ticker': et,
                'close_ts': close_ts,
                'trades': event_trades,
            })

    event_data.sort(key=lambda e: e['close_ts'])
    total_events = len(event_data)
    total_trades = sum(len(e['trades']) for e in event_data)

    first_dt = datetime.fromtimestamp(event_data[0]['close_ts'], tz=timezone.utc)
    last_dt = datetime.fromtimestamp(event_data[-1]['close_ts'], tz=timezone.utc)
    print(f"\nEvents with trade data: {total_events}")
    print(f"Total trades: {total_trades:,}")
    print(f"Date range: {first_dt.strftime('%Y-%m-%d')} to {last_dt.strftime('%Y-%m-%d')}"
          f" ({(last_dt - first_dt).days} days)")

    # Chronological 60/40 split
    split_idx = int(total_events * 0.6)
    train_events = event_data[:split_idx]
    test_events = event_data[split_idx:]

    train_end = datetime.fromtimestamp(
        train_events[-1]['close_ts'], tz=timezone.utc).strftime('%Y-%m-%d')
    test_start = datetime.fromtimestamp(
        test_events[0]['close_ts'], tz=timezone.utc).strftime('%Y-%m-%d')

    print(f"\nTrain: {len(train_events)} events (through {train_end})")
    print(f"Test:  {len(test_events)} events (from {test_start})")

    # ================================================================
    # Analysis function
    # ================================================================
    def analyze(events_list, label):
        print(f"\n{'='*80}")
        print(f"{label}")
        print(f"{'='*80}")

        all_trades = []
        for e in events_list:
            all_trades.extend(e['trades'])
        print(f"Total trades: {len(all_trades):,}")

        time_buckets = [
            ('0-30min', 0, 30),
            ('30-60min', 30, 60),
            ('1-2h', 60, 120),
            ('2-4h', 120, 240),
            ('4-12h', 240, 720),
            ('12h+', 720, 999999),
        ]

        # NO price buckets (what we'd pay for NO)
        # YES at 5c → NO at 95c (expensive, small edge)
        # YES at 30c → NO at 70c (cheaper, potentially big edge)
        # We want to buy NO when it's cheap = when YES is high
        # But the edge is: actual NO rate > NO price
        # Reframe: bucket by YES price, compute NO strategy

        yes_price_buckets = [
            ('YES 3-10c', 0.03, 0.10),    # NO costs 90-97c
            ('YES 10-20c', 0.10, 0.20),   # NO costs 80-90c
            ('YES 20-35c', 0.20, 0.35),   # NO costs 65-80c
            ('YES 35-50c', 0.35, 0.50),   # NO costs 50-65c
            ('YES 50-70c', 0.50, 0.70),   # NO costs 30-50c
            ('YES 70-90c', 0.70, 0.90),   # NO costs 10-30c
            ('YES 90-97c', 0.90, 0.97),   # NO costs 3-10c
        ]

        # BUY NO calibration: for each (time, yes_price_bucket),
        # what is actual NO resolution rate vs NO price paid?
        cal = defaultdict(lambda: {'n': 0, 'no_wins': 0, 'sum_no_price': 0,
                                    'pnl': 0})

        # Per-category during-event (0-60min)
        cat_during = defaultdict(lambda: {'n': 0, 'no_wins': 0,
                                           'sum_no_price': 0, 'pnl': 0})
        # Per-category during-event (0-4h for broader window)
        cat_4h = defaultdict(lambda: {'n': 0, 'no_wins': 0,
                                       'sum_no_price': 0, 'pnl': 0})

        for t in all_trades:
            t_label = None
            for tl, lo, hi in time_buckets:
                if lo <= t['mins_before'] < hi:
                    t_label = tl
                    break
            if not t_label:
                continue

            p_label = None
            for pl, lo, hi in yes_price_buckets:
                if lo <= t['yes_price'] < hi:
                    p_label = pl
                    break
            if not p_label:
                continue

            no_win = not t['resolved_yes']
            no_price = t['no_price']
            pnl = (1 - no_price) if no_win else -no_price

            key = (t_label, p_label)
            cal[key]['n'] += 1
            cal[key]['sum_no_price'] += no_price
            cal[key]['pnl'] += pnl
            if no_win:
                cal[key]['no_wins'] += 1

            # Per-category during event
            if t['mins_before'] < 60:
                cat = t['cat']
                cat_during[cat]['n'] += 1
                cat_during[cat]['sum_no_price'] += no_price
                cat_during[cat]['pnl'] += pnl
                if no_win:
                    cat_during[cat]['no_wins'] += 1

            if t['mins_before'] < 240:
                cat = t['cat']
                cat_4h[cat]['n'] += 1
                cat_4h[cat]['sum_no_price'] += no_price
                cat_4h[cat]['pnl'] += pnl
                if no_win:
                    cat_4h[cat]['no_wins'] += 1

        # Print BUY NO calibration table
        print(f"\n  BUY NO calibration: NO win% (edge vs NO price)")
        print(f"  Positive edge = NO resolves more often than price implies\n")
        print(f"{'YES Price':>12s}", end='')
        for tl, _, _ in time_buckets:
            print(f" | {tl:>18s}", end='')
        print()
        print("-" * 135)

        for pl, plo, phi in yes_price_buckets:
            no_price_range = f"NO {(1-phi)*100:.0f}-{(1-plo)*100:.0f}c"
            print(f"{pl:>12s}", end='')
            for tl, _, _ in time_buckets:
                key = (tl, pl)
                d = cal[key]
                if d['n'] >= 20:
                    no_wr = d['no_wins'] / d['n']
                    avg_no = d['sum_no_price'] / d['n']
                    edge = no_wr - avg_no
                    roi = d['pnl'] / d['sum_no_price'] * 100 if d['sum_no_price'] > 0 else 0
                    sign = '+' if edge >= 0 else ''
                    print(f" | {no_wr*100:5.1f}% {sign}{edge*100:+5.1f}c {roi:+5.0f}%", end='')
                elif d['n'] > 0:
                    print(f" |        n={d['n']:>4d}   ", end='')
                else:
                    print(f" |                    ", end='')
            print()

        # Per-category BUY NO during event (0-60min)
        print(f"\n  BUY NO per-category (0-60 min before close):")
        print(f"  {'Category':>15s}  {'Trades':>7s}  {'NO WR%':>7s}  {'AvgNO':>7s}  {'Edge':>7s}  {'ROI%':>7s}  {'PnL':>10s}")
        print(f"  {'-'*75}")

        sorted_cats = sorted(cat_during.keys(),
                             key=lambda c: cat_during[c]['pnl'] / cat_during[c]['sum_no_price']
                             if cat_during[c]['sum_no_price'] > 0 else 0,
                             reverse=True)
        total_d = {'n': 0, 'no_wins': 0, 'sum_no_price': 0, 'pnl': 0}
        for cat in sorted_cats:
            d = cat_during[cat]
            if d['n'] < 10:
                continue
            no_wr = d['no_wins'] / d['n']
            avg_no = d['sum_no_price'] / d['n']
            edge = no_wr - avg_no
            roi = d['pnl'] / d['sum_no_price'] * 100 if d['sum_no_price'] > 0 else 0
            sign = '+' if edge >= 0 else ''
            print(f"  {cat:>15s}  {d['n']:>7d}  {no_wr*100:6.1f}%  {avg_no*100:6.1f}c  {sign}{edge*100:5.1f}c  {roi:>+6.1f}%  ${d['pnl']:>9.2f}")
            total_d['n'] += d['n']
            total_d['no_wins'] += d['no_wins']
            total_d['sum_no_price'] += d['sum_no_price']
            total_d['pnl'] += d['pnl']

        if total_d['n'] > 0:
            no_wr = total_d['no_wins'] / total_d['n']
            avg_no = total_d['sum_no_price'] / total_d['n']
            edge = no_wr - avg_no
            roi = total_d['pnl'] / total_d['sum_no_price'] * 100
            print(f"  {'TOTAL':>15s}  {total_d['n']:>7d}  {no_wr*100:6.1f}%  {avg_no*100:6.1f}c  {'+' if edge>=0 else ''}{edge*100:5.1f}c  {roi:>+6.1f}%  ${total_d['pnl']:>9.2f}")

        # Per-category BUY NO (0-4h)
        print(f"\n  BUY NO per-category (0-4h before close):")
        print(f"  {'Category':>15s}  {'Trades':>7s}  {'NO WR%':>7s}  {'AvgNO':>7s}  {'Edge':>7s}  {'ROI%':>7s}  {'PnL':>10s}")
        print(f"  {'-'*75}")

        sorted_cats_4h = sorted(cat_4h.keys(),
                                key=lambda c: cat_4h[c]['pnl'] / cat_4h[c]['sum_no_price']
                                if cat_4h[c]['sum_no_price'] > 0 else 0,
                                reverse=True)
        total_4h = {'n': 0, 'no_wins': 0, 'sum_no_price': 0, 'pnl': 0}
        for cat in sorted_cats_4h:
            d = cat_4h[cat]
            if d['n'] < 20:
                continue
            no_wr = d['no_wins'] / d['n']
            avg_no = d['sum_no_price'] / d['n']
            edge = no_wr - avg_no
            roi = d['pnl'] / d['sum_no_price'] * 100 if d['sum_no_price'] > 0 else 0
            sign = '+' if edge >= 0 else ''
            print(f"  {cat:>15s}  {d['n']:>7d}  {no_wr*100:6.1f}%  {avg_no*100:6.1f}c  {sign}{edge*100:5.1f}c  {roi:>+6.1f}%  ${d['pnl']:>9.2f}")
            total_4h['n'] += d['n']
            total_4h['no_wins'] += d['no_wins']
            total_4h['sum_no_price'] += d['sum_no_price']
            total_4h['pnl'] += d['pnl']

        if total_4h['n'] > 0:
            no_wr = total_4h['no_wins'] / total_4h['n']
            avg_no = total_4h['sum_no_price'] / total_4h['n']
            edge = no_wr - avg_no
            roi = total_4h['pnl'] / total_4h['sum_no_price'] * 100
            print(f"  {'TOTAL':>15s}  {total_4h['n']:>7d}  {no_wr*100:6.1f}%  {avg_no*100:6.1f}c  {'+' if edge>=0 else ''}{edge*100:5.1f}c  {roi:>+6.1f}%  ${total_4h['pnl']:>9.2f}")

        # Strategy sims: BUY NO with different filters
        print(f"\n  Strategy simulations (BUY NO, $1/contract):")
        print(f"  {'Strategy':>40s}  {'Trades':>7s}  {'WR%':>6s}  {'PnL':>10s}  {'PnL/tr':>8s}  {'ROI%':>7s}")
        print(f"  {'-'*85}")

        strategies = [
            # (name, max_no_price, max_mins, categories_filter)
            ('All <60min, all cats', 0.99, 60, None),
            ('All <60min, NO<80c', 0.80, 60, None),
            ('All <60min, NO<60c', 0.60, 60, None),
            ('All <4h, NO<80c', 0.80, 240, None),
            ('All <4h, NO<60c', 0.60, 240, None),
            ('NFL only <60min', 0.99, 60, {'NFL'}),
            ('NFL only <4h, NO<80c', 0.80, 240, {'NFL'}),
            ('NCAA only <60min', 0.99, 60, {'NCAA'}),
            ('NCAA only <4h, NO<80c', 0.80, 240, {'NCAA'}),
            ('NBA only <60min', 0.99, 60, {'NBA'}),
            ('NBA only <4h, NO<80c', 0.80, 240, {'NBA'}),
            ('Earnings <60min', 0.99, 60, {'Earnings'}),
            ('Earnings <4h, NO<80c', 0.80, 240, {'Earnings'}),
            ('Trump <60min', 0.99, 60, {'Trump'}),
            ('Trump <4h, NO<80c', 0.80, 240, {'Trump'}),
            ('Press_Briefing <60min', 0.99, 60, {'Press_Briefing'}),
            ('Mamdani <60min', 0.99, 60, {'Mamdani'}),
            ('Governor <60min', 0.99, 60, {'Governor'}),
            ('Other <60min', 0.99, 60, {'Other'}),
            ('No-sports <60min, NO<80c', 0.80, 60,
             {'Earnings', 'Trump', 'Press_Briefing', 'Mamdani', 'Governor',
              'Maddow', 'SNL', 'Talk_Show', 'Fed', 'Olympics', 'Other'}),
            ('No-sports <4h, NO<80c', 0.80, 240,
             {'Earnings', 'Trump', 'Press_Briefing', 'Mamdani', 'Governor',
              'Maddow', 'SNL', 'Talk_Show', 'Fed', 'Olympics', 'Other'}),
        ]

        results = {}
        for name, max_no, max_mins, cat_filter in strategies:
            n = wins = pnl = cost = 0
            for t in all_trades:
                if t['mins_before'] >= max_mins:
                    continue
                if t['no_price'] > max_no:
                    continue
                if cat_filter and t['cat'] not in cat_filter:
                    continue
                n += 1
                cost += t['no_price']
                no_win = not t['resolved_yes']
                if no_win:
                    wins += 1
                    pnl += (1 - t['no_price'])
                else:
                    pnl -= t['no_price']
            if n >= 10:
                wr = wins / n * 100
                roi = pnl / cost * 100 if cost > 0 else 0
                print(f"  {name:>40s}  {n:>7d}  {wr:5.1f}%  ${pnl:>9.2f}  ${pnl/n:>7.4f}  {roi:>+6.1f}%")
                results[name] = {'n': n, 'wr': wr, 'pnl': pnl, 'roi': roi}

        return results

    # Run on all three sets
    train_res = analyze(train_events,
                        f"TRAIN ({len(train_events)} events, through {train_end})")
    test_res = analyze(test_events,
                       f"TEST ({len(test_events)} events, from {test_start})")
    all_res = analyze(event_data,
                      f"ALL DATA ({len(event_data)} events)")

    # Comparison
    print(f"\n{'='*80}")
    print("TRAIN vs TEST — KEY STRATEGIES")
    print(f"{'='*80}")
    print(f"  {'Strategy':>40s}  {'Train ROI':>10s}  {'Test ROI':>10s}  {'All ROI':>10s}  {'Verdict':>10s}")
    print(f"  {'-'*90}")

    for name in train_res:
        if name in test_res and name in all_res:
            tr = train_res[name]['roi']
            te = test_res[name]['roi']
            ar = all_res[name]['roi']
            verdict = 'REAL' if tr > 0 and te > 0 else 'OVERFIT' if tr > 0 else 'NO EDGE'
            print(f"  {name:>40s}  {tr:>+9.1f}%  {te:>+9.1f}%  {ar:>+9.1f}%  {verdict:>10s}")

    print(f"\n  REAL = positive ROI in both train AND test")
    print(f"  OVERFIT = positive in train but not test")
    print(f"  NO EDGE = negative in both")
    print(f"\nDone!")


if __name__ == '__main__':
    main()
