#!/usr/bin/env python3
"""
Kalshi Retail Mean Reversion AUTO-TRADING BOT

Automated version of the scanner that:
- Detects retail surge signals (same logic as before)
- Places real orders on Kalshi via authenticated API
- Sizes bets dynamically based on orderbook depth
- Executes stop-loss and 24h exits automatically
- Has DRY_RUN toggle and safety circuit breakers

Uses RSA-PSS signing for Kalshi API authentication.
"""

import asyncio
import base64
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
KALSHI_PRIVATE_KEY = os.environ.get('KALSHI_PRIVATE_KEY', '')  # Raw PEM or base64-encoded PEM
KALSHI_PRIVATE_KEY_B64 = os.environ.get('KALSHI_PRIVATE_KEY_B64', '')  # Base64-encoded PEM (for Railway)

KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

# Strategy params (adapted from Polymarket backtest)
MIN_SMALL_TRADES = 12       # Min number of small trades on one side
MAX_SMALL_TRADES = 40       # Max (beyond this, it's real news)
SMALL_TRADE_LIMIT = 100     # Contracts — "retail" is < 100 contracts
MIN_SIDE_RATIO = 0.65       # 65%+ on one side
MIN_PRICE_MOVE = 0.15       # 15c move
ENTRY_PRICE_MIN = 0.21
ENTRY_PRICE_MAX = 0.80
STOP_LOSS_PCT = -30.0
HOLD_HOURS = 24
COOLDOWN_HOURS = 4

# Scanner settings
SCAN_INTERVAL_SECONDS = 300  # 5 min
TRADES_PER_PAGE = 1000
WINDOW_MINUTES = 60          # 1-hour signal windows

# Trading config
DRY_RUN = False  # LIVE TRADING ENABLED
MAX_BET_DOLLARS = 50          # Max per signal
MIN_BET_DOLLARS = 1           # Skip if depth too thin
DEPTH_FRACTION = 0.50         # Use 50% of 3-level depth
MAX_DAILY_LOSS = 200          # Stop trading if daily loss exceeds this
MAX_OPEN_POSITIONS = 20       # Cap concurrent positions
ORDER_WAIT_SECONDS = 5        # Wait for fill after placing order
MAX_ORDER_RETRIES = 2         # Retry at next price level
MAX_SLIPPAGE_PCT = 1.0        # Skip if NO price > 1% worse than signal

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
    'KXNHLPROP', 'KXAFCCL',
    # Crypto
    'KXBTC', 'KXETH', 'KXSOL', 'KXCRYPTO', 'KXDOGE', 'KXXRP',
    # Financials (22% WR, -18.6% avg ROI in backtest)
    'KXINX', 'KXNASDAQ', 'KXSP5', 'KXWTI', 'KXINXU',
]
EXCLUDED_CATEGORIES = {'Sports', 'Crypto', 'Financials'}

# 60d backtest: SELL +24.4% avg ROI vs BUY -6.8% — only fade buying surges
SELL_ONLY = True

# State files
STATE_DIR = Path(__file__).parent
POSITIONS_FILE = STATE_DIR / 'kalshi_positions.json'
SIGNAL_HISTORY_FILE = STATE_DIR / 'kalshi_signal_history.json'
TRADE_LOG_FILE = STATE_DIR / 'kalshi_trade_log.json'


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
        Supports two modes:
          1. KALSHI_PRIVATE_KEY — raw PEM content (for Railway / cloud)
          2. KALSHI_PRIVATE_KEY_PATH — path to PEM file (for local)
        """
        # Debug: show what env vars we got
        print(f"  Auth env: KEY_ID={len(KALSHI_API_KEY_ID)} chars, "
              f"PRIVATE_KEY={len(KALSHI_PRIVATE_KEY)} chars, "
              f"B64={len(KALSHI_PRIVATE_KEY_B64)} chars, "
              f"PATH={len(KALSHI_PRIVATE_KEY_PATH)} chars")
        print(f"  DRY_RUN env='{os.environ.get('DRY_RUN', '(not set)')}' -> DRY_RUN={DRY_RUN}")

        # Mode 1a: base64-encoded PEM (best for Railway — no multiline issues)
        if KALSHI_PRIVATE_KEY_B64:
            try:
                pem_data = base64.b64decode(KALSHI_PRIVATE_KEY_B64)
                self.private_key = serialization.load_pem_private_key(pem_data, password=None)
                print("  RSA key loaded from KALSHI_PRIVATE_KEY_B64 env var")
                return
            except Exception as e:
                print(f"  WARNING: Failed to load base64 private key: {e}")

        # Mode 1b: raw PEM content from env var
        if KALSHI_PRIVATE_KEY:
            try:
                pem_data = KALSHI_PRIVATE_KEY.replace('\\n', '\n').encode()
                self.private_key = serialization.load_pem_private_key(pem_data, password=None)
                print("  RSA key loaded from KALSHI_PRIVATE_KEY env var")
                return
            except Exception as e:
                print(f"  WARNING: Failed to load private key from env var: {e}")

        # Mode 2: file path (local)
        if not KALSHI_PRIVATE_KEY_PATH:
            print("  WARNING: No KALSHI_PRIVATE_KEY_B64, KALSHI_PRIVATE_KEY, or PATH set — trading disabled")
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
        max_pages = 50
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
        price_cents: limit price in cents (1-99) for the given side
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
            'client_order_id': str(uuid.uuid4()),
        }
        # Use the appropriate price field for the side
        if side == 'yes':
            body['yes_price'] = price_cents
        else:
            body['no_price'] = price_cents
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

            if fade_action == 'SELL':
                sl_price = round(p_end * (1 + abs(STOP_LOSS_PCT) / 100), 4)
            else:
                sl_price = round(p_end * (1 - abs(STOP_LOSS_PCT) / 100), 4)

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
                'stop_loss_price': sl_price,
            })

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
    Returns (contracts, price_cents) or (0, 0) if too thin.

    Kalshi orderbook format: {'yes': [[price, qty], ...], 'no': [[price, qty], ...]}
    Both 'yes' and 'no' are BIDS (buy orders), NOT asks.

    To BUY NO, we need NO sellers. A YES bid at price P is equivalent to
    a NO ask at (100 - P). So we derive NO asks from YES bids.
    """
    # YES bids = people wanting to buy YES = they'll sell NO at (100 - price)
    yes_bids = orderbook.get('yes', [])
    if isinstance(yes_bids, dict):
        yes_bids = yes_bids.get('bids', yes_bids.get('asks', []))

    if not yes_bids or not isinstance(yes_bids, list):
        return 0, 0

    # Convert YES bids to implied NO asks: NO ask price = 100 - YES bid price
    # Higher YES bids = lower (better) NO ask prices, so sort ascending by NO price
    no_asks = sorted(
        [[100 - b[0], b[1]] for b in yes_bids if isinstance(b, (list, tuple)) and len(b) >= 2],
        key=lambda x: x[0]
    )

    if not no_asks:
        return 0, 0

    # Take top 3 cheapest NO ask levels
    top_levels = no_asks[:3]
    best_ask_cents = top_levels[0][0]

    if best_ask_cents <= 0 or best_ask_cents >= 100:
        return 0, 0

    # Depth in dollars across top 3 levels
    depth_dollars = sum(level[0] * level[1] / 100 for level in top_levels)

    # Our bet = DEPTH_FRACTION of depth, capped
    bet_dollars = min(depth_dollars * DEPTH_FRACTION, MAX_BET_DOLLARS)
    if bet_dollars < MIN_BET_DOLLARS:
        return 0, 0

    # Convert to contracts at the best ask price
    contracts = int(bet_dollars / (best_ask_cents / 100))
    if contracts < 1:
        return 0, 0

    return contracts, best_ask_cents


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

    def _save(self):
        with open(POSITIONS_FILE, 'w') as f:
            json.dump({'open': self.positions, 'closed': self.closed[-100:]}, f, indent=2)

    def add(self, signal, order_info=None):
        pos = {
            'ticker': signal['ticker'],
            'title': signal['title'],
            'fade_action': signal['fade_action'],
            'fade_side': signal['fade_side'],
            'entry_price': signal['entry_price'],
            'pre_signal_price': signal['pre_signal_price'],
            'price_move': signal['price_move'],
            'n_small_trades': signal['n_small_trades'],
            'retail_contracts': signal['retail_contracts'],
            'entry_time': signal['signal_time'],
            'exit_time': signal['signal_time'] + HOLD_HOURS * 3600,
            'stop_loss_price': signal['stop_loss_price'],
            'status': 'open',
        }
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

    def check(self, client):
        """Check for stop-loss or 24h expiry. Returns alerts list."""
        now = time.time()
        alerts = []
        still_open = []

        for pos in self.positions:
            if pos['status'] != 'open':
                continue

            # Force-refresh the market cache for current price
            client.market_cache.pop(pos['ticker'], None)
            current = client.get_current_price(pos['ticker'])
            roi = self._roi(pos, current)

            # 24h exit
            if now >= pos['exit_time']:
                pos['exit_price'] = current
                pos['roi_pct'] = roi
                pos['status'] = 'closed_24h'
                pos['close_time'] = now
                self.closed.append(pos)
                alerts.append(('24h_exit', pos))
                continue

            # Stop-loss
            if roi is not None and roi <= STOP_LOSS_PCT:
                pos['exit_price'] = current
                pos['roi_pct'] = roi
                pos['status'] = 'stopped_out'
                pos['close_time'] = now
                self.closed.append(pos)
                alerts.append(('stop_loss', pos))
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

    def count(self):
        return len(self.positions)

    def live_count(self):
        return sum(1 for p in self.positions if p.get('is_live'))


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
        """
        ticker = signal['ticker']
        entry_cents = int(signal['entry_price'] * 100)

        # Fetch orderbook
        orderbook = self.client.get_orderbook(ticker)
        if not orderbook:
            print(f"    No orderbook for {ticker}, skipping")
            return None

        # Calculate bet size from depth
        contracts, best_ask_cents = calculate_bet_size(orderbook, 'no', entry_cents)
        if contracts < 1:
            print(f"    Book too thin for {ticker} (min ${MIN_BET_DOLLARS}), skipping")
            return None

        bet_dollars = round(contracts * best_ask_cents / 100, 2)
        no_price_cents = 100 - entry_cents  # NO price = 100 - YES price

        # Sanity check: NO ask should be in reasonable range
        if best_ask_cents < 2 or best_ask_cents > 98:
            print(f"    NO ask {best_ask_cents}c out of range, likely orderbook error, skipping")
            return None

        # Slippage check: best available NO ask vs signal-implied NO price
        # Positive = worse (paying more), negative = better (paying less)
        slippage_pct = (best_ask_cents - no_price_cents) / no_price_cents * 100 if no_price_cents > 0 else 0
        if slippage_pct > MAX_SLIPPAGE_PCT:
            print(f"    SLIPPAGE: best NO ask {best_ask_cents}c vs signal {no_price_cents}c "
                  f"({slippage_pct:+.1f}% > {MAX_SLIPPAGE_PCT}%), skipping")
            return None

        print(f"    Sizing: {contracts} NO @ {best_ask_cents}c (signal {no_price_cents}c, slip {slippage_pct:+.1f}%) = ${bet_dollars:.2f}")

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
                'side': 'no',
                'action': 'buy',
                'contracts': contracts,
                'price_cents': no_price_cents,
                'bet_dollars': bet_dollars,
                'dry_run': True,
                'signal': {k: v for k, v in signal.items() if k != 'title'},
            })
            print(f"    DRY RUN: would buy {contracts} NO @ {no_price_cents}c (${bet_dollars:.2f})")
            return order_info

        # Max price we'll pay: signal NO price + 1% slippage
        max_no_price = int(no_price_cents * (1 + MAX_SLIPPAGE_PCT / 100))

        # Live order with retries
        for attempt in range(MAX_ORDER_RETRIES + 1):
            price = best_ask_cents + attempt  # Start at best ask, bump 1c each retry
            if price > max_no_price:
                print(f"    Price {price}c exceeds max {max_no_price}c (1% slip), stopping")
                break
            if price >= 99:
                break

            order = self.client.create_order(
                ticker=ticker,
                side='no',
                action='buy',
                count=contracts,
                price_cents=price,
            )
            if not order:
                print(f"    Order failed (attempt {attempt + 1})")
                continue

            order_id = order.get('order_id', '')
            print(f"    Order placed: {order_id} ({contracts} NO @ {price}c)")

            # Wait for fill
            time.sleep(ORDER_WAIT_SECONDS)

            # Check fill status
            status = self.client.get_order(order_id)
            if status:
                filled = status.get('quantity_filled', 0)
                remaining = status.get('remaining_count', contracts)
                order_status = status.get('status', '')

                if filled > 0:
                    avg_fill = status.get('average_fill_price', price)
                    fill_slip = (avg_fill - no_price_cents) / no_price_cents * 100 if no_price_cents > 0 else 0
                    actual_dollars = round(filled * avg_fill / 100, 2)
                    order_info = {
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
                        'side': 'no',
                        'action': 'buy',
                        'contracts_requested': contracts,
                        'contracts_filled': filled,
                        'price_cents': price,
                        'avg_fill_price': avg_fill,
                        'signal_no_price': no_price_cents,
                        'slippage_pct': round(fill_slip, 2),
                        'bet_dollars': actual_dollars,
                        'dry_run': False,
                    })
                    # Cancel remainder if partial fill
                    if remaining > 0:
                        self.client.cancel_order(order_id)
                    print(f"    FILLED: {filled}/{contracts} NO @ avg {avg_fill}c "
                          f"(slip {fill_slip:+.1f}%, ${actual_dollars:.2f})")
                    return order_info
                else:
                    # Not filled, cancel and retry at higher price
                    self.client.cancel_order(order_id)
                    print(f"    Not filled at {price}c, retrying...")

        print(f"    Failed to fill after {MAX_ORDER_RETRIES + 1} attempts")
        return None

    def execute_exit(self, pos):
        """
        Place exit order for a position. Returns actual exit info.
        For SELL positions: sell NO contracts back.
        """
        ticker = pos['ticker']
        contracts = pos.get('fill_count', 0)

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
            print(f"    DRY RUN: would sell {contracts} NO (P&L: ${pnl:.2f})")
            return {'exit_price': current, 'pnl': round(pnl, 2)}

        # Live exit: sell NO contracts aggressively (1c below best NO bid)
        # To sell NO, we look at NO bids (people wanting to buy NO from us)
        orderbook = self.client.get_orderbook(ticker)
        no_bids = orderbook.get('no', []) if orderbook else []
        if isinstance(no_bids, dict):
            no_bids = no_bids.get('bids', no_bids.get('asks', []))

        sell_price = None
        if no_bids and isinstance(no_bids, list):
            valid_bids = [b[0] for b in no_bids if isinstance(b, (list, tuple)) and len(b) >= 2]
            if valid_bids:
                best_no_bid = max(valid_bids)
                sell_price = max(1, best_no_bid - 1)  # 1c below for fast fill

        if sell_price is None:
            # Fallback: use current market price
            current = self.client.get_current_price(ticker)
            if current:
                sell_price = max(1, int((1 - current) * 100) - 1)
            else:
                sell_price = 1  # Fire sale

        order = self.client.create_order(
            ticker=ticker,
            side='no',
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
            print(f"    EXIT FILLED: {filled}/{contracts} NO @ {avg_fill}c (P&L: ${pnl:.2f})")
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
        sl_cents = int(sig['stop_loss_price'] * 100)
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
            f"Stop-loss: {sl_cents}c ({STOP_LOSS_PCT:.0f}%)\n"
            f"Exit: {exit_time} (24h hold)\n\n"
            f"Why: {sig['n_small_trades']} small trades {move_dir} "
            f"by {abs(sig['price_move'])*100:.0f}c in 1hr "
            f"({sig['retail_contracts']:,} contracts). Fading the crowd.\n\n"
            f"Backtest (60d): 58% WR, +27% avg ROI"
            f"{trade_line}"
            f"{url}"
        )
        await self._send(msg)

    async def send_stop_loss(self, pos, exit_info=None):
        held = (pos['close_time'] - pos['entry_time']) / 3600
        exit_price = pos.get('exit_price', 0) or 0
        entry_cents = int(pos['entry_price'] * 100)
        exit_cents = int(exit_price * 100) if exit_price else 0

        if pos['fade_action'] == 'SELL':
            close = f"SELL your NO position (or buy back YES at {exit_cents}c)"
        else:
            close = f"SELL your YES at {exit_cents}c"

        pnl_line = ""
        if exit_info and 'pnl' in exit_info:
            pnl_line = f"\nActual P&L: ${exit_info['pnl']:.2f}"

        msg = (
            f"STOP-LOSS HIT (-30%)\n\n"
            f"{pos['title']}\n"
            f"Ticker: {pos['ticker']}\n\n"
            f"CLOSE NOW: {close}\n\n"
            f"Entry: {entry_cents}c -> Now: {exit_cents}c\n"
            f"Loss: {pos.get('roi_pct', 0):+.1f}%{pnl_line}\n"
            f"Held: {held:.1f}h"
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
            f"Strategy: SELL-only, fade retail buying surges\n"
            f"Params: 12-40 small trades, 15c+ move, 21-80c\n"
            f"Categories: No sports/crypto/financials\n"
            f"Hold: 24h, -30% stop-loss\n"
            f"Max bet: ${MAX_BET_DOLLARS}/signal\n"
            f"Max positions: {MAX_OPEN_POSITIONS}\n"
            f"Daily loss limit: ${MAX_DAILY_LOSS}\n"
            f"{bal_line}"
            f"Backtest (60d): 58% WR, +27% avg ROI\n\n"
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
        self.positions = KalshiPositionTracker()
        self.notifier = KalshiNotifier()
        self.trade_logger = TradeLogger()
        self.executor = OrderExecutor(self.client, self.trade_logger)

    async def run(self):
        mode = "DRY RUN" if DRY_RUN else "LIVE"
        print("=" * 60)
        print(f"KALSHI AUTO-TRADING BOT [{mode}]")
        print("=" * 60)
        print(f"Telegram: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
        print(f"Auth: {'OK' if self.client.can_trade else 'MISSING (signal-only mode)'}")
        print(f"Strategy: Fade retail surges, 24h hold, -30% SL")
        print(f"Max bet: ${MAX_BET_DOLLARS}/signal, Max positions: {MAX_OPEN_POSITIONS}")
        print(f"Daily loss limit: ${MAX_DAILY_LOSS}")
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

        # Safety: check daily loss circuit breaker
        daily_pnl = self.trade_logger.daily_pnl()
        if daily_pnl < -MAX_DAILY_LOSS:
            print(f"  CIRCUIT BREAKER: Daily loss ${daily_pnl:.2f} exceeds -${MAX_DAILY_LOSS}. No new orders.")
            trading_allowed = False
        else:
            trading_allowed = True

        # Safety: check max positions
        if self.positions.count() >= MAX_OPEN_POSITIONS:
            print(f"  MAX POSITIONS: {self.positions.count()}/{MAX_OPEN_POSITIONS}. No new orders.")
            trading_allowed = False

        # 1. Fetch last 65 min of trades (extra 5min buffer)
        trades = self.client.get_all_recent_trades(since_minutes=65)
        print(f"  Trades fetched: {len(trades)}")

        if trades:
            tickers = set(t.get('ticker', '') for t in trades)
            allowed = [t for t in tickers if self.client.is_allowed_ticker(t)]
            print(f"  Unique markets: {len(tickers)}, allowed: {len(allowed)}")

            # 2. Detect signals
            signals = self.detector.detect(trades, self.client, now)
            print(f"  Signals: {len(signals)}")

            for sig in signals:
                entry_c = int(sig['entry_price'] * 100)
                print(f"  SIGNAL: {sig['fade_action']} '{sig['title'][:50]}' @ {entry_c}c "
                      f"(move {sig['price_move']:+.3f}, {sig['n_small_trades']} trades)")

                order_info = None
                if trading_allowed and self.client.can_trade:
                    order_info = self.executor.execute_entry(sig)

                await self.notifier.send_signal(sig, order_info)
                self.positions.add(sig, order_info)

        # 3. Check positions for stop-loss / 24h exit
        alerts = self.positions.check(self.client)
        for atype, pos in alerts:
            exit_info = None
            if pos.get('is_live') and self.client.can_trade:
                exit_info = self.executor.execute_exit(pos)

            if atype == 'stop_loss':
                print(f"  STOP-LOSS: '{pos['title'][:50]}' ROI: {pos.get('roi_pct',0):+.1f}%")
                await self.notifier.send_stop_loss(pos, exit_info)
            elif atype == '24h_exit':
                print(f"  24h EXIT: '{pos['title'][:50]}' ROI: {pos.get('roi_pct',0):+.1f}%")
                await self.notifier.send_24h_exit(pos, exit_info)

        print(f"  Open positions: {self.positions.count()} ({self.positions.live_count()} live)")
        if daily_pnl != 0:
            print(f"  Daily P&L: ${daily_pnl:.2f}")


if __name__ == "__main__":
    scanner = KalshiReversionScanner()
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        print("\nStopped.")
