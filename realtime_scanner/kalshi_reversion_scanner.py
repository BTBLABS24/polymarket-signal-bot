#!/usr/bin/env python3
"""
Kalshi Retail Mean Reversion AUTO-TRADING BOT

Automated version of the scanner that:
- Detects retail surge signals (same logic as before)
- Places real orders on Kalshi via authenticated API
- Sizes bets dynamically based on orderbook depth
- Executes 24h exits automatically
- Has DRY_RUN toggle and safety circuit breakers

Uses RSA-PSS signing for Kalshi API authentication.
"""

import asyncio
import base64
import csv
import json
import os
import time
import uuid
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
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

# Strategy params (adapted from Polymarket backtest)
MIN_SMALL_TRADES = 12       # Min number of small trades on one side
MAX_SMALL_TRADES = 40       # Max (beyond this, it's real news)
SMALL_TRADE_LIMIT = 100     # Contracts — "retail" is < 100 contracts
MIN_SIDE_RATIO = 0.65       # 65%+ on one side
MIN_PRICE_MOVE = 0.15       # 15c move
ENTRY_PRICE_MIN = 0.30
ENTRY_PRICE_MAX = 0.60
HOLD_HOURS = 24
COOLDOWN_HOURS = 4

# Scanner settings
SCAN_INTERVAL_SECONDS = 300  # 5 min
TRADES_PER_PAGE = 1000
WINDOW_MINUTES = 60          # 1-hour signal windows

# Trading config
DRY_RUN = False
MAX_BET_DOLLARS = 3           # Max per signal
MIN_BET_DOLLARS = 1           # Skip if depth too thin
DEPTH_FRACTION = 0.50         # Use 50% of 3-level depth
MAX_OPEN_POSITIONS = 20       # Cap concurrent reversion positions
MAX_IMPL_POSITIONS = 5        # Cap concurrent implied prob positions
ORDER_WAIT_SECONDS = 5        # Wait for fill after placing order
MAX_ORDER_RETRIES = 2         # Retry at next price level
MAX_SLIPPAGE_PCT = 15.0       # Skip if NO price > 15% worse than signal

# Categories to EXCLUDE (prefix-based fast filter + event category fallback)
EXCLUDED_PREFIXES = [
    # Sports (comprehensive — 588 leaked in 60d backtest at +1.8%, not worth it)
    'KXNCAAMB', 'KXNCAAFB', 'KXNCAAWB', 'KXNCAAB',
    'KXNFL', 'KXNBA', 'KXNHL', 'KXMLB',
    'KXSOCCER', 'KXUFC', 'KXTENNIS', 'KXCRICKET', 'KXHIGHLAX',
    'KXMVESPORTS', 'KXVALORANT', 'KXCS2',
    'KXATPMATCH', 'KXWTAMATCH', 'KXDPWORLDTOUR', 'KXPGA',
    'KXLALIGA', 'KXUCL', 'KXARGLNB', 'KXSB',
    'KXNEXTTEAMNFL', 'KXNBAMVP', 'KXNBAWINS', 'KXNBATOTAL',
    # Sports that leaked in 60d backtest
    'KXATPCHALLENGER', 'KXDOTA2', 'KXLOLMAP', 'KXLOLGAME',
    'KXSERIEASPREAD', 'KXSERIEATOTAL', 'KXR6GAME',
    'KXSCOTTISHPREM', 'KXAHLGAME', 'KXKHLGAME',
    'KXWOCURL', 'KXEFLCHAMPIONSHIP', 'KXLIGUE1',
    'KXSWISSLEAGUE', 'KXWOFREESKI', 'KXWOSBOARD',
    'KXEPLBTTS', 'KXNASCAR', 'KXAAAGASW',
    'KXNEXTTEAMNBA', 'KXLPGA', 'KXWNBA', 'KXMLS',
    'KXNHLPROP', 'KXAFCCL', 'KXAFCCLGAME',
    # Sports that leaked in live trading
    'KXWTACHALLENGER', 'KXWOMHOCKEY', 'KXALEAGUE',
    'KXWOSSKATE', 'KXWOSHORT', 'KXWOSPEED',
    'KXWOFSKATE', 'KXWOBIATHLON', 'KXWOBOB', 'KXWOLUGE',
    'KXWOXC', 'KXWOCOMBI', 'KXWOJUMP', 'KXWOALPINE',
    # Crypto
    'KXBTC', 'KXETH', 'KXSOL', 'KXCRYPTO', 'KXDOGE', 'KXXRP',
    # Financials (22% WR, -18.6% avg ROI in backtest)
    'KXINX', 'KXNASDAQ', 'KXSP5', 'KXWTI', 'KXINXU',
]
EXCLUDED_CATEGORIES = {'Sports', 'Crypto', 'Financials'}

# 60d backtest: SELL +24.4% avg ROI vs BUY -6.8% — only fade buying surges
SELL_ONLY = True

# --- Implied Probability Violation params ---
IMPL_DEVIATION_THRESHOLD = 0.05   # 5c min |prob_sum - 1.0| to trigger
IMPL_MAX_DEVIATION = 0.30         # 30c max — beyond this it's independent outcomes, not mispricing
IMPL_HOLD_HOURS = 12              # 12h hold (violations correct faster)
IMPL_MIN_OUTCOMES = 3             # 2-outcome = binary YES/NO, always complementary
IMPL_MAX_OUTCOMES = 20
IMPL_MIN_PRICE = 0.03
IMPL_MAX_PRICE = 0.97
IMPL_COOLDOWN_HOURS = 6           # Per-event cooldown
IMPL_MAX_BET_DOLLARS = 1          # $1 for testing

# Mention/independent-outcome markets — outcomes are NOT mutually exclusive
# (multiple can resolve YES), so prob sum != 1.0 is expected, not mispricing
MENTION_KEYWORDS = [
    'what will', 'say during', 'say at', 'say in', 'say on',
    'mention', 'announce', 'announcer', 'commentator',
    'play by play', 'color commentary', 'broadcast',
    'press conference', 'speech', 'address', 'interview',
    'debate', 'ceremony', 'halftime show', 'opening remarks',
    'state of the', 'remarks at', 'remarks during',
]

# Combo/parlay event prefixes — multi-leg bets with terrible liquidity
# Deviation is just vig structure, not real mispricing
IMPL_EXCLUDED_PREFIXES = [
    'KXMVESPORTS', 'KXMULTIGAME', 'KXPARLAY', 'KXCOMBO',
    'KXMVESPORTSMULTIGAME',
]

# --- Mention BUY NO Strategy ---
# Backtest: YES is systematically overpriced on mention markets.
# NO 5-65c, ex NBA/Earnings, NO time filter → +73% ROI (train +62%, test +89%)
# 11,104 trades, 232 active days. Only 50 negative days out of 232.
# Kalshi uses can_close_early with far-future deadline, so close_time
# is NOT the event time. We filter by price range only.
MENTION_BET_DOLLARS = 3           # $3 per signal
MENTION_MAX_NO_PRICE = 0.30       # Only buy NO <= 30c (YES >= 70c) — cheap NO sweet spot
MENTION_MIN_NO_PRICE = 0.05       # Skip extremely cheap NO
MENTION_HOLD_UNTIL_SETTLE = True  # Hold until settlement (no early exit)
MENTION_MAX_CLOSE_HOURS = 48      # Wide filter — close_time unreliable (events live with 24h close)
MENTION_MAX_POSITIONS = 40        # Max concurrent mention positions
MENTION_COOLDOWN_SECONDS = 300    # 5 min cooldown per ticker (24h in detector)
MENTION_SCAN_INTERVAL_SECONDS = 120  # Check for new mention markets every 2 min
MENTION_MAX_EVENT_DOLLARS = 10    # Max $ per event (spread across tickers)
MENTION_ORDER_REST_SECONDS = 600  # Leave orders resting 10 min before canceling
MENTION_MAX_RESTING_ORDERS = 10   # Max concurrent resting orders (capital lockup cap)
# Series to scan (ex NBA/Earnings per backtest — weakest ROI categories)
MENTION_SCAN_SERIES = [
    # Sports (ex NBA) — NFL +80%, NCAA +60%, Fight +34%
    'KXNFLMENTION', 'KXNCAAMENTION', 'KXNCAABMENTION',
    'KXSNFMENTION', 'KXTNFMENTION', 'KXCFBMENTION', 'KXMLBMENTION',
    'KXFIGHTMENTION', 'KXSBMENTION',
    # Politics/Gov — Trump +68%, Governor +55%, Press +72%
    'KXTRUMPMENTION', 'KXTRUMPMENTIONB',
    'KXMAMDANIMENTION', 'KXHOCHULMENTION',
    'KXSECPRESSMENTION', 'KXLEAVITTMENTION',
    'KXGOVERNORMENTION',
    # Media — Maddow +175%, Talk shows, SNL
    'KXMADDOWMENTION',
    'KXSNLMENTION', 'KXROGANMENTION', 'KXCOOPERMENTION',
    'KXCOLBERTMENTION', 'KXKIMMELMENTION',
    # Other mention series (catch new ones)
    'KXVANCEMENTION',
]

# State files
STATE_DIR = Path(__file__).parent
POSITIONS_FILE = STATE_DIR / 'kalshi_positions.json'
SIGNAL_HISTORY_FILE = STATE_DIR / 'kalshi_signal_history.json'
TRADE_LOG_FILE = STATE_DIR / 'kalshi_trade_log.json'
IMPL_SIGNAL_HISTORY_FILE = STATE_DIR / 'kalshi_impl_signal_history.json'
MENTION_SIGNAL_HISTORY_FILE = STATE_DIR / 'kalshi_mention_signal_history.json'
TRADE_HISTORY_CSV = STATE_DIR / 'kalshi_trade_history.csv'
EVENT_LOG_FILE = STATE_DIR / 'kalshi_event_log.jsonl'


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
# KALSHI API CLIENT (with RSA-PSS auth)
# =====================================================================

class KalshiClient:
    def __init__(self):
        self.market_cache = {}
        self.category_cache = {}
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

    def get_all_recent_trades(self, since_minutes=65):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        cutoff_str = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')
        all_trades = []
        cursor = None
        pages = 0
        max_pages = 15  # Cap at 15K trades — 50K was crashing the container
        while pages < max_pages:
            trades, cursor = self.get_trades(limit=TRADES_PER_PAGE, cursor=cursor)
            if not trades:
                break
            hit_cutoff = False
            for t in trades:
                ts = t.get('created_time', '')
                if ts < cutoff_str:
                    hit_cutoff = True
                    break
                all_trades.append(t)
            if hit_cutoff:
                break
            pages += 1
            if not cursor:
                break
        return all_trades

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

    def is_allowed_ticker(self, ticker):
        ticker_upper = ticker.upper()
        # Block all mention markets (price moves = real info, not retail herding)
        if 'MENTION' in ticker_upper:
            return False
        for prefix in EXCLUDED_PREFIXES:
            if ticker_upper.startswith(prefix.upper()):
                return False
        if ticker in self.category_cache:
            return self.category_cache[ticker] not in EXCLUDED_CATEGORIES
        market = self.get_market(ticker)
        event_ticker = market.get('event_ticker', '')
        if event_ticker:
            info = self.get_event_info(event_ticker)
            cat = info.get('category', '')
            self.category_cache[ticker] = cat
            if cat in EXCLUDED_CATEGORIES:
                return False
        return True

    def get_event_info(self, event_ticker):
        try:
            resp = self.session.get(f'{KALSHI_BASE}/events/{event_ticker}', timeout=10)
            if resp.status_code == 200:
                event = resp.json().get('event', {})
                return {
                    'title': event.get('title', ''),
                    'category': event.get('category', ''),
                }
        except Exception:
            pass
        return {'title': '', 'category': ''}

    def get_all_open_markets(self):
        """Fetch all open markets (paginated). Used for implied prob scanning."""
        all_markets = []
        cursor = None
        pages = 0
        while pages < 200:
            try:
                params = {'status': 'open', 'limit': 200}
                if cursor:
                    params['cursor'] = cursor
                resp = self.session.get(f'{KALSHI_BASE}/markets', params=params, timeout=15)
                if resp.status_code == 429:
                    time.sleep(3)
                    continue
                if resp.status_code != 200:
                    break
                data = resp.json()
                markets = data.get('markets', [])
                cursor = data.get('cursor', '')
                all_markets.extend(markets)
                pages += 1
                if not markets or not cursor:
                    break
            except Exception as e:
                print(f'  API error (all markets): {e}')
                break
        return all_markets

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
                        # Include series with MENTION in ticker
                        if 'MENTION' in ticker:
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
        for series in self._mention_series_cache:
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
                        break
                    data = resp.json()
                    markets = data.get('markets', [])
                    all_markets.extend(markets)
                    cursor = data.get('cursor', '')
                    pages += 1
                    if not markets or not cursor:
                        break
            except Exception as e:
                print(f'  API error (mention series {series}): {e}')
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
        milestone_map = {}  # event_ticker -> {start_ts, end_ts, title}

        for series in series_list:
            try:
                resp = self.session.get(
                    f'{KALSHI_BASE}/events',
                    params={
                        'series_ticker': series,
                        'limit': 100,
                        'with_milestones': 'true',
                    },
                    timeout=15,
                )
                if resp.status_code == 429:
                    time.sleep(2)
                    continue
                if resp.status_code != 200:
                    continue
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
                    for et in ms.get('primary_event_tickers', []):
                        milestone_map[et] = {
                            'start_ts': start_ts,
                            'end_ts': end_ts,
                            'title': title,
                        }
                    for et in ms.get('related_event_tickers', []):
                        if et not in milestone_map:
                            milestone_map[et] = {
                                'start_ts': start_ts,
                                'end_ts': end_ts,
                                'title': title,
                            }
            except Exception as e:
                print(f'  Milestones fetch error ({series}): {e}')

        self._milestones_cache = milestone_map
        self._milestones_cache_ts = now
        print(f"  Milestones: {len(milestone_map)} events with start times")
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
                return data.get('balance', 0)  # cents
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
        """GET /markets/{ticker}/orderbook — returns yes/no bids and asks."""
        try:
            resp = self.session.get(f'{KALSHI_BASE}/markets/{ticker}/orderbook', timeout=10)
            if resp.status_code == 200:
                return resp.json().get('orderbook', {})
        except Exception as e:
            print(f'  Orderbook error ({ticker}): {e}')
        return {}

    def create_order(self, ticker, side, action, count, price_cents):
        """
        POST /portfolio/orders
        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        count: number of contracts
        price_cents: limit price in cents (1-99)
        Returns order dict or None.
        """
        path = '/trade-api/v2/portfolio/orders'
        headers = self._sign_request('POST', path)
        if not headers:
            return None
        headers['Content-Type'] = 'application/json'
        body = {
            'ticker': ticker,
            'side': side,
            'action': action,
            'type': 'limit',
            'count': count,
            'yes_price': price_cents if side == 'yes' else (100 - price_cents),
            'client_order_id': str(uuid.uuid4()),
        }
        try:
            resp = self.session.post(
                f'{KALSHI_BASE}/portfolio/orders',
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
        """DELETE /portfolio/orders/{order_id}"""
        path = f'/trade-api/v2/portfolio/orders/{order_id}'
        headers = self._sign_request('DELETE', path)
        if not headers:
            return False
        try:
            resp = self.session.delete(
                f'{KALSHI_BASE}/portfolio/orders/{order_id}',
                headers=headers, timeout=10,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f'  Cancel error: {e}')
        return False

    def get_order(self, order_id):
        """GET /portfolio/orders/{order_id}"""
        path = f'/trade-api/v2/portfolio/orders/{order_id}'
        headers = self._sign_request('GET', path)
        if not headers:
            return None
        try:
            resp = self.session.get(
                f'{KALSHI_BASE}/portfolio/orders/{order_id}',
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get('order', {})
        except Exception as e:
            print(f'  Get order error: {e}')
        return None


# =====================================================================
# SIGNAL DETECTOR
# =====================================================================

class KalshiReversionDetector:
    def __init__(self):
        self.signal_history = {}
        self._load()

    def _load(self):
        try:
            with open(SIGNAL_HISTORY_FILE, 'r') as f:
                self.signal_history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        with open(SIGNAL_HISTORY_FILE, 'w') as f:
            json.dump(self.signal_history, f)

    def detect(self, trades, client, now_ts):
        if not trades:
            return []

        by_ticker = {}
        for t in trades:
            ticker = t.get('ticker', '')
            if not ticker:
                continue
            if ticker not in by_ticker:
                by_ticker[ticker] = []
            by_ticker[ticker].append(t)

        signals = []

        for ticker, ticker_trades in by_ticker.items():
            if not client.is_allowed_ticker(ticker):
                continue

            small_trades = [t for t in ticker_trades if t.get('count', 0) <= SMALL_TRADE_LIMIT]
            if len(small_trades) < MIN_SMALL_TRADES:
                continue
            if len(small_trades) > MAX_SMALL_TRADES:
                continue

            yes_count = sum(1 for t in small_trades if t.get('taker_side') == 'yes')
            no_count = len(small_trades) - yes_count
            total = len(small_trades)

            if yes_count / total >= MIN_SIDE_RATIO:
                dominant_side = 'yes'
            elif no_count / total >= MIN_SIDE_RATIO:
                dominant_side = 'no'
            else:
                continue

            if SELL_ONLY and dominant_side != 'yes':
                continue

            sorted_trades = sorted(ticker_trades, key=lambda t: t.get('created_time', ''))
            n5 = max(3, len(sorted_trades) // 5)

            prices_start = []
            prices_end = []
            for t in sorted_trades[:n5]:
                p = t.get('yes_price_dollars')
                if p:
                    prices_start.append(float(p))
            for t in sorted_trades[-n5:]:
                p = t.get('yes_price_dollars')
                if p:
                    prices_end.append(float(p))

            if not prices_start or not prices_end:
                continue

            p_start = sum(prices_start) / len(prices_start)
            p_end = sum(prices_end) / len(prices_end)
            move = p_end - p_start

            if dominant_side == 'yes' and move < MIN_PRICE_MOVE:
                continue
            if dominant_side == 'no' and move > -MIN_PRICE_MOVE:
                continue

            if p_end < ENTRY_PRICE_MIN or p_end > ENTRY_PRICE_MAX:
                continue

            last = self.signal_history.get(ticker, 0)
            if now_ts - last < COOLDOWN_HOURS * 3600:
                continue

            market = client.get_market(ticker)
            title = market.get('title', ticker)
            event_ticker = market.get('event_ticker', '')

            if dominant_side == 'yes':
                fade_action = 'SELL'
                fade_side = 'no'
            else:
                fade_action = 'BUY'
                fade_side = 'yes'

            retail_volume = sum(t.get('count', 0) for t in small_trades)

            self.signal_history[ticker] = now_ts
            self._save()

            signals.append({
                'ticker': ticker,
                'title': title,
                'event_ticker': event_ticker,
                'dominant_side': dominant_side,
                'fade_action': fade_action,
                'fade_side': fade_side,
                'entry_price': round(p_end, 4),
                'pre_signal_price': round(p_start, 4),
                'price_move': round(move, 4),
                'n_small_trades': len(small_trades),
                'n_total_trades': len(ticker_trades),
                'retail_contracts': retail_volume,
                'signal_time': now_ts,
            })

        return signals


# =====================================================================
# IMPLIED PROBABILITY VIOLATION DETECTOR
# =====================================================================

class ImpliedProbDetector:
    """Detects when YES prices across outcomes in a multi-outcome event
    don't sum to ~$1.00, indicating mispricing."""

    def __init__(self):
        self.signal_history = {}  # event_ticker -> last signal timestamp
        self._load()

    def _load(self):
        try:
            with open(IMPL_SIGNAL_HISTORY_FILE, 'r') as f:
                self.signal_history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        with open(IMPL_SIGNAL_HISTORY_FILE, 'w') as f:
            json.dump(self.signal_history, f)

    def detect(self, all_markets, client, now_ts):
        """Scan all open markets for implied probability violations.
        Returns list of signals."""
        from collections import defaultdict

        # Group markets by event_ticker
        events = defaultdict(list)
        for m in all_markets:
            et = m.get('event_ticker', '')
            ticker = m.get('ticker', '')
            if et and ticker:
                events[et].append(m)

        signals = []

        for event_ticker, mkts in events.items():
            if not (IMPL_MIN_OUTCOMES <= len(mkts) <= IMPL_MAX_OUTCOMES):
                continue

            # Skip events with independent props (spread, total, 1H, over/under)
            # These are NOT mutually exclusive — Kalshi groups them under one event
            # but "Kansas wins by 3.5" and "Over 145.5 total" are independent bets.
            # Allow legit multi-outcome events like "Who wins ice skating?" (5 people)
            prop_keywords = [
                'over ', 'under ', 'by over', 'by under', 'spread',
                'total', 'points', '1h ', '1st half', '2nd half',
                'first half', 'second half', 'quarter', 'inning',
                'half time', 'halftime',
            ]
            titles_lower = [m.get('title', '').lower() for m in mkts]
            has_props = any(kw in t for t in titles_lower for kw in prop_keywords)
            if has_props:
                continue

            # Skip mention/independent-outcome markets (not mutually exclusive)
            sample_title = mkts[0].get('title', '').lower()
            is_mention = any(kw in sample_title for kw in MENTION_KEYWORDS)
            if is_mention:
                continue

            # Skip combo/parlay markets (vig structure, not real mispricing)
            event_upper = event_ticker.upper()
            is_combo = any(event_upper.startswith(p.upper()) for p in IMPL_EXCLUDED_PREFIXES)
            if is_combo:
                continue

            # Skip crypto/financials (prices driven by external feeds, not mispricing)
            sample_ticker = mkts[0].get('ticker', '').upper()
            is_crypto_fin = False
            for prefix in ['KXBTC', 'KXETH', 'KXSOL', 'KXCRYPTO', 'KXDOGE', 'KXXRP',
                           'KXINX', 'KXNASDAQ', 'KXSP5', 'KXWTI', 'KXINXU']:
                if sample_ticker.startswith(prefix):
                    is_crypto_fin = True
                    break
            if is_crypto_fin:
                continue

            # Check cooldown
            last_cd = self.signal_history.get(event_ticker, 0)
            if now_ts - last_cd < IMPL_COOLDOWN_HOURS * 3600:
                continue

            # Get YES price for each outcome
            outcome_prices = []
            for m in mkts:
                # Use mid-price (bid+ask)/2 or last_price as fallback
                price = None
                yes_bid = m.get('yes_bid')
                yes_ask = m.get('yes_ask')
                if yes_bid and yes_ask:
                    try:
                        price = (int(yes_bid) + int(yes_ask)) / 2 / 100
                    except (ValueError, TypeError):
                        pass
                if price is None:
                    last = m.get('last_price')
                    if last:
                        try:
                            price = int(last) / 100
                        except (ValueError, TypeError):
                            pass
                if price is not None and IMPL_MIN_PRICE <= price <= IMPL_MAX_PRICE:
                    outcome_prices.append({
                        'ticker': m['ticker'],
                        'price': price,
                        'title': m.get('title', m['ticker']),
                        'event_ticker': event_ticker,
                    })

            if len(outcome_prices) < IMPL_MIN_OUTCOMES:
                continue

            # Compute probability sum
            prob_sum = sum(o['price'] for o in outcome_prices)
            deviation = prob_sum - 1.0
            abs_dev = abs(deviation)

            if abs_dev < IMPL_DEVIATION_THRESHOLD:
                continue

            # Skip if deviation too large — likely independent outcomes, not mispricing
            if abs_dev > IMPL_MAX_DEVIATION:
                continue

            # Determine trade direction and target
            if deviation > 0:
                # Overpriced: sell the highest-priced outcome (buy NO)
                target = max(outcome_prices, key=lambda o: o['price'])
                fade_action = 'SELL'
                fade_side = 'no'
            else:
                # Underpriced: buy the lowest-priced outcome (buy YES)
                target = min(outcome_prices, key=lambda o: o['price'])
                fade_action = 'BUY'
                fade_side = 'yes'

            # Record cooldown
            self.signal_history[event_ticker] = now_ts
            self._save()

            signals.append({
                'ticker': target['ticker'],
                'title': target['title'],
                'event_ticker': event_ticker,
                'fade_action': fade_action,
                'fade_side': fade_side,
                'entry_price': round(target['price'], 4),
                'pre_signal_price': round(target['price'], 4),  # same for impl prob
                'price_move': round(deviation, 4),
                'n_small_trades': len(outcome_prices),  # repurpose: n_outcomes
                'retail_contracts': 0,
                'signal_time': now_ts,
                'signal_type': 'implied_prob',
                'prob_sum': round(prob_sum, 4),
                'deviation': round(deviation, 4),
                'abs_dev': round(abs_dev, 4),
                'n_outcomes': len(outcome_prices),
            })

        return signals


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

        # Fetch milestones for event_start timing filter
        milestone_map = client.get_milestones()

        for m in open_markets:
            ticker = m.get('ticker', '')
            if not ticker:
                continue
            debug_counts['total'] += 1

            # Skip categories with insufficient edge at bot timing (0-1.5h pre-event, 5-30c):
            # Earnings: no event milestones, can't time entry
            # Fight: thin edge (+16%), noisy
            # Press (SecPress/Leavitt): efficiently priced (+2% ROI)
            # NBA: small sample, not significant pre-event (t=1.43); revisit with live-game timing
            ticker_upper = ticker.upper()
            if 'EARNINGS' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue
            if 'FIGHTMENTION' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue
            if 'SECPRESS' in ticker_upper or 'LEAVITT' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue
            if 'NBAMENTION' in ticker_upper or 'NBAFINALS' in ticker_upper:
                debug_counts['skipped_cat'] += 1
                continue

            # Parse close_time — skip if too far out (capital efficiency)
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

            hours_to_close = (close_ts - now_ts) / 3600
            if hours_to_close > MENTION_MAX_CLOSE_HOURS:
                debug_counts['too_far'] += 1
                continue

            # Event start timing filter — per-category windows:
            # Trump: 0-24h before event start (backtest: +88% ROI, t=8.15)
            # NCAA: live only, 0-2h after start (backtest: +98% ROI, t=4.61)
            # Default: 0-1.5h before event start (backtest: +73% ROI, t=4.48)
            event_ticker = m.get('event_ticker', '')
            is_trump = 'TRUMPMENTION' in ticker_upper
            is_ncaa = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
            ms = milestone_map.get(event_ticker)
            if ms:
                event_start_ts = ms.get('start_ts', 0)
                hours_to_event = (event_start_ts - now_ts) / 3600
                if is_ncaa:
                    # NCAA: live games only (0-2h after start)
                    if hours_to_event > 0:
                        debug_counts['too_early'] += 1
                        continue
                    if hours_to_event < -2:
                        debug_counts['too_far'] += 1
                        continue
                elif is_trump:
                    # Trump: 0-24h before event start
                    if hours_to_event > 24:
                        debug_counts['too_early'] += 1
                        continue
                    if hours_to_event < 0:
                        debug_counts['too_far'] += 1
                        continue
                else:
                    # Default: 0-1.5h before event start
                    if hours_to_event > 1.5:
                        debug_counts['too_early'] += 1
                        continue
                    if hours_to_event < 0:
                        debug_counts['too_far'] += 1
                        continue
            else:
                # No milestone data — skip (can't time entry)
                debug_counts['no_milestone'] += 1
                continue

            # Get current YES price → derive NO price
            yes_price = None
            yes_bid = m.get('yes_bid')
            yes_ask = m.get('yes_ask')
            if yes_bid is not None and yes_ask is not None:
                try:
                    yes_price = (int(yes_bid) + int(yes_ask)) / 2 / 100
                except (ValueError, TypeError):
                    pass
            if yes_price is None:
                last = m.get('last_price')
                if last is not None:
                    try:
                        yes_price = int(last) / 100
                    except (ValueError, TypeError):
                        pass
            if yes_price is None:
                debug_counts['no_price'] += 1
                continue

            no_price = 1 - yes_price
            # Per-category price ranges:
            # NCAA live: 5-25c (backtest: +115% ROI, t=4.14)
            # All others: 5-30c
            max_no = 0.25 if is_ncaa else MENTION_MAX_NO_PRICE
            if no_price < MENTION_MIN_NO_PRICE or no_price > max_no:
                debug_counts['price_out_range'] += 1
                continue

            # Cooldown check — use longer cooldown since we're not time-gated
            # Once we bet on a ticker, don't bet again for 24h
            last_signal = self.signal_history.get(ticker, 0)
            if now_ts - last_signal < 24 * 3600:
                debug_counts['cooldown'] += 1
                continue

            debug_counts['eligible'] += 1

            # NOTE: cooldown is now set AFTER order is placed (in run loop)
            # so markets aren't blocked before passing the entry window check

            title = m.get('title', ticker)
            no_price_cents = int(no_price * 100)
            hours_to_event_val = round((ms['start_ts'] - now_ts) / 3600, 2) if ms else None

            # Volume as proxy for "event is live" — high volume = active event
            volume_24h = 0
            try:
                volume_24h = int(m.get('volume_24h', 0) or 0)
            except (ValueError, TypeError):
                pass
            open_interest = 0
            try:
                open_interest = int(m.get('open_interest', 0) or 0)
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
                'signal_type': 'mention_buy_no',
                'no_price': round(no_price, 4),
                'no_price_cents': no_price_cents,
                'hours_before_close': round((close_ts - now_ts) / 3600, 2),
                'hours_to_event': hours_to_event_val,
                'close_ts': close_ts,
                'volume_24h': volume_24h,
                'open_interest': open_interest,
            })

        # Print debug breakdown
        print(f"  Mention filter: {debug_counts['total']} checked, "
              f"{debug_counts['skipped_cat']} skipped(cat), "
              f"{debug_counts['no_milestone']} no milestone, "
              f"{debug_counts['too_early']} too early, "
              f"{debug_counts['too_far']} too far, "
              f"{debug_counts['no_price']} no price, "
              f"{debug_counts['price_out_range']} price OOR, "
              f"{debug_counts['cooldown']} cooldown, "
              f"{debug_counts['eligible']} eligible")

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
                'signal_type': pos.get('signal_type', 'reversion'),
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
        signal_type = signal.get('signal_type', 'reversion')

        if signal_type == 'mention_buy_no':
            # Mention positions: hold until settlement (close_ts from signal)
            exit_time = signal.get('close_ts', signal['signal_time'] + 4 * 3600)
        elif signal_type == 'implied_prob':
            exit_time = signal['signal_time'] + IMPL_HOLD_HOURS * 3600
        else:
            exit_time = signal['signal_time'] + HOLD_HOURS * 3600

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
        if signal_type == 'mention_buy_no':
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

    def event_exposure(self, event_ticker):
        """Total dollars deployed on open positions for a given event."""
        if not event_ticker:
            return 0
        return sum(
            pos.get('bet_dollars', 0)
            for pos in self.positions
            if pos.get('status') == 'open' and pos.get('event_ticker') == event_ticker
        )

    def check(self, client):
        """Check for timed exits (reversion) and settlements (mention).
        Returns alerts list of (alert_type, position) tuples."""
        now = time.time()
        alerts = []
        still_open = []

        for pos in self.positions:
            if pos['status'] != 'open':
                continue

            is_mention = pos.get('signal_type') == 'mention_buy_no'

            if is_mention:
                # Mention positions: poll for settlement every cycle.
                # Clear cache to get fresh status (close_time can be far future
                # because Kalshi uses can_close_early with a distant deadline).
                client.market_cache.pop(pos['ticker'], None)
                market = client.get_market(pos['ticker'])
                status = market.get('status', 'open') if market else 'open'

                if status in ('settled', 'finalized'):
                    result = market.get('result', '')
                    fill_price = pos.get('fill_price', pos.get('no_price', 0))
                    fill_count = pos.get('fill_count', 0)

                    if result == 'no':
                        # NO won — we profit
                        pnl = fill_count * (1 - fill_price)
                    elif result == 'yes':
                        # YES won — we lose our cost
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
                else:
                    still_open.append(pos)
                    continue

            # Reversion/impl positions: timed exit
            client.market_cache.pop(pos['ticker'], None)
            current = client.get_current_price(pos['ticker'])
            roi = self._roi(pos, current)

            if now >= pos['exit_time']:
                pos['exit_price'] = current
                pos['roi_pct'] = roi
                pos['status'] = 'closed_24h'
                pos['close_time'] = now
                self.closed.append(pos)
                self._log_trade_csv(pos, 'EXIT_24H')
                alerts.append(('24h_exit', pos))
                continue

            still_open.append(pos)

        self.positions = still_open
        self._save()
        return alerts

    def _roi(self, pos, current):
        if current is None:
            return None
        entry = pos.get('fill_price', pos['entry_price'])
        if pos['fade_action'] == 'SELL':
            return (entry - current) / entry * 100
        else:
            return (current - entry) / entry * 100

    def count(self, signal_type=None):
        if signal_type:
            return sum(1 for p in self.positions if p.get('signal_type', 'reversion') == signal_type)
        return len(self.positions)

    def live_count(self):
        return sum(1 for p in self.positions if p.get('is_live'))

    def has_open_ticker(self, ticker):
        """Check if there's already an open position for this ticker."""
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
                contracts = max(1, int(IMPL_MAX_BET_DOLLARS / (entry_cents / 100)))
                best_ask_cents = entry_cents
                uncapped_dollars = IMPL_MAX_BET_DOLLARS
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
            max_dollars = IMPL_MAX_BET_DOLLARS if signal.get('signal_type') == 'implied_prob' else MAX_BET_DOLLARS
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

    async def send_signal(self, sig, order_info=None):
        entry_cents = int(sig['entry_price'] * 100)
        exit_time = datetime.fromtimestamp(
            sig['signal_time'] + HOLD_HOURS * 3600, tz=timezone.utc
        ).strftime('%b %d %H:%M UTC')

        move_dir = "pushed YES up" if sig['dominant_side'] == 'yes' else "pushed NO up"

        if sig['fade_side'] == 'yes':
            action = f"BUY YES at {entry_cents}c"
        else:
            action = f"BUY NO at {100 - entry_cents}c"

        url = f"\nhttps://kalshi.com/markets/{sig['ticker']}"

        # Trading info
        if order_info:
            if order_info.get('dry_run'):
                trade_line = (
                    f"\n[DRY RUN] Would buy {order_info['fill_count']} NO "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
            else:
                trade_line = (
                    f"\nORDER FILLED: {order_info['fill_count']} NO "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
        else:
            trade_line = "\n(Signal only — no order placed)"

        msg = (
            f"KALSHI RETAIL REVERSION\n\n"
            f"{sig['title']}\n"
            f"Ticker: {sig['ticker']}\n\n"
            f"ACTION: {action}\n\n"
            f"Yes price: {entry_cents}c\n"
            f"Exit: {exit_time} (24h hold)\n\n"
            f"Why: {sig['n_small_trades']} small trades {move_dir} "
            f"by {abs(sig['price_move'])*100:.0f}c in 1hr "
            f"({sig['retail_contracts']:,} contracts). Fading the crowd.\n\n"
            f"Backtest (60d): 58% WR, +27% avg ROI"
            f"{trade_line}"
            f"{url}"
        )
        await self._send(msg)

    async def send_impl_prob_signal(self, sig, order_info=None):
        entry_cents = int(sig['entry_price'] * 100)
        exit_time = datetime.fromtimestamp(
            sig['signal_time'] + IMPL_HOLD_HOURS * 3600, tz=timezone.utc
        ).strftime('%b %d %H:%M UTC')

        if sig['fade_side'] == 'yes':
            action = f"BUY YES at {entry_cents}c"
        else:
            action = f"BUY NO at {100 - entry_cents}c"

        url = f"\nhttps://kalshi.com/markets/{sig['ticker']}"

        if order_info:
            if order_info.get('dry_run'):
                trade_line = (
                    f"\n[DRY RUN] Would buy {order_info['fill_count']} "
                    f"{'NO' if sig['fade_side'] == 'no' else 'YES'} "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
            else:
                trade_line = (
                    f"\nORDER FILLED: {order_info['fill_count']} "
                    f"{'NO' if sig['fade_side'] == 'no' else 'YES'} "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
        else:
            trade_line = "\n(Signal only -- no order placed)"

        msg = (
            f"KALSHI IMPLIED PROB VIOLATION\n\n"
            f"{sig['title']}\n"
            f"Ticker: {sig['ticker']}\n\n"
            f"ACTION: {action}\n\n"
            f"Prob sum: ${sig['prob_sum']:.2f} across {sig['n_outcomes']} outcomes "
            f"(deviation: {sig['deviation']:+.2f})\n"
            f"Exit: {exit_time} (12h hold)\n\n"
            f"Backtest (60d Kalshi): 70.7% WR, +17.9% avg PnL"
            f"{trade_line}"
            f"{url}"
        )
        await self._send(msg)

    async def send_mention_signal(self, sig, order_info=None):
        no_cents = sig.get('no_price_cents', 0)
        hours = sig.get('hours_before_close', 0)

        url = f"\nhttps://kalshi.com/markets/{sig['ticker']}"

        if order_info:
            if order_info.get('dry_run'):
                trade_line = (
                    f"\n[DRY RUN] Would buy {order_info['fill_count']} NO "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
            else:
                trade_line = (
                    f"\nORDER FILLED: {order_info['fill_count']} NO "
                    f"@ {int(order_info['fill_price']*100)}c (${order_info['bet_dollars']:.2f})"
                )
        else:
            trade_line = "\n(Signal only -- no order placed)"

        msg = (
            f"KALSHI MENTION BUY NO\n\n"
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

    async def send_24h_exit(self, pos, exit_info=None):
        roi = pos.get('roi_pct', 0) or 0
        result = "WIN" if roi > 0 else "LOSS"
        entry_cents = int(pos['entry_price'] * 100)
        exit_price = pos.get('exit_price', 0) or 0
        exit_cents = int(exit_price * 100) if exit_price else 0

        if pos['fade_action'] == 'SELL':
            close = f"SELL your NO position (or buy back YES at {exit_cents}c)"
        else:
            close = f"SELL your YES at {exit_cents}c"

        pnl_line = ""
        if exit_info and 'pnl' in exit_info:
            pnl_line = f"\nActual P&L: ${exit_info['pnl']:.2f}"

        msg = (
            f"24h EXIT - {result}\n\n"
            f"{pos['title']}\n"
            f"Ticker: {pos['ticker']}\n\n"
            f"CLOSE NOW: {close}\n\n"
            f"Entry: {entry_cents}c -> Exit: {exit_cents}c\n"
            f"ROI: {roi:+.1f}%{pnl_line}"
        )
        await self._send(msg)

    async def send_startup(self, n_open, balance=None, mode='LIVE'):
        bal_line = f"Balance: ${balance/100:.2f}\n" if balance else ""
        msg = (
            f"Kalshi Auto-Trading Bot Started\n\n"
            f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}\n"
            f"Strategy: Retail reversion (SELL-only, ${MAX_BET_DOLLARS}/bet, 24h hold)\n"
            f"Categories: No sports/crypto/financials\n"
            f"Max positions: {MAX_OPEN_POSITIONS}\n"
            f"{bal_line}"
            f"Open positions: {n_open}\n"
            f"Scan interval: {SCAN_INTERVAL_SECONDS}s"
        )
        await self._send(msg)

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
        self.detector = KalshiReversionDetector()
        self.impl_detector = ImpliedProbDetector()
        self.mention_detector = MentionBuyNoDetector()
        self.positions = KalshiPositionTracker()
        self.notifier = KalshiNotifier()
        self.trade_logger = TradeLogger()
        self.executor = OrderExecutor(self.client, self.trade_logger)
        self._last_mention_scan = 0  # timestamp of last mention scan
        self._resting_mention_orders = {}  # ticker -> {order_id, placed_time, contracts, price_cents, sig}
        self._event_volume_prev = {}  # event_ticker -> (sum_volume_24h, scan_ts) from previous cycle

    async def run(self):
        mode = "DRY RUN" if DRY_RUN else "LIVE"
        print("=" * 60)
        print(f"KALSHI AUTO-TRADING BOT [{mode}]")
        print("=" * 60)
        print(f"Telegram: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
        print(f"Auth: {'OK' if self.client.can_trade else 'MISSING (signal-only mode)'}")
        print(f"Strategy 1: Fade retail surges, 24h hold")
        print(f"Strategy 2: Mention BUY NO 5-30c, ${MENTION_BET_DOLLARS}/bet, 1h pre-event, ex Fight/Press")
        print(f"Max bet: ${MAX_BET_DOLLARS}/signal (reversion), ${MENTION_BET_DOLLARS}/signal (mention)")
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

            # Seed cooldown from existing API positions so we don't
            # re-bet tickers we already hold (survives redeploys)
            existing = self.client.get_positions()
            seeded = 0
            for pos in existing:
                t = pos.get('ticker', '')
                if 'MENTION' in t.upper() and t not in self.mention_detector.signal_history:
                    self.mention_detector.signal_history[t] = time.time()
                    seeded += 1
            if seeded:
                self.mention_detector._save()
                print(f"  Seeded cooldown from {seeded} existing mention positions")

        await self.notifier.send_startup(self.positions.count(), balance)

        while True:
            try:
                await self._cycle()
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

        reversion_allowed = False  # Disabled — focusing capital on mention strategy
        low_balance = False

        # Check balance
        if self.client.can_trade:
            bal = self.client.get_balance()
            if bal is not None and bal < 100:  # less than $1
                print(f"  Low balance: ${bal/100:.2f} — skipping new orders, waiting for fills/settlements")
                log_event('low_balance', balance_cents=bal)
                low_balance = True

        # Safety: check max positions
        rev_count = self.positions.count('reversion')
        if rev_count >= MAX_OPEN_POSITIONS:
            print(f"  MAX POSITIONS: {rev_count}/{MAX_OPEN_POSITIONS}. No new orders.")
            reversion_allowed = False

        # 1. Fetch last 65 min of trades (extra 5min buffer)
        trades = self.client.get_all_recent_trades(since_minutes=65)
        print(f"  Trades fetched: {len(trades)}")

        if trades:
            tickers = set(t.get('ticker', '') for t in trades)
            print(f"  Unique markets: {len(tickers)}")

            # 2. Detect signals
            signals = self.detector.detect(trades, self.client, now)
            print(f"  Signals: {len(signals)}")

            for sig in signals:
                entry_c = int(sig['entry_price'] * 100)
                print(f"  SIGNAL: {sig['fade_action']} '{sig['title'][:50]}' @ {entry_c}c "
                      f"(move {sig['price_move']:+.3f}, {sig['n_small_trades']} trades)")

                # Skip if we already have an open position on this ticker
                if self.positions.has_open_ticker(sig['ticker']):
                    print(f"    DUPE: already have open position on {sig['ticker']}, skipping")
                    continue

                order_info = None
                event = sig.get('event_ticker', '')
                exposure = self.positions.event_exposure(event)
                if exposure >= MAX_BET_DOLLARS and event:
                    print(f"    EVENT CAP: already ${exposure:.2f} on {event} (max ${MAX_BET_DOLLARS}), skipping")
                elif reversion_allowed and self.client.can_trade and not low_balance:
                    order_info = self.executor.execute_entry(sig)

                if order_info:
                    await self.notifier.send_signal(sig, order_info)
                    self.positions.add(sig, order_info)

        # 3. Mention BUY NO scan
        # First: check resting orders from previous cycles
        if self._resting_mention_orders and self.client.can_trade:
            await self._check_resting_mention_orders()

        mention_count = self.positions.count('mention_buy_no') + len(self._resting_mention_orders)
        mention_allowed = mention_count < MENTION_MAX_POSITIONS
        should_scan_mentions = (now - self._last_mention_scan) >= MENTION_SCAN_INTERVAL_SECONDS

        if should_scan_mentions:
            self._last_mention_scan = now
            print(f"  Mention scan: fetching open mention markets...")
            mention_markets = self.client.get_open_mention_markets()
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
                        vol = int(m.get('volume_24h', 0) or 0)
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
                    is_trump = 'TRUMPMENTION' in ticker_up
                    is_ncaa = 'NCAAMENTION' in ticker_up or 'NCAABMENTION' in ticker_up
                    if is_ncaa:
                        return -2 <= h <= 0  # live games only
                    elif is_trump:
                        return -10/60 <= h <= 24
                    else:
                        return -10/60 <= h <= 1.5

                eligible = [s for s in mention_signals if in_entry_window(s)]
                n_total = len(mention_signals)
                n_with_start = sum(1 for s in mention_signals if s.get('event_start_ts'))
                n_eligible = len(eligible)
                mention_signals = eligible
                print(f"  Mention signals: {n_total} total, {n_with_start} with milestone, {n_eligible} in window")

                for sig in mention_signals:
                    if not mention_allowed:
                        print(f"    MENTION CAP: {mention_count}/{MENTION_MAX_POSITIONS}, skipping")
                        break

                    # Cap resting orders to limit capital lockup
                    if len(self._resting_mention_orders) >= MENTION_MAX_RESTING_ORDERS:
                        print(f"    RESTING CAP: {len(self._resting_mention_orders)}/{MENTION_MAX_RESTING_ORDERS}, waiting for fills")
                        break

                    # Skip if we already have an open position or resting order
                    if self.positions.has_open_ticker(sig['ticker']):
                        continue
                    if sig['ticker'] in self._resting_mention_orders:
                        continue

                    # Per-event exposure cap
                    event = sig.get('event_ticker', '')
                    if event:
                        event_exp = self.positions.event_exposure(event)
                        if event_exp >= MENTION_MAX_EVENT_DOLLARS:
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
                    print(f"  MENTION: BUY NO @ {no_c}c '{sig['title'][:50]}' ({time_str}, evt_vol={evt_vol:,})")

                    if low_balance:
                        continue

                    order_info = None
                    if self.client.can_trade:
                        order_info = self._execute_mention_entry(sig)

                    if order_info:
                        await self.notifier.send_mention_signal(sig, order_info)
                        self.positions.add(sig, order_info)
                        mention_count += 1
                        mention_allowed = mention_count < MENTION_MAX_POSITIONS
                        # Set cooldown AFTER order placed (not on eligibility)
                        self.mention_detector.signal_history[sig['ticker']] = time.time()
                        self.mention_detector._save()
        else:
            print(f"  Mention scan: next in {int(MENTION_SCAN_INTERVAL_SECONDS - (now - self._last_mention_scan))}s")

        # 4. Check positions for exit (24h reversion, settlement for mention)
        alerts = self.positions.check(self.client)
        for atype, pos in alerts:
            exit_info = None
            if pos.get('is_live') and self.client.can_trade:
                # Mention positions held until settlement — no manual exit needed
                if pos.get('signal_type') != 'mention_buy_no':
                    exit_info = self.executor.execute_exit(pos)

            if atype == '24h_exit':
                print(f"  24h EXIT: '{pos['title'][:50]}' ROI: {pos.get('roi_pct',0):+.1f}%")
                await self.notifier.send_24h_exit(pos, exit_info)
            elif atype == 'settled':
                pnl = pos.get('settle_pnl', 0)
                result = "WIN" if pnl > 0 else "LOSS"
                print(f"  SETTLED ({result}): '{pos['title'][:50]}' P&L: ${pnl:+.2f}")
                log_event('settled', ticker=pos.get('ticker'), title=pos.get('title'),
                          signal_type=pos.get('signal_type'), pnl=pnl, result=result,
                          entry_price=pos.get('entry_price'), fill_price=pos.get('fill_price'),
                          fill_count=pos.get('fill_count'), is_live=pos.get('is_live'))

        rev_count = self.positions.count('reversion')
        mention_count = self.positions.count('mention_buy_no')
        resting_count = len(self._resting_mention_orders)
        resting_str = f", {resting_count} resting" if resting_count else ""
        print(f"  Open positions: {self.positions.count()} (rev={rev_count}, mention={mention_count}{resting_str}, {self.positions.live_count()} live)")
        daily_pnl = self.trade_logger.daily_pnl()
        if daily_pnl != 0:
            print(f"  Daily P&L: ${daily_pnl:.2f}")


    async def _check_resting_mention_orders(self):
        """Check resting mention orders for fills, cancel stale ones."""
        if not self._resting_mention_orders:
            return

        now = time.time()
        filled_tickers = []
        canceled_tickers = []

        for ticker, info in list(self._resting_mention_orders.items()):
            order_id = info['order_id']
            age = now - info['placed_time']

            status = self.client.get_order(order_id)
            if not status:
                # Can't check — if old enough, cancel
                if age > MENTION_ORDER_REST_SECONDS:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass
                    canceled_tickers.append(ticker)
                continue

            filled = status.get('quantity_filled', 0)
            remaining = status.get('remaining_count', 0)

            if filled > 0:
                # Got a fill! Cancel remainder if partial
                if remaining > 0:
                    try:
                        self.client.cancel_order(order_id)
                    except Exception:
                        pass

                avg_fill = status.get('average_fill_price', info['price_cents'])
                actual_dollars = round(filled * avg_fill / 100, 2)
                order_info = {
                    'order_id': order_id,
                    'fill_price': avg_fill / 100,
                    'fill_count': filled,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                }
                self.trade_logger.record({
                    'type': 'entry',
                    'strategy': 'mention_buy_no',
                    'ticker': ticker,
                    'order_id': order_id,
                    'side': 'no',
                    'action': 'buy',
                    'contracts_filled': filled,
                    'price_cents': info['price_cents'],
                    'avg_fill_price': avg_fill,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                })
                print(f"  RESTING FILL: {filled} NO @ avg {avg_fill}c (${actual_dollars:.2f}) — {ticker}")
                log_event('mention_filled_resting', ticker=ticker, order_id=order_id,
                          filled=filled, avg_fill_cents=avg_fill, bet_dollars=actual_dollars,
                          rested_seconds=int(age))
                await self.notifier.send_mention_signal(info['sig'], order_info)
                self.positions.add(info['sig'], order_info)
                filled_tickers.append(ticker)

            elif age > MENTION_ORDER_REST_SECONDS:
                # Stale — cancel
                try:
                    self.client.cancel_order(order_id)
                except Exception:
                    pass
                canceled_tickers.append(ticker)
                print(f"  RESTING EXPIRED: {ticker} (no fill in {int(age/60)}min), canceled {order_id}")
                log_event('mention_order_expired', ticker=ticker, order_id=order_id,
                          price_cents=info['price_cents'], contracts=info['contracts'],
                          rested_seconds=int(age))

        for t in filled_tickers + canceled_tickers:
            self._resting_mention_orders.pop(t, None)

        n_resting = len(self._resting_mention_orders)
        if n_resting > 0 or filled_tickers or canceled_tickers:
            print(f"  Resting orders: {n_resting} active, {len(filled_tickers)} filled, {len(canceled_tickers)} expired")

    def _execute_mention_entry(self, sig):
        """Execute a mention BUY NO entry. Simpler than reversion — fixed $5 bet,
        buy NO at market, no complex depth sizing."""
        ticker = sig['ticker']
        no_price = sig['no_price']
        no_price_cents = sig['no_price_cents']

        # Fetch orderbook to get actual ask
        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        # Buy NO: derive NO asks from YES bids
        yes_bids = orderbook.get('yes', [])
        if isinstance(yes_bids, dict):
            yes_bids = yes_bids.get('bids', [])
        if not yes_bids:
            print(f"    No YES bids (= no NO asks) for {ticker}, skipping")
            return None

        # Convert YES bids to NO asks
        no_asks = sorted([[100 - b[0], b[1]] for b in yes_bids], key=lambda x: x[0])
        if not no_asks:
            return None

        best_no_ask = no_asks[0][0]  # cents

        # Check price still in range (NCAA live: 5-25c, others: 5-30c)
        ticker_upper = ticker.upper()
        is_ncaa_order = 'NCAAMENTION' in ticker_upper or 'NCAABMENTION' in ticker_upper
        max_no_order = 0.25 if is_ncaa_order else MENTION_MAX_NO_PRICE
        if best_no_ask / 100 < MENTION_MIN_NO_PRICE or best_no_ask / 100 > max_no_order:
            print(f"    NO ask {best_no_ask}c outside range, skipping")
            log_event('mention_skip_spread', ticker=ticker, signal_no_cents=no_price_cents,
                      book_no_ask_cents=best_no_ask, reason='outside_range',
                      hours_before_close=sig.get('hours_before_close'),
                      hours_to_event=sig.get('hours_to_event'),
                      volume_24h=sig.get('volume_24h', 0),
                      event_volume_24h=sig.get('event_volume_24h', 0),
                      event_velocity=sig.get('event_velocity', 0))
            return None

        # Calculate contracts for MENTION_BET_DOLLARS
        contracts = int(MENTION_BET_DOLLARS / (best_no_ask / 100))
        if contracts < 1:
            print(f"    Can't buy even 1 contract at {best_no_ask}c for ${MENTION_BET_DOLLARS}, skipping")
            return None

        bet_dollars = round(contracts * best_no_ask / 100, 2)
        print(f"    Sizing: {contracts} NO @ {best_no_ask}c = ${bet_dollars:.2f}")

        if DRY_RUN:
            order_info = {
                'order_id': f'DRY-MEN-{uuid.uuid4().hex[:8]}',
                'fill_price': best_no_ask / 100,
                'fill_count': contracts,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            }
            self.trade_logger.record({
                'type': 'entry',
                'strategy': 'mention_buy_no',
                'ticker': ticker,
                'side': 'no',
                'action': 'buy',
                'contracts': contracts,
                'price_cents': best_no_ask,
                'bet_dollars': bet_dollars,
                'dry_run': True,
            })
            print(f"    DRY RUN: would buy {contracts} NO @ {best_no_ask}c (${bet_dollars:.2f})")
            return order_info

        # Live order: buy NO at best ask (hard cap at max NO price per category)
        max_cents = int(max_no_order * 100)
        buy_price = min(best_no_ask, max_cents)
        order = self.client.create_order(
            ticker=ticker,
            side='no',
            action='buy',
            count=contracts,
            price_cents=buy_price,
        )
        if not order:
            print(f"    Mention order failed for {ticker}")
            return None

        order_id = order.get('order_id', '')
        print(f"    Order placed: {order_id} ({contracts} NO @ {buy_price}c) — resting up to {MENTION_ORDER_REST_SECONDS//60}min")
        log_event('mention_order_placed', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=buy_price, bet_dollars=bet_dollars,
                  signal_no_cents=no_price_cents, book_no_ask_cents=best_no_ask,
                  hours_before_close=sig.get('hours_before_close'),
                  hours_to_event=sig.get('hours_to_event'),
                  volume_24h=sig.get('volume_24h', 0),
                  event_volume_24h=sig.get('event_volume_24h', 0),
                  event_velocity=sig.get('event_velocity', 0))

        # Quick check: might fill instantly
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
                }
                self.trade_logger.record({
                    'type': 'entry',
                    'strategy': 'mention_buy_no',
                    'ticker': ticker,
                    'order_id': order_id,
                    'side': 'no',
                    'action': 'buy',
                    'contracts_filled': filled,
                    'price_cents': buy_price,
                    'avg_fill_price': avg_fill,
                    'bet_dollars': actual_dollars,
                    'dry_run': False,
                })
                print(f"    FILLED: {filled}/{contracts} NO @ avg {avg_fill}c (${actual_dollars:.2f})")
                log_event('mention_filled_instant', ticker=ticker, order_id=order_id,
                          filled=filled, requested=contracts, avg_fill_cents=avg_fill,
                          bet_dollars=actual_dollars, hours_before_close=sig.get('hours_before_close'))
                return info

        # Not filled yet — leave resting on the book
        self._resting_mention_orders[ticker] = {
            'order_id': order_id,
            'placed_time': time.time(),
            'contracts': contracts,
            'price_cents': buy_price,
            'sig': sig,
        }
        print(f"    Resting on book (will check next cycle)")
        log_event('mention_order_resting', ticker=ticker, order_id=order_id,
                  contracts=contracts, price_cents=buy_price,
                  hours_before_close=sig.get('hours_before_close'))
        return None


if __name__ == "__main__":
    scanner = KalshiReversionScanner()
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        print("\nStopped.")
