#!/usr/bin/env python3
"""
Kalshi Mention Market Calibration Backtest

Hypothesis: Mention markets ("What will X say during Y?") systematically
underprice YES outcomes during live events.

Approach: For every trade on a settled mention market, bucket by:
  1. Time before close (during event vs pre-event)
  2. YES price (implied probability)
Then compare actual YES resolution rate to implied probability.
If actual > implied, there's an edge buying YES.
"""

import requests
import time
import json
import re
import csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'
CACHE_FILE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE_FILE = Path(__file__).parent / 'mention_markets_cache.json'


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

    def get_mention_series(self):
        """Get all series tickers that contain 'mention' (case-insensitive)."""
        resp = self._get('/series')
        if not resp or resp.status_code != 200:
            return []
        data = resp.json()
        series_list = data.get('series', [])
        mention = [s['ticker'] for s in series_list
                   if 'mention' in s.get('ticker', '').lower()]
        return mention

    def get_markets_by_series(self, series_ticker):
        """Get all markets for a given series_ticker (paginated)."""
        all_markets = []
        cursor = None
        pages = 0
        while pages < 50:
            params = {'series_ticker': series_ticker, 'limit': 200}
            if cursor:
                params['cursor'] = cursor
            resp = self._get('/markets', params=params)
            if not resp or resp.status_code != 200:
                break
            data = resp.json()
            markets = data.get('markets', [])
            cursor = data.get('cursor', '')
            all_markets.extend(markets)
            pages += 1
            if not markets or not cursor:
                break
            time.sleep(0.1)
        return all_markets

    def get_ticker_trades(self, ticker, min_ts=None, max_ts=None):
        """Fetch all trades for a ticker."""
        all_trades = []
        cursor = None
        pages = 0
        while pages < 100:
            params = {'ticker': ticker, 'limit': 1000}
            if min_ts:
                params['min_ts'] = int(min_ts)
            if max_ts:
                params['max_ts'] = int(max_ts)
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
    """Parse Kalshi timestamp string to unix seconds."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return dt.timestamp()
    except Exception:
        return None


def categorize_mention(ticker, title):
    """Categorize a mention market by show/event type."""
    t = ticker.upper()
    tl = (title or '').lower()
    if 'EARNINGS' in t:
        return 'Earnings'
    if 'TRUMP' in t:
        return 'Trump'
    if 'MAMDANI' in t:
        return 'Mamdani'
    if 'SECPRESS' in t or 'LEAVITT' in t:
        return 'Press_Briefing'
    if 'MADDOW' in t:
        return 'Maddow'
    if 'SNL' in t:
        return 'SNL'
    if 'ROGAN' in t or 'COOPER' in t or 'COLBERT' in t or 'KIMMEL' in t:
        return 'Talk_Show'
    if 'NBA' in t or 'NFL' in t or 'MLB' in t or 'SNF' in t or 'TNF' in t or 'CFB' in t:
        return 'Sports'
    if 'WOMENTION' in t or 'olympic' in tl:
        return 'Olympics'
    if any(kw in t for kw in ['FED', 'POWELL', 'WALLER', 'ECB']):
        return 'Fed'
    if 'GOVERNOR' in t or 'HOCHUL' in t:
        return 'Governor'
    return 'Other'


def main():
    client = KalshiClient()

    print("=" * 70)
    print("KALSHI MENTION MARKET CALIBRATION BACKTEST")
    print("=" * 70)

    # ================================================================
    # PHASE 1: Discover mention markets
    # ================================================================
    print("\nPHASE 1: Discovering settled mention markets...")

    # Try loading from cache first
    markets_cache = {}
    if MARKETS_CACHE_FILE.exists():
        try:
            markets_cache = json.loads(MARKETS_CACHE_FILE.read_text())
            print(f"  Loaded {len(markets_cache)} cached mention markets")
        except Exception:
            pass

    if not markets_cache:
        # Discover all mention series, then fetch markets per series
        mention_series = client.get_mention_series()
        print(f"  Found {len(mention_series)} mention series")
        for i, series in enumerate(mention_series):
            mkts = client.get_markets_by_series(series)
            settled = [m for m in mkts
                       if m.get('status') in ('settled', 'finalized')]
            for m in settled:
                markets_cache[m['ticker']] = m
            if (i + 1) % 25 == 0:
                print(f"    Series {i+1}/{len(mention_series)}: "
                      f"{len(markets_cache)} settled markets so far "
                      f"({client.calls} API calls)")
                # Incremental save
                MARKETS_CACHE_FILE.write_text(
                    json.dumps(markets_cache, default=str))
        print(f"  Found {len(markets_cache)} settled mention markets "
              f"({client.calls} API calls)")
        MARKETS_CACHE_FILE.write_text(json.dumps(markets_cache, default=str))
        print(f"  Saved to {MARKETS_CACHE_FILE}")

    # Parse markets
    markets = list(markets_cache.values())

    # Group by event
    events = defaultdict(list)
    for m in markets:
        et = m.get('event_ticker', m.get('ticker', ''))
        events[et].append(m)

    # Stats
    total_yes = sum(1 for m in markets if m.get('result') == 'yes')
    total_no = sum(1 for m in markets if m.get('result') == 'no')
    total = len(markets)

    print(f"\n  Total mention markets: {total}")
    print(f"  Unique events: {len(events)}")
    if total > 0:
        print(f"  Results: YES={total_yes} ({total_yes/total*100:.1f}%), "
              f"NO={total_no} ({total_no/total*100:.1f}%)")
    else:
        print("  No settled mention markets found!")
        return

    # Per-category stats
    cat_stats = defaultdict(lambda: {'yes': 0, 'no': 0, 'total': 0})
    for m in markets:
        cat = categorize_mention(m.get('ticker', ''), m.get('title', ''))
        cat_stats[cat]['total'] += 1
        if m.get('result') == 'yes':
            cat_stats[cat]['yes'] += 1
        else:
            cat_stats[cat]['no'] += 1

    print(f"\n  {'Category':15s} {'Total':>6s} {'YES':>6s} {'NO':>6s} {'YES%':>6s}")
    for cat in sorted(cat_stats.keys(), key=lambda c: -cat_stats[c]['total']):
        s = cat_stats[cat]
        pct = s['yes'] / s['total'] * 100 if s['total'] else 0
        print(f"  {cat:15s} {s['total']:6d} {s['yes']:6d} {s['no']:6d} {pct:5.1f}%")

    # Per-event breakdown (top 15)
    print(f"\n  Top events by outcome count:")
    for et in sorted(events.keys(), key=lambda e: -len(events[e]))[:15]:
        mkts = events[et]
        y = sum(1 for m in mkts if m.get('result') == 'yes')
        n = len(mkts) - y
        sample = mkts[0].get('title', '')[:50]
        print(f"    {len(mkts):3d} outcomes (Y:{y} N:{n}) | {et[:40]:40s} | {sample}")

    # ================================================================
    # PHASE 2: Fetch trade history for each mention market
    # ================================================================
    print(f"\nPHASE 2: Fetching trade history...")

    trade_cache = {}
    if CACHE_FILE.exists():
        try:
            trade_cache = json.loads(CACHE_FILE.read_text())
            print(f"  Loaded cache with {len(trade_cache)} tickers")
        except Exception:
            trade_cache = {}

    # Prioritize interesting categories, cap total fetching
    PRIORITY_CATS = ['Trump', 'Press_Briefing', 'Mamdani', 'Earnings', 'SNL',
                     'Maddow', 'Talk_Show', 'Fed', 'Governor', 'Olympics',
                     'Sports', 'Other']
    priority_tickers = []
    for cat in PRIORITY_CATS:
        cat_tickers = [m['ticker'] for m in markets
                       if categorize_mention(m.get('ticker', ''),
                                             m.get('title', '')) == cat
                       and m['ticker'] not in trade_cache]
        priority_tickers.extend(cat_tickers)

    MAX_FETCH = 0  # Use cached data only for fast iteration
    tickers_to_fetch = priority_tickers[:MAX_FETCH]
    print(f"  Need to fetch: {len(tickers_to_fetch)} tickers "
          f"(cached: {len(trade_cache)}, cap: {MAX_FETCH})")

    for i, ticker in enumerate(tickers_to_fetch):
        trades = client.get_ticker_trades(ticker)
        # Store as list of [timestamp, yes_price, count]
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

        if (i + 1) % 25 == 0:
            CACHE_FILE.write_text(json.dumps(trade_cache))
            print(f"  {i+1}/{len(tickers_to_fetch)} tickers fetched "
                  f"({client.calls} API calls)")
        time.sleep(0.05)

    # Final save
    if tickers_to_fetch:
        CACHE_FILE.write_text(json.dumps(trade_cache))
        print(f"  Saved cache ({len(trade_cache)} tickers)")

    # ================================================================
    # PHASE 3: Calibration analysis
    # ================================================================
    print(f"\nPHASE 3: Calibration analysis...")

    # Time buckets (minutes before close)
    time_buckets = [
        ('0-30min', 0, 30),
        ('30-60min', 30, 60),
        ('1-2h', 60, 120),
        ('2-4h', 120, 240),
        ('4-12h', 240, 720),
        ('12h+', 720, 999999),
    ]

    # Price buckets (YES price in cents)
    price_buckets = [
        ('3-10c', 0.03, 0.10),
        ('10-20c', 0.10, 0.20),
        ('20-30c', 0.20, 0.30),
        ('30-40c', 0.30, 0.40),
        ('40-50c', 0.40, 0.50),
        ('50-60c', 0.50, 0.60),
        ('60-70c', 0.60, 0.70),
        ('70-80c', 0.70, 0.80),
        ('80-90c', 0.80, 0.90),
        ('90-97c', 0.90, 0.97),
    ]

    # Collect: for each (time_bucket, price_bucket), how many resolved YES vs NO?
    cal = defaultdict(lambda: {'yes': 0, 'no': 0, 'total': 0,
                                'sum_price': 0, 'trades': 0,
                                'contracts': 0})

    # Also track per-category calibration
    cat_cal = defaultdict(lambda: defaultdict(lambda: {
        'yes': 0, 'no': 0, 'total': 0, 'sum_price': 0}))

    # Trade-level results for CSV
    all_trade_results = []

    skipped_no_close = 0
    skipped_no_trades = 0
    total_trades_analyzed = 0

    for m in markets:
        ticker = m['ticker']
        result = m.get('result', '')
        resolved_yes = result == 'yes'
        close_ts = parse_ts(m.get('close_time'))
        cat = categorize_mention(ticker, m.get('title', ''))

        if not close_ts:
            skipped_no_close += 1
            continue

        trades = trade_cache.get(ticker, [])
        if not trades:
            skipped_no_trades += 1
            continue

        for trade in trades:
            ts, yes_price_raw, count, taker_side = trade
            # Kalshi prices are in cents (1-99); convert to fraction
            yes_price = yes_price_raw / 100.0 if yes_price_raw > 1 else yes_price_raw
            if yes_price < 0.01 or yes_price > 0.99:
                continue

            mins_before_close = (close_ts - ts) / 60
            if mins_before_close < 0:
                continue  # trade after close (shouldn't happen but safety)

            # Find time bucket
            t_label = None
            for label, lo, hi in time_buckets:
                if lo <= mins_before_close < hi:
                    t_label = label
                    break
            if not t_label:
                continue

            # Find price bucket
            p_label = None
            p_mid = None
            for label, lo, hi in price_buckets:
                if lo <= yes_price < hi:
                    p_label = label
                    p_mid = (lo + hi) / 2
                    break
            if not p_label:
                continue

            total_trades_analyzed += 1
            key = (t_label, p_label)

            cal[key]['total'] += 1
            cal[key]['sum_price'] += yes_price
            cal[key]['contracts'] += count
            if resolved_yes:
                cal[key]['yes'] += 1
            else:
                cal[key]['no'] += 1

            # Per-category
            cat_key = (cat, t_label, p_label)
            cat_cal[cat][(t_label, p_label)]['total'] += 1
            cat_cal[cat][(t_label, p_label)]['sum_price'] += yes_price
            if resolved_yes:
                cat_cal[cat][(t_label, p_label)]['yes'] += 1
            else:
                cat_cal[cat][(t_label, p_label)]['no'] += 1

            # For CSV
            all_trade_results.append({
                'ticker': ticker,
                'event_ticker': m.get('event_ticker', ''),
                'title': m.get('title', ''),
                'result': result,
                'category': cat,
                'trade_ts': ts,
                'close_ts': close_ts,
                'mins_before_close': round(mins_before_close, 1),
                'yes_price': yes_price,
                'count': count,
                'taker_side': taker_side,
                'time_bucket': t_label,
                'price_bucket': p_label,
                'resolved_yes': 1 if resolved_yes else 0,
            })

    print(f"  Total trades analyzed: {total_trades_analyzed:,}")
    print(f"  Skipped (no close_time): {skipped_no_close}")
    print(f"  Skipped (no trades): {skipped_no_trades}")

    # ================================================================
    # PHASE 4: Print calibration tables
    # ================================================================
    print(f"\n{'='*70}")
    print("CALIBRATION: ACTUAL YES RATE vs IMPLIED PROBABILITY")
    print(f"{'='*70}")
    print(f"\nFor each cell: actual_YES% (trades) [edge = actual - implied]")
    print(f"Positive edge = YES is underpriced = buying YES is profitable\n")

    # Header
    t_labels = [t[0] for t in time_buckets]
    p_labels = [p[0] for p in price_buckets]

    header = f"{'Price':>10s}"
    for tl in t_labels:
        header += f" | {tl:>14s}"
    print(header)
    print("-" * len(header))

    for pl, p_lo, p_hi in price_buckets:
        implied = (p_lo + p_hi) / 2  # midpoint as implied prob
        row = f"{pl:>10s}"
        for tl in t_labels:
            key = (tl, pl)
            d = cal.get(key)
            if d and d['total'] >= 3:
                actual = d['yes'] / d['total']
                avg_price = d['sum_price'] / d['total']
                edge = actual - avg_price
                n = d['total']
                row += f" | {actual*100:5.1f}% n={n:<4d}"
            else:
                n = d['total'] if d else 0
                row += f" | {'':>14s}" if n == 0 else f" |  n={n:<10d}"
        print(row)

    # ================================================================
    # Edge summary (only time buckets during event: 0-60min)
    # ================================================================
    print(f"\n{'='*70}")
    print("EDGE ANALYSIS: DURING EVENT (0-60 min before close)")
    print(f"{'='*70}")

    during_event_labels = ['0-30min', '30-60min']

    print(f"\n{'Price':>10s} {'Implied':>8s} {'Actual':>8s} {'Edge':>8s} "
          f"{'Trades':>7s} {'EV/bet':>8s} {'ROI%':>8s}")
    print("-" * 70)

    total_edge_trades = 0
    total_ev = 0

    for pl, p_lo, p_hi in price_buckets:
        # Combine 0-30 and 30-60 buckets
        combined_yes = 0
        combined_total = 0
        combined_price_sum = 0
        for tl in during_event_labels:
            key = (tl, pl)
            d = cal.get(key)
            if d:
                combined_yes += d['yes']
                combined_total += d['total']
                combined_price_sum += d['sum_price']

        if combined_total < 3:
            continue

        avg_price = combined_price_sum / combined_total
        actual = combined_yes / combined_total
        edge = actual - avg_price
        ev_per_dollar = edge  # per $1 risked at this price
        roi = edge / avg_price * 100 if avg_price > 0 else 0

        total_edge_trades += combined_total
        total_ev += edge * combined_total

        marker = " <<<" if edge > 0.05 else (" <" if edge > 0 else "")
        print(f"{pl:>10s} {avg_price*100:7.1f}c {actual*100:7.1f}% "
              f"{edge*100:+7.1f}c {combined_total:7d} "
              f"${edge:+7.4f} {roi:+7.1f}%{marker}")

    if total_edge_trades > 0:
        avg_ev = total_ev / total_edge_trades
        print(f"\n  Overall: {total_edge_trades} trades during events, "
              f"avg edge {avg_ev*100:+.2f}c per trade")

    # ================================================================
    # Per-category during event
    # ================================================================
    print(f"\n{'='*70}")
    print("PER-CATEGORY EDGE (0-60 min before close, all prices)")
    print(f"{'='*70}")

    print(f"\n{'Category':>15s} {'Trades':>7s} {'YES%':>7s} {'AvgPr':>7s} "
          f"{'Edge':>8s} {'ROI%':>8s}")
    print("-" * 60)

    for cat in sorted(cat_stats.keys(), key=lambda c: -cat_stats[c]['total']):
        c_yes = 0
        c_total = 0
        c_price_sum = 0
        for tl in during_event_labels:
            for pl, _, _ in price_buckets:
                key = (tl, pl)
                d = cat_cal.get(cat, {}).get(key)
                if d:
                    c_yes += d['yes']
                    c_total += d['total']
                    c_price_sum += d['sum_price']
        if c_total < 5:
            continue
        avg_p = c_price_sum / c_total
        actual = c_yes / c_total
        edge = actual - avg_p
        roi = edge / avg_p * 100 if avg_p > 0 else 0
        print(f"{cat:>15s} {c_total:7d} {actual*100:6.1f}% {avg_p*100:6.1f}c "
              f"{edge*100:+7.1f}c {roi:+7.1f}%")

    # ================================================================
    # Simulated BUY YES strategy
    # ================================================================
    print(f"\n{'='*70}")
    print("SIMULATED STRATEGY: BUY YES on during-event trades")
    print(f"{'='*70}")

    # For each price threshold x timing, simulate buying YES on every qualifying trade
    strat_results = []
    for max_price_label, _, max_price in price_buckets:
        if max_price > 0.60:
            break  # skip high-priced outcomes

        for tl in during_event_labels:
            wins = 0
            losses = 0
            total_pnl = 0
            trades_list = []

            for r in all_trade_results:
                if r['time_bucket'] != tl:
                    continue
                if r['yes_price'] >= max_price:
                    continue
                entry = r['yes_price']
                pnl = (1.0 - entry) if r['resolved_yes'] else (-entry)
                total_pnl += pnl
                if r['resolved_yes']:
                    wins += 1
                else:
                    losses += 1
                trades_list.append(pnl)

            n = wins + losses
            if n < 5:
                continue
            wr = wins / n * 100
            avg_pnl = total_pnl / n
            roi = avg_pnl / (max_price / 2) * 100  # rough ROI vs avg entry

            strat_results.append({
                'max_price': max_price,
                'max_price_label': max_price_label,
                'time_bucket': tl,
                'trades': n,
                'wins': wins,
                'win_rate': wr,
                'total_pnl': total_pnl,
                'avg_pnl': avg_pnl,
            })

    print(f"\n{'MaxPrice':>10s} {'Time':>10s} {'Trades':>7s} {'WR%':>7s} "
          f"{'AvgPnL':>9s} {'TotalPnL':>10s}")
    print("-" * 60)
    for s in sorted(strat_results, key=lambda x: -x['avg_pnl']):
        print(f"{s['max_price_label']:>10s} {s['time_bucket']:>10s} "
              f"{s['trades']:7d} {s['win_rate']:6.1f}% "
              f"${s['avg_pnl']:+8.4f} ${s['total_pnl']:+9.2f}")

    # ================================================================
    # Best combo profit projection
    # ================================================================
    if strat_results:
        best = max(strat_results, key=lambda x: x['avg_pnl'] if x['trades'] >= 10 else -999)
        print(f"\n{'='*70}")
        print(f"BEST STRATEGY: BUY YES < {best['max_price_label']} "
              f"in {best['time_bucket']} window")
        print(f"{'='*70}")
        print(f"  Trades: {best['trades']}")
        print(f"  Win rate: {best['win_rate']:.1f}%")
        print(f"  Avg PnL per trade: ${best['avg_pnl']:+.4f}")
        print(f"  Total PnL: ${best['total_pnl']:+.2f}")

        # Estimate frequency: trades per week
        # Get date range
        if all_trade_results:
            ts_list = [r['trade_ts'] for r in all_trade_results]
            days = (max(ts_list) - min(ts_list)) / 86400
            if days > 0:
                per_day = best['trades'] / days
                per_week = per_day * 7
                print(f"\n  Date range: {days:.0f} days")
                print(f"  Signal frequency: {per_week:.1f}/week")
                for bet in [10, 25, 50]:
                    weekly = per_week * bet * best['avg_pnl']
                    print(f"  ${bet}/bet: ${weekly:+.2f}/week, "
                          f"${weekly*4:+.2f}/month")

    # Save CSV
    csv_path = Path(__file__).parent / 'mention_backtest_results.csv'
    if all_trade_results:
        fields = list(all_trade_results[0].keys())
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_trade_results)
        print(f"\nSaved {len(all_trade_results)} trade results to {csv_path}")

    print(f"\nTotal API calls: {client.calls}")


if __name__ == '__main__':
    main()
