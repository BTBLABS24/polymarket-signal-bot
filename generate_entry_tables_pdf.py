#!/usr/bin/env python3
"""Generate NBA and NCAAB entry price tables as PDFs.
Same style as NBA_Degradation_Curve_Simple.pdf."""

import json
import math
from datetime import datetime, timezone
from collections import defaultdict
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

# -- Load data --
with open('mention_trade_cache.json') as f:
    trade_cache = json.load(f)
with open('mention_markets_cache.json') as f:
    markets_cache = json.load(f)
with open('mention_milestones_cache.json') as f:
    milestones_cache = json.load(f)

START = datetime(2025, 12, 29, 0, 0, 0, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 2, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp()

events_in_range = set()
for evt_key, evt_data in milestones_cache.items():
    st = evt_data.get('start_ts', 0)
    if isinstance(st, (int, float)) and START <= st <= END:
        events_in_range.add(evt_key)

# Word abbreviation -> full name (from yes_sub_title / custom_strike fields)
WORD_NAMES = {
    # NBA words
    'RETI': 'Retirement', 'BUZZ': 'Buzzer', 'TRIP': 'Triple-Double',
    'ALLE': 'Alley-Oop', 'AIRB': 'Airball', 'ANKL': 'Ankle Breaker',
    'JORD': 'Jordan', 'MVP': 'MVP', 'TRAD': 'Trade',
    'PLAY': 'Playoff', 'TECH': 'Technical', 'ELBO': 'Elbow',
    'DRAF': 'Draft', 'CHAS': 'Chase-Down', 'XFIN': 'Xfinity',
    'ROCK': 'Rocket', 'OVER': 'Overtime', 'INTU': 'Intuit',
    'TOYO': 'Toyota', 'AIR': 'Air Ball', 'INJU': 'Injury',
    'ROOK': 'Rookie', 'CROW': 'Crowd', 'ALL': 'All-Star',
    'CRYP': 'Crypto', 'PAYC': 'Paycom', 'MSG': 'MSG',
    'TARG': 'Target', 'AMER': 'American', 'KING': 'King James',
    'CHAL': 'Challenge', 'SPEC': 'Spectrum', 'ALIE': 'Alien',
    'TD': 'TD Garden', 'MODA': 'Moda Center', 'BANG': 'Bang',
    'BRON': 'LeBron', 'DUNK': 'Dunk', 'SPLA': 'Splash',
    'SHAQ': 'Shaq', 'WEMB': 'Wembanyama', 'JOKE': 'Jokic',
    'GIAN': 'Giannis', 'KAWH': 'Kawhi', 'NIKE': 'Nike',
    'KIA': 'Kia', 'FOUL': 'Foul', 'OT': 'OT',
    'CLUT': 'Clutch', 'LEBR': 'LeBron',
    # NCAAB words
    'RECR': 'Recruit', 'SCHE': 'Schedule', 'RECO': 'Record',
    'DOUB': 'Double-Double', 'NIL': 'NIL', 'MARC': 'March Madness',
    'WALK': 'Walk-On', 'FRES': 'Freshman', 'SAFE': 'Safety',
    'TRAN': 'Transfer', 'CAME': 'Camera',
    # NCAAB arenas
    'MCKA': 'McKale Center', 'STEP': "O'Connell Center",
    'PINN': 'Pinnacle Bank', 'BRES': 'Breslin Center',
    'MACK': 'Mackey Arena', 'GALE': 'Galen Center',
    'RUPP': 'Rupp Arena', 'HILT': 'Hilton Coliseum',
    'KOHL': 'Kohl Center', 'ALLEN': 'Allen Fieldhouse',
    'COLE': 'Coleman Coliseum', 'SAND': 'Sanford Pentagon',
    'CAPI': 'Capital One', 'MEMO': 'Memorial Gym',
    'UNIT': 'United Center',
    # NCAAF words (not used for NCAAB chart but kept for completeness)
    'HEIS': 'Heisman', 'NO': '"No"', 'WILD': 'Wildcard',
    'WIND': 'Windmill', 'TURF': 'Turf', 'WHAT': '"What"',
    'LANE': 'Lane', 'HARD': 'Hardwood', 'FERT': 'Fertile',
    'ONE': '"One"', 'LATE': 'Late', 'ROUG': 'Roughing',
    'TUSH': 'Tush Push', 'SUPE': 'Super Bowl',
}

def get_full_name(abbr):
    return WORD_NAMES.get(abbr, abbr.title())

def get_category(ticker):
    upper = ticker.upper()
    if 'NBAMENTION' in upper or 'NBAFINALS' in upper: return 'NBA'
    # NCAABMENTION = basketball, NCAAMENTION = football — separate them
    if 'NCAABMENTION' in upper: return 'NCAAB'
    if 'NCAAMENTION' in upper: return 'NCAAF'
    return None

def wilson_lower(wins, n, z=1.96):
    """Wilson score interval lower bound (95% CI)."""
    if n == 0:
        return 0
    p = wins / n
    denom = 1 + z**2 / n
    center = p + z**2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return (center - spread) / denom

# Collect per-market outcomes by (category, word, time_bucket)
# One entry per market per bucket (first trade in bucket)
market_entries = []

for ticker, market_info in markets_cache.items():
    category = get_category(ticker)
    if category is None:
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

    word = ticker.split('-')[-1].upper()

    trades = trade_cache.get(ticker, [])
    if not trades:
        continue

    trades_sorted = sorted(trades, key=lambda t: t[0])

    # One entry per half-hour bucket per market
    # Only include trades where NO >= 8c — if NO < 8c the word was already
    # said and the market is effectively resolved. This creates a natural
    # survival filter: late-game buckets only include markets where the word
    # hasn't been said yet, giving higher NO win rates over time.
    bucket_entered = set()
    for t in trades_sorted:
        ts, yes_price = t[0], t[1]
        no_price = 100 - yes_price
        h = (start_ts - ts) / 3600.0  # positive = before event, negative = after start

        # Round to 0.5h bucket
        bucket = round(h * 2) / 2
        if bucket in bucket_entered:
            continue
        if no_price < 8 or no_price > 99:
            continue

        bucket_entered.add(bucket)
        market_entries.append({
            'category': category,
            'word': word,
            'bucket': bucket,
            'no_price': no_price,
            'won': result == 'no',
        })

print(f"Total observations: {len(market_entries)}")

def compute_table(category, time_buckets, bucket_labels, min_n=30, min_roi=20):
    """Compute max NO price table for a category.

    For each (word, bucket), pool observations from that bucket and adjacent
    buckets (±0.5h) to increase sample size. Uses Wilson CI lower bound.
    Prices should increase as game progresses (later = word less likely said).
    After computing all cells, enforce monotonicity: later buckets can't be
    cheaper than earlier ones (carry forward the max).
    """
    # Group by (word, bucket)
    data = defaultdict(list)
    word_totals = defaultdict(lambda: {'n': 0, 'wins': 0})

    for e in market_entries:
        if e['category'] != category:
            continue
        key = (e['word'], e['bucket'])
        data[key].append(e)
        word_totals[e['word']]['n'] += 1
        word_totals[e['word']]['wins'] += int(e['won'])

    # Words with enough data (at least min_n total observations)
    valid_words = {w for w, s in word_totals.items() if s['n'] >= min_n}

    # For each word, compute overall NO win rate (Wilson lower)
    word_wr = {}
    for w in valid_words:
        s = word_totals[w]
        wl = wilson_lower(s['wins'], s['n'])
        word_wr[w] = wl

    # Blacklisted words + arena names — exclude from table
    NBA_BL = {'ROOK', 'INJU', 'CROW', 'ALL'}
    NBA_ARENA = {
        'MSG', 'TD', 'XFIN', 'SPEC', 'MODA', 'TARG', 'PAYC', 'INTU', 'TOYO',
        'KIA', 'AMER', 'CHAS', 'CRYP', 'ROCK', 'FROS', 'FEDE', 'LITT', 'BALL',
        'CAPI', 'GOLD', 'FISE', 'SCOT', 'DELT', 'STAT', 'GAIN', 'KASE',
    }
    NCAA_BL = {'FRES', 'SAFE', 'TRAN', 'OVER'}
    NCAAB_ARENA = {
        'MCKA', 'STEP', 'PINN', 'BRES', 'MACK', 'GALE', 'RUPP', 'HILT', 'KOHL',
        'ALLEN', 'COLE', 'SAND', 'CAPI', 'MEMO', 'UNIT', 'MSG', 'STAT', 'STEG',
        'NEVI', 'CRIS', 'MARR', 'WELS', 'LENO', 'CARV', 'MIZZ', 'DESE', 'CAME',
        'BUD', 'FERT', 'MILL',
    }
    if category == 'NBA':
        blacklist = NBA_BL | NBA_ARENA
    elif category == 'NCAAB':
        blacklist = NCAA_BL | NCAAB_ARENA
    else:
        blacklist = set()

    # For each (word, bucket), compute max NO price for min_roi% ROI
    # Pool from exact bucket + adjacent ±0.5h to get enough samples
    results = {}
    for w in valid_words:
        if w in blacklist:
            continue
        for bucket in time_buckets:
            # Pool from this bucket and adjacent ±0.5h
            obs = []
            for delta in [0, -0.5, 0.5]:
                obs.extend(data.get((w, bucket + delta), []))

            if len(obs) < 5:
                results[(w, bucket)] = None
                continue

            wins = sum(1 for o in obs if o['won'])
            n = len(obs)
            wl = wilson_lower(wins, n)

            max_price = int(100 * wl / (1 + min_roi / 100))
            if max_price < 1:
                results[(w, bucket)] = ('SKIP', 0)
            else:
                actual_roi = (wl * (100 - max_price) - (1 - wl) * max_price) / max_price * 100
                results[(w, bucket)] = (max_price, actual_roi)

    # Enforce monotonicity: as game progresses (left-to-right in table),
    # max NO price should never decrease. Carry forward the running max.
    for w in valid_words:
        if w in blacklist:
            continue
        running_max = 0
        for bucket in time_buckets:
            r = results.get((w, bucket))
            if r is None or r[0] == 'SKIP':
                # Fill with running max if we have one
                if running_max > 0:
                    wl_est = running_max * (1 + min_roi / 100) / 100
                    actual_roi = (wl_est * (100 - running_max) - (1 - wl_est) * running_max) / running_max * 100
                    results[(w, bucket)] = (running_max, actual_roi)
                continue
            price = r[0]
            if price < running_max:
                # Don't let price decrease — use running max
                wl_est = running_max * (1 + min_roi / 100) / 100
                actual_roi = (wl_est * (100 - running_max) - (1 - wl_est) * running_max) / running_max * 100
                results[(w, bucket)] = (running_max, actual_roi)
            else:
                running_max = price

    # Sort words: safest (highest overall WR) to riskiest
    sorted_words = sorted(
        [w for w in valid_words if w not in blacklist],
        key=lambda w: -word_wr[w]
    )

    # Filter to words that have at least one non-SKIP entry
    table_words = []
    for w in sorted_words:
        has_entry = False
        for bucket in time_buckets:
            r = results.get((w, bucket))
            if r is not None and r != ('SKIP', 0) and r[0] != 'SKIP':
                has_entry = True
                break
        if has_entry:
            table_words.append(w)

    return table_words, results, word_totals


def render_pdf(filename, title, subtitle, category, time_buckets, bucket_labels, footnote_events):
    """Render a single-page PDF table."""
    table_words, results, word_totals = compute_table(category, time_buckets, bucket_labels)

    # Limit to top ~15 words
    table_words = table_words[:15]
    n_rows = len(table_words)
    n_cols = len(time_buckets)

    fig_width = 2.5 + n_cols * 1.6
    fig_height = 2.2 + n_rows * 0.55

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.set_xlim(0, fig_width)
    ax.set_ylim(0, fig_height)
    ax.axis('off')

    # Title
    ax.text(fig_width / 2, fig_height - 0.3, title,
            fontsize=22, fontweight='bold', ha='center', va='top', fontfamily='sans-serif')
    ax.text(fig_width / 2, fig_height - 0.75, subtitle,
            fontsize=11, ha='center', va='top', fontfamily='sans-serif', style='italic', color='#444444')

    # Table dimensions
    row_height = 0.50
    col0_width = 2.2  # word column
    col_width = 1.5
    table_left = (fig_width - col0_width - n_cols * col_width) / 2
    table_top = fig_height - 1.2

    # Header row
    header_y = table_top
    header_h = 0.50

    # Header background
    header_rect = plt.Rectangle(
        (table_left, header_y - header_h), col0_width + n_cols * col_width, header_h,
        facecolor='#2C3E50', edgecolor='none')
    ax.add_patch(header_rect)

    ax.text(table_left + col0_width / 2, header_y - header_h / 2, 'Mention Word',
            fontsize=10, fontweight='bold', ha='center', va='center', color='white', fontfamily='sans-serif')

    for j, bl in enumerate(bucket_labels):
        x = table_left + col0_width + j * col_width + col_width / 2
        ax.text(x, header_y - header_h / 2, bl,
                fontsize=10, fontweight='bold', ha='center', va='center', color='white', fontfamily='sans-serif')

    # Data rows
    for i, word in enumerate(table_words):
        y = header_y - header_h - i * row_height

        # Alternating row background
        if i % 2 == 0:
            bg_color = '#F8F9FA'
        else:
            bg_color = 'white'

        row_rect = plt.Rectangle(
            (table_left, y - row_height), col0_width + n_cols * col_width, row_height,
            facecolor=bg_color, edgecolor='#DDDDDD', linewidth=0.5)
        ax.add_patch(row_rect)

        # Word name
        full_name = get_full_name(word)
        s = word_totals[word]
        ax.text(table_left + 0.15, y - row_height / 2, full_name,
                fontsize=10, fontweight='bold', ha='left', va='center', fontfamily='sans-serif')

        # Data cells
        for j, bucket in enumerate(time_buckets):
            x = table_left + col0_width + j * col_width
            r = results.get((word, bucket))

            if r is None:
                cell_text = '--'
                cell_color = '#999999'
                cell_bg = None
            elif r[0] == 'SKIP':
                cell_text = 'SKIP'
                cell_color = '#CC0000'
                cell_bg = '#FFF0F0'
            else:
                price, roi = r
                cell_text = f'{price}c  (+{roi:.0f}%)'
                cell_color = '#1A1A1A'
                # Color gradient: green for high price (safe), yellow for mid, light for low
                if price >= 40:
                    cell_bg = '#D5F5E3'  # green
                elif price >= 25:
                    cell_bg = '#EAFAF1'  # light green
                elif price >= 15:
                    cell_bg = '#FEF9E7'  # light yellow
                else:
                    cell_bg = '#FDEBD0'  # light orange

            if cell_bg:
                cell_rect = plt.Rectangle(
                    (x + 0.02, y - row_height + 0.02), col_width - 0.04, row_height - 0.04,
                    facecolor=cell_bg, edgecolor='none', zorder=2)
                ax.add_patch(cell_rect)

            ax.text(x + col_width / 2, y - row_height / 2, cell_text,
                    fontsize=9, ha='center', va='center', color=cell_color, fontfamily='sans-serif',
                    zorder=3)

    # Border around entire table
    table_h = header_h + n_rows * row_height
    table_w = col0_width + n_cols * col_width
    border = plt.Rectangle(
        (table_left, header_y - table_h), table_w, table_h,
        facecolor='none', edgecolor='#2C3E50', linewidth=1.5)
    ax.add_patch(border)

    # Column separator lines
    for j in range(n_cols + 1):
        x = table_left + col0_width + j * col_width
        ax.plot([x, x], [header_y, header_y - table_h], color='#DDDDDD', linewidth=0.5)
    # First column separator (thicker)
    ax.plot([table_left + col0_width, table_left + col0_width],
            [header_y, header_y - table_h], color='#AAAAAA', linewidth=1)

    # Footnotes
    fn_y = header_y - table_h - 0.3
    ax.text(fig_width / 2, fn_y,
            'Execution: passive NO bid at min(best_bid + 1c, fair_value - 5c). Hold until settlement.',
            fontsize=8.5, ha='center', va='top', fontfamily='sans-serif', style='italic', color='#666666')
    ax.text(fig_width / 2, fn_y - 0.25,
            f'Derived from {footnote_events} events over 60 days. Wilson CI lower bound (95%, n≥5). Ordered safest → riskiest. Min 20% ROI.',
            fontsize=8.5, ha='center', va='top', fontfamily='sans-serif', style='italic', color='#666666')

    # Blacklist footnote
    if category == 'NBA':
        bl_text = 'Blacklisted (never buy NO): Rookie, Injury, Crowd, All-Star'
    elif category == 'NCAAB':
        bl_text = 'Blacklisted (never buy NO): Freshman, Safety, Transfer'
    else:
        bl_text = ''
    ax.text(fig_width / 2, fn_y - 0.5,
            bl_text,
            fontsize=8.5, ha='center', va='top', fontfamily='sans-serif', fontweight='bold', color='#CC0000')

    plt.tight_layout(pad=0.3)
    fig.savefig(filename, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close()
    print(f"Saved {filename}")


# Count events
nba_events = set()
ncaa_events = set()
for ticker in markets_cache:
    upper = ticker.upper()
    et = markets_cache[ticker].get('event_ticker', '')
    if not et:
        parts = ticker.rsplit('-', 1)
        et = parts[0] if len(parts) > 1 else ''
    if et in events_in_range:
        if 'NBAMENTION' in upper or 'NBAFINALS' in upper:
            nba_events.add(et)
        elif 'NCAABMENTION' in upper:
            ncaa_events.add(et)

print(f"NBA events: {len(nba_events)}, NCAA events: {len(ncaa_events)}")

# NBA: pre-game and after tipoff
nba_buckets = [2.0, 1.0, 0.5, 0.0, -0.5, -1.0, -1.5, -2.0, -2.5]
nba_labels = ['2h pre', '1h pre', '30m pre', 'Tipoff', '0.5h in', '1h in', '1.5h in', '2h in', '2.5h in']

render_pdf(
    'NBA_Entry_Prices.pdf',
    'NBA Entry Price Table',
    'Buy NO at or below these prices. Each cell shows max price and expected ROI.',
    'NBA',
    nba_buckets, nba_labels,
    len(nba_events)
)

# NCAA: mix of pre-event and during event
# Buckets: 6h pre, 3h pre, 1.5h pre, 1h pre, 0.5h pre, START, 0.5h in, 1.0h in, 1.5h in
ncaa_buckets = [6.0, 3.0, 1.5, 1.0, 0.5, 0.0, -0.5, -1.0, -1.5]
ncaa_labels = ['6h pre', '3h pre', '1.5h', '1h', '0.5h', 'START', '0.5h in', '1h in', '1.5h in']

render_pdf(
    'NCAAB_Entry_Prices.pdf',
    'NCAAB Entry Price Table',
    'Buy NO at or below these prices. Each cell shows max price and expected ROI.',
    'NCAAB',
    ncaa_buckets, ncaa_labels,
    len(ncaa_events)
)
