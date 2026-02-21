#!/usr/bin/env python3
"""
Sports Mention Market Deep-Dive with Train/Test Split

Previous finding: Sports mention markets showed +30.1c edge buying YES during events.
Question: Is this real or overfit? Test with chronological train/test split.
"""

import requests
import time
import json
import re
import random
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'
TRADE_CACHE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE = Path(__file__).parent / 'mention_markets_cache.json'

SPORTS_SERIES_PREFIXES = [
    'KXNBAMENTION', 'KXNFLMENTION', 'KXNCAAMENTION', 'KXNCAABMENTION',
    'KXSNFMENTION', 'KXTNFMENTION', 'KXFIGHTMENTION', 'KXMMMENTION',
    'KXMLBMENTION', 'KXCFBMENTION', 'KXSBMENTION', 'KXNBAFINALSMENTION',
    'KXGAMEDAYMENTION',
]

MVE_SPORTS_KEYWORDS = [
    'nfl', 'nba', 'football', 'basketball', 'announcer', 'commentator',
    'play-by-play', 'play by play', 'tirico', 'collinsworth', 'michaels',
    'herbstreit', 'nantz', 'romo', 'buck', 'aikman',
]


class KalshiClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'application/json'})
        self.calls = 0

    def _get(self, path, params=None, timeout=15):
        while True:
            try:
                resp = self.session.get(
                    f'{KALSHI_BASE}{path}', params=params, timeout=timeout)
                self.calls += 1
                if resp.status_code == 429:
                    retry = int(resp.headers.get('Retry-After', 3))
                    print(f'    Rate limited, sleeping {retry}s...')
                    time.sleep(retry)
                    continue
                return resp
            except requests.exceptions.RequestException as e:
                print(f'    Request error: {e}')
                time.sleep(2)
                return None

    def get_ticker_trades(self, ticker):
        all_trades = []
        cursor = None
        pages = 0
        while pages < 100:
            params = {'ticker': ticker, 'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            resp = self._get('/markets/trades', params=params)
            if not resp or resp.status_code != 200:
                break
            data = resp.json()
            trades = data.get('trades', [])
            cursor = data.get('cursor', '')
            all_trades.extend(trades)
            pages += 1
            if not trades or not cursor:
                break
            time.sleep(0.05)
        return all_trades


def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.timestamp()
    except Exception:
        return None


def is_sports_ticker(ticker, title=''):
    t = ticker.upper()
    for prefix in SPORTS_SERIES_PREFIXES:
        if t.startswith(prefix.upper()):
            return True
    if 'MVEMENTION' in t:
        tl = title.lower()
        if any(kw in tl for kw in MVE_SPORTS_KEYWORDS):
            return True
    return False


def subcategorize(ticker, title=''):
    t = ticker.upper()
    if 'SNFMENTION' in t or ('tirico' in title.lower() or 'collinsworth' in title.lower()):
        return 'SNF'
    if 'TNFMENTION' in t or ('michaels' in title.lower() or 'herbstreit' in title.lower()):
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
    if 'MVEMENTION' in t:
        return 'MVE_Sports'
    return 'Other_Sports'


def main():
    print("=" * 70)
    print("SPORTS MENTION MARKET DEEP-DIVE — TRAIN/TEST SPLIT")
    print("=" * 70)

    # Load existing caches
    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text()) if TRADE_CACHE.exists() else {}

    # Filter to sports mention markets
    sports_markets = {}
    for ticker, m in markets_all.items():
        if is_sports_ticker(ticker, m.get('title', '')):
            sports_markets[ticker] = m

    print(f"\nSports mention markets: {len(sports_markets)}")
    yes = sum(1 for m in sports_markets.values() if m.get('result') == 'yes')
    no = sum(1 for m in sports_markets.values() if m.get('result') == 'no')
    print(f"Results: YES={yes} ({yes/len(sports_markets)*100:.1f}%), NO={no}")

    # Fetch missing trades
    need_fetch = [t for t in sports_markets if t not in trade_cache]
    print(f"\nNeed to fetch trades for: {len(need_fetch)} tickers "
          f"(already cached: {len(sports_markets) - len(need_fetch)})")

    if need_fetch:
        client = KalshiClient()
        for i, ticker in enumerate(need_fetch):
            trades = client.get_ticker_trades(ticker)
            parsed = []
            for t in trades:
                ts = parse_ts(t.get('created_time'))
                price = t.get('yes_price')
                count = t.get('count', 1)
                taker_side = t.get('taker_side', '')
                if ts and price is not None:
                    try:
                        parsed.append([ts, float(price), int(count), taker_side])
                    except (ValueError, TypeError):
                        pass
            trade_cache[ticker] = parsed

            if (i + 1) % 50 == 0:
                TRADE_CACHE.write_text(json.dumps(trade_cache))
                print(f"  {i+1}/{len(need_fetch)} fetched ({client.calls} API calls)")
            time.sleep(0.05)

        TRADE_CACHE.write_text(json.dumps(trade_cache))
        print(f"  Done: {len(need_fetch)} tickers ({client.calls} API calls)")

    # ================================================================
    # Build event-level dataset
    # ================================================================
    print(f"\n{'='*70}")
    print("BUILDING EVENT-LEVEL DATASET")
    print(f"{'='*70}")

    # Group markets by event
    events = defaultdict(list)
    for ticker, m in sports_markets.items():
        et = m.get('event_ticker', ticker)
        events[et].append(m)

    # For each event, compute: close_time, per-market trades, results
    event_data = []
    for et, mkts in events.items():
        # Get event close time (same for all markets in event)
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
            subcat = subcategorize(ticker, m.get('title', ''))
            raw_trades = trade_cache.get(ticker, [])

            for trade in raw_trades:
                ts, price_raw, count, side = trade
                price = price_raw / 100.0 if price_raw > 1 else price_raw
                if price < 0.01 or price > 0.99:
                    continue
                mins_before = (close_ts - ts) / 60
                if mins_before < 0:
                    continue
                event_trades.append({
                    'ticker': ticker,
                    'event': et,
                    'subcat': subcat,
                    'resolved_yes': resolved_yes,
                    'price': price,
                    'count': count,
                    'side': side,
                    'mins_before': mins_before,
                    'close_ts': close_ts,
                    'trade_ts': ts,
                })

        if event_trades:
            event_data.append({
                'event_ticker': et,
                'close_ts': close_ts,
                'n_markets': len(mkts),
                'n_yes': sum(1 for m in mkts if m.get('result') == 'yes'),
                'n_no': sum(1 for m in mkts if m.get('result') == 'no'),
                'trades': event_trades,
                'subcat': event_trades[0]['subcat'],
            })

    # Sort events by close time
    event_data.sort(key=lambda e: e['close_ts'])

    total_events = len(event_data)
    total_trades = sum(len(e['trades']) for e in event_data)
    print(f"Events with trade data: {total_events}")
    print(f"Total trades: {total_trades}")

    if total_events < 10:
        print("Not enough events for train/test split!")
        return

    # Date range
    first = datetime.fromtimestamp(event_data[0]['close_ts'], tz=timezone.utc)
    last = datetime.fromtimestamp(event_data[-1]['close_ts'], tz=timezone.utc)
    print(f"Date range: {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")

    # Subcategory breakdown
    subcat_counts = defaultdict(int)
    for e in event_data:
        subcat_counts[e['subcat']] += 1
    print(f"\nSubcategory breakdown:")
    for sc, c in sorted(subcat_counts.items(), key=lambda x: -x[1]):
        print(f"  {sc:15s}: {c} events")

    # ================================================================
    # CHRONOLOGICAL TRAIN/TEST SPLIT (60/40)
    # ================================================================
    split_idx = int(total_events * 0.6)
    train_events = event_data[:split_idx]
    test_events = event_data[split_idx:]

    train_split_date = datetime.fromtimestamp(
        train_events[-1]['close_ts'], tz=timezone.utc).strftime('%Y-%m-%d')
    test_start_date = datetime.fromtimestamp(
        test_events[0]['close_ts'], tz=timezone.utc).strftime('%Y-%m-%d')

    print(f"\n{'='*70}")
    print(f"CHRONOLOGICAL SPLIT")
    print(f"{'='*70}")
    print(f"Train: {len(train_events)} events (through {train_split_date})")
    print(f"Test:  {len(test_events)} events (from {test_start_date})")

    # ================================================================
    # Calibration function
    # ================================================================
    def analyze_split(events_list, label):
        print(f"\n{'='*70}")
        print(f"{label}")
        print(f"{'='*70}")

        all_trades = []
        for e in events_list:
            all_trades.extend(e['trades'])
        print(f"Total trades: {len(all_trades):,}")

        # Time buckets
        time_buckets = [
            ('0-30min', 0, 30),
            ('30-60min', 30, 60),
            ('1-2h', 60, 120),
            ('2-4h', 120, 240),
            ('4-12h', 240, 720),
            ('12h+', 720, 999999),
        ]

        price_buckets = [
            ('3-15c', 0.03, 0.15),
            ('15-30c', 0.15, 0.30),
            ('30-50c', 0.30, 0.50),
            ('50-70c', 0.50, 0.70),
            ('70-90c', 0.70, 0.90),
            ('90-97c', 0.90, 0.97),
        ]

        # Calibration grid
        cal = defaultdict(lambda: {'yes': 0, 'no': 0, 'n': 0, 'sum_price': 0})
        # Per-subcat
        subcat_cal = defaultdict(lambda: {'yes': 0, 'no': 0, 'n': 0,
                                           'sum_price': 0, 'pnl': 0})

        for t in all_trades:
            # Find time bucket
            t_label = None
            for tl, lo, hi in time_buckets:
                if lo <= t['mins_before'] < hi:
                    t_label = tl
                    break
            if not t_label:
                continue

            # Find price bucket
            p_label = None
            for pl, lo, hi in price_buckets:
                if lo <= t['price'] < hi:
                    p_label = pl
                    break
            if not p_label:
                continue

            key = (t_label, p_label)
            cal[key]['n'] += 1
            cal[key]['sum_price'] += t['price']
            if t['resolved_yes']:
                cal[key]['yes'] += 1
            else:
                cal[key]['no'] += 1

            # Per-subcat (during event only: 0-60min)
            if t['mins_before'] < 60:
                sc = t['subcat']
                subcat_cal[sc]['n'] += 1
                subcat_cal[sc]['sum_price'] += t['price']
                if t['resolved_yes']:
                    subcat_cal[sc]['yes'] += 1
                    subcat_cal[sc]['pnl'] += (1 - t['price'])
                else:
                    subcat_cal[sc]['no'] += 1
                    subcat_cal[sc]['pnl'] -= t['price']

        # Print calibration table
        print(f"\n{'Price':>10s}", end='')
        for tl, _, _ in time_buckets:
            print(f" | {tl:>14s}", end='')
        print()
        print("-" * 110)

        for pl, plo, phi in price_buckets:
            print(f"{pl:>10s}", end='')
            implied = (plo + phi) / 2
            for tl, _, _ in time_buckets:
                key = (tl, pl)
                d = cal[key]
                if d['n'] >= 10:
                    actual = d['yes'] / d['n']
                    avg_price = d['sum_price'] / d['n']
                    edge = actual - avg_price
                    sign = '+' if edge >= 0 else ''
                    print(f" | {actual*100:4.1f}% n={d['n']:>5d} {sign}{edge*100:.0f}c", end='')
                elif d['n'] > 0:
                    print(f" |     n={d['n']:>5d}    ", end='')
                else:
                    print(f" |                ", end='')
            print()

        # During-event edge by price
        print(f"\n  During-event (0-60min) edge by price:")
        print(f"  {'Price':>10s}  {'Implied':>8s}  {'Actual':>8s}  {'Edge':>8s}  {'Trades':>7s}  {'PnL/trade':>10s}  {'ROI':>8s}")
        print(f"  {'-'*70}")
        total_during = {'n': 0, 'yes': 0, 'pnl': 0, 'cost': 0}
        for pl, plo, phi in price_buckets:
            n = 0
            y = 0
            sp = 0
            for tl in ['0-30min', '30-60min']:
                key = (tl, pl)
                d = cal[key]
                n += d['n']
                y += d['yes']
                sp += d['sum_price']
            if n >= 5:
                actual = y / n
                avg_price = sp / n
                edge = actual - avg_price
                pnl_per = (actual * (1 - avg_price)) - ((1 - actual) * avg_price)
                roi = pnl_per / avg_price * 100 if avg_price > 0 else 0
                total_during['n'] += n
                total_during['yes'] += y
                total_during['cost'] += sp
                total_during['pnl'] += y * (1 - avg_price) - (n - y) * avg_price
                sign = '+' if edge >= 0 else ''
                print(f"  {pl:>10s}  {avg_price*100:7.1f}c  {actual*100:7.1f}%  {sign}{edge*100:6.1f}c  {n:>7d}  ${pnl_per:>9.4f}  {roi:>7.1f}%")

        if total_during['n'] > 0:
            avg_p = total_during['cost'] / total_during['n']
            actual_wr = total_during['yes'] / total_during['n']
            edge_all = actual_wr - avg_p
            pnl_all = total_during['pnl']
            roi_all = pnl_all / total_during['cost'] * 100
            print(f"  {'TOTAL':>10s}  {avg_p*100:7.1f}c  {actual_wr*100:7.1f}%  {'+' if edge_all>=0 else ''}{edge_all*100:6.1f}c  {total_during['n']:>7d}  ${pnl_all/total_during['n']:>9.4f}  {roi_all:>7.1f}%")

        # Per-subcat edge during events
        print(f"\n  Per-subcategory (0-60min):")
        print(f"  {'Subcat':>15s}  {'Trades':>7s}  {'YES%':>6s}  {'AvgPr':>7s}  {'Edge':>7s}  {'ROI%':>7s}  {'TotalPnL':>10s}")
        print(f"  {'-'*70}")
        for sc in sorted(subcat_cal.keys(), key=lambda s: -subcat_cal[s]['n']):
            d = subcat_cal[sc]
            if d['n'] < 5:
                continue
            actual = d['yes'] / d['n']
            avg_p = d['sum_price'] / d['n']
            edge = actual - avg_p
            roi = d['pnl'] / d['sum_price'] * 100 if d['sum_price'] > 0 else 0
            print(f"  {sc:>15s}  {d['n']:>7d}  {actual*100:5.1f}%  {avg_p*100:6.1f}c  {'+' if edge>=0 else ''}{edge*100:5.1f}c  {roi:>6.1f}%  ${d['pnl']:>9.2f}")

        # Strategy simulation: buy YES on all during-event trades
        strategies = [
            ('All <50c, 0-60min', 0.50, 60),
            ('All <30c, 0-60min', 0.30, 60),
            ('All <50c, 0-30min', 0.50, 30),
            ('All <70c, 0-60min', 0.70, 60),
        ]

        print(f"\n  Strategy simulations ($1/contract):")
        print(f"  {'Strategy':>25s}  {'Trades':>7s}  {'WR%':>6s}  {'TotalPnL':>10s}  {'PnL/trade':>10s}  {'ROI%':>7s}")
        print(f"  {'-'*75}")
        for name, max_price, max_mins in strategies:
            n = 0
            wins = 0
            pnl = 0
            cost = 0
            for t in all_trades:
                if t['price'] < max_price and t['mins_before'] < max_mins:
                    n += 1
                    cost += t['price']
                    if t['resolved_yes']:
                        wins += 1
                        pnl += (1 - t['price'])
                    else:
                        pnl -= t['price']
            if n > 0:
                wr = wins / n * 100
                roi = pnl / cost * 100 if cost > 0 else 0
                print(f"  {name:>25s}  {n:>7d}  {wr:5.1f}%  ${pnl:>9.2f}  ${pnl/n:>9.4f}  {roi:>6.1f}%")

        return {
            'total_during': total_during,
            'subcat_cal': dict(subcat_cal),
        }

    # ================================================================
    # Run analysis on both splits
    # ================================================================
    train_result = analyze_split(train_events, f"TRAIN SET ({len(train_events)} events, through {train_split_date})")
    test_result = analyze_split(test_events, f"TEST SET ({len(test_events)} events, from {test_start_date})")
    all_result = analyze_split(event_data, f"ALL DATA ({len(event_data)} events)")

    # ================================================================
    # Summary comparison
    # ================================================================
    print(f"\n{'='*70}")
    print("TRAIN vs TEST COMPARISON")
    print(f"{'='*70}")

    td_train = train_result['total_during']
    td_test = test_result['total_during']
    td_all = all_result['total_during']

    for label, td in [('Train', td_train), ('Test', td_test), ('All', td_all)]:
        if td['n'] > 0:
            wr = td['yes'] / td['n'] * 100
            avg_p = td['cost'] / td['n'] * 100
            edge = wr - avg_p
            roi = td['pnl'] / td['cost'] * 100 if td['cost'] > 0 else 0
            print(f"  {label:6s}: {td['n']:>6d} trades, WR={wr:.1f}%, AvgPrice={avg_p:.1f}c, "
                  f"Edge={'+' if edge>=0 else ''}{edge:.1f}c, ROI={roi:.1f}%, PnL=${td['pnl']:.2f}")

    print(f"\n  If train edge holds in test → real signal")
    print(f"  If train edge disappears in test → overfit")
    print(f"\nDone!")


if __name__ == '__main__':
    main()
