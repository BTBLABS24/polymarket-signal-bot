#!/usr/bin/env python3
"""
Sanity check — is NO 5-65c (ex NBA/Earnings) too good to be true?

Tests:
1. Full dataset train/test split (not just last 21 days)
2. Rolling 21-day windows — was this period unusually good?
3. Execution realism — are we getting fantasy fills?
4. What if we used LAST trade in window instead of FIRST?
5. What if we add 2c slippage per contract?
"""

import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

TRADE_CACHE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE = Path(__file__).parent / 'mention_markets_cache.json'

BET_DOLLARS = 3.00
MAX_HOURS_BEFORE_CLOSE = 4
MIN_NO = 0.05
MAX_NO = 0.65
EXCLUDED_CATS = {'NBA', 'Earnings'}


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


def build_entries(markets_all, trade_cache, min_close_ts=0, use_last=False,
                  slippage=0.0):
    """Build one entry per ticker. use_last=True takes last trade instead of first."""
    entries = []
    for ticker, m in markets_all.items():
        if ticker not in trade_cache or not trade_cache[ticker]:
            continue
        close_ts = parse_ts(m.get('close_time'))
        if not close_ts or close_ts < min_close_ts:
            continue
        result = m.get('result', '')
        if result not in ('yes', 'no'):
            continue

        cat = categorize(ticker)
        if cat in EXCLUDED_CATS:
            continue

        resolved_yes = result == 'yes'
        raw_trades = sorted(trade_cache[ticker], key=lambda t: t[0])

        eligible = []
        for ts, price_raw, count, side in raw_trades:
            yes_price = price_raw / 100.0 if price_raw > 1 else price_raw
            if yes_price < 0.01 or yes_price > 0.99:
                continue
            mins_before = (close_ts - ts) / 60
            if mins_before < 0 or mins_before > MAX_HOURS_BEFORE_CLOSE * 60:
                continue
            no_price = 1 - yes_price + slippage  # add slippage (we pay more)
            if no_price < MIN_NO or no_price > MAX_NO:
                continue
            contracts = int(BET_DOLLARS / no_price)
            if contracts < 1:
                continue
            eligible.append({
                'ticker': ticker,
                'cat': cat,
                'no_price': no_price,
                'yes_price': yes_price,
                'contracts': contracts,
                'cost': contracts * no_price,
                'no_win': not resolved_yes,
                'pnl': contracts * (1 - no_price) if not resolved_yes else -(contracts * no_price),
                'close_ts': close_ts,
                'trade_ts': ts,
                'count': count,
                'side': side,
            })

        if eligible:
            pick = eligible[-1] if use_last else eligible[0]
            entries.append(pick)

    return entries


def sim_stats(entries, label=""):
    if not entries:
        print(f"  {label}: no trades")
        return None
    n = len(entries)
    wins = sum(1 for e in entries if e['no_win'])
    cost = sum(e['cost'] for e in entries)
    pnl = sum(e['pnl'] for e in entries)
    wr = wins / n * 100
    roi = pnl / cost * 100 if cost > 0 else 0

    # Daily
    daily = defaultdict(float)
    for e in entries:
        dt = datetime.fromtimestamp(e['close_ts'], tz=timezone.utc)
        daily[dt.strftime('%Y-%m-%d')] += e['pnl']
    active_days = len(daily)
    win_days = sum(1 for v in daily.values() if v > 0)
    pnl_day = pnl / active_days if active_days > 0 else 0

    return {'n': n, 'wins': wins, 'wr': wr, 'cost': cost, 'pnl': pnl,
            'roi': roi, 'active_days': active_days, 'win_days': win_days,
            'pnl_day': pnl_day, 'daily': daily}


def print_stats(s, label):
    if not s:
        return
    print(f"  {label}:")
    print(f"    Trades: {s['n']}, Wins: {s['wins']} ({s['wr']:.1f}%)")
    print(f"    Cost: ${s['cost']:.2f}, PnL: ${s['pnl']:+.2f}, ROI: {s['roi']:+.1f}%")
    print(f"    Active days: {s['active_days']}, Win days: {s['win_days']}/{s['active_days']}")
    print(f"    $/day: ${s['pnl_day']:+.2f}")


def main():
    print("=" * 80)
    print("SANITY CHECK — NO 5-65c, ex NBA/Earnings, $3/bet")
    print("=" * 80)

    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    # ================================================================
    # TEST 1: Full dataset with train/test split
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 1: FULL DATASET — TRAIN/TEST SPLIT (60/40 chronological)")
    print(f"{'='*80}")

    all_entries = build_entries(markets_all, trade_cache)
    all_entries.sort(key=lambda e: e['close_ts'])

    if not all_entries:
        print("No entries!")
        return

    first_dt = datetime.fromtimestamp(all_entries[0]['close_ts'], tz=timezone.utc)
    last_dt = datetime.fromtimestamp(all_entries[-1]['close_ts'], tz=timezone.utc)
    print(f"Full range: {first_dt.strftime('%Y-%m-%d')} to {last_dt.strftime('%Y-%m-%d')} ({(last_dt-first_dt).days} days)")
    print(f"Total entries: {len(all_entries)}")

    split_idx = int(len(all_entries) * 0.6)
    train = all_entries[:split_idx]
    test = all_entries[split_idx:]

    split_dt = datetime.fromtimestamp(train[-1]['close_ts'], tz=timezone.utc)
    test_start_dt = datetime.fromtimestamp(test[0]['close_ts'], tz=timezone.utc)
    print(f"Train: {len(train)} entries through {split_dt.strftime('%Y-%m-%d')}")
    print(f"Test:  {len(test)} entries from {test_start_dt.strftime('%Y-%m-%d')}")

    s_all = sim_stats(all_entries, "All")
    s_train = sim_stats(train, "Train")
    s_test = sim_stats(test, "Test")

    print_stats(s_train, "TRAIN")
    print_stats(s_test, "TEST")
    print_stats(s_all, "ALL")

    # ================================================================
    # TEST 2: Rolling 21-day windows — was recent period unusually good?
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 2: ROLLING 21-DAY WINDOWS — consistency check")
    print(f"{'='*80}")

    # Group entries by close date
    by_date = defaultdict(list)
    for e in all_entries:
        dt = datetime.fromtimestamp(e['close_ts'], tz=timezone.utc)
        by_date[dt.strftime('%Y-%m-%d')].append(e)

    sorted_dates = sorted(by_date.keys())
    print(f"Date range: {sorted_dates[0]} to {sorted_dates[-1]} ({len(sorted_dates)} active days)")

    print(f"\n{'Window End':>12s}  {'Trades':>7s}  {'WR%':>6s}  {'Cost':>8s}  {'PnL':>9s}  {'ROI%':>7s}  {'$/day':>7s}  {'WinDays':>8s}")
    print(f"{'-'*75}")

    window_rois = []
    for i in range(20, len(sorted_dates)):
        window_dates = sorted_dates[max(0, i-20):i+1]
        window_entries = []
        for d in window_dates:
            window_entries.extend(by_date[d])

        if len(window_entries) < 10:
            continue

        s = sim_stats(window_entries)
        window_rois.append(s['roi'])
        print(f"{window_dates[-1]:>12s}  {s['n']:>7d}  {s['wr']:5.1f}%  ${s['cost']:>7.2f}  ${s['pnl']:>+8.2f}  {s['roi']:>+6.1f}%  ${s['pnl_day']:>+6.2f}  {s['win_days']}/{s['active_days']}")

    if window_rois:
        print(f"\n  Rolling 21d ROI stats:")
        print(f"    Mean:   {sum(window_rois)/len(window_rois):+.1f}%")
        print(f"    Min:    {min(window_rois):+.1f}%")
        print(f"    Max:    {max(window_rois):+.1f}%")
        print(f"    Median: {sorted(window_rois)[len(window_rois)//2]:+.1f}%")
        neg = sum(1 for r in window_rois if r < 0)
        print(f"    Negative windows: {neg}/{len(window_rois)}")

    # ================================================================
    # TEST 3: Execution realism — first vs last trade, slippage
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 3: EXECUTION REALISM")
    print(f"{'='*80}")

    now = datetime.now(timezone.utc)
    cutoff_21d = (now - timedelta(days=21)).timestamp()

    # First trade (our current approach)
    e_first = [e for e in build_entries(markets_all, trade_cache, cutoff_21d, use_last=False)]
    s_first = sim_stats(e_first)
    print_stats(s_first, "First eligible trade (current)")

    # Last trade (worst case — we enter at end of window)
    e_last = [e for e in build_entries(markets_all, trade_cache, cutoff_21d, use_last=True)]
    s_last = sim_stats(e_last)
    print_stats(s_last, "Last eligible trade (worst timing)")

    # With 1c slippage
    e_slip1 = [e for e in build_entries(markets_all, trade_cache, cutoff_21d, slippage=0.01)]
    s_slip1 = sim_stats(e_slip1)
    print_stats(s_slip1, "First trade + 1c slippage")

    # With 2c slippage
    e_slip2 = [e for e in build_entries(markets_all, trade_cache, cutoff_21d, slippage=0.02)]
    s_slip2 = sim_stats(e_slip2)
    print_stats(s_slip2, "First trade + 2c slippage")

    # With 3c slippage
    e_slip3 = [e for e in build_entries(markets_all, trade_cache, cutoff_21d, slippage=0.03)]
    s_slip3 = sim_stats(e_slip3)
    print_stats(s_slip3, "First trade + 3c slippage")

    # ================================================================
    # TEST 4: Price distribution — are we trading at realistic prices?
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 4: WHAT PRICES ARE WE ACTUALLY BUYING AT? (last 21d)")
    print(f"{'='*80}")

    price_buckets = defaultdict(lambda: {'n': 0, 'pnl': 0, 'cost': 0})
    for e in e_first:
        bucket = int(e['no_price'] * 100 / 5) * 5  # 5c buckets
        price_buckets[bucket]['n'] += 1
        price_buckets[bucket]['pnl'] += e['pnl']
        price_buckets[bucket]['cost'] += e['cost']

    print(f"{'NO Price':>10s}  {'Trades':>7s}  {'PnL':>9s}  {'ROI%':>7s}  {'% of PnL':>9s}")
    print(f"{'-'*50}")
    total_pnl = sum(b['pnl'] for b in price_buckets.values())
    for bucket in sorted(price_buckets.keys()):
        d = price_buckets[bucket]
        roi = d['pnl'] / d['cost'] * 100 if d['cost'] > 0 else 0
        pct = d['pnl'] / total_pnl * 100 if total_pnl > 0 else 0
        print(f"  {bucket:>3d}-{bucket+5}c  {d['n']:>7d}  ${d['pnl']:>+8.2f}  {roi:>+6.1f}%  {pct:>+7.1f}%")

    # ================================================================
    # TEST 5: Contract count distribution — are fills realistic?
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 5: CONTRACT SIZES — can we realistically fill these?")
    print(f"{'='*80}")

    contract_dist = defaultdict(int)
    for e in e_first:
        contract_dist[e['contracts']] += 1

    print(f"{'Contracts':>10s}  {'Freq':>6s}  {'Pct':>6s}")
    print(f"{'-'*25}")
    for c in sorted(contract_dist.keys()):
        pct = contract_dist[c] / len(e_first) * 100
        print(f"  {c:>8d}  {contract_dist[c]:>6d}  {pct:5.1f}%")

    # Avg trade sizes by count
    print(f"\n  At $3/bet:")
    print(f"  - NO @ 5c  → 60 contracts × $0.05 = $3.00 cost, $57.00 max win")
    print(f"  - NO @ 10c → 30 contracts × $0.10 = $3.00 cost, $27.00 max win")
    print(f"  - NO @ 25c → 12 contracts × $0.25 = $3.00 cost, $9.00 max win")
    print(f"  - NO @ 50c →  6 contracts × $0.50 = $3.00 cost, $3.00 max win")
    print(f"  - NO @ 65c →  4 contracts × $0.65 = $2.60 cost, $1.40 max win")

    # ================================================================
    # TEST 6: What fraction of PnL comes from cheap NO (5-15c)?
    # ================================================================
    print(f"\n{'='*80}")
    print("TEST 6: PnL CONCENTRATION — how much comes from cheap NO?")
    print(f"{'='*80}")

    cheap = [e for e in e_first if e['no_price'] < 0.15]
    mid = [e for e in e_first if 0.15 <= e['no_price'] < 0.40]
    expensive = [e for e in e_first if e['no_price'] >= 0.40]

    for label, group in [("NO 5-15c (cheap)", cheap),
                          ("NO 15-40c (mid)", mid),
                          ("NO 40-65c (expensive)", expensive)]:
        if not group:
            continue
        s = sim_stats(group)
        pct_pnl = s['pnl'] / total_pnl * 100 if total_pnl > 0 else 0
        print(f"  {label}:")
        print(f"    Trades: {s['n']} ({s['n']/len(e_first)*100:.0f}%), WR: {s['wr']:.1f}%")
        print(f"    PnL: ${s['pnl']:+.2f} ({pct_pnl:.0f}% of total), ROI: {s['roi']:+.1f}%")

    print(f"\nDone!")


if __name__ == '__main__':
    main()
