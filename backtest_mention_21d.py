#!/usr/bin/env python3
"""
21-Day Realistic BUY NO Backtest — $3/bet

Simulates what the bot would have done over the last 21 days:
- BUY NO on mention markets within 4h of close_time
- NO price between 10c and 80c (YES between 20c and 90c)
- $3 per bet (buy as many contracts as $3 buys at that NO price)
- One entry per ticker (first eligible trade in the window)
- Shows daily P&L, cumulative, trade log
"""

import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

TRADE_CACHE = Path(__file__).parent / 'mention_trade_cache.json'
MARKETS_CACHE = Path(__file__).parent / 'mention_markets_cache.json'

# Strategy params (matching bot config)
BET_DOLLARS = 3.00
MAX_NO_PRICE = 0.80
MIN_NO_PRICE = 0.10
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
    if any(kw in t for kw in ['FEDMENTION', 'POWELL', 'WALLER', 'ECBMENTION', 'JPOW']):
        return 'Fed'
    if 'GOVERNOR' in t or 'HOCHUL' in t:
        return 'Governor'
    return 'Other'


def main():
    print("=" * 80)
    print("21-DAY REALISTIC BUY NO BACKTEST — $3/bet")
    print("=" * 80)

    markets_all = json.loads(MARKETS_CACHE.read_text())
    trade_cache = json.loads(TRADE_CACHE.read_text())

    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()

    print(f"Window: {(now - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}")
    print(f"Bet size: ${BET_DOLLARS:.2f}/trade")
    print(f"NO price range: {MIN_NO_PRICE*100:.0f}c - {MAX_NO_PRICE*100:.0f}c")
    print(f"Time window: 0 - {MAX_HOURS_BEFORE_CLOSE}h before close")

    # Filter markets: settled, close_time in last 21 days, have trade data
    eligible_markets = []
    for ticker, m in markets_all.items():
        if ticker not in trade_cache or not trade_cache[ticker]:
            continue
        close_ts = parse_ts(m.get('close_time'))
        if not close_ts or close_ts < cutoff_ts:
            continue
        result = m.get('result', '')
        if result not in ('yes', 'no'):
            continue
        eligible_markets.append({
            'ticker': ticker,
            'close_ts': close_ts,
            'result': result,
            'resolved_yes': result == 'yes',
            'title': m.get('title', ''),
            'event_ticker': m.get('event_ticker', ticker),
            'cat': categorize(ticker),
        })

    print(f"\nSettled mention markets in last {LOOKBACK_DAYS} days: {len(eligible_markets)}")

    # For each market, find the FIRST eligible trade in the 0-4h window
    # This simulates the bot: it sees a market in the window, places one bet
    trades = []
    for mkt in eligible_markets:
        ticker = mkt['ticker']
        raw_trades = trade_cache[ticker]
        close_ts = mkt['close_ts']

        # Sort trades by timestamp
        raw_trades_sorted = sorted(raw_trades, key=lambda t: t[0])

        # Find first trade in the 0-4h window with eligible NO price
        for ts, price_raw, count, side in raw_trades_sorted:
            yes_price = price_raw / 100.0 if price_raw > 1 else price_raw
            if yes_price < 0.01 or yes_price > 0.99:
                continue
            mins_before = (close_ts - ts) / 60
            if mins_before < 0 or mins_before > MAX_HOURS_BEFORE_CLOSE * 60:
                continue
            no_price = 1 - yes_price
            if no_price < MIN_NO_PRICE or no_price > MAX_NO_PRICE:
                continue

            # How many contracts can $3 buy at this NO price?
            contracts = int(BET_DOLLARS / no_price)
            if contracts < 1:
                continue

            cost = contracts * no_price
            no_win = not mkt['resolved_yes']
            if no_win:
                pnl = contracts * (1 - no_price)
            else:
                pnl = -cost

            trade_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)

            trades.append({
                'ticker': ticker,
                'event': mkt['event_ticker'],
                'cat': mkt['cat'],
                'trade_ts': ts,
                'trade_dt': trade_dt,
                'close_ts': close_ts,
                'close_dt': close_dt,
                'date': close_dt.strftime('%Y-%m-%d'),
                'no_price': no_price,
                'yes_price': yes_price,
                'contracts': contracts,
                'cost': cost,
                'no_win': no_win,
                'pnl': pnl,
                'mins_before': mins_before,
                'title': mkt['title'],
            })
            break  # Only one entry per ticker

    trades.sort(key=lambda t: t['trade_ts'])
    print(f"Trades taken: {len(trades)}")
    if not trades:
        print("No trades found in the window!")
        return

    # ================================================================
    # Daily P&L
    # ================================================================
    daily = defaultdict(lambda: {'n': 0, 'wins': 0, 'cost': 0, 'pnl': 0, 'contracts': 0})
    for t in trades:
        d = t['date']
        daily[d]['n'] += 1
        daily[d]['cost'] += t['cost']
        daily[d]['pnl'] += t['pnl']
        daily[d]['contracts'] += t['contracts']
        if t['no_win']:
            daily[d]['wins'] += 1

    print(f"\n{'='*80}")
    print("DAILY P&L")
    print(f"{'='*80}")
    print(f"{'Date':>12s}  {'Trades':>7s}  {'Wins':>5s}  {'WR%':>6s}  {'Cost':>8s}  {'PnL':>8s}  {'Cumul':>8s}  {'ROI%':>7s}")
    print(f"{'-'*75}")

    sorted_dates = sorted(daily.keys())
    cumul_pnl = 0
    cumul_cost = 0
    for date in sorted_dates:
        d = daily[date]
        cumul_pnl += d['pnl']
        cumul_cost += d['cost']
        wr = d['wins'] / d['n'] * 100 if d['n'] > 0 else 0
        roi = d['pnl'] / d['cost'] * 100 if d['cost'] > 0 else 0
        print(f"{date:>12s}  {d['n']:>7d}  {d['wins']:>5d}  {wr:5.1f}%  ${d['cost']:>7.2f}  ${d['pnl']:>+7.2f}  ${cumul_pnl:>+7.2f}  {roi:>+6.1f}%")

    # ================================================================
    # Summary stats
    # ================================================================
    total_cost = sum(t['cost'] for t in trades)
    total_pnl = sum(t['pnl'] for t in trades)
    total_wins = sum(1 for t in trades if t['no_win'])
    total_contracts = sum(t['contracts'] for t in trades)
    total_roi = total_pnl / total_cost * 100 if total_cost > 0 else 0
    avg_no = sum(t['no_price'] for t in trades) / len(trades)
    avg_contracts = total_contracts / len(trades)
    win_rate = total_wins / len(trades) * 100

    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"  Period:          {sorted_dates[0]} to {sorted_dates[-1]} ({len(sorted_dates)} active days)")
    print(f"  Total trades:    {len(trades)}")
    print(f"  Total contracts: {total_contracts}")
    print(f"  Win rate:        {win_rate:.1f}% ({total_wins}/{len(trades)})")
    print(f"  Avg NO price:    {avg_no*100:.1f}c")
    print(f"  Avg contracts:   {avg_contracts:.1f} per trade")
    print(f"  Total deployed:  ${total_cost:.2f}")
    print(f"  Total P&L:       ${total_pnl:+.2f}")
    print(f"  ROI:             {total_roi:+.1f}%")
    print(f"  Avg P&L/trade:   ${total_pnl/len(trades):+.4f}")
    print(f"  Avg P&L/day:     ${total_pnl/len(sorted_dates):+.2f}")

    # ================================================================
    # Per-category breakdown
    # ================================================================
    cat_stats = defaultdict(lambda: {'n': 0, 'wins': 0, 'cost': 0, 'pnl': 0})
    for t in trades:
        cat = t['cat']
        cat_stats[cat]['n'] += 1
        cat_stats[cat]['cost'] += t['cost']
        cat_stats[cat]['pnl'] += t['pnl']
        if t['no_win']:
            cat_stats[cat]['wins'] += 1

    print(f"\n{'='*80}")
    print("PER-CATEGORY")
    print(f"{'='*80}")
    print(f"  {'Category':>15s}  {'Trades':>7s}  {'Wins':>5s}  {'WR%':>6s}  {'Cost':>8s}  {'PnL':>8s}  {'ROI%':>7s}")
    print(f"  {'-'*65}")

    sorted_cats = sorted(cat_stats.keys(),
                         key=lambda c: cat_stats[c]['pnl'],
                         reverse=True)
    for cat in sorted_cats:
        d = cat_stats[cat]
        wr = d['wins'] / d['n'] * 100 if d['n'] > 0 else 0
        roi = d['pnl'] / d['cost'] * 100 if d['cost'] > 0 else 0
        print(f"  {cat:>15s}  {d['n']:>7d}  {d['wins']:>5d}  {wr:5.1f}%  ${d['cost']:>7.2f}  ${d['pnl']:>+7.2f}  {roi:>+6.1f}%")

    # ================================================================
    # Biggest winners and losers
    # ================================================================
    trades_sorted_pnl = sorted(trades, key=lambda t: t['pnl'], reverse=True)

    print(f"\n{'='*80}")
    print("TOP 10 WINNERS")
    print(f"{'='*80}")
    for t in trades_sorted_pnl[:10]:
        print(f"  {t['date']} | {t['cat']:>15s} | {t['ticker'][:35]:<35s} | "
              f"NO@{t['no_price']*100:.0f}c x{t['contracts']} = ${t['pnl']:+.2f}")

    print(f"\nTOP 10 LOSERS")
    for t in trades_sorted_pnl[-10:]:
        print(f"  {t['date']} | {t['cat']:>15s} | {t['ticker'][:35]:<35s} | "
              f"NO@{t['no_price']*100:.0f}c x{t['contracts']} = ${t['pnl']:+.2f}")

    # ================================================================
    # Max drawdown
    # ================================================================
    cumul = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumul += t['pnl']
        if cumul > peak:
            peak = cumul
        dd = peak - cumul
        if dd > max_dd:
            max_dd = dd

    print(f"\n{'='*80}")
    print("RISK METRICS")
    print(f"{'='*80}")
    print(f"  Max drawdown:    ${max_dd:.2f}")
    print(f"  Peak P&L:        ${peak:+.2f}")
    print(f"  Final P&L:       ${total_pnl:+.2f}")
    win_pnls = [t['pnl'] for t in trades if t['no_win']]
    loss_pnls = [t['pnl'] for t in trades if not t['no_win']]
    if win_pnls:
        print(f"  Avg win:         ${sum(win_pnls)/len(win_pnls):+.4f}")
    if loss_pnls:
        print(f"  Avg loss:        ${sum(loss_pnls)/len(loss_pnls):+.4f}")
    if win_pnls and loss_pnls:
        avg_win = sum(win_pnls) / len(win_pnls)
        avg_loss = abs(sum(loss_pnls) / len(loss_pnls))
        if avg_loss > 0:
            print(f"  Win/loss ratio:  {avg_win/avg_loss:.2f}")

    # ================================================================
    # Concurrent positions check
    # ================================================================
    # Each position is held from trade_ts to close_ts
    events_by_time = []
    for t in trades:
        events_by_time.append((t['trade_ts'], +1))
        events_by_time.append((t['close_ts'], -1))
    events_by_time.sort()

    max_concurrent = 0
    current = 0
    for _, delta in events_by_time:
        current += delta
        if current > max_concurrent:
            max_concurrent = current

    print(f"  Max concurrent:  {max_concurrent} positions")
    print(f"  Max capital at risk: ${max_concurrent * BET_DOLLARS:.2f}")

    print(f"\nDone!")


if __name__ == '__main__':
    main()
