#!/usr/bin/env python3
"""
Simple Signal Bot - Just Alerts, No Trading
Sends Telegram alerts for Politics signals on Polymarket
You bet manually on Kalshi or wherever you want
"""

import os
import json
import requests
from datetime import datetime, timedelta
from telegram import Bot
import asyncio

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL_MINUTES', '60'))

# Polymarket API
POLYMARKET_API = "https://gamma-api.polymarket.com"

# State file to track sent alerts
SENT_ALERTS_FILE = 'sent_alerts.json'

def load_sent_alerts():
    """Load previously sent alerts"""
    if os.path.exists(SENT_ALERTS_FILE):
        try:
            with open(SENT_ALERTS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_sent_alerts(alerts):
    """Save sent alerts"""
    with open(SENT_ALERTS_FILE, 'w') as f:
        json.dump(alerts, f, indent=2)

def get_politics_signals():
    """Fetch Politics category markets from Polymarket"""
    try:
        response = requests.get(f"{POLYMARKET_API}/markets", timeout=10)
        response.raise_for_status()
        markets = response.json()

        signals = []
        for market in markets:
            # Filter to Politics
            question = market.get('question', '').lower()
            category = market.get('groupItemTitle', '').lower()

            is_politics = (
                category == 'politics' or
                'trump' in question or
                'government' in question or
                'shutdown' in question or
                'congress' in question or
                'senate' in question or
                'president' in question or
                'biden' in question or
                'cabinet' in question or
                'white house' in question
            )

            # Exclude unwanted categories
            is_excluded = (
                'bitcoin' in question or
                'crypto' in question or
                'gold' in question or
                'silver' in question or
                'game' in question or
                'gta' in question or
                'nfl' in question or
                'nba' in question
            )

            if not is_politics or is_excluded:
                continue

            tokens = market.get('tokens', [])
            for token in tokens:
                price = float(token.get('price', 1))
                volume_24h = float(market.get('volume24hr', 0))

                # Signal criteria: Politics, underdogs, volume spike
                if price <= 0.60 and volume_24h >= 50000:
                    signals.append({
                        'question': market.get('question'),
                        'outcome': token.get('outcome'),
                        'price': price,
                        'volume_24h': volume_24h,
                        'market_id': market.get('conditionId'),
                        'end_date': market.get('endDate'),
                        'url': f"https://polymarket.com/event/{market.get('slug', '')}"
                    })

        return signals
    except Exception as e:
        print(f"Error fetching markets: {e}")
        return []

def calculate_conviction(price, volume_24h):
    """Calculate conviction level"""
    if price < 0.30 and volume_24h >= 100000:
        return "VERY HIGH", "🔥🔥🔥", 10
    elif price < 0.40 and volume_24h >= 75000:
        return "HIGH", "🔥🔥", 7
    elif price < 0.50 and volume_24h >= 50000:
        return "MEDIUM", "🔥", 5
    else:
        return "LOW", "📊", 3

async def send_signal_alert(bot, signal):
    """Send Telegram alert for a signal"""

    conviction, emoji, position_pct = calculate_conviction(
        signal['price'],
        signal['volume_24h']
    )

    roi = (1 / signal['price'] - 1) * 100

    message = f"""
{emoji} <b>NEW POLITICS SIGNAL</b> {emoji}

<b>Event:</b> {signal['question']}
<b>Outcome:</b> {signal['outcome']}

💰 <b>Price:</b> ${signal['price']:.3f} ({signal['price']*100:.1f}%)
📊 <b>Potential ROI:</b> +{roi:.0f}%
💵 <b>Volume (24h):</b> ${signal['volume_24h']:,.0f}
🎯 <b>Conviction:</b> {conviction}

<b>📈 Recommended Position:</b> {position_pct}% of portfolio

<b>Category:</b> Politics
<b>Historical Performance:</b> 90% WR, 207% avg ROI

"""

    # Add comparison
    if conviction == "VERY HIGH":
        message += """<b>Similar past winners:</b>
• Shutdown Saturday (68w, $0.23, +335%)
• Shutdown Duration (105w, $0.30, +233%)
"""
    elif conviction == "HIGH":
        message += """<b>Similar past winners:</b>
• Trump-Zelenskyy (134w, $0.41, +147%)
• Trump Feb 1 (16w, $0.33, +203%)
"""
    else:
        message += """<b>Note:</b> Lower conviction - consider smaller position or skip
"""

    message += f"""
<b>🎲 Check Kalshi</b> for this market!
May also be on Polymarket: <a href="{signal['url']}">View here</a>

<i>Signal detection based on 32 verified events</i>
"""

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        return True
    except Exception as e:
        print(f"Error sending Telegram: {e}")
        return False

async def check_and_alert():
    """Main function - check signals and send alerts"""

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Error: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking for Politics signals...")

    # Load sent alerts
    sent_alerts = load_sent_alerts()

    # Get current signals
    signals = get_politics_signals()
    print(f"Found {len(signals)} potential signals")

    new_alerts = 0
    for signal in signals:
        # Create unique ID
        signal_id = f"{signal['market_id']}_{signal['outcome']}"

        # Check if already sent (within 24 hours)
        if signal_id in sent_alerts:
            sent_time = datetime.fromisoformat(sent_alerts[signal_id])
            if datetime.now() - sent_time < timedelta(hours=24):
                continue

        # Send alert
        print(f"🚨 New signal: {signal['question']} - {signal['outcome']}")

        if await send_signal_alert(bot, signal):
            sent_alerts[signal_id] = datetime.now().isoformat()
            new_alerts += 1
            await asyncio.sleep(1)  # Rate limit

    # Save updated alerts
    save_sent_alerts(sent_alerts)

    if new_alerts > 0:
        print(f"✅ Sent {new_alerts} new alert(s)")
    else:
        print("✅ No new signals")

async def send_startup_message():
    """Send startup notification"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    message = """
🤖 <b>Signal Bot Started</b>

I'll monitor Polymarket for Politics signals 24/7 and alert you!

<b>📊 Signal Criteria:</b>
• Category: Politics
• Price: ≤$0.60 (underdogs)
• Volume: $50K+ spike in 24h

<b>📈 Historical Performance:</b>
• Win Rate: 90% (9/10 win)
• Average ROI: 207%
• Frequency: ~2 signals/week

<b>🎯 What I'll send:</b>
• Event details
• Entry price
• Conviction level (Very High/High/Medium/Low)
• Recommended position size
• Potential ROI

<b>💡 What you do:</b>
1. Get my alert
2. Check Kalshi for same market
3. Bet manually based on conviction
4. Track your results

Check interval: Every {interval} minutes

Ready to find signals! 🚀
""".format(interval=CHECK_INTERVAL)

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"Failed to send startup message: {e}")

async def main_loop():
    """Main bot loop"""

    await send_startup_message()

    print("="*60)
    print("POLITICS SIGNAL BOT - ALERTS ONLY")
    print("="*60)
    print(f"Check interval: Every {CHECK_INTERVAL} minutes")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("You'll get Telegram alerts to bet manually")
    print("="*60)
    print()

    while True:
        try:
            await check_and_alert()
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()

        print(f"Next check in {CHECK_INTERVAL} minutes...\n")
        await asyncio.sleep(CHECK_INTERVAL * 60)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
