#!/usr/bin/env python3
"""
Kalshi Mention, Earnings & Degradation Trading Bot

Automated trading bot that:
- Buys NO on mention markets (YES systematically overpriced)
- Buys NO on earnings call mention markets (live to +30min, 15-70c)
- NBA degradation curve strategy (passive NO bids based on fair value)
- Sizes bets dynamically based on orderbook depth
- Holds until settlement (no early exit)
- Has DRY_RUN toggle and safety circuit breakers

Uses RSA-PSS signing for Kalshi API authentication.
"""

import asyncio
import base64
import csv
import html as _html
import json
import os
import re
import time
import uuid
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from telegram import Bot

# =====================================================================
# CONFIG
# =====================================================================

env_file = Path(__file__).parent / '.env'
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if value:
                    os.environ[key] = value

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Kalshi API auth
KALSHI_API_KEY_ID = os.environ.get('KALSHI_API_KEY_ID', '')
KALSHI_PRIVATE_KEY_PATH = os.environ.get('KALSHI_PRIVATE_KEY_PATH', '')
KALSHI_PRIVATE_KEY = os.environ.get('KALSHI_PRIVATE_KEY', '')  # Raw PEM content (for Railway)
KALSHI_PRIVATE_KEY_B64 = os.environ.get('KALSHI_PRIVATE_KEY_B64', '')  # Base64-encoded PEM (for Railway)

KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

# Scanner settings
SCAN_INTERVAL_SECONDS = 300  # 5 min
# Trading config
DRY_RUN = False
# MAKER_ONLY: when True, the bot NEVER takes (no crossing the spread). It only
# rests NO limit orders (pre-event maker) and skips any signal it cannot rest.
# Enforced as a hard guard inside KalshiClient.create_order (blocks non-maker buys)
# AND by gating every taker strategy's dispatch below.
MAKER_ONLY = True
MAX_BET_DOLLARS = 10          # Max per signal (matches GLOBAL_MAX_MARKET_DOLLARS)
MIN_BET_DOLLARS = 1           # Skip if depth too thin
DEPTH_FRACTION = 0.50         # Use 50% of 3-level depth
ORDER_WAIT_SECONDS = 5        # Wait for fill after placing order
MAX_ORDER_RETRIES = 2         # Retry at next price level
MAX_SLIPPAGE_PCT = 15.0       # Skip if NO price > 15% worse than signal

# --- Mention BUY NO Strategy ---
# Backtest: YES is systematically overpriced on mention markets.
# NO 5-65c, ex NBA/Earnings, NO time filter → +73% ROI (train +62%, test +89%)
# 11,104 trades, 232 active days. Only 50 negative days out of 232.
# Kalshi uses can_close_early with far-future deadline, so close_time
# is NOT the event time. We filter by price range only.
MENTION_BET_DOLLARS = 2            # $2 fixed (taker path; inactive under MAKER_ONLY)
MENTION_BET_NCAA = 1               # $1 for NCAAB/NCAA (-34% clean ROI, -25% actual 21d)
MENTION_BET_OTHER = 2              # $2 fixed for "other" categories
# Per-category bet overrides DISABLED. The prior $10 escalations (Trump, Hegseth,
# LastWord, etc.) drove the oversized live losses (e.g. Trump $1,466 over 132
# markets ~$11/bet). Every maker order now uses the fixed small PREMARKET_BET_DOLLARS.
CATEGORY_BET_OVERRIDE = {}
# --- Hard risk caps (single chokepoint enforcement in create_order) ---
# Fill-conditioned backtest: conditional ROI turns <= 0 above ~38c NO; the
# profitable resting zone is ~15-30c. CLAUDE.md clean-trade rule: cost < $4,
# NO price <= 30c. These are enforced as a hard net inside create_order so no
# strategy/sizing bug can place an oversized or out-of-range maker entry.
MAX_TRADE_DOLLARS = 4             # Max cost ($) of any single maker NO-buy
MAX_MAKER_NO_PRICE_CENTS = 30     # Max NO entry price (cents) for any maker buy
MENTION_MAX_NO_PRICE = 0.30       # Global fallback max — conservative for unmapped categories
MENTION_MIN_NO_PRICE = 0.05       # Global fallback min (per-category overrides below)
# Per-category NO price ranges — backtest-optimized (60 days, taker +4c slippage).
# Cheap NOs (5-30c) are losers for most categories: the word usually gets said.
# Edge is in medium NOs (30-70c): market overestimates word frequency.
# Only NCAA and Newsom are profitable at the cheap end.
CATEGORY_NO_RANGE = {
    # (min_cents, max_cents) — taker range. Maker can be wider (up to PREMARKET_MAX_NO_PRICE).
    'NCAA':     (5, 30),    # -34% clean ROI 21d — reduced to $1/bet
    'Newsom':   (5, 30),    # +228% clean, 57% WR — tier 1 $15
    'LASTWORD': (5, 30),    # +131% clean, 40% WR — tier 1 $15
    'FOXNEWS':  (5, 30),    # +87% clean, 47% WR — tier 2 $10
    'HEGSETH':  (5, 30),    # +141% clean, 50% WR — tier 1 $15
    'THEWEEKNIGHT': (5, 30),# +115% clean, 44% WR — tier 2 $10
    'POLITICS': (5, 30),    # +56% clean, 44% WR — tier 2 $10
    'Trump':    (15, 50),   # +41% clean, 29% WR — tier 2 $10, medium NOs only
    'NFL':      (30, 70),   # +13.1% ROI backtest — cheap is -9%, medium is +13%
    'NBA':      (20, 70),   # +28.4% ROI, halftime+ high-conf words only (separate gating)
    'Fight':    (5, 30),    # +28.2% ROI backtest, small sample
    'Earnings': (15, 50),   # disabled, kept for reference
}
GLOBAL_MAX_MARKET_DOLLARS = 4     # HARD CEILING — total $ per ticker across ALL strategies (maker-only: $4/market)
MENTION_HOLD_UNTIL_SETTLE = True  # Hold until settlement (no early exit)
MENTION_MAX_CLOSE_HOURS = 48      # Wide filter — close_time unreliable (events live with 24h close)
MENTION_MAX_POSITIONS = 40        # Max concurrent mention positions
MENTION_COOLDOWN_SECONDS = 300    # 5 min cooldown per ticker (24h in detector)
MENTION_SCAN_INTERVAL_SECONDS = 120  # Check for new mention markets every 2 min
MENTION_MAX_EVENT_DOLLARS = 50    # Max $ per event across all words (resting + filled) — raised for diversification
MENTION_MAX_MARKET_DOLLARS = 10   # Hard cap $ per individual market/ticker (capped by GLOBAL_MAX_MARKET_DOLLARS)
# Pre-event resting orders — fade retail on wide-spread mention markets
PREMARKET_MAX_RESTING = 500       # Effectively unlimited — most won't fill
PREMARKET_CANCEL_HOURS = 0.5      # Stop new signals 30min before event start
PREMARKET_MAX_HOURS = 168         # Look up to 7 days before event for maker orders
PREMARKET_BET_DOLLARS = 4          # $ per resting maker order — fixed small size
PREMARKET_MAX_TOTAL_RESTING_DOLLARS = 60  # Cap on total $ across ALL resting maker orders
PREMARKET_MAX_MARKET_DOLLARS = 10  # Hard cap $ per market for maker orders (capped by GLOBAL_MAX_MARKET_DOLLARS)
PREMARKET_MIN_SPREAD = 5          # Min spread (cents) to place resting order
PREMARKET_MAX_NO_PRICE = 70       # Max NO price for resting orders (fallback; per-category via get_no_range)
PREMARKET_NEW_SERIES_MIN = 3      # Min resolved events in series before full sizing
PREMARKET_NEW_SERIES_BET = 1      # $ bet for new/unknown series
PREMARKET_NEW_SERIES_EVENT_CAP = 10  # Max $ per event for new/unknown series
# Pre-recorded/scripted shows — insider edge too high, skip entirely
PRERECORDED_SERIES = {
    'KXSURVIVORMENTION',      # Survivor (pre-recorded reality TV)
    'KXSOUTHPARKMENTION',     # South Park (scripted animated)
    'KXMRBEASTMENTION',       # MrBeast (pre-recorded YouTube)
    'KXGOLDENMENTION',        # Golden Bachelor (pre-recorded reality)
    'KXDWTSMENTION',          # Dancing with the Stars (pre-recorded)
    'KXKARDASHIANMENTION',    # Kardashians (pre-recorded reality)
    'KXDOGSHOWMENTION',       # National Dog Show (pre-recorded)
    'KXCENAMENTION',          # WWE (scripted entertainment)
    'KXLEBRONMENTION',        # LeBron entertainment special
    'KXSHAQMENTION',          # NBA studio show
    'KXBARKLEYMENTION',       # NBA studio show
    'KXSNOOPMENTION',         # Entertainment
    'KXVIEWMENTION',          # The View (often pre-taped)
    'KXMINAJMENTION',         # Entertainment
    'KXKINGMENTION',          # King Charles scripted address
    'KXAPPLEMENTION',         # Apple keynote (scripted presentation)
    'KXWWDCMENTION',          # WWDC keynote (scripted presentation)
    'KXGREENDAY',             # Concert (scripted)
    'KXCMAMENTION',           # CMA Awards (scripted)
    'KXAWARDMENTION',         # Awards (scripted)
    'KXGAMEDAY',              # College GameDay
    'KXTHREADGUYMENTION',     # YouTuber (pre-recorded)
    'KXGLASERMENTION',        # Golden Globes (scripted)
    'KXHARTMENTION',          # Pre-taped interview
}

# --- Political pct_words_said Strategy ---
# Backtest: when >=50% of words have been said, buy NO on remaining words.
# 396 days, 952 trades, 60.6% WR, +18.7% ROI (all data).
# Recent 30d: 69% WR, +46.5% ROI. Recent 90d: 66.7% WR, +29.2% ROI.
# Test set (post Jan-28): 331 trades, 62.2% WR, +18.4% ROI, Sharpe +0.336.
POLITICAL_PCT_ENABLED = True
POLITICAL_PCT_THRESHOLD = 0.50       # Entry when >= 50% of words said
POLITICAL_PCT_BET_DOLLARS = 5        # $5/bet
POLITICAL_PCT_MIN_NO_CENTS = 5       # Min NO price (cents)
POLITICAL_PCT_MAX_NO_CENTS = 70      # Max NO price (cents)
POLITICAL_PCT_MAX_POSITIONS = 30     # Independent position cap
POLITICAL_PCT_MAX_EVENT_DOLLARS = 30 # Per-event cap
POLITICAL_PCT_MAX_MARKET_DOLLARS = 5 # Per-market cap (no duplicate markets)
POLITICAL_PCT_MIN_MARKETS = 5        # Min markets per event (skip tiny events)
# Sports prefixes — excluded from political pct strategy (handled by other strategies)
POLITICAL_EXCLUDE_SPORTS = {
    'NBAMENTION', 'NFLMENTION', 'NCAAMENTION', 'NCAABMENTION',
    'SNFMENTION', 'TNFMENTION', 'CFBMENTION', 'MLBMENTION',
    'WBCMENTION',                                                # World Baseball Classic
    'FIGHTMENTION', 'SBMENTION', 'NHLMENTION', 'SOCCERMENTION',
    'GOLFMENTION', 'UFCMENTION', 'TENNISMENTION', 'CRICKETMENTION',
    'WOMENTION', 'NBAFINALS', 'EARNINGSMENTION', 'MMMENTION',
}
# Rally events — excluded (34% WR baseline on train, words get said)
POLITICAL_EXCLUDE_RALLY = True
# Categories with <10% actual WR from live fills — net losers, skip entirely
CATEGORY_KILL_LIST = {
    'KXVANCEMENTION',         # VANCE: -55% ROI actual (3W/15L)
    'KXGOVERNORMENTION',      # GOVERNOR: -99% ROI actual (1W/11L)
    'KXSPANBERGERMENTION',    # SPANBERGER: -100% ROI actual
    'KXBERNIEMENTION',        # BERNIE: -100% ROI actual (1W/8L)
    'KXNEWSNATIONMENTION',    # NEWSNATION: -100% ROI actual
    'KXSNLMENTION',           # SNL: -100% ROI actual
    'KXECBMENTION',           # ECB: -100% ROI actual
    'KXBESSENTMTPMENTION',    # BESSENTMTP: losing
    'KXKIMMELMENTION',        # KIMMEL: -59% ROI backtest
    'KXLEAVITTMENTION',       # LEAVITT: -100% ROI backtest
    'KXROGANMENTION',         # ROGAN: no data, cut for variance
    'KXCOOPERMENTION',        # COOPER: no data, cut for variance
    'KXMLBMENTION',           # MLB: disabled — no edge
    'KXWBCMENTION',           # WBC: 0% WR actual (0W/19L)
    'KXMAMDANIMENTION',       # Mamdani: -82% ROI actual (2W/21L, 9% WR)
    'KXFTNMENTION',           # FTN: -100% ROI actual (0W/13L)
    'KXMTPMENTION',           # MTP: -100% ROI actual (0W/11L)
    'KXPRESMENTION',          # PRES: -69% ROI actual (2W/14L, 12% WR)
    'KXWOMENTION',            # WO: -75% ROI actual (5W/12L, 12% clean WR)
    'KXHOCHULMENTION',        # Hochul: -90% ROI actual (1W/9L, 10% WR)
    'KXPSAKIMENTION',         # PSAKI: -49% ROI actual (5W/11L, 17% clean WR)
}
# Substring blocklist — any series whose ticker CONTAINS one of these is killed.
# Catches new series variants (e.g. KXWBCBCST, KXWBCANNOUNCER, KXMLBBCST, etc.)
SERIES_BLOCK_SUBSTRINGS = {'WBC', 'MLB', 'BASEBALL'}

def _is_killed_series(series):
    """Check if a series should be killed — exact match OR substring match."""
    if series in CATEGORY_KILL_LIST:
        return True
    series_upper = series.upper()
    return any(sub in series_upper for sub in SERIES_BLOCK_SUBSTRINGS)

# --- Stable-Price NO Strategy ---
# Backtest: when a word crosses 98c YES (trigger), snapshot all siblings.
# 5 min later, if YES price stable (within 3c), buy NO as taker.
# "Stubbornly overpriced" signal — market refuses to reprice after trigger.
# Universal range 0.30-0.70: N=316, Edge=+17.3c, WR=72%, Sharpe=0.402, ROI=+32%
# Media excluded (-11.6c edge, only 5 trades).
STABLE_PRICE_ENABLED = True
STABLE_PRICE_BET_DOLLARS = 5             # $5/bet (taker)
STABLE_PRICE_MAX_POSITIONS = 30          # Independent position cap
STABLE_PRICE_MAX_EVENT_DOLLARS = 20      # Per-event cap
STABLE_PRICE_MAX_MARKET_DOLLARS = 5      # Per-market cap (no duplicates)
STABLE_PRICE_TRIGGER_YES_CENTS = 98      # First word crossing this triggers the event
STABLE_PRICE_DELAY_SECONDS = 300         # 5 min delay between trigger and entry check
STABLE_PRICE_STABILITY_CENTS = 3         # Max |P_now - P_trigger| for "stable"
STABLE_PRICE_TRIGGER_EXPIRY = 1800       # Discard triggers older than 30 min
# Universal YES price range (cents). All categories use same range.
# 0.30-0.70 is the sweet spot: Sharpe 0.402, ROI +32% on test.
STABLE_PRICE_YES_MIN = 30               # Min YES price (cents)
STABLE_PRICE_YES_MAX = 70               # Max YES price (cents)
# Media excluded: -11.6c edge, -16% ROI (only 5 trades in test)
STABLE_PRICE_EXCLUDE_MEDIA = True

# --- Taker Adverse Selection Gating ---
# Pre-event taker is -39% ROI from actual fills. Live taker is +13%.
# Gate taker by category:
#   Sports (NBA/NCAA): h2e-based (tipoff times reliable)
#   Trump: volume-gated OR live (shows start ~20min before milestone)
#   Mamdani: maker-only pre-event (even live taker marginal)
#   Earnings: h2e-live OR volume-surging
#   Newsom: h2e-based (reliable timing)
TAKER_MIN_EVENT_VELOCITY = 5.0    # trades/min — volume surge threshold for taker gating
# Active series allowlist — only these get signals. Set to None to allow all.
ACTIVE_SERIES = None              # All categories active
# Series to scan (NBA for degradation, others for mention strategy)
MENTION_SCAN_SERIES = [
    # Sports — per-category NO ranges (see CATEGORY_NO_RANGE)
    'KXNFLMENTION',                                    # NFL 30-70c
    'KXNCAAMENTION', 'KXNCAABMENTION',                 # NCAA 5-30c
    'KXSNFMENTION', 'KXTNFMENTION', 'KXCFBMENTION',
    'KXNBAMENTION',                                      # NBA 20-70c (halftime+ high-conf words)
    'KXWCMENTION',                                      # World Cup 5-30c (backtest +51% maker ROI)
    'KXFIGHTMENTION', 'KXSBMENTION',                   # Fight 5-30c
    # Politics/Gov — per-category NO ranges
    'KXTRUMPMENTION', 'KXTRUMPMENTIONB',               # Trump 15-50c
    'KXMAMDANIMENTION',                                 # Mamdani 30-70c
    'KXNEWSOMMENTION',                                  # Newsom 5-30c
    'KXHOCHULMENTION',                                  # Hochul 10-50c
    'KXSECPRESSMENTION',                                # SecPress 30-70c
    # Media — proven winners + small-sample keeps
    'KXMADDOWMENTION',                                  # Maddow 5-30c (tiny sample, +197%)
    'KXCOLBERTMENTION',                                 # Colbert 5-30c (tiny sample)
    'KXLASTWORDMENTION',                                # LASTWORD 5-30c — NEW (+101% ROI)
    'KXFOXNEWSMENTION',                                 # FOXNEWS 5-30c — NEW (+46% ROI)
    # Removed: KXLEAVITTMENTION, KXKIMMELMENTION (net losers → CATEGORY_KILL_LIST)
    # Removed: KXROGANMENTION, KXCOOPERMENTION (no backtest data, cut for variance)
]

# Series that pre-event maker resting is ALLOWED on. PRUNED to only those with a
# POSITIVE fill-conditioned (adverse-selection-adjusted) backtest edge plus an
# adequate fill sample. The fill-conditioned backtest (backtest_fill_conditioned.py)
# showed ~60 mention series collapse to a 0% conditional win rate (you only fill
# when the word is about to be said), so the prior "all scanned series" allowlist
# was the core driver of the live -38% ROI. Only these survive:
#   KXTRUMPMENTION    142 fills, 30% cond WR, +78% cond ROI  (largest sample)
#   KXTRUMPMENTIONB    31 fills, 19% cond WR, +14% cond ROI  (Trump family)
#   KXNBAMENTION      142 fills, 35% cond WR, +57% cond ROI  (largest sample)
#   KXNCAABMENTION    best live WR (44%); profitable once cost/price-capped
#   KXLASTWORDMENTION  20% cond WR, +186% cond ROI (small sample, tier-1)
# All other discovered series are still scanned but may NOT rest a maker bid.
MENTION_MAKER_SERIES = {
    'KXTRUMPMENTION',
    'KXTRUMPMENTIONB',
    'KXNBAMENTION',
    'KXNCAABMENTION',
    'KXLASTWORDMENTION',
}

# --- NBA Master Blacklist ---
# ONE list checked FIRST in every code path. These words NEVER trade, no exceptions.
# Combines arena/venue names + words almost always said + known losers.
NBA_BLACKLIST = {
    # Words almost always said / unprofitable (<50% NO WR)
    'ROOK', 'INJU', 'CROW', 'ALL', 'ELBO', 'PLAY', 'TECH',
    'MVP', 'AIR', 'ANKL', 'BUZZ', 'DOUB', 'TRIP',  # Losers in live trading
    # Arena/venue/sponsor names (~30% NO WR)
    'MSG', 'TD', 'XFIN', 'SPEC', 'MODA', 'TARG', 'PAYC', 'INTU', 'TOYO',
    'KIA', 'AMER', 'CHAS', 'CRYP', 'ROCK', 'FROS', 'FEDE', 'LITT', 'BALL',
    'CAPI', 'GOLD', 'FISE', 'SCOT', 'DELT', 'STAT', 'GAIN', 'KASE',
    'UNIT', 'BARC', 'SMOO', 'MORT', 'TMOB', 'FOOT',
    'CENT', 'AREN', 'STAD',  # Generic venue words
}
# Legacy aliases — referenced in multiple code paths
NBA_WORD_BLACKLIST = NBA_BLACKLIST
NBA_ARENA_BLACKLIST = NBA_BLACKLIST

# --- NCAAB Master Blacklist ---
# ONE list checked FIRST in every code path. These words NEVER trade, no exceptions.
NCAAB_BLACKLIST = {
    # Words almost always said / unprofitable (<45% NO WR)
    'FRES', 'SAFE', 'TRAN', 'OVER', 'AIRB', 'SCHE', 'ELBO', 'DRAF', 'RECO', 'MARC',
    'DOUB', 'ANKL',  # Losers in live trading
    # Arena/venue names (~24% NO WR)
    'MCKA', 'STEP', 'PINN', 'BRES', 'MACK', 'GALE', 'RUPP', 'HILT', 'KOHL',
    'ALLEN', 'COLE', 'SAND', 'CAPI', 'MEMO', 'UNIT', 'MSG', 'STAT', 'STEG',
    'NEVI', 'CRIS', 'MARR', 'WELS', 'LENO', 'CARV', 'MIZZ', 'DESE', 'CAME',
    'BUD', 'FERT', 'MILL', 'PAUL', 'PURC', 'SIMO', 'SMIT', 'FOOD', 'ALKE',
    'PEOP', 'VALU',
    'CENT', 'AREN', 'STAD',  # Generic venue words
}
# Legacy aliases
NCAAB_WORD_BLACKLIST = NCAAB_BLACKLIST
NCAAB_ARENA_BLACKLIST = NCAAB_BLACKLIST

# --- NBA YES Buy Strategy ---
# Buy YES on words that are almost always said. Entry: pre-game to 30min into game.
# Max YES price = win_rate * 100 / 1.20 (20% ROI threshold), capped at 50c.
NBA_YES_BUY_WORDS = {
    'INJU': 50,   # Injury 96% YES WR, max=80c, target <=50c
    'ROOK': 50,   # Rookie 97% YES WR, max=81c, target <=50c
    'ALL':  50,   # All-Star 87% YES WR, max=73c, target <=50c
    'CROW': 50,   # Crowd 89% YES WR, max=74c, target <=50c
}
NBA_YES_BET_DOLLARS = 1          # $1/bet — validating logic is correct
NBA_YES_MAX_POSITIONS = 20       # independent cap
NBA_YES_MAX_EVENT_DOLLARS = 20   # per-event cap

# --- NBA Halftime NO Strategy ---
# Buy NO on high-confidence T4 words once game is ≥50% done (halftime+).
# Backtest (corrected price): 73% WR, +28.4% ROI, $1.35/trade on test set.
# Only actual NO-side fills from tape (taker-executable prices 20-70c).
# Words selected: train WR ≥ 65% AND EV ≥ 10c.
NBA_HALFTIME_ENABLED = True
NBA_HALFTIME_BET_DOLLARS = 5     # $5/bet
NBA_HALFTIME_MAX_POSITIONS = 20  # independent cap
NBA_HALFTIME_MAX_EVENT_DOLLARS = 30  # per-event cap
NBA_HALFTIME_MAX_MARKET_DOLLARS = 5  # per-market cap
NBA_HALFTIME_MIN_NO_CENTS = 20   # min NO price
NBA_HALFTIME_MAX_NO_CENTS = 70   # max NO price
NBA_HALFTIME_MIN_HOURS_LIVE = 1.3  # ~halftime (50% of game ≈ 1.3h after tipoff)
NBA_HALFTIME_MAX_HOURS_LIVE = 3.0  # don't enter too late (game over)
# High-confidence words only (train WR ≥ 65% AND EV ≥ 10c on corrected data)
NBA_HALFTIME_WORD_ALLOWLIST = {
    'ALLE',   # Alley-oop — 70% WR test, +14.7c EV
    'DRAF',   # Draft — 56% WR test, +0.0c EV (strong on train: 73% WR)
    # REMOVED: TRIP — now in NBA_BLACKLIST (Triple Double lost $20 in live trading)
    'RETI',   # Retire/Retirement — 100%/83% WR test
    # REMOVED: ANKL, BUZZ, AIR — now in NBA_BLACKLIST (losers in live trading)
}

# --- NCAAB Halftime NO Strategy ---
# Buy NO on high-confidence basketball words once game >= 0.75h (halftime).
# Backtest: 71.4% WR, +33.7% ROI on test (42 trades, 19 events).
# NCAAB games ~2h, halftime ~45min in. Entry from 0.75h to 2.5h.
NCAAB_HALFTIME_ENABLED = True
NCAAB_HALFTIME_BET_DOLLARS = 5     # $5/bet
NCAAB_HALFTIME_MAX_POSITIONS = 20  # independent cap
NCAAB_HALFTIME_MAX_EVENT_DOLLARS = 30  # per-event cap
NCAAB_HALFTIME_MAX_MARKET_DOLLARS = 5  # per-market cap
NCAAB_HALFTIME_MIN_NO_CENTS = 20   # min NO price
NCAAB_HALFTIME_MAX_NO_CENTS = 70   # max NO price
NCAAB_HALFTIME_MIN_HOURS_LIVE = 0.75  # ~halftime (NCAAB halves are 20min)
NCAAB_HALFTIME_MAX_HOURS_LIVE = 2.5   # game over ~2h
NCAAB_HALFTIME_WORD_ALLOWLIST = {
    'ALLE',   # Alley-oop — 86% train WR
    'WALK',   # Walk On — 58% train, 83% test WR
    'NIL',    # NIL — 67% train WR
    'RECR',   # Recruit — 67% train, 57% test WR
    # REMOVED: ANKL, DOUB — now in NCAAB_BLACKLIST (losers in live trading)
}

# --- Degradation Curve Strategy (NBA only, layered on top of mention) ---
# Buys NO when market is below statistically-derived fair value based on
# time-into-game degradation curves. Separate from main mention strategy.
DEGRADE_ENABLED = False          # paused — low edge vs mention strategies
DEGRADE_BET_DOLLARS = 10
DEGRADE_MIN_HOURS_LIVE = 1.0   # only bet >= 1h into game
DEGRADE_MAX_POSITIONS = 20     # independent cap (does NOT share with mention)
DEGRADE_MAX_EVENT_DOLLARS = 26 # independent per-event cap
# Fair NO prices (Wilson CI lower bound, 95%, n>=20) by word & half-hour.
# If market NO <= this value, it's a buy.
# Derived from LAST 30 DAYS (63 games, Jan 17 – Feb 16 2026).
# FILTERED: only cells with ROI >= 20% at fair price.
DEGRADE_BUY_BELOW = {
    'AIR':  {'1.0': 53, '1.5': 54, '2.0': 63, '2.5': 76},   # ROI: +31/+32/+28/+22%
    'ALLE': {'1.0': 56, '1.5': 65},                           # ROI: +26/+24%
    'ANKL': {'1.0': 58, '1.5': 59, '2.0': 65},               # ROI: +26/+26/+23%
    'BUZZ': {'1.0': 58, '1.5': 65},                           # ROI: +24/+21%
    'ELBO': {'1.0': 26, '1.5': 32},                           # ROI: +58/+56%
    'JORD': {'1.0': 54, '1.5': 63, '2.5': 77},               # ROI: +30/+26/+21%
    'MVP':  {'1.0': 42, '1.5': 48, '2.0': 66},               # ROI: +36/+34/+26%
    'PLAY': {'1.0': 24, '1.5': 28, '2.0': 44},               # ROI: +59/+58/+48%
    'TECH': {'1.0': 29, '1.5': 32, '2.0': 49, '2.5': 69},   # ROI: +47/+48/+38/+30%
    'TRAD': {'1.0': 33, '1.5': 33},                           # ROI: +52/+57%
    'TRIP': {'1.0': 56, '1.5': 56, '2.0': 68},               # ROI: +25/+26/+22%
}

# --- Earnings Mention Strategy ---
# Buy NO on earnings call mention markets.
# Original 0-0.5h window: +9% ROI (thin edge). Wider 0-24h: +25.5% ROI.
# With word blacklist (0% WR words removed): +23.6% ROI on 1106 markets.
EARNINGS_ENABLED = False
EARNINGS_BET_DOLLARS = 2         # $2/bet — clean trades -43% ROI (21d), reduce exposure
EARNINGS_MIN_NO_PRICE = 0.15     # 15c (was 5c — cheap NOs are -62% ROI losers)
EARNINGS_MAX_NO_PRICE = 0.50     # 50c (was 30c — 15-50c is +20.4% ROI, $0.31/d)
EARNINGS_MAX_POSITIONS = 20      # independent cap
EARNINGS_MAX_EVENT_DOLLARS = 10  # $10 per earnings call (capped at global max)
# Entry window: any time pre-event (maker rests until event start)
# 0-4h: +13.5% ROI (896 mkts), 0-24h: +31.9% (1163 mkts)
EARNINGS_WINDOW_HOURS_BEFORE = 720  # effectively unlimited — maker rests pre-event
# Hot words to exclude — too common/misleading on earnings calls
# 0% NO WR words: INTE, TOKE, GUID, RETE, OPEN, DELI, WAYM, LOYA, DIGI, OMNI, EXPN
EARNINGS_WORD_BLACKLIST = {
    'INTE', 'TOKE', 'GUID', 'RETE', 'OPEN', 'DELI',
    'WAYM', 'LOYA', 'DIGI', 'OMNI', 'EXPN',
    # Robinhood/crypto keynotes — these words are always said
    'BITC', 'BLOC', 'CRYP', 'BANK', 'PRED', 'PERP',
    # Victoria's Secret — always said on their calls
    'FRAG', 'TARI',
}
EARNINGS_EXCLUDED_WORDS = EARNINGS_WORD_BLACKLIST  # legacy alias

# --- Stale Order Strategy (NBA + NCAAB) ---
# After 1h into game, scan orderbooks for forgotten limit orders.
# If cheapest NO ask is >=15c below the next cheapest, someone forgot to cancel.
# Backtest: 68% WR, +318% ROI (30 days, gap>=15c, NO>=5c).
STALE_ENABLED = True
STALE_BET_DOLLARS = 10            # $10/bet max
STALE_MIN_GAP_CENTS = 15          # min gap between cheapest and next NO ask
STALE_MIN_NO_CENTS = 5            # avoid 0-4c trap (words almost always said)
STALE_MIN_HOURS_INTO_GAME = 1.0   # only scan 1h+ after event start
STALE_MAX_POSITIONS = 30          # independent cap
STALE_MAX_MARKET_DOLLARS = 10     # hard cap per market

# NCAA theta re-entry — buy at T+20min if NO price hasn't moved from event start
THETA_REENTRY_ENABLED = True
THETA_REENTRY_BET = 3               # $3 per re-entry bet
THETA_REENTRY_MIN_GAME_MIN = 20     # min minutes into game before re-entry
THETA_REENTRY_MAX_GAME_MIN = 80     # max minutes (don't re-enter too late)
THETA_REENTRY_MIN_NO_CENTS = 10     # min NO price for re-entry
THETA_REENTRY_MAX_NO_CENTS = 26     # max NO price for re-entry (backtest: 10-26c)
THETA_REENTRY_MAX_SLIPPAGE = 4      # max slippage cents
THETA_REENTRY_MAX_DEVIATION = 5     # max cents NO can deviate from start price
THETA_REENTRY_MAX_MARKET_DOLLARS = 3 # per-market cap for theta entries
THETA_REENTRY_MAX_POSITIONS = 20    # independent cap

# --- NCAAB Game Outcome Fade Strategy ---
# When a pregame NCAAB favorite (YES >= 55c) sees their live moneyline drop
# to the trigger price within the first 50 minutes, buy YES expecting reversion.
# Backtest (men): vel>=0.015, drop>=0.25, trigger @45c → 65% WR, +43% ROI (30d, $20/bet).
# Backtest (women): same params → 70% WR, +75% ROI (6d sample, $15/bet).
NCAAB_FADE_ENABLED = True
NCAAB_FADE_BET_DOLLARS = 10           # $10 per trade (capped at global max)
NCAAB_FADE_TRIGGER_CENTS = 45        # Buy YES at this price (limit order)
NCAAB_FADE_MIN_PREGAME_YES = 55      # Min pregame YES price (cents) — must be a favorite
NCAAB_FADE_MIN_DROP_SIZE = 25        # Min drop in cents (pregame - trigger)
NCAAB_FADE_MIN_VELOCITY = 1.5         # Min price drop velocity (cents/min). Backtest: 0.015 in dollars/min = 1.5c/min
NCAAB_FADE_MAX_MINUTES = 50          # Only enter within first 50 min of game
NCAAB_FADE_MAX_POSITIONS = 10        # Independent position cap
NCAAB_FADE_MAX_MARKET_DOLLARS = 10   # Per-market cap (capped at global max)
NCAAB_FADE_SERIES = ['KXNCAAMBGAME', 'KXNCAAWBGAME']  # Men's + Women's
NCAAB_FADE_BET_BY_SERIES = {'KXNCAAMBGAME': 10, 'KXNCAAWBGAME': 5}  # Per-series bet sizing (men capped at $10)
NCAAB_FADE_GAME_DURATION_HOURS = 2.5 # Approximate game duration

# --- Tennis Match Outcome Fade Strategy ---
# Single tier: buy YES on favorites whose price drops early in match.
# 30-day sim: 10 trades, 9W/1L, +77.1% ROI (~2.3/wk)
TENNIS_FADE_ENABLED = True
TENNIS_FADE_SERIES = ['KXATPMATCH', 'KXWTAMATCH']  # ATP + WTA
TENNIS_FADE_MATCH_DURATION_HOURS = 2.0  # Best-of-3 estimate
TENNIS_FADE_MAX_POSITIONS = 20        # Independent position cap
TENNIS_FADE_BET_DOLLARS = 10          # $10 per trade (capped at global max)
TENNIS_FADE_TRIGGER_CENTS = 50        # Buy YES at <=50c
TENNIS_FADE_MIN_PREGAME_YES = 60      # Pregame YES >= 60c
TENNIS_FADE_MAX_MINUTES = 45          # First 45 min only
TENNIS_FADE_MIN_DROP_SIZE = 10        # Min drop 10c

# State files
STATE_DIR = Path(__file__).parent
POSITIONS_FILE = STATE_DIR / 'kalshi_positions.json'
TRADE_LOG_FILE = STATE_DIR / 'kalshi_trade_log.json'
MENTION_SIGNAL_HISTORY_FILE = STATE_DIR / 'kalshi_mention_signal_history.json'
TRADE_HISTORY_CSV = STATE_DIR / 'kalshi_trade_history.csv'
EVENT_LOG_FILE = STATE_DIR / 'kalshi_event_log.jsonl'

# --- Truth-Social cheap-word YES-buy strategy ---
# Thesis: when Trump posts a word on Truth Social within TRUTH_YES_WINDOW_H before
# one of his mention events, and YES is still priced <= TRUTH_YES_MAX_CENTS, buy YES
# as a taker. Backtest (24h window, <30c): +45.9% ROI on n=73, spread over 47 words,
# robust to leave-one-out; OOS time-split test half +71-78%. Underpowered (CI grazes
# zero) so sizing is tiny. Some words are "podium-avoided" (posted-but-never-said) —
# excluded via TRUTH_YES_AVOID_WORDS.
TRUTH_YES_ENABLED = True
TRUTH_YES_BET_DOLLARS = 5           # $5/trade (user-specified)
TRUTH_YES_MAX_CENTS = 30            # only buy YES priced <= 30c
TRUTH_YES_WINDOW_H = 24             # word must be posted within 24h before event start
TRUTH_YES_MAX_POSITIONS = 20        # independent cap across the strategy
TRUTH_YES_MAX_EVENT_DOLLARS = 40    # per-event cap
TRUTH_YES_AVOID_WORDS = {'epstein', 'shutdown', 'shut down', 'border'}
# Trump's real spellings that differ from Kalshi's listed strike word
TRUTH_YES_SYNONYMS = {'Dumbocrat / Dumacrat': ['dumocrat', 'dumbocrat', 'dumacrat']}
TRUTH_FEED_URL = 'https://trumpstruth.org/feed'
TRUTH_POSTS_CACHE = STATE_DIR / 'truth_social_recent.json'
TRUTH_TRADED_LEDGER = STATE_DIR / 'truth_yes_traded.json'  # per-word-per-event dedup (by ticker)
TRUTH_FEED_REFRESH_S = 600          # re-fetch feed at most every 10 min


# =====================================================================
# STRUCTURED EVENT LOG — append-only JSONL for post-hoc analysis
# =====================================================================

def log_event(event_type, **kwargs):
    """Append a structured event to the JSONL log file."""
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'event': event_type,
        **kwargs,
    }
    try:
        with open(EVENT_LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass  # never crash the bot for logging


# =====================================================================
# TRUTH SOCIAL FEED — recent Trump posts for the cheap-word YES strategy
# =====================================================================

class TruthSocialFeed:
    """Fetches Trump's recent Truth Social posts from the trumpstruth.org RSS
    feed and answers 'was WORD posted in [lo, hi]?' with prefix/synonym matching.

    The feed is date-filtered and capped at the newest 100 items per day, so we
    query day-by-day over a short lookback and cache the result on disk. Never
    raises — on any failure it keeps the last good cache so the bot keeps running.
    """

    _TAG = re.compile(r'<[^>]+>')

    def __init__(self):
        self.posts = []          # list of {'ts': float, 'low': str}
        self._last_fetch = 0.0
        self._rx_cache = {}
        self._load()

    def _load(self):
        try:
            if TRUTH_POSTS_CACHE.exists():
                data = json.loads(TRUTH_POSTS_CACHE.read_text())
                self.posts = data.get('posts', [])
                self._last_fetch = data.get('fetched_ts', 0.0)
        except Exception:
            self.posts = []

    def _save(self):
        try:
            TRUTH_POSTS_CACHE.write_text(json.dumps(
                {'fetched_ts': self._last_fetch, 'posts': self.posts[-1000:]}))
        except Exception:
            pass

    def refresh(self, lookback_h):
        """Re-fetch the last lookback_h hours of posts, at most every
        TRUTH_FEED_REFRESH_S seconds. Safe to call every scan cycle."""
        now = time.time()
        if now - self._last_fetch < TRUTH_FEED_REFRESH_S and self.posts:
            return
        cutoff = now - lookback_h * 3600
        today = datetime.now(timezone.utc).date()
        days = int(lookback_h // 24) + 2
        merged = {p['ts']: p for p in self.posts if p['ts'] >= cutoff}
        sess = requests.Session()
        sess.headers.update({'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
        for i in range(days):
            dd = today - timedelta(days=i)
            try:
                r = sess.get(TRUTH_FEED_URL,
                             params={'start_date': dd.isoformat(), 'end_date': dd.isoformat()},
                             timeout=25)
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.text)
            except (requests.RequestException, ET.ParseError):
                continue
            for it in root.iter('item'):
                try:
                    dt = datetime.strptime(it.findtext('pubDate') or '',
                                           '%a, %d %b %Y %H:%M:%S %z')
                except ValueError:
                    continue
                ts = dt.timestamp()
                if ts < cutoff:
                    continue
                txt = _html.unescape(self._TAG.sub(
                    ' ', it.findtext('description') or it.findtext('title') or ''))
                merged[ts] = {'ts': ts, 'low': re.sub(r'\s+', ' ', txt).strip().lower()}
            time.sleep(0.2)
        self.posts = sorted(merged.values(), key=lambda p: p['ts'])
        self._last_fetch = now
        self._save()

    def _rx(self, word):
        if word in self._rx_cache:
            return self._rx_cache[word]
        alts = TRUTH_YES_SYNONYMS.get(word) or [a.strip() for a in word.split('/') if a.strip()]
        pats = [re.escape(a.lower()) if ' ' in a else re.escape(a.lower()) + r'[a-z]*' for a in alts]
        rx = re.compile(r'\b(?:' + '|'.join(pats) + r')\b') if pats else None
        self._rx_cache[word] = rx
        return rx

    def posted(self, word, lo, hi):
        """True if `word` appears in any post with lo <= ts <= hi."""
        rx = self._rx(word)
        if rx is None:
            return False
        for p in self.posts:
            if p['ts'] < lo:
                continue
            if p['ts'] > hi:
                break
            if rx.search(p['low']):
                return True
        return False


def get_mention_category(ticker):
    """Derive mention category from ticker string. Used for per-category price ranges."""
    t = ticker.upper()
    if 'TRUMPMENTION' in t or 'TRUMPSAY' in t: return 'Trump'
    if 'MAMDANIMENTION' in t: return 'Mamdani'
    if 'NCAAMENTION' in t or 'NCAABMENTION' in t: return 'NCAA'
    if 'NBAMENTION' in t or 'NBAFINALS' in t: return 'NBA'
    if 'EARNINGSMENTION' in t: return 'Earnings'
    if 'NEWSOMMENTION' in t: return 'Newsom'
    if 'FIGHTMENTION' in t: return 'Fight'
    if 'NFLMENTION' in t: return 'NFL'
    if 'MENTION' in t:
        prefix = t.split('MENTION')[0].replace('KX', '')
        return prefix if prefix else 'Other'
    return 'Other'


def _market_cents(m, field):
    """Read a market price field in cents, handling the March 12 2026 API migration.

    Kalshi removed integer-cent fields (yes_bid, yes_ask, last_price, etc.)
    on March 12 2026. New fields use _dollars suffix and return string values
    like "0.56". This helper tries _dollars first, falls back to legacy cents.
    Returns int cents or None.
    """
    # New _dollars field (string like "0.56")
    dollars_val = m.get(f'{field}_dollars')
    if dollars_val is not None:
        try:
            return int(round(float(dollars_val) * 100))
        except (ValueError, TypeError):
            pass
    # Legacy integer cents field
    legacy = m.get(field)
    if legacy is not None:
        try:
            return int(legacy)
        except (ValueError, TypeError):
            pass
    return None


def _market_count(m, field):
    """Read a market count/volume field, handling _fp migration.

    Kalshi removed integer count fields on March 12 2026.
    New fields use _fp suffix (fixed-point string like "150.00").
    Falls back to legacy integer field.
    Returns int or 0.
    """
    fp_val = m.get(f'{field}_fp')
    if fp_val is not None:
        try:
            return int(float(fp_val))
        except (ValueError, TypeError):
            pass
    legacy = m.get(field)
    if legacy is not None:
        try:
            return int(legacy)
        except (ValueError, TypeError):
            pass
    return 0


def get_no_range(ticker):
    """Get (min_cents, max_cents) NO price range for this ticker's category.
    Returns per-category range from CATEGORY_NO_RANGE, or global fallback."""
    cat = get_mention_category(ticker)
    if cat in CATEGORY_NO_RANGE:
        return CATEGORY_NO_RANGE[cat]
    return (int(MENTION_MIN_NO_PRICE * 100), int(MENTION_MAX_NO_PRICE * 100))


# =====================================================================
# KALSHI API CLIENT (with RSA-PSS auth)
# =====================================================================

class KalshiClient:
    def __init__(self):
        self.market_cache = {}

        self.session = requests.Session()
        self.private_key = None
        self._load_private_key()

    def _load_private_key(self):
        """Load RSA private key for API authentication.
        Supports three modes:
          1. KALSHI_PRIVATE_KEY — raw PEM content (for Railway / cloud)
          2. KALSHI_PRIVATE_KEY_B64 — base64-encoded PEM (for Railway / cloud)
          3. KALSHI_PRIVATE_KEY_PATH — path to PEM file (for local)
        """
        # Debug: show which env vars are set (not the values)
        print(f"  Key env vars: KALSHI_PRIVATE_KEY={'SET' if KALSHI_PRIVATE_KEY else 'EMPTY'} "
              f"({len(KALSHI_PRIVATE_KEY)} chars), "
              f"B64={'SET' if KALSHI_PRIVATE_KEY_B64 else 'EMPTY'} "
              f"({len(KALSHI_PRIVATE_KEY_B64)} chars), "
              f"PATH={'SET' if KALSHI_PRIVATE_KEY_PATH else 'EMPTY'}, "
              f"API_KEY_ID={'SET' if KALSHI_API_KEY_ID else 'EMPTY'}")

        # Mode 1: raw PEM content from env var (Railway)
        if KALSHI_PRIVATE_KEY:
            try:
                pem_data = KALSHI_PRIVATE_KEY.replace('\\n', '\n').encode()
                self.private_key = serialization.load_pem_private_key(pem_data, password=None)
                print("  RSA key loaded from KALSHI_PRIVATE_KEY env var")
                return
            except Exception as e:
                print(f"  WARNING: Failed to load private key from KALSHI_PRIVATE_KEY: {e}")

        # Mode 2: base64-encoded PEM from env var (Railway)
        if KALSHI_PRIVATE_KEY_B64:
            try:
                import base64
                pem_data = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
                self.private_key = serialization.load_pem_private_key(pem_data, password=None)
                print("  RSA key loaded from KALSHI_PRIVATE_KEY_B64 env var")
                return
            except Exception as e:
                print(f"  WARNING: Failed to load private key from KALSHI_PRIVATE_KEY_B64: {e}")

        # Mode 3: file path (local)
        if not KALSHI_PRIVATE_KEY_PATH:
            print("  WARNING: No KALSHI_PRIVATE_KEY, KALSHI_PRIVATE_KEY_B64, or KALSHI_PRIVATE_KEY_PATH set — trading disabled")
            return
        key_path = Path(KALSHI_PRIVATE_KEY_PATH).expanduser()
        if not key_path.exists():
            print(f"  WARNING: Private key not found at {key_path} — trading disabled")
            return
        try:
            with open(key_path, 'rb') as f:
                self.private_key = serialization.load_pem_private_key(f.read(), password=None)
            print(f"  RSA key loaded from {key_path}")
        except Exception as e:
            print(f"  WARNING: Failed to load private key: {e}")

    def _sign_request(self, method, path):
        """Generate RSA-PSS auth headers for authenticated endpoints."""
        if not self.private_key or not KALSHI_API_KEY_ID:
            return {}
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            'KALSHI-ACCESS-KEY': KALSHI_API_KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode(),
            'KALSHI-ACCESS-TIMESTAMP': timestamp,
        }

    @property
    def can_trade(self):
        return self.private_key is not None and KALSHI_API_KEY_ID != ''

    # --- Public endpoints (no auth) ---

    def get_markets(self, status='open', limit=200, cursor=None):
        params = {'status': status, 'limit': limit}
        if cursor:
            params['cursor'] = cursor
        try:
            resp = self.session.get(f'{KALSHI_BASE}/markets', params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('markets', []), data.get('cursor', '')
        except Exception as e:
            print(f'  API error (markets): {e}')
        return [], ''

    def get_market(self, ticker):
        if ticker in self.market_cache:
            return self.market_cache[ticker]
        try:
            resp = self.session.get(f'{KALSHI_BASE}/markets/{ticker}', timeout=10)
            if resp.status_code == 200:
                market = resp.json().get('market', {})
                self.market_cache[ticker] = market
                return market
        except Exception:
            pass
        return {}

    def get_trades(self, ticker=None, limit=1000, cursor=None, min_ts=None, max_ts=None):
        params = {'limit': limit}
        if ticker:
            params['ticker'] = ticker
        if cursor:
            params['cursor'] = cursor
        if min_ts:
            params['min_ts'] = int(min_ts)
        if max_ts:
            params['max_ts'] = int(max_ts)
        try:
            resp = self.session.get(f'{KALSHI_BASE}/markets/trades', params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('trades', []), data.get('cursor', '')
        except Exception as e:
            print(f'  API error (trades): {e}')
        return [], ''

    def get_current_price(self, ticker):
        market = self.get_market(ticker)
        if market:
            yes_bid = market.get('yes_bid_dollars')
            yes_ask = market.get('yes_ask_dollars')
            if yes_bid and yes_ask:
                try:
                    return (float(yes_bid) + float(yes_ask)) / 2
                except (ValueError, TypeError):
                    pass
            last = market.get('last_price_dollars')
            if last:
                try:
                    return float(last)
                except (ValueError, TypeError):
                    pass
        self.market_cache.pop(ticker, None)
        market = self.get_market(ticker)
        if market:
            last = market.get('last_price_dollars')
            if last:
                try:
                    return float(last)
                except (ValueError, TypeError):
                    pass
        return None

    def get_open_mention_markets(self):
        """Fetch all open mention markets by dynamically discovering series.

        Instead of a hardcoded list, we:
        1. Fetch all series from the API
        2. Filter for series with MENTION in ticker or mention-type keywords in title
        3. Query each for open markets
        This catches new series as Kalshi adds them (debates, rallies, interviews, etc.)
        """
        # Step 1: discover mention series (cached for 1 hour)
        now = time.time()
        if not hasattr(self, '_mention_series_cache') or now - self._mention_series_cache_ts > 3600:
            try:
                resp = self.session.get(f'{KALSHI_BASE}/series', params={'limit': 10000}, timeout=20)
                if resp.status_code == 200:
                    all_series = resp.json().get('series', [])
                    discovered = set()
                    for s in all_series:
                        ticker = (s.get('ticker', '') or '').upper()
                        title = (s.get('title', '') or '').lower()
                        # Include series with MENTION or EARNINGS in ticker
                        if 'MENTION' in ticker or 'EARNINGS' in ticker:
                            discovered.add(s.get('ticker', ''))
                        # Include series with mention-type keywords in title
                        elif any(kw in title for kw in [
                            'what will', 'say during', 'say at', 'say on', 'say in',
                            'announcer', 'commentator', 'broadcast mention',
                        ]):
                            discovered.add(s.get('ticker', ''))
                    # Always include the hardcoded list as fallback
                    for s in MENTION_SCAN_SERIES:
                        discovered.add(s)
                    self._mention_series_cache = sorted(discovered)
                    self._mention_series_cache_ts = now
                    print(f"  Mention series discovered: {len(self._mention_series_cache)}")
                else:
                    # Fallback to hardcoded list
                    self._mention_series_cache = list(MENTION_SCAN_SERIES)
                    self._mention_series_cache_ts = now
            except Exception as e:
                print(f'  Series discovery error: {e}')
                self._mention_series_cache = list(MENTION_SCAN_SERIES)
                self._mention_series_cache_ts = now

        # Step 2: query each series for open markets
        all_markets = []
        LOGGED_KEYS = ('NBA', 'NCAA', 'NCAAB', 'TRUMP', 'MAMDANI', 'NEWSOM', 'EARNINGS')
        logged_series = [s for s in self._mention_series_cache
                         if any(k in s.upper() for k in LOGGED_KEYS)]
        if not any('NBA' in s.upper() for s in self._mention_series_cache):
            print(f"  WARNING: no NBA series in discovered list ({len(self._mention_series_cache)} series)")
        for series in self._mention_series_cache:
            series_count = 0
            try:
                cursor = None
                pages = 0
                while pages < 10:
                    params = {
                        'series_ticker': series,
                        'status': 'open',
                        'limit': 200,
                    }
                    if cursor:
                        params['cursor'] = cursor
                    resp = self.session.get(
                        f'{KALSHI_BASE}/markets', params=params, timeout=15
                    )
                    if resp.status_code == 429:
                        time.sleep(2)
                        continue
                    if resp.status_code != 200:
                        if series in logged_series:
                            print(f"  {series}: HTTP {resp.status_code}")
                        break
                    data = resp.json()
                    markets = data.get('markets', [])
                    series_count += len(markets)
                    all_markets.extend(markets)
                    cursor = data.get('cursor', '')
                    pages += 1
                    if not markets or not cursor:
                        break
            except Exception as e:
                print(f'  API error (mention series {series}): {e}')
            if series in logged_series:
                print(f"  {series}: {series_count} open markets")
        return all_markets

    def get_milestones(self):
        """Fetch milestones for mention series from Kalshi API.

        Milestones contain the real-world event start time (start_date) that
        the Kalshi UI shows as "Begins in X hours". This is NOT available on
        market or event endpoints — it's a separate system.

        Uses GET /events?series_ticker=X&with_milestones=true to efficiently
        fetch milestones scoped to our mention series (vs paginating 10k+
        milestones on the standalone /milestones endpoint).

        Returns dict: event_ticker -> {'start_ts': float, 'end_ts': float|None, 'title': str}
        """
        now = time.time()
        # Cache milestones for 10 minutes
        if (hasattr(self, '_milestones_cache') and
                now - self._milestones_cache_ts < 600):
            return self._milestones_cache

        series_list = getattr(self, '_mention_series_cache', list(MENTION_SCAN_SERIES))
        # Start from previous cache so partial failures don't lose milestones
        milestone_map = dict(getattr(self, '_milestones_cache', {}))
        series_resolved = dict(getattr(self, '_series_resolved_counts', {}))

        series_ms_counts = {}  # series -> milestone count (for logging)
        sports_no_ms = []      # sport events with no milestone
        for series in series_list:
            try:
                ms_before = len(milestone_map)
                # Paginate: some series (KXNBAMENTION) have 150+ events
                cursor = None
                while True:
                    params = {
                        'series_ticker': series,
                        'limit': 200,
                        'with_milestones': 'true',
                    }
                    if cursor:
                        params['cursor'] = cursor
                    # Retry up to 3 times on rate limit
                    resp = None
                    for attempt in range(3):
                        resp = self.session.get(
                            f'{KALSHI_BASE}/events',
                            params=params,
                            timeout=15,
                        )
                        if resp.status_code != 429:
                            break
                        time.sleep(2 * (attempt + 1))
                    if resp is None or resp.status_code == 429:
                        break
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    for ms in data.get('milestones', []):
                        start_str = ms.get('start_date', '')
                        if not start_str:
                            continue
                        try:
                            start_ts = datetime.fromisoformat(
                                start_str.replace('Z', '+00:00')
                            ).timestamp()
                        except Exception:
                            continue

                        end_ts = None
                        end_str = ms.get('end_date', '')
                        if end_str:
                            try:
                                end_ts = datetime.fromisoformat(
                                    end_str.replace('Z', '+00:00')
                                ).timestamp()
                            except Exception:
                                pass

                        title = ms.get('title', '')
                        new_entry = {
                            'start_ts': start_ts,
                            'end_ts': end_ts,
                            'title': title,
                        }
                        for et in ms.get('primary_event_tickers', []):
                            # Keep the nearest future milestone (avoid stale overrides
                            # when multiple milestones map to the same event)
                            existing = milestone_map.get(et)
                            if existing:
                                old_dist = abs(existing['start_ts'] - now)
                                new_dist = abs(start_ts - now)
                                if new_dist < old_dist:
                                    milestone_map[et] = new_entry
                            else:
                                milestone_map[et] = new_entry
                        for et in ms.get('related_event_tickers', []):
                            existing = milestone_map.get(et)
                            if existing:
                                old_dist = abs(existing['start_ts'] - now)
                                new_dist = abs(start_ts - now)
                                if new_dist < old_dist:
                                    milestone_map[et] = new_entry
                            else:
                                milestone_map[et] = new_entry
                    # Count resolved events per series (for new-series bet sizing)
                    for ev in data.get('events', []):
                        ev_status = ev.get('status', '')
                        if ev_status in ('settled', 'finalized', 'closed'):
                            series_resolved[series] = series_resolved.get(series, 0) + 1

                    # Fallback: parse sub_title date for political/other events
                    # without milestones. Kalshi stores the event date in sub_title
                    # (e.g. "Feb 24, 2026") but sometimes doesn't create a milestone
                    # record. For Trump/political events we assume 9pm ET.
                    # Sports (NBA/NCAA) MUST have milestones — no fallback.
                    for ev in data.get('events', []):
                        et = ev.get('event_ticker', '')
                        if et in milestone_map:
                            continue
                        et_upper = et.upper()
                        is_sport = ('NBAMENTION' in et_upper or 'NBAFINALS' in et_upper
                                    or 'NCAAMENTION' in et_upper or 'NCAABMENTION' in et_upper)

                        # Sports: milestone required, no fallback
                        if is_sport:
                            ev_status = ev.get('status', '')
                            if ev_status not in ('settled', 'finalized', 'closed'):
                                sports_no_ms.append(et)
                            continue

                        # Political/Other fallback: parse date from sub_title
                        sub = ev.get('sub_title', '')
                        if not sub:
                            continue
                        # Strip leading "On " if present
                        sub_clean = sub.replace('On ', '').strip()
                        try:
                            # Parse "Feb 24, 2026" → assume 9pm ET = 02:00 UTC next day
                            dt = datetime.strptime(sub_clean, '%b %d, %Y')
                            # 9pm ET = next day 02:00 UTC
                            start_ts = dt.replace(
                                hour=2, minute=0, second=0,
                                tzinfo=timezone.utc,
                            ).timestamp() + 86400
                            milestone_map[et] = {
                                'start_ts': start_ts,
                                'end_ts': None,
                                'title': ev.get('title', ''),
                            }
                        except ValueError:
                            pass
                    # Check for next page
                    cursor = data.get('cursor', '')
                    if not cursor or not data.get('events', []):
                        break
                    time.sleep(0.3)
                series_ms_counts[series] = len(milestone_map) - ms_before
                # Small delay between series to avoid rate limits (295 series)
                time.sleep(0.1)
            except Exception as e:
                print(f'  Milestones fetch error ({series}): {e}')

        self._milestones_cache = milestone_map
        self._milestones_cache_ts = now
        self._series_resolved_counts = series_resolved
        print(f"  Milestones: {len(milestone_map)} events with start times")
        # Log sports series milestone counts
        sport_keys = [s for s in series_ms_counts if any(k in s.upper() for k in ('NBA', 'NCAA', 'NCAAB', 'EARNINGS'))]
        if sport_keys:
            parts = [f"{s}={series_ms_counts[s]}" for s in sport_keys]
            print(f"  Sports milestones: {', '.join(parts)}")
        if sports_no_ms:
            print(f"  Sports events WITHOUT milestone ({len(sports_no_ms)}): {sports_no_ms[:5]}")
        return milestone_map

    # --- Authenticated endpoints (trading) ---

    def get_balance(self):
        """GET /portfolio/balance — returns balance in cents."""
        path = '/trade-api/v2/portfolio/balance'
        headers = self._sign_request('GET', path)
        if not headers:
            return None
        try:
            resp = self.session.get(f'{KALSHI_BASE}/portfolio/balance', headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Try _dollars field first (string like "182.68")
                bal_dollars = data.get('balance_dollars')
                if bal_dollars is not None:
                    try:
                        return int(round(float(bal_dollars) * 100))
                    except (ValueError, TypeError):
                        pass
                return data.get('balance', 0)  # legacy cents
            else:
                print(f'  Balance error {resp.status_code}: {resp.text[:200]}')
        except Exception as e:
            print(f'  Balance error: {e}')
        return None

    def get_positions(self):
        """GET /portfolio/positions — returns list of positions."""
        path = '/trade-api/v2/portfolio/positions'
        headers = self._sign_request('GET', path)
        if not headers:
            return []
        try:
            resp = self.session.get(f'{KALSHI_BASE}/portfolio/positions', headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('market_positions', [])
            else:
                print(f'  Positions error {resp.status_code}: {resp.text[:200]}')
        except Exception as e:
            print(f'  Positions error: {e}')
        return []

    def get_orderbook(self, ticker):
        """GET /markets/{ticker}/orderbook — returns yes/no bids as [[price_cents, qty], ...].

        After March 12 2026 migration, Kalshi returns orderbook_fp with
        yes_dollars/no_dollars (string arrays). We normalize back to
        [[int_cents, int_qty], ...] for compatibility with the rest of the bot.
        """
        try:
            resp = self.session.get(f'{KALSHI_BASE}/markets/{ticker}/orderbook', timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Try new format first (orderbook_fp with _dollars arrays)
                ob_fp = data.get('orderbook_fp', {})
                if ob_fp:
                    result = {}
                    for side, key in [('yes', 'yes_dollars'), ('no', 'no_dollars')]:
                        raw = ob_fp.get(key, [])
                        levels = []
                        for entry in raw:
                            try:
                                price_cents = int(round(float(entry[0]) * 100))
                                qty = int(round(float(entry[1])))
                                levels.append([price_cents, qty])
                            except (ValueError, TypeError, IndexError):
                                continue
                        result[side] = levels
                    return result
                # Legacy format (orderbook with int arrays)
                ob = data.get('orderbook', {})
                if ob:
                    return ob
        except Exception as e:
            print(f'  Orderbook error ({ticker}): {e}')
        return {}

    def create_order(self, ticker, side, action, count, price_cents, expiration_ts=None, maker=False, force_taker=False):
        """
        POST /portfolio/orders
        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        count: number of contracts
        price_cents: limit price in cents (1-99)
        expiration_ts: optional Unix timestamp — order auto-cancels at this time
        maker: True only for resting pre-event limit orders. When MAKER_ONLY is
               set, any non-maker BUY is blocked here as a hard safety net so no
               taker entry can ever execute, regardless of calling strategy.
               In MAKER_ONLY mode EVERY order (buy AND sell) is also forced
               post_only below, so the exchange itself rejects any price that
               would cross the spread — no order can ever fill as a taker.
        force_taker: SCOPED carve-out — only the Truth-Social cheap-word YES-buy
               strategy passes this. When True, this single order is allowed to
               cross the spread (post_only off) even under MAKER_ONLY. The resting
               NO maker strategy never sets this, so its post_only behaviour is
               completely unchanged.
        Returns order dict or None.
        """
        if MAKER_ONLY and action == 'buy' and not maker and not force_taker:
            print(f"  MAKER_ONLY: blocked taker buy {ticker} {count}@{price_cents}c (maker-only mode)")
            return None
        # Hard risk caps on maker NO-buy entries (single chokepoint). Reject
        # oversized bets and out-of-range entry prices that the fill-conditioned
        # backtest shows are unprofitable (conditional ROI <= 0 above ~38c).
        if maker and action == 'buy' and side == 'no':
            if price_cents > MAX_MAKER_NO_PRICE_CENTS:
                print(f"  RISK CAP: blocked {ticker} NO buy @ {price_cents}c "
                      f"> {MAX_MAKER_NO_PRICE_CENTS}c max")
                return None
            if count * price_cents / 100.0 > MAX_TRADE_DOLLARS + 1e-9:
                print(f"  RISK CAP: blocked {ticker} {count}@{price_cents}c = "
                      f"${count * price_cents / 100:.2f} > ${MAX_TRADE_DOLLARS} max")
                return None
        # V2 create-order endpoint. The legacy POST /portfolio/orders now
        # returns HTTP 410 (deprecated_v1_order_endpoint). The v2 endpoint uses
        # a YES-centric single book: side is 'bid' (buy YES) or 'ask' (sell YES),
        # selling YES is economically buying NO at 1-price, and price is ALWAYS
        # the YES price in fixed-point dollars.
        path = '/trade-api/v2/portfolio/events/orders'
        headers = self._sign_request('POST', path)
        if not headers:
            return None
        headers['Content-Type'] = 'application/json'
        yes_price_cents = price_cents if side == 'yes' else (100 - price_cents)
        # buy YES or sell NO -> bid ; sell YES or buy NO -> ask
        book_side = 'bid' if (side == 'yes') == (action == 'buy') else 'ask'
        body = {
            'ticker': ticker,
            'side': book_side,
            'count': f'{int(count)}',
            'price': f'{yes_price_cents / 100:.4f}',
            'time_in_force': 'good_till_canceled',
            'self_trade_prevention_type': 'maker',
            # MAKER_ONLY: force post_only on EVERY order (buys and sells alike).
            # The exchange rejects any post_only order that would cross the
            # spread, so nothing can ever execute as a taker — this is the single
            # hard chokepoint, regardless of which strategy/price computed it.
            # Crossing exit-sells (execute_exit) get rejected and the position
            # simply holds to settlement, which is the intended maker thesis.
            # force_taker (Truth cheap-word YES buy only) opts THIS order out of
            # post_only so it can cross and fill as a taker.
            'post_only': False if force_taker else (True if MAKER_ONLY else bool(maker)),
            'client_order_id': str(uuid.uuid4()),
        }
        if expiration_ts:
            body['expiration_time'] = int(expiration_ts)
        try:
            resp = self.session.post(
                f'{KALSHI_BASE}/portfolio/events/orders',
                headers=headers, json=body, timeout=15,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get('order', data)
            else:
                print(f'  Order error {resp.status_code}: {resp.text[:300]}')
        except Exception as e:
            print(f'  Order error: {e}')
        return None

    def cancel_order(self, order_id):
        """DELETE /portfolio/events/orders/{order_id} (v2)"""
        path = f'/trade-api/v2/portfolio/events/orders/{order_id}'
        headers = self._sign_request('DELETE', path)
        if not headers:
            return False
        try:
            resp = self.session.delete(
                f'{KALSHI_BASE}/portfolio/events/orders/{order_id}',
                headers=headers, timeout=10,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f'  Cancel error: {e}')
        return False

    def get_order(self, order_id):
        """GET /portfolio/events/orders/{order_id} (v2)"""
        path = f'/trade-api/v2/portfolio/events/orders/{order_id}'
        headers = self._sign_request('GET', path)
        if not headers:
            return None
        try:
            resp = self.session.get(
                f'{KALSHI_BASE}/portfolio/events/orders/{order_id}',
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get('order', data)
        except Exception as e:
            print(f'  Get order error: {e}')
        return None




# =====================================================================
# MENTION BUY NO DETECTOR
# =====================================================================

class MentionBuyNoDetector:
    """Scans open mention markets and generates BUY NO signals for those
    within 4h of close_time with NO price in the 5-65c range.

    Backtest: YES is systematically overpriced on mention markets.
    Buying NO at 5-65c (ex NBA/Earnings) yields +53% ROI over 383 days,
    with 0 negative rolling 21-day windows out of 135.
    """

    def __init__(self):
        self.signal_history = {}  # ticker -> last signal timestamp
        self._load()

    def _load(self):
        try:
            with open(MENTION_SIGNAL_HISTORY_FILE, 'r') as f:
                self.signal_history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        with open(MENTION_SIGNAL_HISTORY_FILE, 'w') as f:
            json.dump(self.signal_history, f)

    def detect(self, open_markets, client, now_ts):
        """Scan open mention markets for BUY NO opportunities.

        For each market:
        - YES price must imply NO price in [5c, 65c]
        - Must not be in cooldown
        - Market must be open/active (fetched with status=open)

        NOTE: We do NOT filter by close_time. Kalshi mention markets use
        can_close_early=True and set close_time to a far-future deadline
        (sometimes 200-500h out). The actual event closes much earlier.
        The price range itself is the real filter — if NO is 5-65c,
        the market is actively trading and worth betting on.

        Returns list of signal dicts compatible with OrderExecutor.
        """
        signals = []
        debug_counts = {'total': 0, 'skipped_cat': 0, 'too_far': 0,
                        'no_price': 0, 'price_out_range': 0,
                        'cooldown': 0, 'eligible': 0,
                        'no_milestone': 0, 'too_early': 0}
        # Per-category filter tracking
        cat_debug = {}  # category -> {filter_name -> count}

        # Fetch milestones for event_start timing filter
        milestone_map = client.get_milestones()

        for m in open_markets:
            ticker = m.get('ticker', '')
            if not ticker:
                continue
            debug_counts['total'] += 1

            # Skip categories with insufficient edge:
            # Earnings: gated on EARNINGS_ENABLED (live call + milestone timing)
            # Fight: thin edge (+16%), noisy
            # Press (SecPress/Leavitt): efficiently priced (+2% ROI)
            ticker_upper = ticker.upper()
            is_earnings = 'EARNINGS' in ticker_upper
            if is_earnings and not EARNINGS_ENABLED:
                debug_counts['skipped_cat'] += 1
                continue
            if 'FIGHTMENTION' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue
            if 'SECPRESS' in ticker_upper or 'LEAVITT' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue

            # Skip if too far out. Mention markets set close_time to a
            # far-future deadline (200-500h), so close_time is NOT a reliable
            # "soon" signal — e.g. World Cup mention markets close ~2 weeks out
            # even when the game is today. Prefer the milestone event-start
            # time; fall back to close_time only when no milestone exists.
            event_ticker = m.get('event_ticker', '')
            ms_pre = milestone_map.get(event_ticker)
            # Always parse close_ts up front: the milestone branch below does not
            # touch it, but the signal dict (hours_before_close / close_ts) needs
            # it in every path. Leaving it unset crashed detect() with an
            # UnboundLocalError for any market that has a milestone.
            close_time_str = m.get('close_time', '')
            close_ts = now_ts + 24 * 3600  # default: 24h from now
            if close_time_str:
                try:
                    close_dt = datetime.fromisoformat(
                        close_time_str.replace('Z', '+00:00')
                    )
                    close_ts = close_dt.timestamp()
                except Exception:
                    pass
            if ms_pre and ms_pre.get('start_ts'):
                hours_to_event_pre = (ms_pre['start_ts'] - now_ts) / 3600
                # >24h before start = too early; precise per-category window
                # is enforced below. Earnings rest much earlier (own window).
                if hours_to_event_pre > 24 and not is_earnings:
                    debug_counts['too_early'] += 1
                    continue
            else:
                hours_to_close = (close_ts - now_ts) / 3600
                if hours_to_close > MENTION_MAX_CLOSE_HOURS and not is_earnings:
                    debug_counts['too_far'] += 1
                    continue

            # Event start timing filter — per-category windows:
            # Earnings: live to +30min (backtest: best ROI 0-20min live)
            # Trump: 0-24h before event start (backtest: +88% ROI, t=8.15)
            # Mamdani: 0-24h before event start (backtest: +235% ROI, t=7.55, N=175)
            # NCAA: live only, 0.5-1.5h after start (backtest: +151% ROI, t=9.13)
            # NBA: live only, 0.5-2h after start (backtest: +113% ROI, t=11.79)
            # Default: 0-1.5h before event start (backtest: +73% ROI, t=4.48)
            event_ticker = m.get('event_ticker', '')
            # Skip pre-recorded/scripted shows — insider edge too high
            series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
            if series in PRERECORDED_SERIES:
                debug_counts['prerecorded'] = debug_counts.get('prerecorded', 0) + 1
                continue
            # Kill categories with <10% actual WR — net losers (exact + substring)
            if _is_killed_series(series):
                debug_counts['killed_category'] = debug_counts.get('killed_category', 0) + 1
                continue
            # Active series allowlist — skip anything not in the list
            if ACTIVE_SERIES is not None and series not in ACTIVE_SERIES:
                debug_counts['paused_series'] = debug_counts.get('paused_series', 0) + 1
                continue
            is_trump = 'TRUMPMENTION' in ticker_upper
            is_mamdani = 'MAMDANIMENTION' in ticker_upper
            is_newsom = 'NEWSOMMENTION' in ticker_upper
            is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
            is_nba = 'NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper
            _cat = get_mention_category(ticker)
            if _cat not in cat_debug:
                cat_debug[_cat] = {'total': 0, 'no_ms': 0, 'timing': 0, 'price': 0, 'cooldown': 0, 'blacklist': 0, 'eligible': 0}
            cat_debug[_cat]['total'] += 1

            # Word blacklist — skip words almost always said
            word_suffix = ticker.split('-')[-1].upper()
            # Master blacklist — checked FIRST, overrides everything (including allowlists)
            if is_nba and word_suffix in NBA_BLACKLIST:
                cat_debug[_cat]['blacklist'] = cat_debug[_cat].get('blacklist', 0) + 1
                continue
            if is_ncaa and word_suffix in NCAAB_BLACKLIST:
                cat_debug[_cat]['blacklist'] = cat_debug[_cat].get('blacklist', 0) + 1
                continue
            # NBA halftime allowlist (only matters for words NOT in master blacklist)
            if is_nba and NBA_HALFTIME_ENABLED:
                if word_suffix not in NBA_HALFTIME_WORD_ALLOWLIST:
                    cat_debug[_cat]['blacklist'] = cat_debug[_cat].get('blacklist', 0) + 1
                    continue
            if word_suffix in EARNINGS_WORD_BLACKLIST:
                cat_debug[_cat]['blacklist'] = cat_debug[_cat].get('blacklist', 0) + 1
                continue

            ms = milestone_map.get(event_ticker)
            if ms:
                # Skip if milestone end_date has passed (event is over)
                end_ts = ms.get('end_ts')
                if end_ts and end_ts < now_ts:
                    debug_counts['too_far'] += 1
                    cat_debug[_cat]['timing'] += 1
                    continue
                event_start_ts = ms.get('start_ts', 0)
                hours_to_event = (event_start_ts - now_ts) / 3600
                if is_earnings:
                    # Earnings: maker pre 0-24h + live taker when surging/h2e<=0
                    if hours_to_event > EARNINGS_WINDOW_HOURS_BEFORE:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    if hours_to_event < -1:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                elif is_ncaa:
                    # NCAA: pre 1-24h + live window
                    # NCAAB (basketball): live to 2.5h (halftime strat needs 0.75-2.5h)
                    # NCAA (football): live to 1.5h
                    is_ncaab_det = 'NCAABMENTION' in ticker_upper
                    ncaa_max_live = 2.5 if is_ncaab_det else 1.5
                    ncaa_skip_count = cat_debug[_cat].get('_logged', 0)
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    if hours_to_event < -ncaa_max_live:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        if ncaa_skip_count < 3:
                            print(f"    NCAA timing skip: {ticker} h2e={hours_to_event:.2f}h ({event_ticker})")
                            cat_debug[_cat]['_logged'] = ncaa_skip_count + 1
                        continue
                    # Skip gap: last 1h pre-event through first 0.5h live
                    if 1 > hours_to_event > -0.5:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        if ncaa_skip_count < 3:
                            print(f"    NCAA gap skip: {ticker} h2e={hours_to_event:.2f}h ({event_ticker})")
                            cat_debug[_cat]['_logged'] = ncaa_skip_count + 1
                        continue
                elif is_nba:
                    # NBA: pre-event maker up to 24h + live taker from halftime (~1.3h) to 3h
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        nba_skip_count = cat_debug[_cat].get('_logged', 0)
                        if nba_skip_count < 3:
                            print(f"    NBA timing skip: {ticker} h2e={hours_to_event:.2f}h ({event_ticker})")
                            cat_debug[_cat]['_logged'] = nba_skip_count + 1
                        continue
                    if hours_to_event < -NBA_HALFTIME_MAX_HOURS_LIVE:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    # Gap: last 0h pre-event through first 0.5h live — no taker, but
                    # maker orders placed earlier keep running
                    # (price filter handles maker vs taker routing downstream)
                elif is_trump:
                    # Trump: maker pre 0-24h + live taker when volume surging
                    # Shows start ~20min before milestone, so allow h2e down to -2h
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    if hours_to_event < -2:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                elif is_mamdani or is_newsom:
                    # Mamdani/Newsom: maker pre 0-24h + live window to -2h
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    if hours_to_event < -2:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                else:
                    # Other: taker pre 1h to live 0.5h, maker up to 24h pre
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
                    if hours_to_event < -0.5:
                        debug_counts['too_far'] += 1
                        cat_debug[_cat]['timing'] += 1
                        continue
            else:
                # No milestone found — skip and log.
                # Milestones are required for timing windows.
                if debug_counts.get('no_milestone', 0) < 3:
                    print(f"    [no-milestone] {ticker} ({_cat}) — skipping, no milestone for {event_ticker}")
                debug_counts['no_milestone'] += 1
                cat_debug[_cat]['no_ms'] += 1
                continue

            # Get current YES price → derive NO price
            yes_price = None
            yes_bid = _market_cents(m, 'yes_bid')
            yes_ask = _market_cents(m, 'yes_ask')
            if yes_bid is not None and yes_ask is not None:
                yes_price = (yes_bid + yes_ask) / 2 / 100
            if yes_price is None:
                last = _market_cents(m, 'last_price')
                if last is not None:
                    yes_price = last / 100
            if yes_price is None:
                # Kalshi's /markets summary fields (yes_bid/ask/last_price) are
                # sometimes None even when the orderbook has real resting
                # liquidity — observed on World Cup mention markets, which were
                # being silently dropped here as "no_price". We've already passed
                # the timing gate (only in-window milestoned markets reach this
                # point), so fetch the orderbook directly and derive YES from
                # top-of-book: best_yes_bid = max yes bids; yes_ask = 100 - best_no_bid.
                ob = client.get_orderbook(ticker)
                yb_raw = ob.get('yes', []) if ob else []
                nb_raw = ob.get('no', []) if ob else []
                ob_yes_bid = max((b[0] for b in yb_raw), default=None)
                ob_no_bid = max((b[0] for b in nb_raw), default=None)
                ob_yes_ask = (100 - ob_no_bid) if ob_no_bid is not None else None
                if ob_yes_bid is not None and ob_yes_ask is not None:
                    yes_price = (ob_yes_bid + ob_yes_ask) / 2 / 100
                elif ob_yes_ask is not None:
                    yes_price = ob_yes_ask / 100
                elif ob_yes_bid is not None:
                    yes_price = ob_yes_bid / 100
            if yes_price is None:
                debug_counts['no_price'] += 1
                cat_debug[_cat]['price'] += 1
                continue

            no_price = 1 - yes_price
            no_price_c = int(no_price * 100)

            # Per-category price ranges from CATEGORY_NO_RANGE (backtest-optimized)
            _min_c, _max_c = get_no_range(ticker)
            # Override price range for halftime strategies when game is live
            is_ncaab_det = 'NCAABMENTION' in ticker_upper
            if is_ncaab_det and NCAAB_HALFTIME_ENABLED and word_suffix in NCAAB_HALFTIME_WORD_ALLOWLIST:
                hours_into_game = -hours_to_event if hours_to_event is not None else 0
                if hours_into_game >= NCAAB_HALFTIME_MIN_HOURS_LIVE:
                    _min_c, _max_c = NCAAB_HALFTIME_MIN_NO_CENTS, NCAAB_HALFTIME_MAX_NO_CENTS
            if is_nba and NBA_HALFTIME_ENABLED and word_suffix in NBA_HALFTIME_WORD_ALLOWLIST:
                hours_into_game = -hours_to_event if hours_to_event is not None else 0
                if hours_into_game >= NBA_HALFTIME_MIN_HOURS_LIVE:
                    _min_c, _max_c = NBA_HALFTIME_MIN_NO_CENTS, NBA_HALFTIME_MAX_NO_CENTS
            min_no, max_no = _min_c / 100, _max_c / 100
            if no_price < min_no or no_price > max_no:
                # Mid-price is out of range — but for pre-event maker-eligible
                # markets, check if NO bid + 1c is in range (resting order target).
                # Wide spreads (e.g. NO bid 20c, ask 46c, mid 33c) would otherwise
                # be rejected even though 21c is a valid resting order price.
                maker_eligible = (
                    hours_to_event is not None
                    and hours_to_event > PREMARKET_CANCEL_HOURS
                    and ('MENTION' in ticker.upper() or 'FINALS' in ticker.upper())
                )
                no_bid_price = None
                if maker_eligible and yes_ask is not None:
                    try:
                        no_bid_price = 1 - int(yes_ask) / 100  # NO bid = 1 - YES ask
                    except (ValueError, TypeError):
                        pass
                if maker_eligible and no_bid_price is not None:
                    maker_price = no_bid_price + 0.01  # bid + 1c
                    if min_no <= maker_price <= max_no:
                        # Let through for maker fallback — override no_price
                        # so downstream sees the maker-relevant price
                        pass
                    else:
                        debug_counts['price_out_range'] += 1
                        cat_debug[_cat]['price'] += 1
                        if cat_debug[_cat].get('_price_logged', 0) < 5:
                            h2e_str = f"{hours_to_event:.2f}h" if hours_to_event is not None else "?"
                            print(f"    {_cat} price_OOR: {ticker} no_mid={no_price:.2f} no_bid={no_bid_price:.2f} [{min_no:.2f}-{max_no:.2f}] h2e={h2e_str}")
                            cat_debug[_cat]['_price_logged'] = cat_debug[_cat].get('_price_logged', 0) + 1
                        continue
                else:
                    debug_counts['price_out_range'] += 1
                    cat_debug[_cat]['price'] += 1
                    if cat_debug[_cat].get('_price_logged', 0) < 5:
                        h2e_str = f"{hours_to_event:.2f}h" if hours_to_event is not None else "?"
                        print(f"    {_cat} price_OOR: {ticker} no={no_price:.2f} [{min_no:.2f}-{max_no:.2f}] h2e={h2e_str}")
                        cat_debug[_cat]['_price_logged'] = cat_debug[_cat].get('_price_logged', 0) + 1
                    continue

            # Cooldown check — 24h per ticker
            last_signal = self.signal_history.get(ticker, 0)
            if now_ts - last_signal < 24 * 3600:
                debug_counts['cooldown'] += 1
                cat_debug[_cat]['cooldown'] += 1
                if cat_debug[_cat].get('_cd_logged', 0) < 3:
                    ago_h = (now_ts - last_signal) / 3600
                    print(f"    {_cat} cooldown: {ticker} (traded {ago_h:.1f}h ago)")
                    cat_debug[_cat]['_cd_logged'] = cat_debug[_cat].get('_cd_logged', 0) + 1
                continue

            debug_counts['eligible'] += 1
            cat_debug[_cat]['eligible'] += 1

            # NOTE: cooldown is now set AFTER order is placed (in run loop)
            # so markets aren't blocked before passing the entry window check

            title = m.get('title', ticker)
            no_price_cents = int(no_price * 100)
            hours_to_event_val = round(hours_to_event, 2) if hours_to_event is not None else None

            # Volume as proxy for "event is live" — high volume = active event
            volume_24h = 0
            try:
                volume_24h = _market_count(m, 'volume_24h')
            except (ValueError, TypeError):
                pass
            open_interest = 0
            try:
                open_interest = _market_count(m, 'open_interest')
            except (ValueError, TypeError):
                pass

            signals.append({
                'ticker': ticker,
                'title': title,
                'event_ticker': event_ticker,
                'fade_action': 'SELL',   # "selling YES" = buying NO
                'fade_side': 'no',
                'entry_price': round(yes_price, 4),
                'pre_signal_price': round(yes_price, 4),
                'price_move': 0,
                'n_small_trades': 0,
                'retail_contracts': 0,
                'signal_time': now_ts,
                'signal_type': 'earnings_buy_no' if is_earnings else 'mention_buy_no',
                'is_earnings': is_earnings,
                'no_price': round(no_price, 4),
                'no_price_cents': no_price_cents,
                'hours_before_close': round((close_ts - now_ts) / 3600, 2),
                'hours_to_event': hours_to_event_val,
                'close_ts': close_ts,
                'volume_24h': volume_24h,
                'open_interest': open_interest,
            })

        # Print debug breakdown
        self._last_filter_stats = dict(debug_counts)
        print(f"  Mention filter: {debug_counts['total']} checked, "
              f"{debug_counts['skipped_cat']} skipped(cat), "
              f"{debug_counts.get('prerecorded', 0)} prerecorded, "
              f"{debug_counts.get('paused_series', 0)} paused, "
              f"{debug_counts['no_milestone']} no milestone, "
              f"{debug_counts['too_early']} too early, "
              f"{debug_counts['too_far']} too far, "
              f"{debug_counts['no_price']} no price, "
              f"{debug_counts['price_out_range']} price OOR, "
              f"{debug_counts['cooldown']} cooldown, "
              f"{debug_counts['eligible']} eligible"
)
        # Per-category breakdown (only categories with markets)
        for cat_name in ('NCAA', 'NBA', 'Trump', 'Mamdani', 'Other'):
            cd = cat_debug.get(cat_name)
            if cd and cd['total'] > 0:
                bl = cd.get('blacklist', 0)
                bl_str = f"{bl} blacklist, " if bl else ""
                print(f"    {cat_name}: {cd['total']} mkts → "
                      f"{cd['no_ms']} no_ms, {bl_str}{cd['timing']} timing, "
                      f"{cd['price']} price, {cd['cooldown']} cooldown, "
                      f"{cd['eligible']} eligible")

        return signals


# =====================================================================
# POLITICAL PCT_WORDS_SAID DETECTOR
# =====================================================================

class PoliticalPctDetector:
    """Detects political/media mention events where >=50% of words have been
    said (YES resolved), then generates BUY NO signals on remaining markets.

    Backtest: 952 trades, 60.6% WR, +18.7% ROI (full dataset).
    Recent 30d: 69% WR, +46.5% ROI. Edge comes from timing, not word selection.

    How it works:
    1. Group all open mention markets by event_ticker
    2. For each event, count markets where YES price >= 98 (word was said)
    3. Compute pct_words_said = yes_resolved / total_markets
    4. If pct >= threshold, emit BUY NO signals on remaining unsettled markets
    """

    def __init__(self):
        self._cooldown = {}  # ticker -> timestamp (no duplicate market entries)

    def _is_sports_or_earnings(self, ticker):
        t = ticker.upper()
        for sp in POLITICAL_EXCLUDE_SPORTS:
            if sp in t:
                return True
        return False

    def _is_rally(self, title):
        """Detect Trump rallies — excluded due to 34% WR (words get said)."""
        if not POLITICAL_EXCLUDE_RALLY:
            return False
        u = (title or '').upper()
        return 'RALLY' in u

    def _is_prerecorded(self, event_ticker):
        """Check if event belongs to a pre-recorded series."""
        series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
        return series in PRERECORDED_SERIES

    def detect(self, open_markets, client, now_ts):
        """Scan open mention markets for political pct_words_said signals.

        Args:
            open_markets: list of market dicts from Kalshi API
            client: KalshiClient instance (for milestones)
            now_ts: current timestamp

        Returns: list of signal dicts for OrderExecutor
        """
        if not POLITICAL_PCT_ENABLED:
            return []

        milestones = client.get_milestones()

        # Group markets by event_ticker
        events = {}  # event_ticker -> list of market dicts
        for m in open_markets:
            ticker = m.get('ticker', '')
            event_ticker = m.get('event_ticker', '')
            if not ticker or not event_ticker:
                continue
            # Skip sports, earnings, pre-recorded
            if self._is_sports_or_earnings(ticker) or self._is_sports_or_earnings(event_ticker):
                continue
            if self._is_prerecorded(event_ticker):
                continue
            # Must be a mention market
            if 'MENTION' not in ticker.upper() and 'MENTION' not in event_ticker.upper():
                continue
            # Skip killed categories (exact + substring)
            series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
            if _is_killed_series(series):
                continue
            events.setdefault(event_ticker, []).append(m)

        signals = []
        n_events_checked = 0
        n_events_qualified = 0
        n_rally_skipped = 0

        for event_ticker, event_markets in events.items():
            total_markets = len(event_markets)
            if total_markets < POLITICAL_PCT_MIN_MARKETS:
                continue

            n_events_checked += 1

            # Check milestone — event must be live (started)
            ms = milestones.get(event_ticker)
            if not ms:
                continue
            event_start_ts = ms.get('start_ts', 0)
            event_end_ts = ms.get('end_ts')
            title = ms.get('title', '')

            # Must have started
            if event_start_ts > now_ts:
                continue
            # Must not have ended
            if event_end_ts and event_end_ts < now_ts:
                continue

            # Skip rallies
            if self._is_rally(title):
                n_rally_skipped += 1
                continue

            # Count YES-resolved markets (last_price >= 98 OR result == 'yes')
            yes_count = 0
            for m in event_markets:
                result = m.get('result', '')
                last_price = _market_cents(m, 'last_price') or 0
                if result == 'yes' or last_price >= 98:
                    yes_count += 1

            pct_said = yes_count / total_markets
            if pct_said < POLITICAL_PCT_THRESHOLD:
                continue

            n_events_qualified += 1

            # Emit signals for remaining unsettled markets with NO in range
            for m in event_markets:
                ticker = m.get('ticker', '')
                result = m.get('result', '')
                last_price = _market_cents(m, 'last_price') or 0
                # Skip already resolved (YES or NO)
                if result in ('yes', 'no') or last_price >= 98:
                    continue

                # Get YES price → derive NO price
                yes_price = None
                yes_bid = _market_cents(m, 'yes_bid')
                yes_ask = _market_cents(m, 'yes_ask')
                if yes_bid is not None and yes_ask is not None:
                    yes_price = (yes_bid + yes_ask) / 2 / 100
                if yes_price is None:
                    lp = _market_cents(m, 'last_price')
                    if lp is not None:
                        yes_price = lp / 100
                if yes_price is None:
                    continue

                no_price = 1 - yes_price
                no_cents = int(no_price * 100)
                if no_cents < POLITICAL_PCT_MIN_NO_CENTS or no_cents > POLITICAL_PCT_MAX_NO_CENTS:
                    continue

                # Cooldown — 24h per ticker (no duplicate market entries)
                last_sig = self._cooldown.get(ticker, 0)
                if now_ts - last_sig < 24 * 3600:
                    continue

                signals.append({
                    'ticker': ticker,
                    'title': m.get('title', ticker),
                    'event_ticker': event_ticker,
                    'fade_action': 'SELL',
                    'fade_side': 'no',
                    'entry_price': round(yes_price, 4),
                    'pre_signal_price': round(yes_price, 4),
                    'price_move': 0,
                    'n_small_trades': 0,
                    'retail_contracts': 0,
                    'signal_time': now_ts,
                    'signal_type': 'political_pct_no',
                    'is_earnings': False,
                    'no_price': round(no_price, 4),
                    'no_price_cents': no_cents,
                    'hours_before_close': 0,
                    'hours_to_event': round((event_start_ts - now_ts) / 3600, 2),
                    'close_ts': event_end_ts or (now_ts + 4 * 3600),
                    'volume_24h': _market_count(m, 'volume_24h'),
                    'open_interest': _market_count(m, 'open_interest'),
                    'pct_words_said': round(pct_said, 3),
                    'event_title': title,
                    'event_yes_count': yes_count,
                    'event_total_markets': total_markets,
                })

        print(f"  Political pct: {n_events_checked} events checked, "
              f"{n_events_qualified} qualified (>={POLITICAL_PCT_THRESHOLD:.0%}), "
              f"{n_rally_skipped} rallies skipped, "
              f"{len(signals)} signals")
        return signals


class StablePriceDetector:
    """Detects mention events where the first word just crossed 98c YES (trigger),
    then after 5 min checks if remaining words' YES prices are stable (within 3c).
    Stable = stubbornly overpriced → buy NO as taker.

    Backtest (test set, taker pricing):
    - YES 0.10-0.80: N=1091, Edge=+5.7c, Sharpe=0.128
    - Best: Trump 0.30-0.50, Politician 0.60-0.90, Sports 0.20-0.70

    State machine per event:
    1. No trigger yet → scan for first word crossing 98c
    2. Trigger detected → record trigger_ts + YES prices of all siblings
    3. trigger_ts + 5min elapsed → check stability, emit signals, clear trigger
    """

    def __init__(self):
        # event_ticker -> {trigger_ts, prices: {ticker: yes_cents}, event_title}
        self._triggers = {}
        self._cooldown = {}  # ticker -> timestamp (no duplicate market entries)

    @staticmethod
    def _classify_event(event_ticker):
        """Classify event into a category for YES price range lookup."""
        et = event_ticker.upper()
        if 'TRUMPMENTION' in et:
            return 'Trump'
        if any(s in et for s in ('NBAMENTION', 'NFLMENTION', 'NCAAMENTION',
                                  'NCAABMENTION', 'SNFMENTION', 'TNFMENTION',
                                  'CFBMENTION', 'FIGHTMENTION', 'SBMENTION',
                                  'NHLMENTION', 'SOCCERMENTION', 'GOLFMENTION',
                                  'UFCMENTION', 'TENNISMENTION', 'CRICKETMENTION',
                                  'WOMENTION')):
            return 'Sports'
        if 'EARNINGSMENTION' in et:
            return 'Earnings'
        if any(s in et for s in ('MADDOWMENTION', 'COLBERTMENTION', 'LASTWORDMENTION',
                                  'FOXNEWSMENTION', 'HEGSETH', 'THEWEEKNIGHT')):
            return 'Media'
        if any(s in et for s in ('NEWSOMMENTION', 'HOCHULMENTION', 'SECPRESSMENTION',
                                  'MAMDANIMENTION', 'BERNMENTION', 'POLITICS',
                                  'GOVERNORMENTION', 'VANCEMENTION', 'PSAKIMENTION')):
            return 'Politician'
        return 'Other'

    def detect(self, open_markets, client, now_ts):
        """Scan open mention markets and manage trigger state.

        Called every scan cycle (~2 min). Returns signals only when
        a trigger has matured (5+ min old) and words are price-stable.
        """
        signals = []

        # Group markets by event_ticker
        events = {}
        for m in open_markets:
            et = m.get('event_ticker', '')
            if not et or 'MENTION' not in et.upper():
                continue
            # Skip killed / prerecorded series (exact + substring)
            series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', et)
            if _is_killed_series(series) or series in PRERECORDED_SERIES:
                continue
            events.setdefault(et, []).append(m)

        # Expire old triggers (> 30 min)
        stale = [et for et, t in self._triggers.items()
                 if now_ts - t['trigger_ts'] > STABLE_PRICE_TRIGGER_EXPIRY]
        for et in stale:
            del self._triggers[et]

        # Fetch milestones once (cached 10 min inside client)
        milestones = client.get_milestones()

        n_new_triggers = 0
        n_mature = 0

        for event_ticker, event_markets in events.items():
            if len(event_markets) < 3:  # Need enough siblings for meaningful signal
                continue

            ms = milestones.get(event_ticker)
            if not ms:
                continue
            event_start_ts = ms.get('start_ts', 0)
            event_end_ts = ms.get('end_ts')
            # Only look at live events (started and not ended)
            if event_start_ts > now_ts:
                continue
            if event_end_ts and event_end_ts < now_ts:
                continue

            category = self._classify_event(event_ticker)
            # Exclude Media (negative edge in backtest)
            if STABLE_PRICE_EXCLUDE_MEDIA and category == 'Media':
                continue
            yes_min, yes_max = STABLE_PRICE_YES_MIN, STABLE_PRICE_YES_MAX

            # Check if any word just crossed 98c (trigger)
            has_trigger = event_ticker in self._triggers
            if not has_trigger:
                # Look for first word crossing 98c
                for m in event_markets:
                    last_price = _market_cents(m, 'last_price') or 0
                    result = m.get('result', '')
                    if result == 'yes' or last_price >= STABLE_PRICE_TRIGGER_YES_CENTS:
                        # Trigger! Snapshot all sibling YES prices
                        prices = {}
                        for sib in event_markets:
                            sib_ticker = sib.get('ticker', '')
                            sib_result = sib.get('result', '')
                            sib_lp = _market_cents(sib, 'last_price') or 0
                            # Skip already resolved siblings
                            if sib_result in ('yes', 'no') or sib_lp >= 98:
                                continue
                            # Get YES price in cents (mid or last)
                            yb = _market_cents(sib, 'yes_bid')
                            ya = _market_cents(sib, 'yes_ask')
                            if yb is not None and ya is not None:
                                yes_c = round((yb + ya) / 2)
                            elif _market_cents(sib, 'last_price') is not None:
                                yes_c = _market_cents(sib, 'last_price')
                            else:
                                continue
                            prices[sib_ticker] = yes_c
                        if prices:
                            self._triggers[event_ticker] = {
                                'trigger_ts': now_ts,
                                'prices': prices,
                                'event_title': ms.get('title', '')[:60],
                            }
                            n_new_triggers += 1
                        break  # Only need one trigger word per event

            # Check mature triggers (>= 5 min old)
            trig = self._triggers.get(event_ticker)
            if not trig:
                continue
            age = now_ts - trig['trigger_ts']
            if age < STABLE_PRICE_DELAY_SECONDS:
                continue

            n_mature += 1

            # Build current price map
            current_prices = {}
            market_map = {}
            for m in event_markets:
                t = m.get('ticker', '')
                result = m.get('result', '')
                lp = _market_cents(m, 'last_price') or 0
                if result in ('yes', 'no') or lp >= 98:
                    continue
                yb = _market_cents(m, 'yes_bid')
                ya = _market_cents(m, 'yes_ask')
                if yb is not None and ya is not None:
                    current_prices[t] = round((yb + ya) / 2)
                elif _market_cents(m, 'last_price') is not None:
                    current_prices[t] = _market_cents(m, 'last_price')
                market_map[t] = m

            # Check stability: |current - trigger| <= threshold
            for ticker, trigger_yes_c in trig['prices'].items():
                if ticker not in current_prices:
                    continue
                now_yes_c = current_prices[ticker]
                delta = abs(now_yes_c - trigger_yes_c)
                if delta > STABLE_PRICE_STABILITY_CENTS:
                    continue  # Price moved — not stable

                # YES price range filter (category-specific)
                if now_yes_c < yes_min or now_yes_c > yes_max:
                    continue

                # Cooldown — 24h per ticker
                if now_ts - self._cooldown.get(ticker, 0) < 24 * 3600:
                    continue

                m = market_map.get(ticker)
                if not m:
                    continue

                no_cents = 100 - now_yes_c
                no_price = no_cents / 100

                signals.append({
                    'ticker': ticker,
                    'title': m.get('title', ticker),
                    'event_ticker': event_ticker,
                    'fade_action': 'SELL',
                    'fade_side': 'no',
                    'entry_price': round(now_yes_c / 100, 4),
                    'pre_signal_price': round(trigger_yes_c / 100, 4),
                    'price_move': now_yes_c - trigger_yes_c,
                    'n_small_trades': 0,
                    'retail_contracts': 0,
                    'signal_time': now_ts,
                    'signal_type': 'stable_price_no',
                    'is_earnings': 'EARNINGSMENTION' in event_ticker.upper(),
                    'no_price': round(no_price, 4),
                    'no_price_cents': no_cents,
                    'hours_before_close': 0,
                    'hours_to_event': round((event_start_ts - now_ts) / 3600, 2),
                    'close_ts': event_end_ts or (now_ts + 4 * 3600),
                    'volume_24h': _market_count(m, 'volume_24h'),
                    'open_interest': _market_count(m, 'open_interest'),
                    'trigger_yes_cents': trigger_yes_c,
                    'current_yes_cents': now_yes_c,
                    'price_delta_cents': delta,
                    'trigger_age_seconds': int(age),
                    'event_title': trig['event_title'],
                    'category': category,
                })

            # Clear mature trigger — it's been processed
            del self._triggers[event_ticker]

        if n_new_triggers or n_mature or signals:
            print(f"  Stable-price: {n_new_triggers} new triggers, "
                  f"{n_mature} mature, {len(signals)} signals, "
                  f"{len(self._triggers)} pending")
        return signals


# =====================================================================
# TRADE LOGGER
# =====================================================================

class TradeLogger:
    def __init__(self):
        self.log = []
        self._load()

    def _load(self):
        try:
            with open(TRADE_LOG_FILE, 'r') as f:
                self.log = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.log = []

    def _save(self):
        with open(TRADE_LOG_FILE, 'w') as f:
            json.dump(self.log[-500:], f, indent=2)

    def record(self, entry):
        entry['logged_at'] = datetime.now(timezone.utc).isoformat()
        self.log.append(entry)
        self._save()

    def daily_pnl(self):
        """Sum realized P&L for today (UTC)."""
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        total = 0
        for e in self.log:
            if e.get('logged_at', '').startswith(today) and 'actual_pnl_dollars' in e:
                total += e['actual_pnl_dollars']
        return total


# =====================================================================
# DYNAMIC BET SIZER
# =====================================================================

def calculate_bet_size(orderbook, side, entry_price_cents):
    """
    Calculate bet size from orderbook depth.
    We're buying NO side (since SELL-only = fading YES buyers).

    Kalshi orderbook only returns BIDS (not asks).
    To buy NO, we look at YES bids: a YES bid at price P = NO ask at (100-P).
    Returns (contracts, price_cents, uncapped_dollars) or (0, 0, 0) if too thin.
    """
    # YES bids represent the prices where someone will sell NO to us.
    # A YES bid at P cents means we can buy NO at (100-P) cents.
    yes_bids = orderbook.get('yes', [])
    if isinstance(yes_bids, dict):
        yes_bids = yes_bids.get('bids', [])
    if not yes_bids:
        return 0, 0, 0

    # Convert YES bids to NO ask prices: [no_price, quantity]
    no_asks = [[100 - b[0], b[1]] for b in yes_bids]

    # Sort asks by price ascending (best/cheapest first)
    asks_sorted = sorted(no_asks, key=lambda x: x[0])

    # Take top 3 levels
    top_levels = asks_sorted[:3]
    if not top_levels:
        return 0, 0, 0

    total_contracts = sum(level[1] for level in top_levels)
    best_ask_cents = top_levels[0][0]

    # Each contract costs best_ask_cents cents, so depth in dollars:
    depth_dollars = sum(level[0] * level[1] / 100 for level in top_levels)

    # Our bet = DEPTH_FRACTION of depth, capped
    uncapped_dollars = round(depth_dollars * DEPTH_FRACTION, 2)
    bet_dollars = min(uncapped_dollars, MAX_BET_DOLLARS)
    if bet_dollars < MIN_BET_DOLLARS:
        return 0, 0, uncapped_dollars

    # Convert to contracts at the best ask price
    contracts = int(bet_dollars / (best_ask_cents / 100))
    if contracts < 1:
        return 0, 0, uncapped_dollars

    return contracts, best_ask_cents, uncapped_dollars


# =====================================================================
# POSITION TRACKER (enhanced for real trading)
# =====================================================================

class KalshiPositionTracker:
    def __init__(self):
        self.positions = []
        self.closed = []
        self._load()

    def _load(self):
        try:
            with open(POSITIONS_FILE, 'r') as f:
                data = json.load(f)
                self.positions = data.get('open', [])
                self.closed = data.get('closed', [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # One-time backfill: write existing positions to CSV if the file doesn't exist yet
        if not TRADE_HISTORY_CSV.exists() and (self.positions or self.closed):
            for pos in self.closed:
                exit_type = 'EXIT_SETTLED' if pos.get('status') == 'settled' else 'EXIT_CLOSED'
                self._log_trade_csv(pos, exit_type)
            for pos in self.positions:
                self._log_trade_csv(pos, 'ENTRY')

    def _save(self):
        with open(POSITIONS_FILE, 'w') as f:
            json.dump({'open': self.positions, 'closed': self.closed[-100:]}, f, indent=2)

    def _log_trade_csv(self, pos, event_type):
        """Append a row to the CSV trade history. Called on every entry and exit."""
        csv_headers = [
            'timestamp', 'event_type', 'ticker', 'title', 'signal_type',
            'fade_action', 'fade_side', 'entry_price', 'fill_price',
            'exit_price', 'pre_signal_price', 'price_move',
            'n_small_trades', 'retail_contracts', 'fill_count', 'bet_dollars',
            'roi_pct', 'pnl_dollars', 'status', 'entry_time', 'close_time',
            'is_live',
        ]
        file_exists = TRADE_HISTORY_CSV.exists()
        with open(TRADE_HISTORY_CSV, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=csv_headers, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()

            entry_ts = pos.get('entry_time')
            close_ts = pos.get('close_time')
            entry_dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M') if entry_ts else ''
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M') if close_ts else ''

            # Compute PnL dollars
            pnl = pos.get('settle_pnl')  # mention
            if pnl is None:
                fill_price = pos.get('fill_price', pos.get('entry_price', 0))
                exit_price = pos.get('exit_price', 0)
                fill_count = pos.get('fill_count', 0)
                if fill_price and exit_price and fill_count:
                    if pos.get('fade_action') == 'SELL':
                        pnl = round((fill_price - exit_price) * fill_count, 2)
                    else:
                        pnl = round((exit_price - fill_price) * fill_count, 2)

            writer.writerow({
                'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                'event_type': event_type,
                'ticker': pos.get('ticker', ''),
                'title': pos.get('title', ''),
                'signal_type': pos.get('signal_type', 'mention_buy_no'),
                'fade_action': pos.get('fade_action', ''),
                'fade_side': pos.get('fade_side', ''),
                'entry_price': pos.get('entry_price', ''),
                'fill_price': pos.get('fill_price', ''),
                'exit_price': pos.get('exit_price', ''),
                'pre_signal_price': pos.get('pre_signal_price', ''),
                'price_move': pos.get('price_move', ''),
                'n_small_trades': pos.get('n_small_trades', ''),
                'retail_contracts': pos.get('retail_contracts', ''),
                'fill_count': pos.get('fill_count', ''),
                'bet_dollars': pos.get('bet_dollars', ''),
                'roi_pct': pos.get('roi_pct', ''),
                'pnl_dollars': pnl if pnl is not None else '',
                'status': pos.get('status', ''),
                'entry_time': entry_dt,
                'close_time': close_dt,
                'is_live': pos.get('is_live', False),
            })

    def add(self, signal, order_info=None):
        signal_type = signal.get('signal_type', 'mention_buy_no')

        # All positions hold until settlement
        exit_time = signal.get('close_ts', signal['signal_time'] + 48 * 3600)

        pos = {
            'ticker': signal['ticker'],
            'event_ticker': signal.get('event_ticker', ''),
            'title': signal['title'],
            'fade_action': signal['fade_action'],
            'fade_side': signal['fade_side'],
            'entry_price': signal['entry_price'],
            'pre_signal_price': signal['pre_signal_price'],
            'price_move': signal['price_move'],
            'n_small_trades': signal['n_small_trades'],
            'retail_contracts': signal['retail_contracts'],
            'entry_time': signal['signal_time'],
            'exit_time': exit_time,
            'status': 'open',
            'signal_type': signal_type,
        }
        if signal_type in ('mention_buy_no', 'ncaa_theta_reentry', 'ncaab_fade_yes', 'tennis_fade_yes'):
            pos['no_price'] = signal.get('no_price', 0)
            pos['hold_until_settle'] = True
        if order_info:
            pos['order_id'] = order_info.get('order_id', '')
            pos['fill_price'] = order_info.get('fill_price', 0)
            pos['fill_count'] = order_info.get('fill_count', 0)
            pos['bet_dollars'] = order_info.get('bet_dollars', 0)
            pos['is_live'] = True
        else:
            pos['is_live'] = False
        self.positions.append(pos)
        self._save()
        self._log_trade_csv(pos, 'ENTRY')

    def event_exposure(self, event_ticker, signal_type=None):
        """Total dollars deployed on open positions for a given event.
        If signal_type is given, only count positions of that type."""
        if not event_ticker:
            return 0
        return sum(
            pos.get('bet_dollars', 0)
            for pos in self.positions
            if pos.get('status') == 'open'
            and pos.get('event_ticker') == event_ticker
            and (signal_type is None or pos.get('signal_type') == signal_type)
        )

    def check(self, client):
        """Check for settlements (mention/degrade positions hold until settled).
        Returns alerts list of (alert_type, position) tuples."""
        now = time.time()
        alerts = []
        still_open = []

        for pos in self.positions:
            if pos['status'] != 'open':
                continue

            # All positions hold until settlement
            client.market_cache.pop(pos['ticker'], None)
            market = client.get_market(pos['ticker'])
            status = market.get('status', 'open') if market else 'open'

            if status in ('settled', 'finalized'):
                result = market.get('result', '')
                fill_price = pos.get('fill_price', pos.get('no_price', 0))
                fill_count = pos.get('fill_count', 0)
                is_yes_buy = pos.get('signal_type') in ('mention_buy_yes', 'ncaab_fade_yes', 'tennis_fade_yes', 'truth_cheap_yes')

                if is_yes_buy:
                    # YES-buy position: wins when result='yes'
                    if result == 'yes':
                        pnl = fill_count * (1 - fill_price)
                    elif result == 'no':
                        pnl = -(fill_count * fill_price)
                    else:
                        pnl = 0
                elif result == 'no':
                    # NO-buy position: wins when result='no'
                    pnl = fill_count * (1 - fill_price)
                elif result == 'yes':
                    # NO-buy position: loses when result='yes'
                    pnl = -(fill_count * fill_price)
                else:
                    pnl = 0

                pos['settle_pnl'] = round(pnl, 2)
                pos['result'] = result
                pos['status'] = 'settled'
                pos['close_time'] = now
                self.closed.append(pos)
                self._log_trade_csv(pos, 'EXIT_SETTLED')
                alerts.append(('settled', pos))
                continue

            still_open.append(pos)

        self.positions = still_open
        self._save()
        return alerts

    def count(self, signal_type=None):
        if signal_type:
            return sum(1 for p in self.positions if p.get('signal_type', 'mention_buy_no') == signal_type)
        return len(self.positions)

    def live_count(self):
        return sum(1 for p in self.positions if p.get('is_live'))

    def has_open_ticker(self, ticker, signal_type=None):
        """Check if there's already an open position for this ticker.
        If signal_type is given, only check positions of that type."""
        if signal_type:
            return any(p.get('ticker') == ticker and p.get('signal_type') == signal_type
                       for p in self.positions)
        return any(p.get('ticker') == ticker for p in self.positions)


# =====================================================================
# ORDER EXECUTOR
# =====================================================================

class OrderExecutor:
    def __init__(self, client, trade_logger):
        self.client = client
        self.logger = trade_logger

    def execute_entry(self, signal):
        """
        Place entry order for a signal. Returns order_info dict or None.
        For SELL signals: buy NO contracts.
        For BUY signals: buy YES contracts.
        """
        ticker = signal['ticker']
        entry_cents = int(signal['entry_price'] * 100)
        order_side = signal.get('fade_side', 'no')  # 'no' for SELL fades, 'yes' for BUY fades

        # Fetch orderbook
        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        if order_side == 'no':
            # Buy NO: derive NO asks from YES bids
            contracts, best_ask_cents, uncapped_dollars = calculate_bet_size(orderbook, 'no', entry_cents)
            target_price_cents = 100 - entry_cents  # NO price = 100 - YES price
            side_label = 'NO'
        else:
            # Buy YES: use YES asks directly (derived from NO bids)
            # NO bid at P = YES ask at (100-P)
            no_bids = orderbook.get('no', []) or []
            if isinstance(no_bids, dict):
                no_bids = no_bids.get('bids', [])
            if not no_bids:
                # Fallback: just use entry price
                contracts = max(1, int(MAX_BET_DOLLARS / (entry_cents / 100)))
                best_ask_cents = entry_cents
                uncapped_dollars = MAX_BET_DOLLARS
            else:
                yes_asks = [[100 - b[0], b[1]] for b in no_bids]
                asks_sorted = sorted(yes_asks, key=lambda x: x[0])
                top_levels = asks_sorted[:3]
                if not top_levels:
                    print(f"    No YES ask levels for {ticker}, skipping")
                    return None
                best_ask_cents = top_levels[0][0]
                depth_dollars = sum(l[0] * l[1] / 100 for l in top_levels)
                uncapped_dollars = round(depth_dollars * DEPTH_FRACTION, 2)
                bet_dollars_raw = min(uncapped_dollars, MAX_BET_DOLLARS)
                if bet_dollars_raw < MIN_BET_DOLLARS:
                    return None
                contracts = int(bet_dollars_raw / (best_ask_cents / 100)) if best_ask_cents > 0 else 0
                if contracts < 1:
                    return None
            target_price_cents = entry_cents
            side_label = 'YES'

        if contracts < 1:
            print(f"    Book too thin for {ticker} (min ${MIN_BET_DOLLARS}), skipping")
            return None

        # Hard cap: re-derive contracts so dollar cost never exceeds MAX_BET_DOLLARS
        bet_dollars = round(contracts * best_ask_cents / 100, 2)
        if bet_dollars > MAX_BET_DOLLARS and best_ask_cents > 0:
            contracts = int(MAX_BET_DOLLARS / (best_ask_cents / 100))
            bet_dollars = round(contracts * best_ask_cents / 100, 2)
            if contracts < 1:
                print(f"    Can't fit within ${MAX_BET_DOLLARS} at {best_ask_cents}c, skipping")
                return None

        # Slippage check
        slippage_pct = (best_ask_cents - target_price_cents) / target_price_cents * 100 if target_price_cents > 0 else 0
        if slippage_pct > MAX_SLIPPAGE_PCT:
            print(f"    SLIPPAGE: best {side_label} ask {best_ask_cents}c vs signal {target_price_cents}c "
                  f"({slippage_pct:+.1f}% > {MAX_SLIPPAGE_PCT}%), skipping")
            return None

        capped_note = f" [depth: ${uncapped_dollars:.2f}, capped to ${MAX_BET_DOLLARS}]" if uncapped_dollars > MAX_BET_DOLLARS else ""
        print(f"    Sizing: {contracts} {side_label} @ {best_ask_cents}c (signal {target_price_cents}c, slip {slippage_pct:+.1f}%) = ${bet_dollars:.2f}{capped_note}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-{uuid.uuid4().hex[:8]}',
                'fill_price': signal['entry_price'],
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.logger.record({
                'type': 'entry',
                'ticker': ticker,
                'side': order_side,
                'action': 'buy',
                'contracts': contracts,
                'price_cents': target_price_cents,
                'bet_dollars': bet_dollars,
                'dry_run': True,
                'signal': {k: v for k, v in signal.items() if k != 'title'},
            })
            print(f"    DRY RUN: would buy {contracts} {side_label} @ {target_price_cents}c (${bet_dollars:.2f})")
            return order_info

        # Max price we'll pay: signal price + slippage tolerance
        max_price = int(target_price_cents * (1 + MAX_SLIPPAGE_PCT / 100))

        # Live order with retries -- track all placed order IDs so we can
        # clean up any that are still resting if the loop exits without a fill.
        placed_order_ids = []

        def _handle_fill(order_id, status, price):
            """Process a filled/partially-filled order and return order_info."""
            filled = status.get('quantity_filled', 0)
            remaining = status.get('remaining_count', contracts)
            avg_fill = status.get('average_fill_price', price)
            fill_slip = (avg_fill - target_price_cents) / target_price_cents * 100 if target_price_cents > 0 else 0
            actual_dollars = round(filled * avg_fill / 100, 2)
            info = {
                'order_id': order_id,
                'fill_price': avg_fill / 100,
                'fill_count': filled,
                'bet_dollars': actual_dollars,
                'slippage_pct': round(fill_slip, 2),
                'dry_run': False,
            }
            self.logger.record({
                'type': 'entry',
                'ticker': ticker,
                'order_id': order_id,
                'side': order_side,
                'action': 'buy',
                'contracts_requested': contracts,
                'contracts_filled': filled,
                'price_cents': price,
                'avg_fill_price': avg_fill,
                'signal_price': target_price_cents,
                'slippage_pct': round(fill_slip, 2),
                'bet_dollars': actual_dollars,
                'dry_run': False,
            })
            if remaining > 0:
                self.client.cancel_order(order_id)
            print(f"    FILLED: {filled}/{contracts} {side_label} @ avg {avg_fill}c "
                  f"(slip {fill_slip:+.1f}%, ${actual_dollars:.2f})")
            return info

        for attempt in range(MAX_ORDER_RETRIES + 1):
            price = best_ask_cents + attempt  # Start at best ask, bump 1c each retry
            if price > max_price:
                print(f"    Price {price}c exceeds max {max_price}c ({MAX_SLIPPAGE_PCT:.0f}% slip), stopping")
                break
            if price >= 99:
                break

            # Re-derive contract count at this price so dollar cost stays <= MAX_BET_DOLLARS
            max_dollars = MAX_BET_DOLLARS
            retry_contracts = min(contracts, int(max_dollars / (price / 100))) if price > 0 else contracts
            if retry_contracts < 1:
                print(f"    Price {price}c too high to buy even 1 contract within ${max_dollars}, stopping")
                break

            order = self.client.create_order(
                ticker=ticker,
                side=order_side,
                action='buy',
                count=retry_contracts,
                price_cents=price,
            )
            if not order:
                print(f"    Order failed (attempt {attempt + 1})")
                continue

            order_id = order.get('order_id', '')
            placed_order_ids.append(order_id)
            print(f"    Order placed: {order_id} ({retry_contracts} {side_label} @ {price}c, ${retry_contracts * price / 100:.2f})")

            # Wait for fill
            time.sleep(ORDER_WAIT_SECONDS)

            # Check fill status
            status = self.client.get_order(order_id)
            if status:
                filled = status.get('quantity_filled', 0)

                if filled > 0:
                    # Cancel any earlier resting orders before returning
                    for prev_id in placed_order_ids:
                        if prev_id != order_id:
                            self.client.cancel_order(prev_id)
                    return _handle_fill(order_id, status, price)
                else:
                    # Not filled -- cancel
                    self.client.cancel_order(order_id)
                    # Wait and KEEP re-checking until order is confirmed dead
                    # (status = 'canceled' or filled > 0). This prevents placing
                    # a new order while the old one might still fill.
                    for _wait in range(6):  # up to 3s total
                        time.sleep(0.5)
                        recheck = self.client.get_order(order_id)
                        if not recheck:
                            break
                        recheck_filled = recheck.get('quantity_filled', 0)
                        if recheck_filled > 0:
                            print(f"    Late fill detected on {order_id}")
                            for prev_id in placed_order_ids:
                                if prev_id != order_id:
                                    self.client.cancel_order(prev_id)
                            return _handle_fill(order_id, recheck, price)
                        recheck_status = recheck.get('status', '')
                        if recheck_status in ('canceled', 'cancelled'):
                            break
                    else:
                        # Couldn't confirm canceled — don't retry, bail out
                        print(f"    Could not confirm cancel for {order_id}, stopping retries to avoid dupes")
                        break
                    print(f"    Not filled at {price}c, retrying...")

        # Loop exited without a fill -- cancel ALL resting orders to prevent
        # late fills that would exceed the $50 max bet.
        for oid in placed_order_ids:
            self.client.cancel_order(oid)
            time.sleep(0.3)
            # Double-check: if any fill came through, return it
            final_check = self.client.get_order(oid)
            if final_check and final_check.get('quantity_filled', 0) > 0:
                print(f"    Late fill detected during cleanup on {oid}")
                for prev_id in placed_order_ids:
                    if prev_id != oid:
                        self.client.cancel_order(prev_id)
                return _handle_fill(oid, final_check, best_ask_cents)
        print(f"    Failed to fill after {MAX_ORDER_RETRIES + 1} attempts (all orders canceled)")
        return None

    def execute_exit(self, pos):
        """
        Place exit order for a position. Returns actual exit info.
        For SELL positions (fade_side=no): sell NO contracts back.
        For BUY positions (fade_side=yes): sell YES contracts back.
        """
        ticker = pos['ticker']
        contracts = pos.get('fill_count', 0)
        exit_side = pos.get('fade_side', 'no')  # sell the same side we bought
        side_label = 'NO' if exit_side == 'no' else 'YES'

        if not contracts or not pos.get('is_live'):
            return None

        if DRY_RUN:
            current = self.client.get_current_price(ticker)
            entry_price = pos.get('fill_price', pos['entry_price'])
            if current and entry_price:
                pnl = (entry_price - current) * contracts if pos['fade_action'] == 'SELL' else (current - entry_price) * contracts
            else:
                pnl = 0
            self.logger.record({
                'type': 'exit',
                'ticker': ticker,
                'exit_reason': pos.get('status', 'unknown'),
                'contracts': contracts,
                'exit_price': current,
                'actual_pnl_dollars': round(pnl, 2),
                'dry_run': True,
            })
            print(f"    DRY RUN: would sell {contracts} {side_label} (P&L: ${pnl:.2f})")
            return {'exit_price': current, 'pnl': round(pnl, 2)}

        orderbook = self.client.get_orderbook(ticker)

        if exit_side == 'no':
            # Sell NO: look at YES asks (someone buying YES = we sell NO to them)
            yes_asks = orderbook.get('yes', []) if orderbook else []
            if isinstance(yes_asks, dict):
                yes_asks = yes_asks.get('asks', [])
            if yes_asks:
                best_yes_ask = min(a[0] for a in yes_asks)
                sell_price = max(1, (100 - best_yes_ask) - 1)
            else:
                current = self.client.get_current_price(ticker)
                sell_price = max(1, int((1 - current) * 100) - 1) if current else 1
        else:
            # Sell YES: look at NO asks (derived from NO bids: NO bid at P = YES ask at 100-P)
            # Actually to sell YES, we want YES bids (best price someone will buy YES at)
            yes_bids = orderbook.get('yes', []) if orderbook else []
            if isinstance(yes_bids, dict):
                yes_bids = yes_bids.get('bids', [])
            if yes_bids:
                best_yes_bid = max(b[0] for b in yes_bids)
                sell_price = max(1, best_yes_bid - 1)
            else:
                current = self.client.get_current_price(ticker)
                sell_price = max(1, int(current * 100) - 1) if current else 1

        order = self.client.create_order(
            ticker=ticker,
            side=exit_side,
            action='sell',
            count=contracts,
            price_cents=sell_price,
        )
        if order:
            order_id = order.get('order_id', '')
            time.sleep(ORDER_WAIT_SECONDS)
            status = self.client.get_order(order_id)
            filled = status.get('quantity_filled', 0) if status else 0
            avg_fill = status.get('average_fill_price', sell_price) if status else sell_price

            entry_price = pos.get('fill_price', pos['entry_price'])
            pnl = round((entry_price - avg_fill / 100) * filled, 2) if pos['fade_action'] == 'SELL' else round((avg_fill / 100 - entry_price) * filled, 2)

            self.logger.record({
                'type': 'exit',
                'ticker': ticker,
                'order_id': order_id,
                'exit_reason': pos.get('status', 'unknown'),
                'contracts_requested': contracts,
                'contracts_filled': filled,
                'sell_price_cents': sell_price,
                'avg_fill_price': avg_fill,
                'actual_pnl_dollars': pnl,
                'dry_run': False,
            })
            print(f"    EXIT FILLED: {filled}/{contracts} {side_label} @ {avg_fill}c (P&L: ${pnl:.2f})")
            return {'exit_price': avg_fill / 100, 'pnl': pnl}

        print(f"    EXIT FAILED for {ticker}")
        return None


# =====================================================================
# TELEGRAM
# =====================================================================

class KalshiNotifier:
    def __init__(self):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

    async def send_mention_signal(self, sig, order_info=None, trade_label=None):
        no_cents = sig.get('no_price_cents', 0)
        hours = sig.get('hours_before_close', 0)

        url = f"\nhttps://kalshi.com/markets/{sig['ticker']}"

        if order_info:
            side = order_info.get('side', 'no').upper()
            price_c = int(order_info['fill_price'] * 100)
            if order_info.get('dry_run'):
                trade_line = (
                    f"\n[DRY RUN] Would buy {order_info['fill_count']} {side} "
                    f"@ {price_c}c (${order_info['bet_dollars']:.2f})"
                )
            else:
                trade_line = (
                    f"\nORDER FILLED: {order_info['fill_count']} {side} "
                    f"@ {price_c}c (${order_info['bet_dollars']:.2f})"
                )
        else:
            trade_line = "\n(Signal only -- no order placed)"

        label = trade_label or "MENTION BUY NO"
        msg = (
            f"KALSHI {label}\n\n"
            f"{sig['title']}\n"
            f"Ticker: {sig['ticker']}\n\n"
            f"ACTION: BUY NO at {no_cents}c\n\n"
            f"Close in: {hours:.1f}h\n"
            f"Hold: until settlement\n\n"
            f"Why: YES systematically overpriced on mention markets. "
            f"Backtest: +53% ROI, 0/135 negative 21d windows."
            f"{trade_line}"
            f"{url}"
        )
        await self._send(msg)

    async def send_startup(self, n_open, balance=None, mode='LIVE'):
        bal_line = f"Balance: ${balance/100:.2f}\n" if balance else ""
        msg = (
            f"Kalshi Auto-Trading Bot Started\n\n"
            f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}\n"
            f"Strategies: Mention BUY NO (${MENTION_BET_DOLLARS}/named, ${MENTION_BET_NCAA}/NCAA, ${MENTION_BET_OTHER}/other), Theta re-entry ({'ON' if THETA_REENTRY_ENABLED else 'OFF'}, ${THETA_REENTRY_BET}/bet at T+{THETA_REENTRY_MIN_GAME_MIN}min), Degradation ({'PAUSED' if not DEGRADE_ENABLED else f'${DEGRADE_BET_DOLLARS}/bet'})\n"
            f"Max mention positions: {MENTION_MAX_POSITIONS}\n"
            f"{bal_line}"
            f"Open positions: {n_open}\n"
            f"Scan interval: {SCAN_INTERVAL_SECONDS}s"
        )
        await self._send(msg)

    async def send_daily_summary(self, closed_positions):
        """Send daily P&L summary for bot mention trades (NO 5-30c + YES buys)."""
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # Filter: mention trades settled today
        todays = []
        for pos in closed_positions:
            sig_type = pos.get('signal_type', '')
            close_ts = pos.get('close_time')
            if not close_ts:
                continue
            close_date = datetime.fromtimestamp(close_ts, tz=timezone.utc).strftime('%Y-%m-%d')
            if close_date != today:
                continue
            fill_price = pos.get('fill_price', 0)
            if sig_type == 'mention_buy_no':
                if fill_price < 0.05 or fill_price > 0.30:
                    continue
            elif sig_type == 'mention_buy_yes':
                if fill_price < 0.05 or fill_price > 0.50:
                    continue
            else:
                continue
            todays.append(pos)

        if not todays:
            return  # Nothing settled today

        # Calculate today's stats
        wins = []
        losses = []
        for pos in todays:
            pnl = pos.get('settle_pnl', 0)
            ticker = pos.get('ticker', '')
            title = pos.get('title', '')[:40]
            fill_price = pos.get('fill_price', 0)
            fill_count = pos.get('fill_count', 0)
            cost = round(fill_count * fill_price, 2)
            result = pos.get('result', '?')
            entry = {'ticker': ticker, 'title': title, 'pnl': pnl, 'cost': cost,
                     'fill_price': fill_price, 'fill_count': fill_count, 'result': result}
            if pnl > 0:
                wins.append(entry)
            else:
                losses.append(entry)

        n = len(todays)
        total_cost = sum(p.get('fill_count', 0) * p.get('fill_price', 0) for p in todays)
        total_pnl = sum(p.get('settle_pnl', 0) for p in todays)
        wr = len(wins) / n * 100 if n > 0 else 0
        roi = total_pnl / total_cost * 100 if total_cost > 0 else 0

        # Cumulative: all-time bot mention trades (NO 5-30c + YES 5-50c)
        all_bot = [p for p in closed_positions
                   if (p.get('signal_type') == 'mention_buy_no'
                       and 0.05 <= p.get('fill_price', 0) <= 0.30)
                   or (p.get('signal_type') == 'mention_buy_yes'
                       and 0.05 <= p.get('fill_price', 0) <= 0.50)]
        cum_pnl = sum(p.get('settle_pnl', 0) for p in all_bot)
        cum_cost = sum(p.get('fill_count', 0) * p.get('fill_price', 0) for p in all_bot)
        cum_roi = cum_pnl / cum_cost * 100 if cum_cost > 0 else 0
        cum_trades = len(all_bot)
        cum_wins = sum(1 for p in all_bot if p.get('settle_pnl', 0) > 0)

        # Build message
        lines = [f"DAILY P&L SUMMARY — {today}", ""]

        lines.append(f"Settled today: {n} trades")
        lines.append(f"Wins: {len(wins)}, Losses: {len(losses)} ({wr:.0f}% WR)")
        lines.append(f"Day cost: ${total_cost:.2f}")
        lines.append(f"Day P&L: ${total_pnl:+.2f} ({roi:+.0f}% ROI)")
        lines.append("")

        if wins:
            lines.append("WINS:")
            for w in sorted(wins, key=lambda x: x['pnl'], reverse=True):
                lines.append(f"  +${w['pnl']:.2f}  {w['ticker']}")
        if losses:
            lines.append("LOSSES:")
            for l in sorted(losses, key=lambda x: x['pnl']):
                lines.append(f"  -${abs(l['pnl']):.2f}  {l['ticker']}")

        lines.append("")
        lines.append(f"ALL-TIME (NO 5-30c):")
        lines.append(f"  {cum_trades} trades, {cum_wins} wins ({cum_wins/cum_trades*100:.0f}% WR)" if cum_trades > 0 else "  0 trades")
        lines.append(f"  Cost: ${cum_cost:.2f}")
        lines.append(f"  Cumulative P&L: ${cum_pnl:+.2f} ({cum_roi:+.0f}% ROI)")

        msg = "\n".join(lines)
        await self._send(msg)

    async def send_order_event(self, event_type, ticker, **kwargs):
        """Send telegram for any order event: placed, filled, rebid, cancelled."""
        price = kwargs.get('price_cents', 0)
        contracts = kwargs.get('contracts', 0)
        dollars = kwargs.get('bet_dollars', 0)
        title = kwargs.get('title', '')
        extra = kwargs.get('extra', '')
        url = f"https://kalshi.com/markets/{ticker}"
        lines = [event_type, '']
        if title:
            lines.append(title)
        lines.append(f"Ticker: {ticker}")
        lines.append(f"{contracts} NO @ {price}c (${dollars:.2f})")
        if extra:
            lines.append(extra)
        lines.append(url)
        await self._send('\n'.join(lines))

    async def _send(self, message):
        if not self.bot or not TELEGRAM_CHAT_ID:
            print(f'[TG] {message[:200]}...')
            return
        try:
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=message,
                disable_web_page_preview=True,
            )
        except Exception as e:
            print(f'Telegram error: {e}')


# =====================================================================
# MAIN SCANNER + AUTO-TRADER
# =====================================================================

class KalshiReversionScanner:
    def __init__(self):
        self.client = KalshiClient()
        self.mention_detector = MentionBuyNoDetector()
        self.political_pct_detector = PoliticalPctDetector()
        self.stable_price_detector = StablePriceDetector()
        self.positions = KalshiPositionTracker()
        self.notifier = KalshiNotifier()
        self.trade_logger = TradeLogger()
        self.executor = OrderExecutor(self.client, self.trade_logger)
        self._last_mention_scan = 0  # timestamp of last mention scan
        self._degrade_bucket_placed = {}  # ticker -> set of hh_keys already bet on
        self._event_volume_prev = {}  # event_ticker -> (sum_volume_24h, scan_ts) from previous cycle
        self._last_daily_summary_date = ''  # YYYY-MM-DD of last daily summary sent
        self._resting_premarket_orders = {}  # order_id -> {ticker, price_cents, contracts, bet_dollars, placed_ts, category, signal}
        self._entered_this_cycle = set()  # tickers entered this scan cycle (reset each cycle)
        self._resting_file = Path(__file__).parent / 'resting_orders.json'
        self._load_resting_orders()
        self._pending_tg = []  # (event_type, ticker, kwargs) — flushed in async main loop
        self._pending_tg_raw = []  # raw text messages — flushed alongside _pending_tg
        self._theta_start_prices = {}  # ticker -> NO price (cents) at event start
        self.truth_feed = TruthSocialFeed()  # Trump Truth Social posts (cheap-word YES strat)
        self._truth_traded = self._load_truth_ledger()  # set of tickers already YES-bought

    def _queue_tg(self, event_type, ticker, **kwargs):
        """Queue a telegram notification from sync code. Flushed in async main loop."""
        self._pending_tg.append((event_type, ticker, kwargs))

    async def _flush_tg(self):
        """Send all queued telegram notifications."""
        while self._pending_tg:
            event_type, ticker, kwargs = self._pending_tg.pop(0)
            await self.notifier.send_order_event(event_type, ticker, **kwargs)
        while self._pending_tg_raw:
            msg = self._pending_tg_raw.pop(0)
            await self.notifier._send(msg)

    def _load_resting_orders(self):
        """Load resting orders from disk (survives redeploys)."""
        if self._resting_file.exists():
            try:
                data = json.loads(self._resting_file.read_text())
                self._resting_premarket_orders = data
                if data:
                    print(f"  Restored {len(data)} resting orders from disk")
            except Exception as e:
                print(f"  WARNING: Failed to load resting orders: {e}")

    def _save_resting_orders(self):
        """Persist resting orders to disk."""
        try:
            # Strip non-serializable signal data before saving
            saveable = {}
            for oid, info in self._resting_premarket_orders.items():
                entry = dict(info)
                sig = entry.get('signal', {})
                # Keep only serializable signal fields
                entry['signal'] = {k: v for k, v in sig.items()
                                   if isinstance(v, (str, int, float, bool, type(None)))}
                saveable[oid] = entry
            self._resting_file.write_text(json.dumps(saveable, indent=2))
        except Exception as e:
            print(f"  WARNING: Failed to save resting orders: {e}")

    def _is_new_series(self, event_ticker):
        """Check if an event belongs to a new/unknown series (<3 resolved events)."""
        if not event_ticker:
            return False
        series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
        if series in MENTION_SCAN_SERIES:
            return False  # Curated series are never "new"
        resolved = getattr(self.client, '_series_resolved_counts', {}).get(series, 0)
        return resolved < PREMARKET_NEW_SERIES_MIN

    def _total_event_exposure(self, event_ticker, signal_type=None):
        """Total event exposure including BOTH open positions AND resting maker orders.
        This prevents oversizing when multiple words in the same event each get maker orders."""
        if not event_ticker:
            return 0
        pos_exp = self.positions.event_exposure(event_ticker, signal_type=signal_type)
        resting_exp = sum(
            info.get('bet_dollars', 0)
            for info in self._resting_premarket_orders.values()
            if info.get('signal', {}).get('event_ticker') == event_ticker
        )
        return pos_exp + resting_exp

    def _global_ticker_exposure(self, ticker):
        """Total $ exposure on a single ticker across ALL strategies and resting orders.
        Used to enforce GLOBAL_MAX_MARKET_DOLLARS — the hard ceiling."""
        exp = sum(p.get('bet_dollars', 0) for p in self.positions.positions
                  if p.get('ticker') == ticker and p.get('status') == 'open')
        exp += sum(info.get('bet_dollars', 0) for info in self._resting_premarket_orders.values()
                   if info.get('ticker') == ticker)
        return exp

    async def run(self):
        mode = "DRY RUN" if DRY_RUN else "LIVE"
        print("=" * 60)
        print(f"KALSHI AUTO-TRADING BOT [{mode}]")
        print("=" * 60)
        print(f"Telegram: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
        print(f"Auth: {'OK' if self.client.can_trade else 'MISSING (signal-only mode)'}")
        range_summary = ', '.join(f"{cat} {lo}-{hi}c" for cat, (lo, hi) in sorted(CATEGORY_NO_RANGE.items(), key=lambda x: -x[1][1])[:6])
        print(f"Strategy 1: Mention BUY NO — per-category ranges: {range_summary}, ...")
        print(f"Strategy 2: Degradation curve — {'PAUSED' if not DEGRADE_ENABLED else f'${DEGRADE_BET_DOLLARS}/bet, NBA passive NO bids'}")
        print(f"Strategy 3: Earnings BUY NO — {'ON' if EARNINGS_ENABLED else 'OFF'}, ${EARNINGS_BET_DOLLARS}/bet, {EARNINGS_MIN_NO_PRICE*100:.0f}-{EARNINGS_MAX_NO_PRICE*100:.0f}c, {EARNINGS_WINDOW_HOURS_BEFORE*60:.0f}min pre-event")
        print(f"Strategy 4: Stale snipe (all mention markets) — {'ON' if STALE_ENABLED else 'OFF'}, ${STALE_BET_DOLLARS}/bet, gap>={STALE_MIN_GAP_CENTS}c, NO>={STALE_MIN_NO_CENTS}c, {STALE_MIN_HOURS_INTO_GAME}h+ into event")
        print(f"Strategy 5: Political pct_words_said — {'ON' if POLITICAL_PCT_ENABLED else 'OFF'}, ${POLITICAL_PCT_BET_DOLLARS}/bet, NO {POLITICAL_PCT_MIN_NO_CENTS}-{POLITICAL_PCT_MAX_NO_CENTS}c, threshold>={POLITICAL_PCT_THRESHOLD:.0%}, excl. rallies={'Y' if POLITICAL_EXCLUDE_RALLY else 'N'}")
        ncaab_words = ', '.join(sorted(NCAAB_HALFTIME_WORD_ALLOWLIST))
        print(f"Strategy 6: NCAAB halftime NO — {'ON' if NCAAB_HALFTIME_ENABLED else 'OFF'}, ${NCAAB_HALFTIME_BET_DOLLARS}/bet, NO {NCAAB_HALFTIME_MIN_NO_CENTS}-{NCAAB_HALFTIME_MAX_NO_CENTS}c, >={NCAAB_HALFTIME_MIN_HOURS_LIVE}h, words: {ncaab_words}")
        print(f"Strategy 7: NCAAB fade BUY YES — {'ON' if NCAAB_FADE_ENABLED else 'OFF'}, ${NCAAB_FADE_BET_DOLLARS}/bet, trigger={NCAAB_FADE_TRIGGER_CENTS}c, vel>={NCAAB_FADE_MIN_VELOCITY}, drop>={NCAAB_FADE_MIN_DROP_SIZE}c, <{NCAAB_FADE_MAX_MINUTES}min")
        print(f"Strategy 8: Tennis fade BUY YES — {'ON' if TENNIS_FADE_ENABLED else 'OFF'}, ${TENNIS_FADE_BET_DOLLARS}/bet, trigger={TENNIS_FADE_TRIGGER_CENTS}c, pre>={TENNIS_FADE_MIN_PREGAME_YES}c, drop>={TENNIS_FADE_MIN_DROP_SIZE}c, <={TENNIS_FADE_MAX_MINUTES}min")
        print(f"Open positions: {self.positions.count()}")
        print("=" * 60)

        # Check balance on startup
        balance = None
        if self.client.can_trade:
            balance = self.client.get_balance()
            if balance is not None:
                print(f"Account balance: ${balance/100:.2f}")
            else:
                print("WARNING: Could not fetch balance — check API keys")

            # Seed cooldown AND position tracker from existing API positions
            # so we don't re-bet tickers we already hold (survives redeploys)
            existing = self.client.get_positions()
            seeded = 0
            reconciled = 0
            for pos in existing:
                t = pos.get('ticker', '')
                # Try _fp/_dollars fields first (March 12 2026 migration)
                qty = 0
                tt_dollars = pos.get('total_traded_dollars')
                if tt_dollars:
                    try:
                        qty = float(tt_dollars)
                    except (ValueError, TypeError):
                        pass
                if not qty:
                    pos_fp = pos.get('position_fp')
                    if pos_fp:
                        try:
                            qty = float(pos_fp)
                        except (ValueError, TypeError):
                            pass
                if not qty:
                    qty = pos.get('total_traded', 0) or pos.get('position', 0) or 0
                if not t or qty <= 0:
                    continue
                # Seed mention detector cooldown
                if 'MENTION' in t.upper() and t not in self.mention_detector.signal_history:
                    self.mention_detector.signal_history[t] = time.time()
                    seeded += 1
                # Reconcile position tracker: add stub for any ticker we hold
                # but don't have in position tracker (lost on redeploy)
                if not self.positions.has_open_ticker(t):
                    # Prefer _dollars field (cents fields deprecated March 12 2026)
                    exp_str = pos.get('market_exposure_dollars')
                    if exp_str:
                        try:
                            bet_dollars = abs(float(exp_str))
                        except (ValueError, TypeError):
                            bet_dollars = qty * 0.50
                    else:
                        exposure = pos.get('market_exposure', 0)
                        bet_dollars = abs(exposure) / 100 if isinstance(exposure, (int, float)) else qty * 0.50
                    # Infer signal type from ticker
                    tu = t.upper()
                    if 'FADE' in tu or 'BGAME' in tu:
                        sig_type = 'ncaab_fade_yes' if 'BGAME' in tu else 'mention_buy_no'
                    elif 'TENNIS' in tu:
                        sig_type = 'tennis_fade_yes'
                    else:
                        sig_type = 'mention_buy_no'
                    stub = {
                        'ticker': t,
                        'event_ticker': re.sub(r'-[A-Z]{2,6}$', '', t),  # strip word suffix
                        'signal_type': sig_type,
                        'status': 'open',
                        'bet_dollars': round(bet_dollars, 2),
                        'is_live': True,
                        'hold_until_settle': True,
                        'reconciled': True,  # flag so we know this was restored
                        'opened_at': datetime.now(timezone.utc).isoformat(),
                    }
                    self.positions.positions.append(stub)
                    reconciled += 1
            if seeded:
                self.mention_detector._save()
                print(f"  Seeded cooldown from {seeded} existing mention positions")
            if reconciled:
                self.positions._save()
                print(f"  Reconciled {reconciled} positions from API (per-market caps restored)")

        await self.notifier.send_startup(self.positions.count(), balance)

        while True:
            try:
                await self._cycle()
                await self._flush_tg()
                print(f"Next scan in {SCAN_INTERVAL_SECONDS}s...")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(60)

    async def _cycle(self):
        now = time.time()
        now_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        print(f"\n[{now_str}] Scan cycle")

        low_balance = False

        # Check balance
        if self.client.can_trade:
            bal = self.client.get_balance()
            if bal is not None and bal < 100:  # less than $1
                print(f"  Low balance: ${bal/100:.2f} — skipping new orders, waiting for fills/settlements")
                log_event('low_balance', balance_cents=bal)
                low_balance = True

        # Check resting pre-event limit orders for fills / expiry
        if self._resting_premarket_orders:
            await self._check_resting_premarket_orders()

        # Mention BUY NO scan
        mention_count = self.positions.count('mention_buy_no')
        mention_allowed = mention_count < MENTION_MAX_POSITIONS
        should_scan_mentions = (now - self._last_mention_scan) >= MENTION_SCAN_INTERVAL_SECONDS

        if should_scan_mentions:
            self._last_mention_scan = now
            self._entered_this_cycle = set()  # Prevent any ticker from being entered twice per scan
            print(f"  Mention scan: fetching open mention markets...")
            mention_markets = self.client.get_open_mention_markets()
            self._last_mention_market_count = len(mention_markets)
            print(f"  Mention markets found: {len(mention_markets)}")

            if mention_markets:
                mention_signals = self.mention_detector.detect(mention_markets, self.client, now)

                # Event-level velocity: sum volume_24h across all sibling markets
                # sharing the same event_ticker, then track delta between scans.
                # Backtest: when 20%+ of event volume has traded, ROI jumps to 119-276%.
                # Event-level aggregation is a much stronger live signal than per-market.
                event_vol_now = {}  # event_ticker -> sum of volume_24h
                for m in mention_markets:
                    et = m.get('event_ticker', m.get('ticker', ''))
                    vol = 0
                    try:
                        vol = _market_count(m, 'volume_24h')
                    except (ValueError, TypeError):
                        pass
                    event_vol_now[et] = event_vol_now.get(et, 0) + vol

                # Compute event velocity (delta/min since last scan)
                event_velocity = {}
                for et, vol_sum in event_vol_now.items():
                    prev = self._event_volume_prev.get(et)
                    if prev:
                        prev_vol, prev_ts = prev
                        elapsed_min = max((now - prev_ts) / 60, 0.5)
                        delta = max(vol_sum - prev_vol, 0)
                        event_velocity[et] = round(delta / elapsed_min, 1)
                    else:
                        event_velocity[et] = 0

                # Update history for next cycle
                for et, vol_sum in event_vol_now.items():
                    self._event_volume_prev[et] = (vol_sum, now)
                stale = [et for et in self._event_volume_prev if et not in event_vol_now]
                for et in stale:
                    del self._event_volume_prev[et]

                # Fetch milestones to get real event start times
                # The Kalshi UI "Begins in X hours" comes from milestones API
                milestones = self.client.get_milestones()

                # Inject event-level metrics + milestone start time into each signal
                for sig in mention_signals:
                    et = sig.get('event_ticker', '')
                    sig['event_volume_24h'] = event_vol_now.get(et, 0)
                    sig['event_velocity'] = event_velocity.get(et, 0)

                    # Inject milestone start/end time
                    ms = milestones.get(et)
                    if ms:
                        sig['event_start_ts'] = ms['start_ts']
                        sig['event_end_ts'] = ms.get('end_ts')
                        sig['hours_to_event'] = round((ms['start_ts'] - now) / 3600, 2)
                        # Live = started and not yet ended
                        started = ms['start_ts'] <= now
                        ended = ms.get('end_ts') and ms['end_ts'] <= now
                        sig['event_live'] = started and not ended
                    elif sig.get('hours_to_event') is not None:
                        # Detector already computed hours_to_event via open_time fallback
                        sig.setdefault('event_start_ts', None)
                        sig.setdefault('event_end_ts', None)
                        sig.setdefault('event_live', sig['hours_to_event'] <= 0)
                    else:
                        sig['event_start_ts'] = None
                        sig['event_end_ts'] = None
                        sig['hours_to_event'] = None
                        sig['event_live'] = False

                # Filter: event start timing window — per-category:
                # Trump: 0-24h before event start (backtest: +88% ROI, t=8.15)
                # NCAA: live only, 0-2h after start (backtest: +98% ROI, t=4.61)
                # Default: 0-1.5h before event start
                def in_entry_window(s):
                    h = s.get('hours_to_event')
                    if h is None:
                        return False
                    ticker_up = s.get('ticker', '').upper()
                    is_ncaa = 'NCAAMENTION' in ticker_up or 'NCAABMENTION' in ticker_up
                    is_nba = 'NBAMENTION' in ticker_up or 'NBAFINALS' in ticker_up
                    if s.get('is_earnings'):
                        # Earnings: maker pre + live taker up to 1h after start
                        return -1 <= h <= EARNINGS_WINDOW_HOURS_BEFORE
                    elif is_ncaa:
                        # NCAA: pre 1-24h + live window
                        # NCAAB (basketball): live to 2.5h for halftime strat
                        is_ncaab_ew = 'NCAABMENTION' in ticker_up
                        ncaa_max = 2.5 if is_ncaab_ew else 1.5
                        return (-ncaa_max <= h <= -0.5) or (1 <= h <= 24)
                    elif is_nba:
                        # NBA: pre-event maker up to 24h + live taker from halftime to 3h
                        return -NBA_HALFTIME_MAX_HOURS_LIVE <= h <= 24
                    else:
                        # Trump, Mamdani, Newsom, etc:
                        # pre-event maker up to 24h + live window to 2h after milestone
                        return -2 <= h <= 24

                eligible = [s for s in mention_signals if in_entry_window(s)]
                n_total = len(mention_signals)
                n_with_start = sum(1 for s in mention_signals if s.get('event_start_ts'))
                n_eligible = len(eligible)
                mention_signals = eligible
                self._last_mention_signal_stats = (n_total, n_with_start, n_eligible)
                print(f"  Mention signals: {n_total} total, {n_with_start} with milestone, {n_eligible} in window")

                for sig in mention_signals:
                    sig_type = sig.get('signal_type', 'mention_buy_no')
                    is_earn = sig.get('is_earnings', False)

                    # Global scan-cycle dedup: never enter same ticker twice in one scan
                    if sig['ticker'] in self._entered_this_cycle:
                        continue

                    # Skip if we have a resting maker order on this ticker (applies to ALL signal types)
                    if any(info['ticker'] == sig['ticker'] for info in self._resting_premarket_orders.values()):
                        continue

                    sig_is_nba = 'NBAMENTION' in sig.get('ticker', '').upper()

                    if is_earn:
                        # Earnings: check cap and execute
                        earn_count = self.positions.count('earnings_buy_no')
                        if earn_count >= EARNINGS_MAX_POSITIONS:
                            print(f"    EARNINGS CAP: {earn_count}/{EARNINGS_MAX_POSITIONS}, skipping")
                            continue
                        if self.positions.has_open_ticker(sig['ticker']):
                            continue
                    else:
                        # NBA halftime has its own position cap
                        if sig_is_nba and NBA_HALFTIME_ENABLED:
                            nba_ht_count = self.positions.count('nba_halftime_no')
                            if nba_ht_count >= NBA_HALFTIME_MAX_POSITIONS:
                                print(f"    NBA HT CAP: {nba_ht_count}/{NBA_HALFTIME_MAX_POSITIONS}, skipping")
                                continue
                        else:
                            # Mention caps
                            if not mention_allowed:
                                print(f"    MENTION CAP: {mention_count}/{MENTION_MAX_POSITIONS}, skipping")
                                break

                        # Skip if we already have ANY position on this ticker (any signal type)
                        if self.positions.has_open_ticker(sig['ticker']):
                            continue

                        # Per-event exposure cap (includes resting maker orders)
                        event = sig.get('event_ticker', '')
                        if event:
                            evt_cap = NBA_HALFTIME_MAX_EVENT_DOLLARS if (sig_is_nba and NBA_HALFTIME_ENABLED) else MENTION_MAX_EVENT_DOLLARS
                            if self._is_new_series(event):
                                evt_cap = min(evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
                            st = 'nba_halftime_no' if (sig_is_nba and NBA_HALFTIME_ENABLED) else 'mention_buy_no'
                            event_exp = self._total_event_exposure(event, signal_type=st)
                            if event_exp >= evt_cap:
                                continue

                    no_c = sig.get('no_price_cents', 0)
                    h2e = sig.get('hours_to_event')
                    evt_vol = sig.get('event_volume_24h', 0)
                    if h2e is not None and h2e > 0:
                        time_str = f"starts in {h2e:.1f}h"
                    elif h2e is not None:
                        time_str = f"started {abs(h2e)*60:.0f}m ago"
                    else:
                        time_str = "?"
                    # Tag NBA halftime signals with distinct signal_type
                    if sig_is_nba and NBA_HALFTIME_ENABLED:
                        sig['signal_type'] = 'nba_halftime_no'
                        label = "NBA-HT"
                    elif is_earn:
                        label = "EARNINGS"
                    else:
                        label = "MENTION"
                    print(f"  {label}: BUY NO @ {no_c}c '{sig['title'][:50]}' ({time_str}, evt_vol={evt_vol:,})")

                    if low_balance:
                        continue

                    order_info = None
                    if self.client.can_trade:
                        order_info = self._execute_mention_entry(sig)

                    if order_info:
                        evt_vel = sig.get('event_velocity', 0)
                        if sig_is_nba and NBA_HALFTIME_ENABLED:
                            tg_label = "NBA HALFTIME TAKER"
                        elif is_earn:
                            tg_label = "EARNINGS TAKER"
                        elif h2e is not None and h2e < 0:
                            tg_label = "LIVE TAKER"
                        elif evt_vel >= TAKER_MIN_EVENT_VELOCITY:
                            tg_label = "SURGE TAKER"
                        else:
                            tg_label = "PRE-EVENT TAKER"
                        await self.notifier.send_mention_signal(sig, order_info, trade_label=tg_label)
                        self.positions.add(sig, order_info)
                        self._entered_this_cycle.add(sig['ticker'])
                        if not is_earn and not (sig_is_nba and NBA_HALFTIME_ENABLED):
                            mention_count += 1
                            mention_allowed = mention_count < MENTION_MAX_POSITIONS

                # 3b. Degradation curve strategy (NBA, paused) — TAKER, off in maker-only
                if DEGRADE_ENABLED and not MAKER_ONLY:
                    await self._scan_degradation_curve(mention_markets, milestones, now)

                # 3c. NCAA theta re-entry — TAKER, off in maker-only
                if not low_balance and not MAKER_ONLY:
                    await self._scan_theta_reentry(mention_markets, milestones, now)

                # 3d. YES-buy strategy — TAKER, off in maker-only
                if not MAKER_ONLY:
                    await self._scan_yes_buys(mention_markets, milestones, now)

                # 3e. Stale order strategy — TAKER, off in maker-only
                if not low_balance and not MAKER_ONLY:
                    await self._scan_stale_orders(mention_markets, milestones, now)

                # 3f. Political pct_words_said strategy — TAKER, off in maker-only
                if POLITICAL_PCT_ENABLED and not low_balance and not MAKER_ONLY:
                    await self._scan_political_pct(mention_markets, now)

                # 3g. Stable-price strategy — TAKER, off in maker-only
                if STABLE_PRICE_ENABLED and not low_balance and not MAKER_ONLY:
                    await self._scan_stable_price(mention_markets, now)

                # 3h. Truth-Social cheap-word YES buy — TAKER via scoped
                # force_taker carve-out; intentionally runs even in MAKER_ONLY
                # mode (does NOT touch the resting NO maker strategy).
                if TRUTH_YES_ENABLED and not low_balance:
                    await self._scan_truth_cheap_words(mention_markets, milestones, now)
        else:
            print(f"  Mention scan: next in {int(MENTION_SCAN_INTERVAL_SECONDS - (now - self._last_mention_scan))}s")

        # NCAAB fade strategy — TAKER, off in maker-only
        if NCAAB_FADE_ENABLED and not low_balance and not MAKER_ONLY:
            await self._scan_ncaab_fade(now)

        # Tennis fade strategy — TAKER, off in maker-only
        if TENNIS_FADE_ENABLED and not low_balance and not MAKER_ONLY:
            await self._scan_tennis_fade(now)

        # Check positions for settlement
        alerts = self.positions.check(self.client)
        for atype, pos in alerts:
            if atype == 'settled':
                pnl = pos.get('settle_pnl', 0)
                result = "WIN" if pnl > 0 else "LOSS"
                print(f"  SETTLED ({result}): '{pos['title'][:50]}' P&L: ${pnl:+.2f}")
                log_event('settled', ticker=pos.get('ticker'), title=pos.get('title'),
                          signal_type=pos.get('signal_type'), pnl=pnl, result=result,
                          entry_price=pos.get('entry_price'), fill_price=pos.get('fill_price'),
                          fill_count=pos.get('fill_count'), is_live=pos.get('is_live'))

        mention_count = self.positions.count('mention_buy_no')
        nba_ht_count = self.positions.count('nba_halftime_no')
        degrade_count = self.positions.count('degrade_buy_no')
        earnings_count = self.positions.count('earnings_buy_no')
        yes_buy_count = self.positions.count('mention_buy_yes')
        theta_count = self.positions.count('ncaa_theta_reentry')
        pol_pct_count = self.positions.count('political_pct_no')
        stable_count = self.positions.count('stable_price_no')
        fade_count = self.positions.count('ncaab_fade_yes')
        tennis_fade_count = self.positions.count('tennis_fade_yes')
        parts = [f"mention={mention_count}"]
        if nba_ht_count:
            parts.append(f"nba_ht={nba_ht_count}")
        if pol_pct_count:
            parts.append(f"pol_pct={pol_pct_count}")
        if stable_count:
            parts.append(f"stable={stable_count}")
        if yes_buy_count:
            parts.append(f"yes_buy={yes_buy_count}")
        if degrade_count:
            parts.append(f"degrade={degrade_count}")
        if earnings_count:
            parts.append(f"earnings={earnings_count}")
        if theta_count:
            parts.append(f"theta={theta_count}")
        if fade_count:
            parts.append(f"fade={fade_count}")
        if tennis_fade_count:
            parts.append(f"tennis_fade={tennis_fade_count}")
        if self._resting_premarket_orders:
            parts.append(f"resting={len(self._resting_premarket_orders)}")
        print(f"  Open positions: {self.positions.count()} ({', '.join(parts)}, {self.positions.live_count()} live)")
        daily_pnl = self.trade_logger.daily_pnl()
        if daily_pnl != 0:
            print(f"  Daily P&L: ${daily_pnl:.2f}")

        # Diagnostic Telegram: send scan summary every 30 min so we can debug without Railway logs
        if not hasattr(self, '_last_diag_tg'):
            self._last_diag_tg = 0
        if now - self._last_diag_tg >= 1800:  # every 30 min
            self._last_diag_tg = now
            bal = self.client.get_balance() if self.client.can_trade else None
            bal_str = f"${bal/100:.2f}" if bal is not None else "N/A"
            mkts = getattr(self, '_last_mention_market_count', '?')
            sig_stats = getattr(self, '_last_mention_signal_stats', None)
            sig_str = f"{sig_stats[0]} total, {sig_stats[1]} w/milestone, {sig_stats[2]} in window" if sig_stats else "no scan yet"
            fs = getattr(self.mention_detector, '_last_filter_stats', None)
            if fs:
                filter_str = (f"checked={fs.get('total',0)} kill={fs.get('skipped_cat',0)} "
                              f"prerec={fs.get('prerecorded',0)} no_ms={fs.get('no_milestone',0)} "
                              f"early={fs.get('too_early',0)} far={fs.get('too_far',0)} "
                              f"no_px={fs.get('no_price',0)} OOR={fs.get('price_out_range',0)} "
                              f"cd={fs.get('cooldown',0)} ok={fs.get('eligible',0)}")
            else:
                filter_str = "no scan yet"
            diag_lines = [
                f"DIAG {now_str}",
                f"can_trade: {self.client.can_trade}",
                f"balance: {bal_str}",
                f"low_balance: {low_balance}",
                f"markets: {mkts}",
                f"signals: {sig_str}",
                f"filter: {filter_str}",
                f"positions: {self.positions.count()} ({', '.join(parts)})",
                f"resting: {len(self._resting_premarket_orders)}",
            ]
            self._pending_tg_raw.append('\n'.join(diag_lines))

        # Daily P&L summary via Telegram — send once per day after 23:00 UTC
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        today_str = now_utc.strftime('%Y-%m-%d')
        if now_utc.hour >= 23 and self._last_daily_summary_date != today_str:
            self._last_daily_summary_date = today_str
            try:
                await self.notifier.send_daily_summary(self.positions.closed)
                print(f"  Sent daily P&L summary for {today_str}")
            except Exception as e:
                print(f"  Daily summary error: {e}")


    def _rebid_resting_order(self, order_id, info, new_price, spread, to_remove):
        """Cancel existing resting order and place a new one at new_price.
        Used by outbid detection and gap optimization.
        Checks for partial fills on old order before cancelling."""
        ticker = info['ticker']
        old_price = info['price_cents']

        # Check for partial fills BEFORE cancelling the old order
        old_status = self.client.get_order(order_id)
        partial_fills = 0
        if old_status:
            partial_fills = old_status.get('quantity_filled', 0)
            prev_recorded = info.get('_recorded_fills', 0)
            new_fills = partial_fills - prev_recorded
            if new_fills > 0:
                avg_fill = old_status.get('average_fill_price', old_price)
                fill_dollars = round(new_fills * avg_fill / 100, 2)
                print(f"    REBID PARTIAL FILL: {ticker} {new_fills} filled @ {avg_fill}c (${fill_dollars:.2f}) before rebid")
                sig = info.get('signal', {})
                sig['signal_type'] = 'mention_buy_no'
                order_info = {
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'no', 'action': 'buy',
                    'contracts_filled': new_fills, 'price_cents': old_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': fill_dollars,
                    'fill_price': avg_fill / 100, 'fill_count': new_fills,
                }
                self.positions.add(sig, order_info)
                log_event('premarket_rebid_partial_fill', ticker=ticker,
                          order_id=order_id, filled=new_fills, avg_fill=avg_fill,
                          bet_dollars=fill_dollars)
                info['_recorded_fills'] = partial_fills

        self.client.cancel_order(order_id)
        # Subtract dollars already filled from the budget before rebidding
        recorded = info.get('_recorded_fills', 0)
        avg_fill_so_far = info.get('signal', {}).get('no_price_cents', info['price_cents'])
        filled_dollars = round(recorded * avg_fill_so_far / 100, 2)
        remaining_budget = max(info['bet_dollars'] - filled_dollars, 0)
        if remaining_budget < 0.50:
            print(f"    REBID SKIP: {ticker} budget exhausted (${info['bet_dollars']:.2f} - ${filled_dollars:.2f} filled)")
            to_remove.append(order_id)
            return
        new_contracts = int(remaining_budget / (new_price / 100))
        if new_contracts < 1:
            new_contracts = 1
        new_bet = round(new_contracts * new_price / 100, 2)
        new_order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=new_contracts, price_cents=new_price,
            expiration_ts=info.get('signal', {}).get('event_start_ts'),
            maker=True,
        )
        if new_order:
            new_oid = new_order.get('order_id', '')
            self._resting_premarket_orders[new_oid] = {
                'ticker': ticker,
                'price_cents': new_price,
                'contracts': new_contracts,
                'bet_dollars': new_bet,
                'placed_ts': time.time(),
                'category': info['category'],
                'signal': info.get('signal', {}),
            }
            print(f"    PREMARKET REBID: {new_oid} {new_contracts} NO @ {new_price}c (was {old_price}c)")
            log_event('premarket_rebid', ticker=ticker, old_order=order_id,
                      new_order=new_oid, old_price=old_price, new_price=new_price,
                      spread=spread)
            self._save_resting_orders()
        else:
            print(f"    PREMARKET REBID FAILED: {ticker} cancel succeeded but new order failed")
        to_remove.append(order_id)

    async def _check_resting_premarket_orders(self):
        """Check pre-event resting orders for fills and adverse spread movement.
        Kalshi auto-cancels at event start via expiration_ts."""
        if not self._resting_premarket_orders:
            return
        to_remove = []
        for order_id, info in list(self._resting_premarket_orders.items()):
            ticker = info['ticker']
            price_cents = info['price_cents']
            contracts = info['contracts']
            category = info['category']

            # Cancel resting orders on blacklisted words (catches pre-deploy orders)
            word_suffix = ticker.split('-')[-1].upper()
            ticker_upper = ticker.upper()
            is_nba = 'NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper
            is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
            if (is_nba and word_suffix in NBA_BLACKLIST) or (is_ncaa and word_suffix in NCAAB_BLACKLIST):
                print(f"    CANCEL BLACKLISTED RESTING: {word_suffix} ({ticker}), cancelling order {order_id}")
                try:
                    self.client.cancel_order(order_id)
                except Exception as e:
                    print(f"    Cancel failed: {e}")
                to_remove.append(order_id)
                continue

            # Cancel resting orders on de-vetted series. The maker-eligibility
            # gate only stops NEW rests; orders rested before the gate deployed
            # stay live on the book and can still fill (e.g. the James Corden
            # "FOX After Hours" / Obama Presidential Center novelty events).
            # This purges them. Mirrors the executor gate predicate exactly.
            maker_series = ticker_upper.split('-')[0]
            # Pure allowlist: the pruned MENTION_MAKER_SERIES is authoritative.
            # (Trump/NBA/NCAAB winners are in that set; de-vetted series' stale
            # resting orders are cancelled here.)
            vetted = maker_series in MENTION_MAKER_SERIES
            if 'MENTION' in ticker_upper and not vetted:
                print(f"    CANCEL DE-VETTED RESTING: {maker_series} not in vetted maker series, cancelling {order_id}")
                try:
                    self.client.cancel_order(order_id)
                except Exception as e:
                    print(f"    Cancel failed: {e}")
                to_remove.append(order_id)
                continue

            # Cancel resting orders where ticker already exceeds global cap
            global_exp = self._global_ticker_exposure(ticker)
            if global_exp > GLOBAL_MAX_MARKET_DOLLARS:
                print(f"    CANCEL OVER-CAP RESTING: {ticker} exposure ${global_exp:.2f} > ${GLOBAL_MAX_MARKET_DOLLARS}, cancelling {order_id}")
                try:
                    self.client.cancel_order(order_id)
                except Exception as e:
                    print(f"    Cancel failed: {e}")
                to_remove.append(order_id)
                continue

            # Cancel resting orders once the event has started (belt-and-suspenders
            # with Kalshi's expiration_ts, which may be None if milestone was missing)
            event_start = info.get('signal', {}).get('event_start_ts')
            if event_start and time.time() >= event_start:
                print(f"    CANCEL EVENT-STARTED RESTING: {ticker} event started, cancelling {order_id}")
                try:
                    self.client.cancel_order(order_id)
                except Exception as e:
                    print(f"    Cancel failed: {e}")
                to_remove.append(order_id)
                continue

            # Fetch orderbook — used for taker retry, spread check, outbid
            ob = self.client.get_orderbook(ticker)
            if ob:
                no_bids = ob.get('no', [])
                yes_bids = ob.get('yes', [])
                best_no_bid = max(b[0] for b in no_bids) if no_bids else 0
                best_no_ask = (100 - max(b[0] for b in yes_bids)) if yes_bids else 99
                spread = best_no_ask - best_no_bid if best_no_bid > 0 and best_no_ask > 0 else 99

                # --- TAKER RETRY: if ask is now in range, cancel resting and take ---
                sig = info.get('signal', {})
                signal_cents = sig.get('no_price_cents', price_cents)
                max_slip = 4
                min_no_c, max_no_c = get_no_range(ticker)
                if (not MAKER_ONLY
                        and min_no_c <= best_no_ask <= max_no_c
                        and best_no_ask <= signal_cents + max_slip):
                    # Guard: skip taker if we already have a position from partial fills
                    if self.positions.has_open_ticker(ticker):
                        print(f"    PREMARKET→TAKER SKIP: {ticker} already has open position from partial fills, cancelling resting only")
                        self.client.cancel_order(order_id)
                        to_remove.append(order_id)
                        continue
                    print(f"    PREMARKET→TAKER: {ticker} ask now {best_no_ask}c (in range), cancelling resting @ {price_cents}c")
                    self.client.cancel_order(order_id)
                    to_remove.append(order_id)
                    # Execute taker via the normal path
                    sig['signal_type'] = 'mention_buy_no'
                    taker_info = self._execute_mention_entry(sig)
                    if taker_info:
                        self.positions.add(sig, taker_info)
                        await self.notifier.send_order_event(
                            f"RESTING->TAKER [{category}]", ticker,
                            price_cents=taker_info.get('fill_price', best_no_ask) * 100 if isinstance(taker_info.get('fill_price'), float) else best_no_ask,
                            contracts=taker_info.get('fill_count', 0),
                            bet_dollars=taker_info.get('bet_dollars', 0),
                            title=sig.get('title', '')[:60],
                            extra=f"Was resting @ {price_cents}c, taker filled @ {best_no_ask}c")
                        log_event('premarket_taker_upgrade', ticker=ticker,
                                  old_order=order_id, resting_price=price_cents,
                                  taker_ask=best_no_ask, category=category)
                    continue

                # --- Penny-above only (climb-to-ask DISABLED) ---
                # Rest-and-hold at the bid: stay 1c above the next real bidder for
                # queue priority, but NEVER chase the ask. Climbing toward the ask
                # filled us exactly when informed YES flow was crossing into our
                # bid (adverse selection) and at worse prices where the
                # fill-conditioned backtest shows conditional ROI <= 0. We only
                # fill when a YES-taker crosses DOWN to our resting bid.
                # Find highest OTHER bid (excluding our own price level)
                other_bids = [b[0] for b in no_bids if b[0] != price_cents]
                # Exclude dust bids (<$1.50 total size at that level)
                other_bids_filtered = []
                for lvl in set(other_bids):
                    lvl_size = sum(b[1] for b in no_bids if b[0] == lvl) * lvl / 100.0
                    if lvl_size > 1.50:
                        other_bids_filtered.append(lvl)
                next_best_bid = max(other_bids_filtered) if other_bids_filtered else 0

                # Penny-above next best, or floor if we're alone. No climb price.
                penny_price = next_best_bid + 1 if next_best_bid > 0 else min_no_c
                ideal_price = penny_price

                # Clamp to valid range
                ideal_price = max(ideal_price, min_no_c)
                ideal_price = min(ideal_price, max_no_c)

                # Only rebid if price needs to change AND ideal is below the ask
                if ideal_price != price_cents and ideal_price < best_no_ask:
                    direction = "UP" if ideal_price > price_cents else "DOWN"
                    print(f"    PREMARKET REBID {direction}: {ticker} {price_cents}c → {ideal_price}c "
                          f"(next_best={next_best_bid}c, ask={best_no_ask}c, spread={spread}c)")
                    self._rebid_resting_order(order_id, info, ideal_price, spread, to_remove)
                    if ideal_price > price_cents:
                        await self.notifier.send_order_event(
                            f"RESTING REBID [{category}]", ticker,
                            price_cents=ideal_price,
                            contracts=int(info['bet_dollars'] / (ideal_price / 100)) or 1,
                            bet_dollars=info['bet_dollars'],
                            title=info.get('signal', {}).get('title', '')[:60],
                            extra=f"Was {price_cents}c → {ideal_price}c (next_best={next_best_bid}c)")
                    log_event('premarket_rebid_penny', ticker=ticker, old_price=price_cents,
                              new_price=ideal_price, next_best=next_best_bid,
                              spread=spread, direction=direction)
                    continue

            # Check fill status
            status = self.client.get_order(order_id)
            if not status:
                to_remove.append(order_id)
                continue

            filled = status.get('quantity_filled', 0)
            order_status = status.get('status', '')

            if filled > 0:
                avg_fill = status.get('average_fill_price', price_cents)
                actual_dollars = round(filled * avg_fill / 100, 2)
                remaining = status.get('remaining_count', contracts - filled)
                fully_filled = (remaining == 0)

                # Only record position for NEW fills (avoid double-counting)
                prev_filled = info.get('_recorded_fills', 0)
                new_fills = filled - prev_filled

                if new_fills > 0:
                    new_dollars = round(new_fills * avg_fill / 100, 2)
                    print(f"    PREMARKET FILLED: {ticker} {filled}/{contracts} NO @ {avg_fill}c (${actual_dollars:.2f}){'' if fully_filled else f' — {remaining} still resting'}")

                    order_info = {
                        'ticker': ticker, 'order_id': order_id,
                        'side': 'no', 'action': 'buy',
                        'contracts_filled': new_fills, 'price_cents': price_cents,
                        'avg_fill_price': avg_fill, 'bet_dollars': new_dollars,
                        'fill_price': avg_fill / 100, 'fill_count': new_fills,
                    }
                    sig = info.get('signal', {})
                    sig['signal_type'] = 'mention_buy_no'
                    self.positions.add(sig, order_info)
                    await self.notifier.send_order_event(
                        f"RESTING FILLED [{category}]", ticker,
                        price_cents=avg_fill, contracts=new_fills, bet_dollars=new_dollars,
                        title=sig.get('title', '')[:60],
                        extra=f"Rested @ {price_cents}c, filled {filled}/{contracts}{'' if fully_filled else f' ({remaining} still resting)'}")
                    log_event('premarket_filled', ticker=ticker, order_id=order_id,
                              filled=new_fills, total_filled=filled, avg_fill_cents=avg_fill,
                              bet_dollars=new_dollars, remaining=remaining, category=category)
                    info['_recorded_fills'] = filled

                if fully_filled:
                    to_remove.append(order_id)
                # else: keep tracking — still has resting contracts, can rebid

            elif order_status in ('canceled', 'cancelled', 'expired'):
                print(f"    PREMARKET EXPIRED: {ticker} order {order_status}")
                to_remove.append(order_id)

        for oid in to_remove:
            self._resting_premarket_orders.pop(oid, None)

        if to_remove:
            self._save_resting_orders()

        if self._resting_premarket_orders:
            tickers = [v['ticker'].split('-')[-1] for v in self._resting_premarket_orders.values()]
            print(f"  Premarket resting: {len(self._resting_premarket_orders)} orders ({', '.join(tickers)})")

    async def _scan_degradation_curve(self, mention_markets, milestones, now):
        """Degradation curve strategy: buy NO on NBA mention markets where the
        market is cheaper than the statistically-derived fair value, 1h+ into
        a live game.  Runs completely independently of the main mention strategy."""
        degrade_count = self.positions.count('degrade_buy_no')
        if degrade_count >= DEGRADE_MAX_POSITIONS:
            return

        nba_all = [m for m in mention_markets
                   if 'NBAMENTION' in m.get('ticker', '').upper()]
        nba_markets = [m for m in nba_all if m.get('status') in ('open', 'active')]
        if nba_all and not nba_markets:
            statuses = {}
            for m in nba_all:
                s = m.get('status', 'MISSING')
                statuses[s] = statuses.get(s, 0) + 1
            print(f"  DEGRADE: {len(nba_all)} NBA markets but 0 open — statuses: {statuses}")
        print(f"  DEGRADE scan: {len(nba_markets)} open NBA markets, "
              f"{degrade_count}/{DEGRADE_MAX_POSITIONS} positions")
        if not nba_markets:
            return

        skip_reasons = {'no_word': 0, 'no_milestone': 0, 'too_early': 0,
                        'ended': 0, 'bucket_placed': 0, 'no_bucket': 0,
                        'no_price': 0, 'said': 0, 'too_expensive': 0,
                        'event_cap': 0}
        signals = []
        for m in nba_markets:
            ticker = m.get('ticker', '')
            ticker_upper = ticker.upper()
            event_ticker = m.get('event_ticker', '')

            # Extract word from ticker: KXNBAMENTION-26FEB22CLEOKC-PLAYOFF → PLAYOFF
            parts = ticker.split('-')
            if len(parts) < 3:
                continue
            word = parts[-1].upper()
            if word not in DEGRADE_BUY_BELOW:
                skip_reasons['no_word'] += 1
                continue

            # Check game is live and >= 1h in
            ms = milestones.get(event_ticker)
            if not ms or not ms.get('start_ts'):
                skip_reasons['no_milestone'] += 1
                continue
            hours_live = (now - ms['start_ts']) / 3600
            if hours_live < DEGRADE_MIN_HOURS_LIVE:
                skip_reasons['too_early'] += 1
                continue
            # Don't bet after game is over
            if ms.get('end_ts') and ms['end_ts'] <= now:
                skip_reasons['ended'] += 1
                continue

            # Bucket into half-hour
            half_hour = round(hours_live * 2) / 2
            hh_key = f'{half_hour:.1f}' if half_hour != int(half_hour) else f'{half_hour:.1f}'
            word_table = DEGRADE_BUY_BELOW[word]
            if hh_key not in word_table:
                skip_reasons['no_bucket'] += 1
                continue
            max_buy_cents = word_table[hh_key]

            # Only one limit order per half-hour bucket per ticker;
            # new bucket = new order allowed
            placed_buckets = self._degrade_bucket_placed.get(ticker, set())
            if hh_key in placed_buckets:
                skip_reasons['bucket_placed'] += 1
                continue

            # Get current YES price
            yes_price = None
            yes_bid = _market_cents(m, 'yes_bid')
            yes_ask = _market_cents(m, 'yes_ask')
            if yes_bid is not None and yes_ask is not None:
                yes_price = (yes_bid + yes_ask) / 2 / 100
            if yes_price is None:
                last = _market_cents(m, 'last_price')
                if last is not None:
                    yes_price = last / 100
            if yes_price is None:
                skip_reasons['no_price'] += 1
                continue

            # Word already said? (YES >= 90%)
            if yes_price >= 0.90:
                skip_reasons['said'] += 1
                continue

            no_price_cents = round((1 - yes_price) * 100)

            # Core check: is market NO cheap enough vs fair value?
            if no_price_cents > max_buy_cents:
                skip_reasons['too_expensive'] += 1
                continue

            # Per-event exposure cap (degrade only — independent of mention)
            if event_ticker:
                event_exp = self.positions.event_exposure(event_ticker, signal_type='degrade_buy_no')
                if event_exp >= DEGRADE_MAX_EVENT_DOLLARS:
                    skip_reasons['event_cap'] += 1
                    continue

            signals.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'title': m.get('title', ''),
                'no_price': no_price_cents / 100,
                'no_price_cents': no_price_cents,
                'max_buy_cents': max_buy_cents,
                'word': word,
                'hours_live': round(hours_live, 2),
                'hh_bucket': hh_key,
                'signal_type': 'degrade_buy_no',
                'signal_time': time.time(),
                # Required by positions.add()
                'fade_action': 'BUY',
                'fade_side': 'no',
                'entry_price': no_price_cents / 100,
                'pre_signal_price': no_price_cents / 100,
                'price_move': 0,
                'n_small_trades': 0,
                'retail_contracts': 0,
            })

        active_skips = {k: v for k, v in skip_reasons.items() if v > 0}
        print(f"  DEGRADE filter: {len(signals)} signals, skips: {active_skips}")

        for sig in signals:
            if degrade_count >= DEGRADE_MAX_POSITIONS:
                print(f"    DEGRADE CAP: {degrade_count}/{DEGRADE_MAX_POSITIONS}, skipping")
                break
            print(f"  DEGRADE: BUY NO @ {sig['no_price_cents']}c <= fair {sig['max_buy_cents']}c "
                  f"'{sig['word']}' {sig['hh_bucket']}h live ({sig['hours_live']:.1f}h) "
                  f"'{sig['title'][:40]}'")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_degradation_entry(sig)

            if order_info:
                await self.notifier.send_mention_signal(sig, order_info, trade_label="DEGRADE TAKER")
                self.positions.add(sig, order_info)
                degrade_count += 1
                # Record this bucket so we don't re-bet same half-hour
                self._degrade_bucket_placed.setdefault(sig['ticker'], set()).add(sig['hh_bucket'])

    def _execute_degradation_entry(self, sig):
        """Execute a degradation-curve BUY NO entry. Taker order at the ask,
        capped at fair value from degradation table."""
        ticker = sig['ticker']
        max_buy_cents = sig['max_buy_cents']

        # Global per-ticker cap
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    DEGRADE: no orderbook for {ticker}, skipping")
            return None

        # Get best NO ask from orderbook (NO ask = 100 - best YES bid)
        yes_bids_raw = orderbook.get('yes', [])
        if not isinstance(yes_bids_raw, list):
            yes_bids_raw = []
        best_no_ask = None
        if yes_bids_raw:
            best_no_ask = 100 - max(b[0] for b in yes_bids_raw)

        if best_no_ask is None or best_no_ask < 1:
            print(f"    DEGRADE: no NO ask for {ticker} (no YES bids), skipping")
            return None

        # Respect fair value ceiling from degradation table
        taker_price = min(best_no_ask, max_buy_cents)
        if taker_price < 1:
            taker_price = 1

        if best_no_ask > max_buy_cents:
            print(f"    DEGRADE: ask {best_no_ask}c > fair {max_buy_cents}c, skipping")
            return None

        contracts = int(DEGRADE_BET_DOLLARS / (taker_price / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * taker_price / 100, 2)

        print(f"    DEGRADE taker: {contracts} NO @ {taker_price}c (ask={best_no_ask}c, fair={max_buy_cents}c) = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-DEG-{uuid.uuid4().hex[:8]}',
                'fill_price': taker_price / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'degrade_buy_no',
                'ticker': ticker, 'side': 'no', 'action': 'buy',
                'contracts': contracts, 'price_cents': taker_price,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    DEGRADE DRY RUN: {contracts} NO @ {taker_price}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=contracts, price_cents=taker_price,
        )
        if not order:
            print(f"    DEGRADE: order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    DEGRADE taker placed: {order_id} ({contracts} NO @ {taker_price}c, ${bet_dollars:.2f})")
        log_event('degrade_taker_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=taker_price, bet_dollars=bet_dollars,
                  word=sig.get('word'), hours_live=sig.get('hours_live'),
                  hh_bucket=sig.get('hh_bucket'), max_buy_cents=max_buy_cents)

        # Taker should fill instantly
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', taker_price)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'degrade_buy_no',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'no', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': taker_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    DEGRADE FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('degrade_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars)
                self._queue_tg("DEGRADE TAKER FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled even as taker — cancel and give up
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    DEGRADE taker not filled for {ticker}, canceled")
        log_event('degrade_taker_unfilled', ticker=ticker, order_id=order_id)
        return None

    # ------------------------------------------------------------------
    # YES-buy strategy: buy YES on always-said NBA words (cheap YES)
    # ------------------------------------------------------------------
    async def _scan_yes_buys(self, mention_markets, milestones, now):
        """Scan NBA mention markets for cheap YES on words that are almost always
        said (Injury, Rookie, All-Star, Crowd).  Runs independently of the
        main mention (NO) strategy and the degradation strategy."""
        yes_count = self.positions.count('mention_buy_yes')
        if yes_count >= NBA_YES_MAX_POSITIONS:
            return

        nba_markets = [m for m in mention_markets
                       if m.get('status') in ('open', 'active')
                       and ('NBAMENTION' in m.get('ticker', '').upper()
                            or 'NBAFINALS' in m.get('ticker', '').upper())]
        if not nba_markets:
            return

        skip_reasons = {'no_word': 0, 'no_milestone': 0, 'too_early': 0,
                        'too_late': 0, 'ended': 0, 'said': 0, 'no_price': 0,
                        'too_expensive': 0, 'event_cap': 0, 'already_pos': 0}
        signals = []
        for m in nba_markets:
            ticker = m.get('ticker', '')
            event_ticker = m.get('event_ticker', '')

            # Extract word suffix
            parts = ticker.split('-')
            if len(parts) < 3:
                continue
            word = parts[-1].upper()
            if word not in NBA_YES_BUY_WORDS:
                skip_reasons['no_word'] += 1
                continue

            max_yes_c = NBA_YES_BUY_WORDS[word]

            # Check milestone / timing
            ms = milestones.get(event_ticker)
            if not ms or not ms.get('start_ts'):
                skip_reasons['no_milestone'] += 1
                continue
            hours_to_event = (ms['start_ts'] - now) / 3600
            # Entry window: pre-game up to 30min into game
            if hours_to_event > 24:
                skip_reasons['too_early'] += 1
                continue
            if hours_to_event < -0.5:
                skip_reasons['too_late'] += 1
                continue
            # Don't bet after game ended
            if ms.get('end_ts') and ms['end_ts'] <= now:
                skip_reasons['ended'] += 1
                continue

            # Get YES price estimate from market data
            yes_price = None
            yes_bid = _market_cents(m, 'yes_bid')
            yes_ask = _market_cents(m, 'yes_ask')
            if yes_bid is not None and yes_ask is not None:
                yes_price = (yes_bid + yes_ask) / 2 / 100
            if yes_price is None:
                last = _market_cents(m, 'last_price')
                if last is not None:
                    yes_price = last / 100
            if yes_price is None:
                skip_reasons['no_price'] += 1
                continue

            yes_price_cents = round(yes_price * 100)

            # Word already said? (YES >= 90c) — no edge left
            if yes_price >= 0.90:
                skip_reasons['said'] += 1
                continue

            # Check price affordable
            if yes_price_cents > max_yes_c:
                skip_reasons['too_expensive'] += 1
                continue

            # Per-event cap (YES strategy only)
            if event_ticker:
                event_exp = self.positions.event_exposure(event_ticker, signal_type='mention_buy_yes')
                if event_exp >= NBA_YES_MAX_EVENT_DOLLARS:
                    skip_reasons['event_cap'] += 1
                    continue

            # Skip if already holding this ticker
            if any(p.get('ticker') == ticker for p in self.positions.positions
                   if p.get('signal_type') == 'mention_buy_yes'):
                skip_reasons['already_pos'] += 1
                continue

            signals.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'title': m.get('title', ''),
                'yes_price': yes_price,
                'yes_price_cents': yes_price_cents,
                'max_yes_cents': max_yes_c,
                'word': word,
                'hours_to_event': round(hours_to_event, 2),
                'signal_type': 'mention_buy_yes',
                'signal_time': time.time(),
                # Required by positions.add()
                'fade_action': 'BUY',
                'fade_side': 'yes',
                'entry_price': yes_price,
                'pre_signal_price': yes_price,
                'price_move': 0,
                'n_small_trades': 0,
                'retail_contracts': 0,
            })

        active_skips = {k: v for k, v in skip_reasons.items() if v > 0}
        if signals or active_skips:
            print(f"  YES-BUY scan: {len(signals)} signals from {len(nba_markets)} NBA mkts, "
                  f"{yes_count}/{NBA_YES_MAX_POSITIONS} pos, skips: {active_skips}")

        for sig in signals:
            if yes_count >= NBA_YES_MAX_POSITIONS:
                print(f"    YES-BUY CAP: {yes_count}/{NBA_YES_MAX_POSITIONS}, stopping")
                break
            print(f"  YES-BUY: {sig['word']} YES ~{sig['yes_price_cents']}c <= {sig['max_yes_cents']}c "
                  f"h2e={sig['hours_to_event']:.1f}h '{sig['title'][:40]}'")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_yes_entry(sig)

            if order_info:
                await self.notifier.send_mention_signal(sig, order_info, trade_label="YES BUY TAKER")
                self.positions.add(sig, order_info)
                yes_count += 1

    def _execute_yes_entry(self, sig):
        """Execute a YES buy on an always-said NBA word.  Taker order: buy YES
        at the ask, capped at max_yes_cents from NBA_YES_BUY_WORDS."""
        ticker = sig['ticker']
        max_yes_c = sig['max_yes_cents']

        # Global per-ticker cap
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    YES-BUY: no orderbook for {ticker}, skipping")
            return None

        # Best YES ask = lowest offer to sell YES
        # In Kalshi, YES ask = 100 - best NO bid
        no_bids_raw = orderbook.get('no', [])
        if not isinstance(no_bids_raw, list):
            no_bids_raw = []
        best_yes_ask = None
        if no_bids_raw:
            best_yes_ask = 100 - max(b[0] for b in no_bids_raw)

        if best_yes_ask is None or best_yes_ask < 1:
            print(f"    YES-BUY: no YES ask for {ticker} (no NO bids), skipping")
            return None

        if best_yes_ask > max_yes_c:
            print(f"    YES-BUY: ask {best_yes_ask}c > max {max_yes_c}c, skipping")
            return None

        taker_price = best_yes_ask
        contracts = int(NBA_YES_BET_DOLLARS / (taker_price / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * taker_price / 100, 2)

        print(f"    YES-BUY taker: {contracts} YES @ {taker_price}c = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-YES-{uuid.uuid4().hex[:8]}',
                'fill_price': taker_price / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'mention_buy_yes',
                'ticker': ticker, 'side': 'yes', 'action': 'buy',
                'contracts': contracts, 'price_cents': taker_price,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    YES-BUY DRY RUN: {contracts} YES @ {taker_price}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='yes', action='buy',
            count=contracts, price_cents=taker_price,
        )
        if not order:
            print(f"    YES-BUY: order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    YES-BUY placed: {order_id} ({contracts} YES @ {taker_price}c, ${bet_dollars:.2f})")
        log_event('yes_buy_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=taker_price, bet_dollars=bet_dollars,
                  word=sig.get('word'), hours_to_event=sig.get('hours_to_event'))

        # Taker — should fill instantly
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', taker_price)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'mention_buy_yes',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'yes', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': taker_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    YES-BUY FILLED: {filled}/{contracts} YES @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('yes_buy_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars)
                self._queue_tg("YES-BUY FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    YES-BUY taker not filled for {ticker}, canceled")
        log_event('yes_buy_unfilled', ticker=ticker, order_id=order_id)
        return None

    # --- Truth-Social cheap-word YES buy -----------------------------------
    def _load_truth_ledger(self):
        """Persistent per-word-per-event dedup: set of tickers already bought."""
        try:
            if TRUTH_TRADED_LEDGER.exists():
                return set(json.loads(TRUTH_TRADED_LEDGER.read_text()))
        except Exception:
            pass
        return set()

    def _truth_mark_traded(self, ticker):
        self._truth_traded.add(ticker)
        try:
            TRUTH_TRADED_LEDGER.write_text(json.dumps(sorted(self._truth_traded)))
        except Exception:
            pass

    async def _scan_truth_cheap_words(self, mention_markets, milestones, now):
        """Buy YES on a Trump mention word AFTER he posts it on Truth Social
        within TRUTH_YES_WINDOW_H before the event, only while YES ask is still
        <= TRUTH_YES_MAX_CENTS. Taker entry, $5/trade, one position per
        word-per-event (ticker) forever. Runs alongside — and never touches —
        the resting NO maker strategy."""
        if not TRUTH_YES_ENABLED:
            return
        count = self.positions.count('truth_cheap_yes')
        if count >= TRUTH_YES_MAX_POSITIONS:
            return

        trump_mkts = [m for m in mention_markets
                      if m.get('strike_type') == 'custom'
                      and 'TRUMPMENTION' in m.get('ticker', '').upper()
                      and m.get('status') in ('open', 'active')]
        if not trump_mkts:
            return

        # Refresh the feed once per cycle (self-throttled to every 10 min).
        try:
            self.truth_feed.refresh(TRUTH_YES_WINDOW_H + 24)
        except Exception as e:
            print(f"  TRUTH-YES: feed refresh failed ({e}); using cached posts")

        skip = {'no_word': 0, 'avoid': 0, 'no_milestone': 0, 'ended': 0,
                'not_posted': 0, 'dup': 0, 'no_price': 0, 'too_expensive': 0,
                'event_cap': 0}
        signals = []
        for m in trump_mkts:
            ticker = m.get('ticker', '')
            event_ticker = m.get('event_ticker', '')
            word = (m.get('custom_strike') or {}).get('Word')
            if not word:
                skip['no_word'] += 1
                continue
            wl = word.lower()
            if wl in TRUTH_YES_AVOID_WORDS or any(a.strip() in TRUTH_YES_AVOID_WORDS
                                                  for a in wl.split('/')):
                skip['avoid'] += 1
                continue
            ms = milestones.get(event_ticker)
            if not ms or not ms.get('start_ts'):
                skip['no_milestone'] += 1
                continue
            T = ms['start_ts']
            if ms.get('end_ts') and ms['end_ts'] <= now:
                skip['ended'] += 1
                continue
            # Word must have been posted within the window before event start.
            if not self.truth_feed.posted(word, T - TRUTH_YES_WINDOW_H * 3600, now):
                skip['not_posted'] += 1
                continue
            # Dedup: one YES buy per word-per-event (ticker), ever.
            if ticker in self._truth_traded or \
                    self.positions.has_open_ticker(ticker, signal_type='truth_cheap_yes'):
                skip['dup'] += 1
                continue
            # Price: YES ask = 100 - best NO bid.
            ob = self.client.get_orderbook(ticker)
            no_bids = ob.get('no', []) if ob else []
            if not no_bids:
                skip['no_price'] += 1
                continue
            yes_ask = 100 - max(b[0] for b in no_bids)
            if yes_ask < 1:
                skip['no_price'] += 1
                continue
            if yes_ask > TRUTH_YES_MAX_CENTS:
                skip['too_expensive'] += 1
                continue
            if event_ticker and self.positions.event_exposure(
                    event_ticker, signal_type='truth_cheap_yes') >= TRUTH_YES_MAX_EVENT_DOLLARS:
                skip['event_cap'] += 1
                continue

            yes_price = yes_ask / 100.0
            signals.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'title': m.get('title', ''),
                'word': word,
                'yes_ask_cents': yes_ask,
                'yes_price': yes_price,
                'hours_to_event': round((T - now) / 3600, 2),
                'signal_type': 'truth_cheap_yes',
                'signal_time': time.time(),
                # Required by positions.add()
                'fade_action': 'BUY',
                'fade_side': 'yes',
                'entry_price': yes_price,
                'pre_signal_price': yes_price,
                'price_move': 0,
                'n_small_trades': 0,
                'retail_contracts': 0,
            })

        active = {k: v for k, v in skip.items() if v}
        if signals or active:
            print(f"  TRUTH-YES scan: {len(signals)} signals from {len(trump_mkts)} Trump mkts, "
                  f"{count}/{TRUTH_YES_MAX_POSITIONS} pos, skips: {active}")

        for sig in signals:
            if count >= TRUTH_YES_MAX_POSITIONS:
                print(f"    TRUTH-YES CAP: {count}/{TRUTH_YES_MAX_POSITIONS}, stopping")
                break
            print(f"  TRUTH-YES: '{sig['word']}' YES ask {sig['yes_ask_cents']}c <= "
                  f"{TRUTH_YES_MAX_CENTS}c h2e={sig['hours_to_event']:.1f}h '{sig['title'][:40]}'")
            order_info = None
            if self.client.can_trade:
                order_info = self._execute_truth_yes_entry(sig)
            if order_info:
                await self.notifier.send_mention_signal(sig, order_info, trade_label="TRUTH YES TAKER")
                self.positions.add(sig, order_info)
                self._truth_mark_traded(sig['ticker'])
                count += 1

    def _execute_truth_yes_entry(self, sig):
        """Taker YES buy for the Truth cheap-word strategy: $5, price = best YES
        ask, hard-capped at TRUTH_YES_MAX_CENTS. Uses create_order(force_taker=True)
        so this single order may cross the spread even under MAKER_ONLY."""
        ticker = sig['ticker']

        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    TRUTH-YES: global market cap ${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}, skip {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        no_bids = orderbook.get('no', []) if orderbook else []
        if not no_bids:
            print(f"    TRUTH-YES: no YES ask for {ticker} (no NO bids), skip")
            return None
        best_yes_ask = 100 - max(b[0] for b in no_bids)
        if best_yes_ask < 1 or best_yes_ask > TRUTH_YES_MAX_CENTS:
            print(f"    TRUTH-YES: ask {best_yes_ask}c outside 1-{TRUTH_YES_MAX_CENTS}c, skip")
            return None

        taker_price = best_yes_ask
        contracts = int(TRUTH_YES_BET_DOLLARS / (taker_price / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * taker_price / 100, 2)
        print(f"    TRUTH-YES taker: {contracts} YES @ {taker_price}c = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-TRUTH-{uuid.uuid4().hex[:8]}',
                'fill_price': taker_price / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'truth_cheap_yes',
                'ticker': ticker, 'side': 'yes', 'action': 'buy',
                'contracts': contracts, 'price_cents': taker_price,
                'bet_dollars': bet_dollars, 'word': sig.get('word'), 'dry_run': True,
            })
            print(f"    TRUTH-YES DRY RUN: {contracts} YES @ {taker_price}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='yes', action='buy',
            count=contracts, price_cents=taker_price, force_taker=True,
        )
        if not order:
            print(f"    TRUTH-YES: order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    TRUTH-YES placed: {order_id} ({contracts} YES @ {taker_price}c, ${bet_dollars:.2f})")
        log_event('truth_yes_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=taker_price, bet_dollars=bet_dollars,
                  word=sig.get('word'), hours_to_event=sig.get('hours_to_event'))

        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', taker_price)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'truth_cheap_yes',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'yes', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': taker_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                    'word': sig.get('word'),
                })
                print(f"    TRUTH-YES FILLED: {filled}/{contracts} YES @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('truth_yes_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars,
                          word=sig.get('word'))
                self._queue_tg("TRUTH YES FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    TRUTH-YES taker not filled for {ticker}, canceled")
        log_event('truth_yes_unfilled', ticker=ticker, order_id=order_id)
        return None

    async def _scan_theta_reentry(self, mention_markets, milestones, now):
        """NCAA theta: snapshot NO prices near game start, then buy at T+20min
        if NO price hasn't deviated from start. Backtest: +93.7% ROI."""
        if not THETA_REENTRY_ENABLED:
            return

        theta_count = self.positions.count('ncaa_theta_reentry')
        if theta_count >= THETA_REENTRY_MAX_POSITIONS:
            return

        # Phase 1: Snapshot NO prices for NCAA markets near game start (0-10 min in)
        # so we have a baseline to compare against at T+20
        for m in mention_markets:
            ticker = m.get('ticker', '')
            ticker_upper = ticker.upper()
            if 'NCAAMENTION' not in ticker_upper and 'NCAABMENTION' not in ticker_upper:
                continue
            if ticker in self._theta_start_prices:
                continue  # already cached

            word = ticker.split('-')[-1].upper()
            if word in NCAAB_BLACKLIST:
                continue

            event_ticker = m.get('event_ticker', '')
            ms_data = milestones.get(event_ticker)
            if not ms_data or not ms_data.get('start_ts'):
                continue

            minutes_into = (now - ms_data['start_ts']) / 60.0
            if minutes_into < 0 or minutes_into > 10:
                continue  # only snapshot in first 10 min of game

            # Get current NO price from market data (no orderbook fetch needed)
            no_price = m.get('no_price')
            if no_price is None:
                yes_price = m.get('yes_price')
                if yes_price is not None:
                    no_price = 1.0 - yes_price
            if no_price is not None:
                no_cents = int(round(no_price * 100))
                if 3 <= no_cents <= 50:  # sane range
                    self._theta_start_prices[ticker] = no_cents
                    print(f"    Theta snapshot: {ticker.split('-')[-1]} @ {no_cents}c (T+{minutes_into:.0f}min)")

        # Clean up stale entries (events that ended 2+ hours ago)
        stale_tickers = []
        for ticker in self._theta_start_prices:
            parts = ticker.rsplit('-', 1)
            et = parts[0] if len(parts) > 1 else ''
            ms_data = milestones.get(et)
            if ms_data and ms_data.get('end_ts') and ms_data['end_ts'] < now - 7200:
                stale_tickers.append(ticker)
        for t in stale_tickers:
            del self._theta_start_prices[t]

        # Phase 2: Find candidates — NCAA markets 20-80 min in where NO hasn't deviated
        candidates = []
        for m in mention_markets:
            ticker = m.get('ticker', '')
            ticker_upper = ticker.upper()

            if 'NCAAMENTION' not in ticker_upper and 'NCAABMENTION' not in ticker_upper:
                continue

            word = ticker.split('-')[-1].upper()
            if word in NCAAB_BLACKLIST:
                continue

            event_ticker = m.get('event_ticker', '')
            ms_data = milestones.get(event_ticker)
            if not ms_data or not ms_data.get('start_ts'):
                continue

            minutes_into = (now - ms_data['start_ts']) / 60.0
            if minutes_into < THETA_REENTRY_MIN_GAME_MIN:
                continue
            if minutes_into > THETA_REENTRY_MAX_GAME_MIN:
                continue

            if ms_data.get('end_ts') and ms_data['end_ts'] <= now:
                continue

            # Must have a start price snapshot
            start_price = self._theta_start_prices.get(ticker)
            if start_price is None:
                continue

            # Skip if we already have a theta position on this ticker
            if self.positions.has_open_ticker(ticker, signal_type='ncaa_theta_reentry'):
                continue

            # Per-market theta exposure check
            ticker_exp = 0
            for p in self.positions.positions:
                if p.get('ticker') == ticker and p.get('status') == 'open' and p.get('signal_type') == 'ncaa_theta_reentry':
                    ticker_exp += p.get('bet_dollars', 0)
            if ticker_exp >= THETA_REENTRY_MAX_MARKET_DOLLARS:
                continue

            candidates.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'word': word,
                'minutes_into': minutes_into,
                'start_price': start_price,
                'title': m.get('title', m.get('subtitle', '')),
            })

        if not candidates:
            return

        n_filled = 0
        for c in candidates:
            if theta_count >= THETA_REENTRY_MAX_POSITIONS:
                break

            ticker = c['ticker']
            start_price = c['start_price']

            # Fetch orderbook for current NO price
            try:
                orderbook = self.client.get_orderbook(ticker)
            except Exception as e:
                print(f"    Theta: orderbook error for {ticker}: {e}")
                continue

            if not orderbook:
                continue

            yes_bids = orderbook.get('yes', {}).get('bids', [])
            if not yes_bids:
                continue

            yes_bids_raw = []
            for b in yes_bids:
                price = b.get('price', 0)
                qty = b.get('quantity', 0)
                if price > 0 and qty > 0:
                    yes_bids_raw.append((price, qty))

            if not yes_bids_raw:
                continue

            yes_bids_raw.sort(key=lambda x: -x[0])
            best_yes_bid = yes_bids_raw[0][0]
            no_ask_cents = 100 - best_yes_bid

            # Price range check
            if no_ask_cents < THETA_REENTRY_MIN_NO_CENTS or no_ask_cents > THETA_REENTRY_MAX_NO_CENTS:
                continue

            # Deviation check: NO price must be within ±MAX_DEVIATION of start price
            deviation = abs(no_ask_cents - start_price)
            if deviation > THETA_REENTRY_MAX_DEVIATION:
                continue

            # Slippage guard
            max_slip_price = no_ask_cents + THETA_REENTRY_MAX_SLIPPAGE
            taker_price = no_ask_cents

            # Depth within slippage window
            min_yes_bid = 100 - max_slip_price
            depth_contracts = 0
            for bid_price, bid_qty in yes_bids_raw:
                if bid_price >= min_yes_bid:
                    depth_contracts += bid_qty
            depth_dollars = round(depth_contracts * taker_price / 100, 2) if depth_contracts > 0 else 0

            # Bet sizing
            bet = THETA_REENTRY_BET
            ticker_exp = 0
            for p in self.positions.positions:
                if p.get('ticker') == ticker and p.get('status') == 'open' and p.get('signal_type') == 'ncaa_theta_reentry':
                    ticker_exp += p.get('bet_dollars', 0)
            remaining = THETA_REENTRY_MAX_MARKET_DOLLARS - ticker_exp
            if remaining <= 0:
                continue
            bet = min(bet, remaining)

            if depth_dollars > 0 and bet > depth_dollars:
                bet = depth_dollars

            contracts = int(bet / (taker_price / 100))
            if contracts < 1:
                contracts = 1
            bet_dollars = round(contracts * taker_price / 100, 2)

            order_price = max_slip_price

            print(f"  THETA: {c['word']} @ {taker_price}c (start={start_price}c, dev={deviation}c) "
                  f"T+{c['minutes_into']:.0f}min, ${bet_dollars:.2f}")

            if DRY_RUN:
                order_info = {
                    'order_id': f'DRY-THETA-{uuid.uuid4().hex[:8]}',
                    'fill_price': taker_price / 100,
                    'fill_count': contracts,
                    'bet_dollars': bet_dollars,
                    'dry_run': True,
                }
                sig = {
                    'ticker': ticker,
                    'event_ticker': c['event_ticker'],
                    'title': c['title'],
                    'signal_type': 'ncaa_theta_reentry',
                    'fade_action': 'buy', 'fade_side': 'no',
                    'entry_price': taker_price / 100,
                    'pre_signal_price': start_price / 100,
                    'price_move': 0, 'n_small_trades': 0, 'retail_contracts': 0,
                    'signal_time': now,
                    'no_price': taker_price / 100,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'ncaa_theta_reentry',
                    'ticker': ticker, 'side': 'no', 'action': 'buy',
                    'contracts': contracts, 'price_cents': taker_price,
                    'start_price_cents': start_price,
                    'bet_dollars': bet_dollars, 'dry_run': True,
                })
                self.positions.add(sig, order_info)
                theta_count += 1
                n_filled += 1
                print(f"    DRY RUN: {contracts} NO @ {taker_price}c (${bet_dollars:.2f})")
                continue

            order = self.client.create_order(
                ticker=ticker, side='no', action='buy',
                count=contracts, price_cents=order_price,
            )
            if not order:
                print(f"    Theta order failed for {ticker}")
                continue

            order_id = order.get('order_id', '')
            print(f"    Theta taker order: {order_id} ({contracts} NO @ limit {order_price}c)")
            log_event('theta_reentry_placed', ticker=ticker, order_id=order_id,
                      contracts=contracts, price_cents=order_price, bet_dollars=bet_dollars,
                      start_price_cents=start_price, minutes_into=c['minutes_into'])

            time.sleep(2)
            status = self.client.get_order(order_id)
            if status:
                filled = status.get('quantity_filled', 0)
                if filled > 0:
                    remaining_ords = status.get('remaining_count', 0)
                    if remaining_ords > 0:
                        try:
                            self.client.cancel_order(order_id)
                        except Exception:
                            pass
                    avg_fill = status.get('average_fill_price', taker_price)
                    actual_dollars = round(filled * avg_fill / 100, 2)
                    info = {
                        'order_id': order_id,
                        'fill_price': avg_fill / 100,
                        'fill_count': filled,
                        'bet_dollars': actual_dollars,
                        'dry_run': False,
                    }
                    sig = {
                        'ticker': ticker,
                        'event_ticker': c['event_ticker'],
                        'title': c['title'],
                        'signal_type': 'ncaa_theta_reentry',
                        'fade_action': 'buy', 'fade_side': 'no',
                        'entry_price': avg_fill / 100,
                        'pre_signal_price': start_price / 100,
                        'price_move': 0, 'n_small_trades': 0, 'retail_contracts': 0,
                        'signal_time': now,
                        'no_price': avg_fill / 100,
                    }
                    self.trade_logger.record({
                        'type': 'entry', 'strategy': 'ncaa_theta_reentry',
                        'ticker': ticker, 'order_id': order_id,
                        'side': 'no', 'action': 'buy',
                        'contracts_filled': filled, 'price_cents': taker_price,
                        'start_price_cents': start_price,
                        'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                    })
                    self.positions.add(sig, info)
                    theta_count += 1
                    n_filled += 1
                    print(f"    THETA FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                    log_event('theta_reentry_filled', ticker=ticker, order_id=order_id,
                              filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars,
                              start_price_cents=start_price, minutes_into=c['minutes_into'])
                    self._queue_tg("THETA BUY", ticker,
                                   price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                                   title=c['title'][:60])
                    continue

            # Not filled — cancel
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass
            print(f"    Theta not filled for {ticker}, canceled")
            log_event('theta_reentry_unfilled', ticker=ticker, order_id=order_id)

        if n_filled:
            print(f"  Theta: {n_filled} new fills from {len(candidates)} candidates")

    async def _scan_stale_orders(self, mention_markets, milestones, now):
        """Scan all mention markets 1h+ into event for forgotten limit orders.
        A stale order = cheapest NO ask is >=15c below the next cheapest.
        Buy at exactly the stale price (limit order, zero slippage)."""
        if not STALE_ENABLED:
            return

        stale_count = self.positions.count('stale_buy_no')
        if stale_count >= STALE_MAX_POSITIONS:
            return

        # Filter to mention markets that are live and 1h+ into event
        candidates = []
        for m in mention_markets:
            ticker = m.get('ticker', '')
            ticker_upper = ticker.upper()

            # Must be a mention market (has MENTION or FINALS in ticker)
            if 'MENTION' not in ticker_upper and 'FINALS' not in ticker_upper:
                continue

            # Skip killed categories and pre-recorded shows
            event_ticker = m.get('event_ticker', '')
            series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
            if _is_killed_series(series) or series in PRERECORDED_SERIES:
                continue

            # Detect category for blacklists and labeling
            is_nba = 'NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper
            is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper

            # Master blacklist check (NBA/NCAAB)
            word = ticker.split('-')[-1].upper()
            if is_nba and word in NBA_BLACKLIST:
                continue
            if is_ncaa and word in NCAAB_BLACKLIST:
                continue

            ms = milestones.get(event_ticker)
            if not ms or not ms.get('start_ts'):
                continue

            hours_into = (now - ms['start_ts']) / 3600.0
            if hours_into < STALE_MIN_HOURS_INTO_GAME:
                continue
            # Don't scan after event ended
            if ms.get('end_ts') and ms['end_ts'] <= now:
                continue

            # Determine category label
            if is_nba:
                cat_label = 'NBA'
            elif is_ncaa:
                cat_label = 'NCAA'
            elif 'TRUMPMENTION' in ticker_upper:
                cat_label = 'Trump'
            elif 'EARNINGSMENTION' in ticker_upper:
                cat_label = 'Earnings'
            elif 'NFLMENTION' in ticker_upper:
                cat_label = 'NFL'
            elif 'WOMENTION' in ticker_upper:
                cat_label = 'Olympics'
            elif 'FIGHTMENTION' in ticker_upper:
                cat_label = 'Fight'
            else:
                cat_label = 'Other'

            candidates.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'word': word,
                'hours_into': hours_into,
                'title': m.get('title', m.get('subtitle', '')),
                'cat': cat_label,
            })

        if not candidates:
            return

        n_filled = 0
        for c in candidates:
            if stale_count >= STALE_MAX_POSITIONS:
                break

            ticker = c['ticker']

            # Skip if we already have any position on this ticker (any signal type)
            if self.positions.has_open_ticker(ticker):
                continue

            # Per-market cap — include both open positions AND resting premarket orders
            ticker_exp = self._global_ticker_exposure(ticker)
            if ticker_exp >= min(STALE_MAX_MARKET_DOLLARS, GLOBAL_MAX_MARKET_DOLLARS):
                continue

            # Fetch orderbook
            ob = self.client.get_orderbook(ticker)
            if not ob:
                continue

            # Build sorted NO ask levels from YES bids.
            # YES bids: [[price, qty], ...] → NO ask = 100 - yes_bid_price
            yes_bids = ob.get('yes', [])
            if not isinstance(yes_bids, list) or len(yes_bids) < 2:
                continue

            # Aggregate quantity per YES bid level, then convert to NO asks
            yes_levels = {}  # yes_price -> total_qty
            for price, qty in yes_bids:
                yes_levels[price] = yes_levels.get(price, 0) + qty

            # Sort YES bids descending → NO asks ascending
            sorted_yes = sorted(yes_levels.items(), key=lambda x: x[0], reverse=True)
            # Convert to NO ask levels: (no_price, qty)
            no_asks = [(100 - yp, yq) for yp, yq in sorted_yes]
            # no_asks is now sorted ascending by NO price

            if len(no_asks) < 2:
                continue

            cheapest_no, cheapest_qty = no_asks[0]
            next_no = no_asks[1][0]
            gap = next_no - cheapest_no

            if gap < STALE_MIN_GAP_CENTS:
                continue
            if cheapest_no < STALE_MIN_NO_CENTS:
                continue

            # Stale order found! Buy at exactly this price (no slippage).
            # Cap to available quantity, strategy cap, and global cap
            remaining_global = GLOBAL_MAX_MARKET_DOLLARS - ticker_exp
            stale_cap = min(STALE_BET_DOLLARS, remaining_global)
            max_contracts = min(cheapest_qty, int(stale_cap / (cheapest_no / 100)))
            if max_contracts < 1:
                max_contracts = 1
            # Only buy what's at the stale level
            contracts = min(max_contracts, cheapest_qty)
            bet_dollars = round(contracts * cheapest_no / 100, 2)
            if bet_dollars > stale_cap:
                contracts = int(stale_cap / (cheapest_no / 100))
                bet_dollars = round(contracts * cheapest_no / 100, 2)
            if contracts < 1:
                continue

            cat = c['cat']
            print(f"  STALE [{cat}]: {c['word']} NO @ {cheapest_no}c (next={next_no}c, gap={gap}c, qty={cheapest_qty}) "
                  f"{contracts}x = ${bet_dollars:.2f} ({c['hours_into']:.1f}h in)")

            if DRY_RUN:
                order_info = {
                    'order_id': f'DRY-STALE-{uuid.uuid4().hex[:8]}',
                    'fill_price': cheapest_no / 100,
                    'fill_count': contracts,
                    'bet_dollars': bet_dollars,
                    'dry_run': True,
                }
                sig = {
                    'signal_type': 'stale_buy_no',
                    'ticker': ticker,
                    'event_ticker': c['event_ticker'],
                    'title': c['title'],
                    'no_price_cents': cheapest_no,
                    'hours_to_event': -c['hours_into'],
                    'is_live': True,
                    'stale_gap': gap,
                    'stale_next_no': next_no,
                    # Required by positions.add()
                    'signal_time': time.time(),
                    'fade_action': 'BUY',
                    'fade_side': 'no',
                    'entry_price': cheapest_no / 100,
                    'pre_signal_price': cheapest_no / 100,
                    'price_move': 0,
                    'n_small_trades': 0,
                    'retail_contracts': 0,
                }
                self.positions.add(sig, order_info)
                stale_count += 1
                n_filled += 1
                continue

            # Place limit order at exactly the stale price (no slippage)
            order = self.client.create_order(
                ticker=ticker, side='no', action='buy',
                count=contracts, price_cents=cheapest_no,
            )
            if not order:
                print(f"    STALE order failed for {ticker}")
                continue

            order_id = order.get('order_id', '')
            log_event('stale_order_placed', ticker=ticker, order_id=order_id,
                      contracts=contracts, price_cents=cheapest_no, gap=gap,
                      next_no=next_no, bet_dollars=bet_dollars,
                      hours_into=c['hours_into'], category=cat)

            # Taker at exact price — should fill instantly if order still there
            time.sleep(2)
            status = self.client.get_order(order_id)
            if status:
                filled = status.get('quantity_filled', 0)
                remaining = status.get('remaining_count', 0)
                # Cancel any unfilled remainder immediately — no resting
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass

                if filled > 0:
                    avg_fill = status.get('average_fill_price', cheapest_no)
                    actual_dollars = round(filled * avg_fill / 100, 2)
                    sig = {
                        'signal_type': 'stale_buy_no',
                        'ticker': ticker,
                        'event_ticker': c['event_ticker'],
                        'title': c['title'],
                        'no_price_cents': cheapest_no,
                        'hours_to_event': -c['hours_into'],
                        'is_live': True,
                        'stale_gap': gap,
                        'stale_next_no': next_no,
                        # Required by positions.add()
                        'signal_time': time.time(),
                        'fade_action': 'BUY',
                        'fade_side': 'no',
                        'entry_price': avg_fill / 100,
                        'pre_signal_price': cheapest_no / 100,
                        'price_move': 0,
                        'n_small_trades': 0,
                        'retail_contracts': 0,
                    }
                    info = {
                        'order_id': order_id,
                        'fill_price': avg_fill / 100,
                        'fill_count': filled,
                        'bet_dollars': actual_dollars,
                        'dry_run': False,
                    }
                    self.positions.add(sig, info)
                    stale_count += 1
                    n_filled += 1

                    self.trade_logger.record({
                        'type': 'entry', 'strategy': 'stale_buy_no',
                        'ticker': ticker, 'order_id': order_id,
                        'side': 'no', 'action': 'buy',
                        'contracts_filled': filled, 'price_cents': cheapest_no,
                        'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                        'gap': gap, 'next_no': next_no,
                    })
                    print(f"    STALE FILLED: {filled}/{contracts} NO @ {avg_fill}c (${actual_dollars:.2f}) gap={gap}c")
                    log_event('stale_filled', ticker=ticker, order_id=order_id,
                              filled=filled, avg_fill_cents=avg_fill,
                              bet_dollars=actual_dollars, gap=gap)
                    await self.notifier.send_order_event(
                        f"STALE SNIPE [{cat}]", ticker,
                        price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                        title=c['title'][:60],
                        extra=f"Gap={gap}c (stale={cheapest_no}c, next={next_no}c), {c['hours_into']:.1f}h into game")
                else:
                    print(f"    STALE not filled (order gone?): {ticker}")
                    log_event('stale_unfilled', ticker=ticker, order_id=order_id)
            else:
                # Cancel just in case
                try:
                    self.client.cancel_order(order_id)
                except Exception:
                    pass

        if n_filled > 0:
            print(f"  Stale orders filled: {n_filled}")

    # ------------------------------------------------------------------
    # NCAAB Game Outcome Fade Strategy: buy YES when live favorite drops
    # ------------------------------------------------------------------
    async def _scan_ncaab_fade(self, now):
        """NCAAB fade strategy: when a pregame favorite's YES price drops to
        the trigger level within the first 50 minutes of the game, buy YES
        expecting reversion to the mean.

        Backtest: vel>=0.015, drop>=0.25, trigger @45c → 65% WR, +43% ROI.
        """
        if not NCAAB_FADE_ENABLED:
            return

        fade_count = self.positions.count('ncaab_fade_yes')
        if fade_count >= NCAAB_FADE_MAX_POSITIONS:
            return

        # Discover NCAAB markets (men's + women's) — cache for 5 minutes
        fade_series_list = NCAAB_FADE_SERIES if isinstance(NCAAB_FADE_SERIES, list) else [NCAAB_FADE_SERIES]
        if not hasattr(self, '_ncaab_fade_markets_cache') or now - self._ncaab_fade_markets_cache_ts > 300:
            try:
                all_markets = []
                for series_ticker in fade_series_list:
                    cursor = None
                    pages = 0
                    while pages < 20:
                        params = {
                            'series_ticker': series_ticker,
                            'status': 'open',
                            'limit': 200,
                        }
                        if cursor:
                            params['cursor'] = cursor
                        resp = self.client.session.get(
                            f'{KALSHI_BASE}/markets', params=params, timeout=15
                        )
                        if resp.status_code == 429:
                            time.sleep(2)
                            continue
                        if resp.status_code != 200:
                            break
                        data = resp.json()
                        markets = data.get('markets', [])
                        all_markets.extend(markets)
                        cursor = data.get('cursor', '')
                        pages += 1
                        if not markets or not cursor:
                            break
                self._ncaab_fade_markets_cache = all_markets
                self._ncaab_fade_markets_cache_ts = now
            except Exception as e:
                print(f"  NCAAB FADE: market fetch error: {e}")
                self._ncaab_fade_markets_cache = getattr(self, '_ncaab_fade_markets_cache', [])
                self._ncaab_fade_markets_cache_ts = now

        ncaab_markets = self._ncaab_fade_markets_cache
        if not ncaab_markets:
            return

        # Get milestones for game start times
        milestones = self.client.get_milestones()

        trigger_c = NCAAB_FADE_TRIGGER_CENTS
        min_pregame = NCAAB_FADE_MIN_PREGAME_YES
        min_drop = NCAAB_FADE_MIN_DROP_SIZE
        min_vel = NCAAB_FADE_MIN_VELOCITY
        max_minutes = NCAAB_FADE_MAX_MINUTES

        skip_reasons = {
            'no_milestone': 0, 'not_live': 0, 'too_late': 0,
            'no_price': 0, 'price_above_trigger': 0,
            'already_pos': 0, 'no_pregame': 0,
            'drop_too_small': 0, 'vel_too_low': 0,
        }
        signals = []

        for m in ncaab_markets:
            ticker = m.get('ticker', '')
            event_ticker = m.get('event_ticker', '')

            # Game start from milestone or expected_expiration
            ms = milestones.get(event_ticker)
            game_start_ts = None
            if ms and ms.get('start_ts'):
                game_start_ts = ms['start_ts']
            else:
                # Fallback: expected_expiration - 2.5h
                exp_str = m.get('expected_expiration_time', '')
                if exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(exp_str.replace('Z', '+00:00'))
                        game_start_ts = exp_dt.timestamp() - NCAAB_FADE_GAME_DURATION_HOURS * 3600
                    except Exception:
                        pass

            if game_start_ts is None:
                skip_reasons['no_milestone'] += 1
                continue

            minutes_into_game = (now - game_start_ts) / 60.0
            if minutes_into_game < 0:
                skip_reasons['not_live'] += 1
                continue
            if minutes_into_game > max_minutes:
                skip_reasons['too_late'] += 1
                continue

            # Get current YES price
            yes_price_c = None
            yes_bid = _market_cents(m, 'yes_bid')
            yes_ask = _market_cents(m, 'yes_ask')
            if yes_bid is not None and yes_ask is not None:
                yes_price_c = (yes_bid + yes_ask) // 2
            if yes_price_c is None:
                yes_price_c = _market_cents(m, 'last_price')
            if yes_price_c is None:
                skip_reasons['no_price'] += 1
                continue

            # Price must be at or below trigger
            if yes_price_c > trigger_c:
                skip_reasons['price_above_trigger'] += 1
                continue

            # Dedup: skip if already holding this ticker
            if self.positions.has_open_ticker(ticker, signal_type='ncaab_fade_yes'):
                skip_reasons['already_pos'] += 1
                continue

            # Global scan-cycle dedup
            if ticker in self._entered_this_cycle:
                skip_reasons['already_pos'] += 1
                continue

            # Need pregame price — fetch recent trades from before game start
            # Use a cached pregame price to avoid repeated API calls
            pregame_key = f'_fade_pregame_{ticker}'
            pregame_price_c = getattr(self, pregame_key, None)

            if pregame_price_c is None:
                # Fetch trades in the 60-min window before game start
                pregame_start = int(game_start_ts - 3600)
                pregame_end = int(game_start_ts)
                try:
                    trades, _ = self.client.get_trades(
                        ticker=ticker, limit=100,
                        min_ts=pregame_start, max_ts=pregame_end,
                    )
                    if trades:
                        prices = []
                        for t in trades:
                            try:
                                prices.append(int(t.get('yes_price', 0)))
                            except (ValueError, TypeError):
                                pass
                        if prices:
                            pregame_price_c = int(sum(prices) / len(prices))
                            setattr(self, pregame_key, pregame_price_c)
                except Exception:
                    pass
                time.sleep(0.3)  # Rate limit

            if pregame_price_c is None or pregame_price_c < min_pregame:
                skip_reasons['no_pregame'] += 1
                continue

            # Check drop size
            drop_size = pregame_price_c - trigger_c
            if drop_size < min_drop:
                skip_reasons['drop_too_small'] += 1
                continue

            # Check velocity: cents dropped per minute
            if minutes_into_game > 0:
                velocity = (pregame_price_c - yes_price_c) / minutes_into_game
            else:
                velocity = 0
            if velocity < min_vel:
                skip_reasons['vel_too_low'] += 1
                continue

            # Determine series for per-series bet sizing
            series = ''
            for s in fade_series_list:
                if s in ticker:
                    series = s
                    break

            signals.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'title': m.get('title', ticker),
                'yes_price_c': yes_price_c,
                'pregame_price_c': pregame_price_c,
                'drop_size': drop_size,
                'velocity': round(velocity, 4),
                'minutes_into_game': round(minutes_into_game, 1),
                'game_start_ts': game_start_ts,
                'series': series,
            })

        active_skips = {k: v for k, v in skip_reasons.items() if v > 0}
        if signals or active_skips:
            print(f"  NCAAB FADE: {len(ncaab_markets)} markets, {len(signals)} signals, "
                  f"{fade_count}/{NCAAB_FADE_MAX_POSITIONS} pos, skips: {active_skips}")

        for sig in signals:
            if fade_count >= NCAAB_FADE_MAX_POSITIONS:
                print(f"    NCAAB FADE CAP: {fade_count}/{NCAAB_FADE_MAX_POSITIONS}, stopping")
                break

            tag = 'NCAAW' if 'NCAAWB' in sig.get('series', '') else 'NCAAB'
            sig_bet = NCAAB_FADE_BET_BY_SERIES.get(sig.get('series', ''), NCAAB_FADE_BET_DOLLARS)
            print(f"  {tag} FADE: BUY YES @ {trigger_c}c ${sig_bet} '{sig['title'][:50]}' "
                  f"(pre={sig['pregame_price_c']}c, drop={sig['drop_size']}c, "
                  f"vel={sig['velocity']:.3f}c/min, T+{sig['minutes_into_game']:.0f}min)")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_ncaab_fade_entry(sig)

            if order_info:
                # Build signal dict for position tracker
                pos_sig = {
                    'ticker': sig['ticker'],
                    'event_ticker': sig['event_ticker'],
                    'title': sig['title'],
                    'signal_type': 'ncaab_fade_yes',
                    'fade_action': 'BUY',
                    'fade_side': 'yes',
                    'entry_price': trigger_c / 100,
                    'pre_signal_price': sig['pregame_price_c'] / 100,
                    'price_move': sig['drop_size'] / 100,
                    'n_small_trades': 0,
                    'retail_contracts': 0,
                    'signal_time': now,
                    'no_price': (100 - trigger_c) / 100,
                    'no_price_cents': 100 - trigger_c,
                    'hours_before_close': 0,
                    'hours_to_event': -(sig['minutes_into_game'] / 60),
                    'close_ts': sig['game_start_ts'] + NCAAB_FADE_GAME_DURATION_HOURS * 3600,
                }
                self.positions.add(pos_sig, order_info)
                self._entered_this_cycle.add(sig['ticker'])
                fade_count += 1
                await self.notifier.send_order_event(
                    f"{tag} FADE BUY YES", sig['ticker'],
                    price_cents=order_info.get('fill_price', trigger_c / 100) * 100
                        if isinstance(order_info.get('fill_price'), float) else trigger_c,
                    contracts=order_info.get('fill_count', 0),
                    bet_dollars=order_info.get('bet_dollars', 0),
                    title=sig['title'][:60],
                    extra=f"Pre={sig['pregame_price_c']}c, drop={sig['drop_size']}c, vel={sig['velocity']:.3f}c/min, T+{sig['minutes_into_game']:.0f}min")
                log_event('ncaab_fade_filled', ticker=sig['ticker'],
                          pregame=sig['pregame_price_c'], trigger=trigger_c,
                          drop=sig['drop_size'], velocity=sig['velocity'],
                          minutes_into=sig['minutes_into_game'],
                          bet_dollars=order_info.get('bet_dollars', 0))

    def _execute_ncaab_fade_entry(self, sig):
        """Execute a NCAAB fade BUY YES entry at the trigger price.
        Taker order: buy YES at 45c (or best ask if cheaper)."""
        ticker = sig['ticker']
        trigger_c = NCAAB_FADE_TRIGGER_CENTS
        series = sig.get('series', '')
        bet_dollars = NCAAB_FADE_BET_BY_SERIES.get(series, NCAAB_FADE_BET_DOLLARS)

        # Global per-market hard ceiling
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    NCAAB FADE: no orderbook for {ticker}, skipping")
            return None

        # Best YES ask = 100 - best NO bid
        no_bids_raw = orderbook.get('no', [])
        if not isinstance(no_bids_raw, list):
            no_bids_raw = []
        if not no_bids_raw:
            print(f"    NCAAB FADE: no NO bids for {ticker} (no YES ask available), skipping")
            return None

        best_no_bid = max(b[0] for b in no_bids_raw)
        best_yes_ask = 100 - best_no_bid

        # Only buy at or below trigger
        if best_yes_ask > trigger_c:
            print(f"    NCAAB FADE: YES ask {best_yes_ask}c > trigger {trigger_c}c, skipping")
            return None

        # Buy at trigger price (limit order — may fill at better price)
        buy_price = trigger_c
        contracts = int(bet_dollars / (buy_price / 100))
        if contracts < 1:
            contracts = 1
        actual_dollars = round(contracts * buy_price / 100, 2)

        # Cap to series bet limit
        if actual_dollars > bet_dollars:
            contracts = int(bet_dollars / (buy_price / 100))
            actual_dollars = round(contracts * buy_price / 100, 2)
            if contracts < 1:
                print(f"    NCAAB FADE: can't fit within ${bet_dollars} at {buy_price}c, skipping")
                return None
        bet_dollars = actual_dollars

        print(f"    NCAAB FADE taker: {contracts} YES @ {buy_price}c (ask={best_yes_ask}c) = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-FADE-{uuid.uuid4().hex[:8]}',
                'fill_price': buy_price / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
                'side': 'yes',
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'ncaab_fade_yes',
                'ticker': ticker, 'side': 'yes', 'action': 'buy',
                'contracts': contracts, 'price_cents': buy_price,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    DRY RUN: {contracts} YES @ {buy_price}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='yes', action='buy',
            count=contracts, price_cents=buy_price,
        )
        if not order:
            print(f"    NCAAB FADE: order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    NCAAB FADE order placed: {order_id} ({contracts} YES @ {buy_price}c, ${bet_dollars:.2f})")
        log_event('ncaab_fade_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=buy_price, bet_dollars=bet_dollars,
                  pregame=sig.get('pregame_price_c'), velocity=sig.get('velocity'))

        # Taker — should fill instantly
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', buy_price)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                    'side': 'yes',
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'ncaab_fade_yes',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'yes', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': buy_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    NCAAB FADE FILLED: {filled}/{contracts} YES @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('ncaab_fade_entry_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars)
                self._queue_tg("NCAAB FADE FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    NCAAB FADE taker not filled for {ticker}, canceled")
        log_event('ncaab_fade_unfilled', ticker=ticker, order_id=order_id)
        return None

    # ================================================================
    # TENNIS MATCH OUTCOME FADE — BUY YES on early-match price drops
    # ================================================================

    async def _scan_tennis_fade(self, now):
        """Tennis fade strategy: when a pre-match favorite's YES price drops to
        the trigger level within the first 60 minutes of the match, buy YES
        expecting reversion.

        Sizing: $3 base, $10 for early entry (<=45min) or fast velocity (>2c/min).
        Backtest: 562 trades, 41.8% WR, +37.7% ROI overall;
        early entry (<=45min): 55.4% WR, +86.5% ROI.
        """
        if not TENNIS_FADE_ENABLED:
            return

        fade_count = self.positions.count('tennis_fade_yes')
        if fade_count >= TENNIS_FADE_MAX_POSITIONS:
            return

        # Discover ATP/WTA match markets — cache for 5 min
        if not hasattr(self, '_tennis_fade_markets_cache') or now - self._tennis_fade_markets_cache_ts > 300:
            try:
                all_markets = []
                for series in TENNIS_FADE_SERIES:
                    cursor = None
                    pages = 0
                    while pages < 20:
                        params = {
                            'series_ticker': series,
                            'status': 'open',
                            'limit': 200,
                        }
                        if cursor:
                            params['cursor'] = cursor
                        resp = self.client.session.get(
                            f'{KALSHI_BASE}/markets', params=params, timeout=15
                        )
                        if resp.status_code == 429:
                            time.sleep(2)
                            continue
                        if resp.status_code != 200:
                            break
                        data = resp.json()
                        markets = data.get('markets', [])
                        all_markets.extend(markets)
                        cursor = data.get('cursor', '')
                        pages += 1
                        if not markets or not cursor:
                            break
                self._tennis_fade_markets_cache = all_markets
                self._tennis_fade_markets_cache_ts = now
            except Exception as e:
                print(f"  TENNIS FADE: market fetch error: {e}")
                self._tennis_fade_markets_cache = getattr(self, '_tennis_fade_markets_cache', [])
                self._tennis_fade_markets_cache_ts = now

        tennis_markets = self._tennis_fade_markets_cache
        if not tennis_markets:
            return

        milestones = self.client.get_milestones()

        trigger_c = TENNIS_FADE_TRIGGER_CENTS
        min_pregame = TENNIS_FADE_MIN_PREGAME_YES
        min_drop = TENNIS_FADE_MIN_DROP_SIZE
        max_minutes = TENNIS_FADE_MAX_MINUTES

        skip_reasons = {
            'no_timing': 0, 'not_live': 0, 'too_late': 0,
            'no_price': 0, 'price_above_trigger': 0,
            'already_pos': 0, 'no_pregame': 0,
            'drop_too_small': 0, 'no_match': 0,
        }
        signals = []

        for m in tennis_markets:
            ticker = m.get('ticker', '')
            event_ticker = m.get('event_ticker', '')

            # Match start from milestone or expected_expiration
            ms = milestones.get(event_ticker)
            match_start_ts = None
            if ms and ms.get('start_ts'):
                match_start_ts = ms['start_ts']
            else:
                exp_str = m.get('expected_expiration_time', '')
                if exp_str:
                    try:
                        exp_dt = datetime.fromisoformat(exp_str.replace('Z', '+00:00'))
                        match_start_ts = exp_dt.timestamp() - TENNIS_FADE_MATCH_DURATION_HOURS * 3600
                    except Exception:
                        pass

            if match_start_ts is None:
                skip_reasons['no_timing'] += 1
                continue

            minutes_into_match = (now - match_start_ts) / 60.0
            if minutes_into_match < 0:
                skip_reasons['not_live'] += 1
                continue
            if minutes_into_match > max_minutes:
                skip_reasons['too_late'] += 1
                continue

            # Get current YES price
            yes_price_c = None
            yes_bid = _market_cents(m, 'yes_bid')
            yes_ask = _market_cents(m, 'yes_ask')
            if yes_bid is not None and yes_ask is not None:
                yes_price_c = (yes_bid + yes_ask) // 2
            if yes_price_c is None:
                yes_price_c = _market_cents(m, 'last_price')
            if yes_price_c is None:
                skip_reasons['no_price'] += 1
                continue

            # Price must be at or below trigger
            if yes_price_c > trigger_c:
                skip_reasons['price_above_trigger'] += 1
                continue

            # Dedup — one position per ticker
            if self.positions.has_open_ticker(ticker, signal_type='tennis_fade_yes'):
                skip_reasons['already_pos'] += 1
                continue
            if ticker in self._entered_this_cycle:
                skip_reasons['already_pos'] += 1
                continue

            # Need pregame price
            pregame_key = f'_tennis_fade_pregame_{ticker}'
            pregame_price_c = getattr(self, pregame_key, None)

            if pregame_price_c is None:
                pregame_start = int(match_start_ts - 3600)
                pregame_end = int(match_start_ts)
                try:
                    trades, _ = self.client.get_trades(
                        ticker=ticker, limit=100,
                        min_ts=pregame_start, max_ts=pregame_end,
                    )
                    if trades:
                        prices = []
                        for t in trades:
                            try:
                                prices.append(int(t.get('yes_price', 0)))
                            except (ValueError, TypeError):
                                pass
                        if prices:
                            pregame_price_c = int(sum(prices) / len(prices))
                            setattr(self, pregame_key, pregame_price_c)
                except Exception:
                    pass
                time.sleep(0.3)

            if pregame_price_c is None or pregame_price_c < min_pregame:
                skip_reasons['no_pregame'] += 1
                continue

            drop_size = pregame_price_c - yes_price_c
            if drop_size < min_drop:
                skip_reasons['drop_too_small'] += 1
                continue

            velocity = (pregame_price_c - yes_price_c) / max(minutes_into_match, 1)
            tour = 'ATP' if 'KXATPMATCH' in ticker else 'WTA'

            signals.append({
                'ticker': ticker,
                'event_ticker': event_ticker,
                'title': m.get('title', ticker),
                'yes_price_c': yes_price_c,
                'pregame_price_c': pregame_price_c,
                'drop_size': drop_size,
                'velocity': round(velocity, 4),
                'minutes_into_match': round(minutes_into_match, 1),
                'match_start_ts': match_start_ts,
                'tour': tour,
            })

        active_skips = {k: v for k, v in skip_reasons.items() if v > 0}
        if signals or active_skips:
            print(f"  TENNIS FADE: {len(tennis_markets)} markets, {len(signals)} signals, "
                  f"{fade_count}/{TENNIS_FADE_MAX_POSITIONS} pos, skips: {active_skips}")

        for sig in signals:
            if fade_count >= TENNIS_FADE_MAX_POSITIONS:
                print(f"    TENNIS FADE CAP: {fade_count}/{TENNIS_FADE_MAX_POSITIONS}, stopping")
                break

            bet_dollars = TENNIS_FADE_BET_DOLLARS

            print(f"  TENNIS FADE: BUY YES @ {trigger_c}c '{sig['title'][:50]}' "
                  f"({sig['tour']}, pre={sig['pregame_price_c']}c, drop={sig['drop_size']}c, "
                  f"vel={sig['velocity']:.3f}c/min, T+{sig['minutes_into_match']:.0f}min)")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_tennis_fade_entry(sig, bet_dollars)

            if order_info:
                pos_sig = {
                    'ticker': sig['ticker'],
                    'event_ticker': sig['event_ticker'],
                    'title': sig['title'],
                    'signal_type': 'tennis_fade_yes',
                    'fade_action': 'BUY',
                    'fade_side': 'yes',
                    'entry_price': trigger_c / 100,
                    'pre_signal_price': sig['pregame_price_c'] / 100,
                    'price_move': sig['drop_size'] / 100,
                    'n_small_trades': 0,
                    'retail_contracts': 0,
                    'signal_time': now,
                    'no_price': (100 - trigger_c) / 100,
                    'no_price_cents': 100 - trigger_c,
                    'hours_before_close': 0,
                    'hours_to_event': -(sig['minutes_into_match'] / 60),
                    'close_ts': sig['match_start_ts'] + TENNIS_FADE_MATCH_DURATION_HOURS * 3600,
                }
                self.positions.add(pos_sig, order_info)
                self._entered_this_cycle.add(sig['ticker'])
                fade_count += 1
                await self.notifier.send_order_event(
                    "TENNIS FADE BUY YES", sig['ticker'],
                    price_cents=order_info.get('fill_price', trigger_c / 100) * 100
                        if isinstance(order_info.get('fill_price'), float) else trigger_c,
                    contracts=order_info.get('fill_count', 0),
                    bet_dollars=order_info.get('bet_dollars', 0),
                    title=sig['title'][:60],
                    extra=f"{sig['tour']} Pre={sig['pregame_price_c']}c, drop={sig['drop_size']}c, vel={sig['velocity']:.3f}c/min, T+{sig['minutes_into_match']:.0f}min")
                log_event('tennis_fade_filled', ticker=sig['ticker'],
                          pregame=sig['pregame_price_c'], trigger=trigger_c,
                          drop=sig['drop_size'], velocity=sig['velocity'],
                          minutes_into=sig['minutes_into_match'], tour=sig['tour'],
                          bet_dollars=order_info.get('bet_dollars', 0))

    def _execute_tennis_fade_entry(self, sig, bet_dollars):
        """Execute a tennis fade BUY YES entry. Taker order at trigger price."""
        ticker = sig['ticker']
        trigger_c = TENNIS_FADE_TRIGGER_CENTS

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    TENNIS FADE: no orderbook for {ticker}, skipping")
            return None

        no_bids_raw = orderbook.get('no', [])
        if not isinstance(no_bids_raw, list):
            no_bids_raw = []
        if not no_bids_raw:
            print(f"    TENNIS FADE: no NO bids for {ticker}, skipping")
            return None

        best_no_bid = max(b[0] for b in no_bids_raw)
        best_yes_ask = 100 - best_no_bid

        if best_yes_ask > trigger_c:
            print(f"    TENNIS FADE: YES ask {best_yes_ask}c > trigger {trigger_c}c, skipping")
            return None

        buy_price = trigger_c
        contracts = int(bet_dollars / (buy_price / 100))
        if contracts < 1:
            contracts = 1
        actual_dollars = round(contracts * buy_price / 100, 2)

        # Cap to bet_dollars
        if actual_dollars > bet_dollars:
            contracts = int(bet_dollars / (buy_price / 100))
            actual_dollars = round(contracts * buy_price / 100, 2)
            if contracts < 1:
                print(f"    TENNIS FADE: can't fit within ${bet_dollars} at {buy_price}c, skipping")
                return None

        print(f"    TENNIS FADE taker: {contracts} YES @ {buy_price}c (ask={best_yes_ask}c) = ${actual_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-TFADE-{uuid.uuid4().hex[:8]}',
                'fill_price': buy_price / 100,
                'fill_count': contracts,
                'bet_dollars': actual_dollars,
                'dry_run': True,
                'side': 'yes',
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'tennis_fade_yes',
                'ticker': ticker, 'side': 'yes', 'action': 'buy',
                'contracts': contracts, 'price_cents': buy_price,
                'bet_dollars': actual_dollars, 'dry_run': True,
            })
            print(f"    DRY RUN: {contracts} YES @ {buy_price}c (${actual_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='yes', action='buy',
            count=contracts, price_cents=buy_price,
        )
        if not order:
            print(f"    TENNIS FADE: order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    TENNIS FADE order placed: {order_id} ({contracts} YES @ {buy_price}c, ${actual_dollars:.2f})")
        log_event('tennis_fade_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=buy_price, bet_dollars=actual_dollars,
                  pregame=sig.get('pregame_price_c'), velocity=sig.get('velocity'),
                  tour=sig.get('tour'))

        # Taker — should fill instantly
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', buy_price)
                filled_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': filled_dollars,
                    'dry_run': False,
                    'side': 'yes',
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'tennis_fade_yes',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'yes', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': buy_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': filled_dollars,
                })
                print(f"    TENNIS FADE FILLED: {filled}/{contracts} YES @ avg {avg_fill}c (${filled_dollars:.2f})")
                log_event('tennis_fade_entry_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=filled_dollars)
                self._queue_tg("TENNIS FADE FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=filled_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    TENNIS FADE taker not filled for {ticker}, canceled")
        log_event('tennis_fade_unfilled', ticker=ticker, order_id=order_id)
        return None

    def _execute_premarket_maker(self, sig, orderbook, yes_bids_raw, best_no_ask, category='NCAA'):
        """Place a single resting NO buy limit order for pre-event markets.
        Price: 1c above highest other bidder (penny-above rule).
        Returns None — fill detected by _check_resting_premarket_orders."""
        ticker = sig['ticker']
        no_price_cents = sig['no_price_cents']
        event_start_ts = sig.get('event_start_ts')

        # Require event_start_ts — without it, the order has no auto-cancel
        if not event_start_ts:
            print(f"    Maker skip: no event_start_ts for {ticker}, order would never auto-cancel")
            return None

        # Global per-market hard ceiling
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping maker {ticker}")
            return None

        # Total resting-exposure ceiling across ALL open maker orders
        total_resting = sum(o.get('bet_dollars', 0) for o in self._resting_premarket_orders.values())
        if total_resting >= PREMARKET_MAX_TOTAL_RESTING_DOLLARS:
            print(f"    TOTAL RESTING cap reached (${total_resting:.2f}/${PREMARKET_MAX_TOTAL_RESTING_DOLLARS}), skipping maker {ticker}")
            return None

        # Skip if we already have a resting order on this ticker
        for info in self._resting_premarket_orders.values():
            if info['ticker'] == ticker:
                return None

        # Get best NO bid from orderbook
        no_bids_raw = orderbook.get('no', [])
        if not isinstance(no_bids_raw, list):
            no_bids_raw = []
        best_no_bid = max(b[0] for b in no_bids_raw) if no_bids_raw else 0

        # Penny-above rule: 1c above highest other bidder, ignore dust (<$1.50)
        other_bids_filtered = []
        for lvl in set(b[0] for b in no_bids_raw):
            lvl_size = sum(b[1] for b in no_bids_raw if b[0] == lvl) * lvl / 100.0
            if lvl_size > 1.50:
                other_bids_filtered.append(lvl)
        next_best_bid = max(other_bids_filtered) if other_bids_filtered else 0

        min_no_c, max_no_c = get_no_range(ticker)
        if next_best_bid > 0:
            resting_price = next_best_bid + 1
        else:
            resting_price = min_no_c  # No other bids — sit at floor

        # Clamp to category range
        resting_price = max(resting_price, min_no_c)
        resting_price = min(resting_price, max_no_c)

        # Empty book (no NO ask yet): rest at our computed floor price and let
        # the penny-above refresh climb it as the book fills. There's no ask to
        # cross and no spread to enforce.
        empty_book = best_no_ask is None or best_no_ask < 1
        if empty_book:
            spread = max_no_c  # treat as wide for sizing purposes
        else:
            if resting_price >= best_no_ask:
                return None  # Would cross the spread
            spread = best_no_ask - best_no_bid if best_no_bid > 0 else best_no_ask
            if spread < PREMARKET_MIN_SPREAD:
                print(f"    Spread {spread}c too narrow (<{PREMARKET_MIN_SPREAD}c), skipping maker — taker may be better")
                return None

        # Bet sizing — maker uses PREMARKET_BET_DOLLARS, with per-category override for winners
        maker_cat_name = get_mention_category(ticker)
        if maker_cat_name in CATEGORY_BET_OVERRIDE:
            mention_bet = CATEGORY_BET_OVERRIDE[maker_cat_name]
        else:
            mention_bet = PREMARKET_BET_DOLLARS

        # Spread-based cap: narrow spreads get smaller orders
        if spread < 10:
            mention_bet = min(mention_bet, 5)
        else:
            mention_bet = min(mention_bet, 10)

        # New/unknown series cap
        event_ticker = sig.get('event_ticker', '')
        series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker)
        if series not in MENTION_SCAN_SERIES:
            resolved = getattr(self.client, '_series_resolved_counts', {}).get(series, 0)
            if resolved < PREMARKET_NEW_SERIES_MIN:
                mention_bet = min(mention_bet, PREMARKET_NEW_SERIES_BET)

        # NBA halftime uses tighter per-market cap even for maker orders (also capped by global)
        is_nba_maker = 'NBAMENTION' in ticker.upper() or 'NBAFINALS' in ticker.upper()
        maker_market_cap = NBA_HALFTIME_MAX_MARKET_DOLLARS if (is_nba_maker and NBA_HALFTIME_ENABLED) else PREMARKET_MAX_MARKET_DOLLARS
        maker_market_cap = min(maker_market_cap, GLOBAL_MAX_MARKET_DOLLARS)
        mention_bet = min(mention_bet, maker_market_cap)

        # Per-market exposure check (includes resting maker orders)
        ticker_exp = self._global_ticker_exposure(ticker)
        remaining_market_cap = maker_market_cap - ticker_exp
        if remaining_market_cap <= 0:
            return None
        mention_bet = min(mention_bet, remaining_market_cap)

        # Per-event exposure cap (includes resting maker orders)
        if event_ticker:
            evt_cap = MENTION_MAX_EVENT_DOLLARS
            if self._is_new_series(event_ticker):
                evt_cap = min(evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
            event_exp = self._total_event_exposure(event_ticker, signal_type='mention_buy_no')
            remaining_cap = evt_cap - event_exp
            if remaining_cap <= 0:
                return None
            mention_bet = min(mention_bet, remaining_cap)

        contracts = int(mention_bet / (resting_price / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * resting_price / 100, 2)

        h2e = sig.get('hours_to_event', 0)
        print(f"    OB: NO_BIDS={no_bids_raw[:5]} YES_BIDS={yes_bids_raw[:5] if isinstance(yes_bids_raw, list) else []} "
              f"best_no_bid={best_no_bid} no_ask={best_no_ask} next_best={next_best_bid}")
        print(f"    Maker: {contracts} NO @ {resting_price}c (spread={spread}c) "
              f"${bet_dollars:.2f} h2e={h2e:.1f}h [{category}]")

        if DRY_RUN:
            order_id = f'DRY-MKR-{uuid.uuid4().hex[:8]}'
            self._resting_premarket_orders[order_id] = {
                'ticker': ticker, 'price_cents': resting_price,
                'contracts': contracts, 'bet_dollars': bet_dollars,
                'placed_ts': time.time(), 'category': category,
                'signal': sig,
            }
            self.trade_logger.record({
                'type': 'maker_placed', 'strategy': 'mention_buy_no',
                'ticker': ticker, 'side': 'no', 'action': 'buy',
                'contracts': contracts, 'price_cents': resting_price,
                'bet_dollars': bet_dollars, 'dry_run': True,
                'expiration_ts': event_start_ts,
            })
            self.mention_detector.signal_history[ticker] = time.time()
            self.mention_detector._save()
            print(f"    DRY RUN MAKER: resting {contracts} NO @ {resting_price}c (${bet_dollars:.2f})")
            return None

        order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=contracts, price_cents=resting_price,
            expiration_ts=event_start_ts, maker=True,
        )
        if not order:
            print(f"    Maker order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        self._resting_premarket_orders[order_id] = {
            'ticker': ticker, 'price_cents': resting_price,
            'contracts': contracts, 'bet_dollars': bet_dollars,
            'placed_ts': time.time(), 'category': category,
            'signal': sig,
        }
        self.mention_detector.signal_history[ticker] = time.time()
        self.mention_detector._save()
        self._save_resting_orders()

        print(f"    MAKER RESTING: {order_id} {contracts} NO @ {resting_price}c (${bet_dollars:.2f}) expires at event start")
        log_event('premarket_maker_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=resting_price, bet_dollars=bet_dollars,
                  category=category, hours_to_event=h2e,
                  no_bid=best_no_bid, no_ask=best_no_ask, spread=spread,
                  expiration_ts=event_start_ts)
        self._queue_tg(f"RESTING ORDER [{category}]", ticker,
                       price_cents=resting_price, contracts=contracts, bet_dollars=bet_dollars,
                       title=sig.get('title', '')[:60])

        return None

    async def _scan_political_pct(self, mention_markets, now):
        """Political pct_words_said strategy: buy NO on remaining markets
        when >= 50% of words have been said in a live event.

        Taker-only (event is live). $5/bet, NO 5-70c, per-market cap $5.
        Backtest: 62.2% WR, +18.4% ROI on test set.
        """
        pol_count = self.positions.count('political_pct_no')
        if pol_count >= POLITICAL_PCT_MAX_POSITIONS:
            print(f"    POLITICAL PCT CAP: {pol_count}/{POLITICAL_PCT_MAX_POSITIONS}, skipping")
            return

        pol_signals = self.political_pct_detector.detect(mention_markets, self.client, now)
        if not pol_signals:
            return

        for sig in pol_signals:
            ticker = sig['ticker']

            # Global scan-cycle dedup
            if ticker in self._entered_this_cycle:
                continue

            # Skip if resting maker order on this ticker
            if any(info['ticker'] == ticker for info in self._resting_premarket_orders.values()):
                continue

            # Position cap
            if pol_count >= POLITICAL_PCT_MAX_POSITIONS:
                break

            # Skip if already holding this ticker (any strategy)
            if self.positions.has_open_ticker(ticker):
                continue

            # Per-event cap (includes resting orders; lower for new series)
            event = sig.get('event_ticker', '')
            if event:
                pol_evt_cap = POLITICAL_PCT_MAX_EVENT_DOLLARS
                if self._is_new_series(event):
                    pol_evt_cap = min(pol_evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
                event_exp = self._total_event_exposure(event, signal_type='political_pct_no')
                if event_exp >= pol_evt_cap:
                    continue

            no_c = sig['no_price_cents']
            pct = sig.get('pct_words_said', 0)
            evt_title = sig.get('event_title', '')[:40]
            yes_n = sig.get('event_yes_count', 0)
            total_n = sig.get('event_total_markets', 0)
            print(f"  POL-PCT: BUY NO @ {no_c}c '{sig['title'][:50]}' "
                  f"(pct={pct:.0%}, {yes_n}/{total_n} said, event='{evt_title}')")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_political_pct_entry(sig)

            if order_info:
                await self.notifier.send_mention_signal(sig, order_info,
                    trade_label=f"POLITICAL PCT ({pct:.0%} said)")
                self.positions.add(sig, order_info)
                self._entered_this_cycle.add(ticker)
                pol_count += 1
                # Set cooldown on the political detector
                self.political_pct_detector._cooldown[ticker] = now

    def _execute_political_pct_entry(self, sig):
        """Execute a political pct BUY NO entry. Taker at the ask.
        $5/bet, NO 5-70c, per-market cap $5."""
        ticker = sig['ticker']
        no_price_cents = sig['no_price_cents']

        # Global per-market hard ceiling
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        yes_bids_raw = orderbook.get('yes', [])
        if not isinstance(yes_bids_raw, list):
            yes_bids_raw = []
        if not yes_bids_raw:
            print(f"    No YES bids for {ticker}, skipping")
            return None

        best_yes_bid = max(b[0] for b in yes_bids_raw)
        best_no_ask = 100 - best_yes_bid

        if best_no_ask < POLITICAL_PCT_MIN_NO_CENTS or best_no_ask > POLITICAL_PCT_MAX_NO_CENTS:
            print(f"    NO ask {best_no_ask}c outside [{POLITICAL_PCT_MIN_NO_CENTS}-{POLITICAL_PCT_MAX_NO_CENTS}c], skipping")
            return None

        # Slippage guard: 4c
        max_slip = 4
        if best_no_ask > no_price_cents + max_slip:
            print(f"    Slippage: ask {best_no_ask}c > signal {no_price_cents}c + {max_slip}c, skipping")
            return None

        # Bet sizing: $5, capped by per-market and per-event
        bet = POLITICAL_PCT_BET_DOLLARS

        # New series cap: $1/bet for series with < 3 resolved events
        event = sig.get('event_ticker', '')
        if self._is_new_series(event):
            bet = min(bet, PREMARKET_NEW_SERIES_BET)

        # Per-market cap check (includes existing positions)
        ticker_exp = 0
        for p in self.positions.positions:
            if p.get('ticker') == ticker and p.get('status') == 'open':
                ticker_exp += p.get('bet_dollars', 0)
        remaining_market = POLITICAL_PCT_MAX_MARKET_DOLLARS - ticker_exp
        if remaining_market <= 0:
            print(f"    Market cap reached (${ticker_exp:.2f}/${POLITICAL_PCT_MAX_MARKET_DOLLARS}), skipping")
            return None
        bet = min(bet, remaining_market)

        # Per-event cap (includes resting orders; lower for new series)
        if event:
            pol_evt_cap = POLITICAL_PCT_MAX_EVENT_DOLLARS
            if self._is_new_series(event):
                pol_evt_cap = min(pol_evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
            event_exp = self._total_event_exposure(event, signal_type='political_pct_no')
            remaining_event = pol_evt_cap - event_exp
            if remaining_event <= 0:
                return None
            bet = min(bet, remaining_event)

        # Depth within slippage window
        max_slip_price = no_price_cents + max_slip
        min_yes_bid = 100 - max_slip_price
        depth_contracts = sum(qty for bid_p, qty in yes_bids_raw if bid_p >= min_yes_bid)
        depth_dollars = round(depth_contracts * best_no_ask / 100, 2) if depth_contracts > 0 else 0
        if depth_dollars > 0 and bet > depth_dollars:
            bet = depth_dollars

        contracts = int(bet / (best_no_ask / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * best_no_ask / 100, 2)

        order_price = max_slip_price
        print(f"    Political taker: {contracts} NO @ {best_no_ask}c (limit {order_price}c) = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-POL-{uuid.uuid4().hex[:8]}',
                'fill_price': best_no_ask / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'political_pct_no',
                'ticker': ticker, 'side': 'no', 'action': 'buy',
                'contracts': contracts, 'price_cents': best_no_ask,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    DRY RUN: {contracts} NO @ {best_no_ask}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=contracts, price_cents=order_price,
        )
        if not order:
            print(f"    Political order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    Political taker order placed: {order_id}")
        log_event('political_pct_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=order_price, bet_dollars=bet_dollars,
                  pct_words_said=sig.get('pct_words_said'))

        # Check fill
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', best_no_ask)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'political_pct_no',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'no', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': best_no_ask,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('political_pct_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars)
                self._queue_tg("POL-PCT FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    Political taker not filled for {ticker}, canceled")
        return None

    async def _scan_stable_price(self, mention_markets, now):
        """Stable-price strategy: buy NO when YES price hasn't moved 5 min
        after first word in event crossed 98c.

        Taker-only (event is live). $3/bet, category-specific YES range.
        Backtest: YES 0.10-0.80 combined, Edge=+5.7c, Sharpe=0.128 on test.
        """
        sp_count = self.positions.count('stable_price_no')
        if sp_count >= STABLE_PRICE_MAX_POSITIONS:
            return

        sp_signals = self.stable_price_detector.detect(mention_markets, self.client, now)
        if not sp_signals:
            return

        for sig in sp_signals:
            ticker = sig['ticker']

            # Global scan-cycle dedup
            if ticker in self._entered_this_cycle:
                continue

            # Skip if resting maker order on this ticker
            if any(info['ticker'] == ticker for info in self._resting_premarket_orders.values()):
                continue

            # Position cap
            if sp_count >= STABLE_PRICE_MAX_POSITIONS:
                break

            # Skip if already holding this ticker (any strategy)
            if self.positions.has_open_ticker(ticker):
                continue

            # Per-event cap (includes resting orders; lower for new series)
            event = sig.get('event_ticker', '')
            if event:
                sp_evt_cap = STABLE_PRICE_MAX_EVENT_DOLLARS
                if self._is_new_series(event):
                    sp_evt_cap = min(sp_evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
                event_exp = self._total_event_exposure(event, signal_type='stable_price_no')
                if event_exp >= sp_evt_cap:
                    continue

            cat = sig.get('category', 'Other')
            trig_yes = sig.get('trigger_yes_cents', 0)
            now_yes = sig.get('current_yes_cents', 0)
            delta = sig.get('price_delta_cents', 0)
            no_c = sig['no_price_cents']
            evt_title = sig.get('event_title', '')[:40]
            print(f"  STABLE: BUY NO @ {no_c}c '{sig['title'][:50]}' "
                  f"(cat={cat}, YES {now_yes}c, Δ={delta}c, trigger_age={sig.get('trigger_age_seconds', 0)}s, "
                  f"event='{evt_title}')")

            order_info = None
            if self.client.can_trade:
                order_info = self._execute_stable_price_entry(sig)

            if order_info:
                await self.notifier.send_mention_signal(sig, order_info,
                    trade_label=f"STABLE-PRICE ({cat}, YES={now_yes}c)")
                self.positions.add(sig, order_info)
                self._entered_this_cycle.add(ticker)
                sp_count += 1
                # Set cooldown on detector
                self.stable_price_detector._cooldown[ticker] = now

    def _execute_stable_price_entry(self, sig):
        """Execute a stable-price BUY NO entry. Taker at the ask.
        $3/bet, category-specific YES range, per-market cap $3."""
        ticker = sig['ticker']
        no_price_cents = sig['no_price_cents']

        # Global per-market hard ceiling
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        yes_bids_raw = orderbook.get('yes', [])
        if not isinstance(yes_bids_raw, list):
            yes_bids_raw = []
        if not yes_bids_raw:
            print(f"    No YES bids for {ticker}, skipping")
            return None

        best_yes_bid = max(b[0] for b in yes_bids_raw)
        best_no_ask = 100 - best_yes_bid

        # Re-check NO price range at execution time (may have moved since detection)
        category = sig.get('category', 'Other')
        yes_min, yes_max = STABLE_PRICE_YES_MIN, STABLE_PRICE_YES_MAX
        current_yes = 100 - best_no_ask
        if current_yes < yes_min or current_yes > yes_max:
            print(f"    YES {current_yes}c outside [{yes_min}-{yes_max}c], skipping")
            return None

        # Slippage guard: 4c
        max_slip = 4
        if best_no_ask > no_price_cents + max_slip:
            print(f"    Slippage: ask {best_no_ask}c > signal {no_price_cents}c + {max_slip}c, skipping")
            return None

        # Bet sizing: $3, capped by per-market and per-event
        bet = STABLE_PRICE_BET_DOLLARS

        # New series cap
        event = sig.get('event_ticker', '')
        if self._is_new_series(event):
            bet = min(bet, PREMARKET_NEW_SERIES_BET)

        # Per-market cap
        ticker_exp = 0
        for p in self.positions.positions:
            if p.get('ticker') == ticker and p.get('status') == 'open':
                ticker_exp += p.get('bet_dollars', 0)
        remaining_market = STABLE_PRICE_MAX_MARKET_DOLLARS - ticker_exp
        if remaining_market <= 0:
            print(f"    Market cap reached (${ticker_exp:.2f}/${STABLE_PRICE_MAX_MARKET_DOLLARS}), skipping")
            return None
        bet = min(bet, remaining_market)

        # Per-event cap
        if event:
            sp_evt_cap = STABLE_PRICE_MAX_EVENT_DOLLARS
            if self._is_new_series(event):
                sp_evt_cap = min(sp_evt_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
            event_exp = self._total_event_exposure(event, signal_type='stable_price_no')
            remaining_event = sp_evt_cap - event_exp
            if remaining_event <= 0:
                return None
            bet = min(bet, remaining_event)

        # Depth within slippage window
        max_slip_price = no_price_cents + max_slip
        min_yes_bid = 100 - max_slip_price
        depth_contracts = sum(qty for bid_p, qty in yes_bids_raw if bid_p >= min_yes_bid)
        depth_dollars = round(depth_contracts * best_no_ask / 100, 2) if depth_contracts > 0 else 0
        if depth_dollars > 0 and bet > depth_dollars:
            bet = depth_dollars

        contracts = int(bet / (best_no_ask / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * best_no_ask / 100, 2)

        order_price = max_slip_price
        print(f"    Stable taker: {contracts} NO @ {best_no_ask}c (limit {order_price}c) = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-STABLE-{uuid.uuid4().hex[:8]}',
                'fill_price': best_no_ask / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'stable_price_no',
                'ticker': ticker, 'side': 'no', 'action': 'buy',
                'contracts': contracts, 'price_cents': best_no_ask,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    DRY RUN: {contracts} NO @ {best_no_ask}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=contracts, price_cents=order_price,
        )
        if not order:
            print(f"    Stable order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    Stable taker order placed: {order_id}")
        log_event('stable_price_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=order_price, bet_dollars=bet_dollars,
                  category=sig.get('category'), trigger_yes=sig.get('trigger_yes_cents'),
                  current_yes=sig.get('current_yes_cents'))

        # Check fill
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', best_no_ask)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'stable_price_no',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'no', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': best_no_ask,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('stable_price_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars,
                          category=sig.get('category'))
                self._queue_tg("STABLE-PRICE FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info

        # Not filled — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass
        print(f"    Stable taker not filled for {ticker}, canceled")
        return None

    def _execute_mention_entry(self, sig):
        """Execute a mention BUY NO entry. Taker order at the ask for
        immediate fill — avoids adverse selection from passive bids."""
        ticker = sig['ticker']
        no_price_cents = sig['no_price_cents']

        # Global per-market hard ceiling — no ticker can exceed this across all strategies
        global_exp = self._global_ticker_exposure(ticker)
        if global_exp >= GLOBAL_MAX_MARKET_DOLLARS:
            print(f"    GLOBAL market cap reached (${global_exp:.2f}/${GLOBAL_MAX_MARKET_DOLLARS}), skipping {ticker}")
            return None

        # Word blacklist — skip words that are almost always said (<20% NO win rate)
        ticker_upper = ticker.upper()
        word_suffix = ticker.split('-')[-1].upper()
        if ('NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper) and word_suffix in NBA_BLACKLIST:
            print(f"    Blacklisted NBA word: {word_suffix} ({ticker}), skipping")
            return None
        if ('NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper) and word_suffix in NCAAB_BLACKLIST:
            print(f"    Blacklisted NCAAB word: {word_suffix} ({ticker}), skipping")
            return None
        if word_suffix in EARNINGS_WORD_BLACKLIST:
            print(f"    Blacklisted always-said word: {word_suffix} ({ticker}), skipping")
            return None

        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        # Get best NO ask from orderbook.
        # Kalshi orderbook format: {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
        # "yes" list = YES bids; "no" list = NO bids.
        # To BUY NO as taker: NO ask = 100 - YES bid.
        yes_bids_raw = orderbook.get('yes', [])
        if not isinstance(yes_bids_raw, list):
            yes_bids_raw = []
        best_no_ask = None
        if yes_bids_raw:
            best_yes_bid = max(b[0] for b in yes_bids_raw)
            best_no_ask = 100 - best_yes_bid

        # Empty book (no YES bids → no NO ask): normally skip. But in MAKER_ONLY
        # mode a pre-event signal can still rest a NO bid at the category floor
        # and climb via the penny-above refresh as the book fills — this is how
        # we get queue position before retail arrives (the maker thesis).
        _h2e_chk = sig.get('hours_to_event')
        _allow_empty_book = (
            MAKER_ONLY
            and _h2e_chk is not None
            and _h2e_chk > PREMARKET_CANCEL_HOURS
            and 'MENTION' in ticker.upper()
        )
        if (best_no_ask is None or best_no_ask < 1) and not _allow_empty_book:
            print(f"    No NO ask for {ticker} (no YES bids in book), skipping")
            return None

        # Per-category price range (taker)
        ticker_upper = ticker.upper()
        is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
        is_ncaab = 'NCAABMENTION' in ticker_upper  # basketball only (not football)
        is_nba = 'NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper

        # Pre-event maker eligibility: all mention categories can fall back to resting limit orders
        h2e = sig.get('hours_to_event')
        is_trump = 'TRUMPMENTION' in ticker_upper
        is_mamdani = 'MAMDANIMENTION' in ticker_upper
        is_newsom = 'NEWSOMMENTION' in ticker_upper
        pre_event = h2e is not None and h2e > PREMARKET_CANCEL_HOURS
        # Only rest maker on vetted series. Previously this fell through to a
        # blanket "all other mention markets too" catch-all, which made the bot
        # short obvious words on novelty events. Now a market must either match a
        # hardcoded category or have its series in the vetted allowlist.
        maker_series = ticker_upper.split('-')[0]
        # Pure allowlist gate: only the pruned MENTION_MAKER_SERIES may rest a
        # maker bid. The is_trump/is_mamdani/is_newsom bypasses were removed —
        # Mamdani/Newsom are net losers (kill list) and the proven Trump/NBA/NCAAB
        # winners are already in the allowlist.
        series_vetted = maker_series in MENTION_MAKER_SERIES
        can_rest_maker = pre_event and series_vetted
        if pre_event and not can_rest_maker and 'MENTION' in ticker_upper:
            print(f"    Skip maker: {maker_series} not in vetted maker series")
        if can_rest_maker:
            if is_ncaa:
                maker_cat = 'NCAA'
            elif is_nba:
                maker_cat = 'NBA'
            elif is_trump:
                maker_cat = 'Trump'
            elif is_mamdani:
                maker_cat = 'Mamdani'
            elif is_newsom:
                maker_cat = 'Newsom'
            else:
                # Derive category from ticker
                prefix = ticker_upper.split('MENTION')[0].replace('KX', '')
                maker_cat = prefix if prefix else 'Other'

        # MAKER_ONLY: never take. Pre-event signals rest a NO limit; anything that
        # can't rest (live / inside the cancel window) is skipped entirely.
        if MAKER_ONLY:
            if can_rest_maker:
                print(f"    MAKER_ONLY: resting maker [{maker_cat}] for {ticker} (h2e={h2e})")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            print(f"    MAKER_ONLY: cannot rest {ticker} (h2e={h2e}), skipping (no taker)")
            return None

        # NBA halftime strategy: taker only from halftime (~1.3h into game) onward.
        # Pre-game: maker only. Pre-halftime live: skip taker entirely.
        if is_nba and NBA_HALFTIME_ENABLED:
            hours_into_game = -h2e if h2e is not None else 0
            if h2e is not None and h2e > 0:
                # Pre-game: no maker or taker for NBA halftime strat
                # (the edge is halftime+, not pre-game)
                print(f"    NBA halftime strat: pre-game ({h2e:.1f}h to tipoff), skipping")
                return None
            if hours_into_game < NBA_HALFTIME_MIN_HOURS_LIVE:
                print(f"    NBA halftime strat: too early ({hours_into_game:.1f}h into game, need {NBA_HALFTIME_MIN_HOURS_LIVE}h), skipping")
                return None
            # Word allowlist check in executor (belt-and-suspenders with detector)
            if word_suffix not in NBA_HALFTIME_WORD_ALLOWLIST:
                print(f"    NBA halftime strat: word {word_suffix} not in allowlist, skipping")
                return None
        elif is_nba:
            # Fallback: original NBA pre-game maker logic
            nba_pre_game = h2e is not None and h2e > 0
            if nba_pre_game:
                if can_rest_maker:
                    print(f"    NBA pre-game ({h2e:.1f}h to tipoff), maker only [{maker_cat}]")
                    return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
                else:
                    print(f"    NBA pre-game ({h2e:.1f}h to tipoff) but too close for maker, skipping")
                    return None

        # NCAAB halftime strategy: taker only from 0.75h into game onward.
        # Basketball only (NCAABMENTION), not football (NCAAMENTION).
        if is_ncaab and NCAAB_HALFTIME_ENABLED:
            hours_into_game = -h2e if h2e is not None else 0
            if h2e is not None and h2e > 0:
                print(f"    NCAAB halftime strat: pre-game ({h2e:.1f}h to tipoff), skipping")
                return None
            if hours_into_game < NCAAB_HALFTIME_MIN_HOURS_LIVE:
                print(f"    NCAAB halftime strat: too early ({hours_into_game:.1f}h into game, need {NCAAB_HALFTIME_MIN_HOURS_LIVE}h), skipping")
                return None
            if hours_into_game > NCAAB_HALFTIME_MAX_HOURS_LIVE:
                print(f"    NCAAB halftime strat: too late ({hours_into_game:.1f}h, game likely over), skipping")
                return None
            if word_suffix not in NCAAB_HALFTIME_WORD_ALLOWLIST:
                print(f"    NCAAB halftime strat: word {word_suffix} not in allowlist, skipping")
                return None
            # Override price range for NCAAB halftime
            if best_no_ask < NCAAB_HALFTIME_MIN_NO_CENTS or best_no_ask > NCAAB_HALFTIME_MAX_NO_CENTS:
                print(f"    NCAAB halftime strat: NO ask {best_no_ask}c outside [{NCAAB_HALFTIME_MIN_NO_CENTS}-{NCAAB_HALFTIME_MAX_NO_CENTS}c], skipping")
                return None

        # --- Taker adverse selection gating ---
        # Pre-event taker is -39% ROI from actual fills. Route to maker unless live.
        event_velocity = sig.get('event_velocity', 0)
        is_live_h2e = h2e is not None and h2e <= 0
        is_volume_surging = event_velocity >= TAKER_MIN_EVENT_VELOCITY

        is_earnings_cat = 'EARNINGSMENTION' in ticker_upper

        # Trump: taker only when live (h2e<=0) OR volume surging (show started early)
        if is_trump and not is_live_h2e and not is_volume_surging:
            if can_rest_maker:
                print(f"    Trump pre-event ({h2e:.1f}h, vel={event_velocity:.1f}), maker only [{maker_cat}]")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            else:
                print(f"    Trump pre-event, no taker (vel={event_velocity:.1f} < {TAKER_MIN_EVENT_VELOCITY}), skipping")
                return None

        # Mamdani: maker only for all pre-event (even volume-gated taker is marginal)
        if is_mamdani and not is_live_h2e:
            if can_rest_maker:
                print(f"    Mamdani pre-event ({h2e:.1f}h), maker only [{maker_cat}]")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            else:
                print(f"    Mamdani pre-event, no taker allowed, skipping")
                return None

        # Earnings: taker only when live (h2e<=0) OR volume surging
        if is_earnings_cat and not is_live_h2e and not is_volume_surging:
            if can_rest_maker:
                print(f"    Earnings pre-event ({h2e:.1f}h, vel={event_velocity:.1f}), maker only [{maker_cat}]")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            else:
                print(f"    Earnings pre-event, no taker (vel={event_velocity:.1f} < {TAKER_MIN_EVENT_VELOCITY}), skipping")
                return None

        # Log when taker is allowed due to volume surge
        if is_volume_surging and not is_live_h2e:
            print(f"    Volume surge detected (vel={event_velocity:.1f}), allowing taker pre-milestone")

        min_no_c, max_no_c = get_no_range(ticker)

        taker_price = best_no_ask
        if taker_price < min_no_c or taker_price > max_no_c:
            if can_rest_maker:
                print(f"    NO ask {taker_price}c outside taker range [{min_no_c}-{max_no_c}c], trying maker [{maker_cat}]")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            print(f"    NO ask {taker_price}c outside range [{min_no_c}-{max_no_c}c], skipping")
            log_event('mention_skip_range', ticker=ticker, no_ask_cents=taker_price,
                      min_no_c=min_no_c, max_no_c=max_no_c)
            return None

        # Category detection for sizing and slippage
        is_earnings_taker = 'EARNINGSMENTION' in ticker_upper
        taker_cat_check = get_mention_category(ticker)
        is_winner_cat = taker_cat_check in CATEGORY_BET_OVERRIDE
        is_other = not is_winner_cat and not any(k in ticker_upper for k in (
            'TRUMPMENTION', 'MAMDANIMENTION', 'NEWSOMMENTION',
            'NBAMENTION', 'NBAFINALS', 'NCAAMENTION', 'NCAABMENTION',
            'VANCEMENTION', 'EARNINGSMENTION',
        ))

        # Slippage guard: named categories 4c, other 2c
        max_slip = 2 if is_other else 4
        max_slip_price = no_price_cents + max_slip
        if taker_price > max_slip_price:
            if can_rest_maker:
                print(f"    Slippage: ask {taker_price}c > signal {no_price_cents}c + {max_slip}c, trying maker [{maker_cat}]")
                return self._execute_premarket_maker(sig, orderbook, yes_bids_raw, best_no_ask, category=maker_cat)
            print(f"    Slippage: ask {taker_price}c > signal {no_price_cents}c + {max_slip}c, skipping")
            log_event('mention_skip_slippage', ticker=ticker,
                      no_ask_cents=taker_price, signal_cents=no_price_cents)
            return None

        # Depth within slippage window: sum YES bid contracts where (100 - bid) <= max_slip_price
        # i.e. YES bids >= (100 - max_slip_price)
        min_yes_bid = 100 - max_slip_price
        depth_contracts = 0
        for bid_price, bid_qty in yes_bids_raw:
            if bid_price >= min_yes_bid:
                depth_contracts += bid_qty
        depth_dollars = round(depth_contracts * taker_price / 100, 2) if depth_contracts > 0 else 0

        # Category-based bet sizing
        is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
        if is_earnings_taker:
            mention_bet = EARNINGS_BET_DOLLARS
        elif is_nba and NBA_HALFTIME_ENABLED:
            mention_bet = NBA_HALFTIME_BET_DOLLARS
        elif taker_cat_check in CATEGORY_BET_OVERRIDE:
            mention_bet = CATEGORY_BET_OVERRIDE[taker_cat_check]
        elif is_ncaa:
            mention_bet = MENTION_BET_NCAA
        elif is_other:
            mention_bet = MENTION_BET_OTHER
        else:
            mention_bet = MENTION_BET_DOLLARS
        # New/unknown series: cap at $2 until we have enough history
        # Skip this cap for curated series in MENTION_SCAN_SERIES
        event_ticker_taker = sig.get('event_ticker', '')
        series = re.sub(r'-\d{2}[A-Z]{3}\d{0,2}.*$', '', event_ticker_taker)
        if series not in MENTION_SCAN_SERIES:
            resolved = getattr(self.client, '_series_resolved_counts', {}).get(series, 0)
            if resolved < PREMARKET_NEW_SERIES_MIN:
                mention_bet = min(mention_bet, PREMARKET_NEW_SERIES_BET)
                print(f"    New series {series} ({resolved} resolved < {PREMARKET_NEW_SERIES_MIN}), capping at ${PREMARKET_NEW_SERIES_BET}")

        # Per-market hard cap (strategy-specific AND global)
        market_cap = NBA_HALFTIME_MAX_MARKET_DOLLARS if (is_nba and NBA_HALFTIME_ENABLED) else MENTION_MAX_MARKET_DOLLARS
        market_cap = min(market_cap, GLOBAL_MAX_MARKET_DOLLARS)
        mention_bet = min(mention_bet, market_cap)

        # Per-market exposure check (includes resting maker orders)
        ticker_exp = self._global_ticker_exposure(ticker)
        remaining_market_cap = market_cap - ticker_exp
        if remaining_market_cap <= 0:
            print(f"    Market cap reached (${ticker_exp:.2f}/${market_cap} incl resting), skipping")
            return None
        mention_bet = min(mention_bet, remaining_market_cap)

        # Per-event exposure cap (includes resting maker orders)
        event_cap = NBA_HALFTIME_MAX_EVENT_DOLLARS if (is_nba and NBA_HALFTIME_ENABLED) else MENTION_MAX_EVENT_DOLLARS
        event = sig.get('event_ticker', '')
        if event:
            if self._is_new_series(event):
                event_cap = min(event_cap, PREMARKET_NEW_SERIES_EVENT_CAP)
            evt_sig_type = 'nba_halftime_no' if (is_nba and NBA_HALFTIME_ENABLED) else 'mention_buy_no'
            event_exp = self._total_event_exposure(event, signal_type=evt_sig_type)
            remaining_cap = event_cap - event_exp
            if remaining_cap <= 0:
                print(f"    Event cap reached (${event_exp:.0f}/${event_cap} incl resting), skipping")
                return None
            mention_bet = min(mention_bet, remaining_cap)

        # Cap bet to depth within 2c of signal
        if depth_dollars > 0 and mention_bet > depth_dollars:
            print(f"    Depth cap: ${depth_dollars:.2f} within {max_slip_price}c (wanted ${mention_bet:.2f})")
            mention_bet = depth_dollars

        contracts = int(mention_bet / (taker_price / 100))
        if contracts < 1:
            contracts = 1
        bet_dollars = round(contracts * taker_price / 100, 2)

        # Set order price at max_slip_price so it fills at best available within 2c
        order_price = max_slip_price

        print(f"    Mention taker: {contracts} NO @ {taker_price}c (limit {order_price}c, depth ${depth_dollars:.2f}) = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-MEN-{uuid.uuid4().hex[:8]}',
                'fill_price': taker_price / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry', 'strategy': 'mention_buy_no',
                'ticker': ticker, 'side': 'no', 'action': 'buy',
                'contracts': contracts, 'price_cents': taker_price,
                'bet_dollars': bet_dollars, 'dry_run': True,
            })
            print(f"    DRY RUN: {contracts} NO @ {taker_price}c (${bet_dollars:.2f})")
            return order_info

        order = self.client.create_order(
            ticker=ticker, side='no', action='buy',
            count=contracts, price_cents=order_price,
        )
        if not order:
            print(f"    Mention order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')

        print(f"    Taker order placed: {order_id} ({contracts} NO @ limit {order_price}c, ${bet_dollars:.2f})")
        log_event('mention_taker_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=order_price, bet_dollars=bet_dollars,
                  signal_no_cents=no_price_cents,
                  hours_to_event=sig.get('hours_to_event'),
                  event_volume_24h=sig.get('event_volume_24h', 0),
                  event_velocity=sig.get('event_velocity', 0))

        # Set cooldown on placement as safety net — cleared below if confirmed no-fill.
        # Prevents re-entry when get_order() fails (timeout/API error) but order filled.
        self.mention_detector.signal_history[ticker] = time.time()
        self.mention_detector._save()

        # Taker should fill instantly — check after brief delay
        time.sleep(2)
        status = self.client.get_order(order_id)
        if status:
            filled = status.get('quantity_filled', 0)
            if filled > 0:
                remaining = status.get('remaining_count', 0)
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                avg_fill = status.get('average_fill_price', taker_price)
                actual_dollars = round(filled * avg_fill / 100, 2)
                info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry', 'strategy': 'mention_buy_no',
                    'ticker': ticker, 'order_id': order_id,
                    'side': 'no', 'action': 'buy',
                    'contracts_filled': filled, 'price_cents': taker_price,
                    'avg_fill_price': avg_fill, 'bet_dollars': actual_dollars,
                })
                print(f"    FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('mention_filled', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars)
                self._queue_tg("TAKER FILLED", ticker,
                               price_cents=avg_fill, contracts=filled, bet_dollars=actual_dollars,
                               title=sig.get('title', '')[:60])
                return info
            else:
                # Confirmed 0 fills — clear safety cooldown so we can retry
                self.mention_detector.signal_history.pop(ticker, None)
                self.mention_detector._save()
        else:
            # get_order() failed — keep safety cooldown (order may have filled)
            print(f"    WARNING: get_order failed for {order_id}, keeping safety cooldown on {ticker}")

        # Not filled even as taker — cancel
        try:
            self.client.cancel_order(order_id)
        except Exception:
            pass

        # Pre-event: fall back to maker resting order instead of giving up
        h2e = sig.get('hours_to_event')
        pre_event_fb = h2e is not None and h2e > PREMARKET_CANCEL_HOURS
        ticker_upper_fb = ticker.upper()
        has_mention = 'MENTION' in ticker_upper_fb or 'FINALS' in ticker_upper_fb
        if pre_event_fb and has_mention:
            # Re-derive maker category
            if 'NCAAMENTION' in ticker_upper_fb or 'NCAABMENTION' in ticker_upper_fb:
                fb_cat = 'NCAA'
            elif 'NBAMENTION' in ticker_upper_fb or 'NBAFINALS' in ticker_upper_fb:
                fb_cat = 'NBA'
            elif 'TRUMPMENTION' in ticker_upper_fb:
                fb_cat = 'Trump'
            elif 'EARNINGSMENTION' in ticker_upper_fb:
                fb_cat = 'Earnings'
            else:
                prefix = ticker_upper_fb.split('MENTION')[0].replace('KX', '')
                fb_cat = prefix if prefix else 'Other'
            print(f"    Taker not filled for {ticker}, falling back to maker [{fb_cat}]")
            log_event('mention_taker_unfilled', ticker=ticker, order_id=order_id, fallback='maker')
            # Re-fetch orderbook for maker
            ob = self.client.get_orderbook(ticker)
            if ob:
                yb = ob.get('yes', [])
                if isinstance(yb, list) and yb:
                    fb_no_ask = 100 - max(b[0] for b in yb)
                    return self._execute_premarket_maker(sig, ob, yb, fb_no_ask, category=fb_cat)
            return None

        print(f"    Taker not filled for {ticker}, canceled")
        log_event('mention_taker_unfilled', ticker=ticker, order_id=order_id)
        return None



if __name__ == "__main__":
    import sys
    print("Bot starting...", flush=True)
    try:
        scanner = KalshiReversionScanner()
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"FATAL: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(1)
