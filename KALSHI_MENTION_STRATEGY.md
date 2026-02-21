# Kalshi Mention Market Strategy — Full Documentation

## Strategy Overview

**Edge:** "What will X say during Y?" markets systematically overprice YES outcomes. People expect mentions that don't happen. We BUY NO cheaply and hold to settlement.

**Action:** BUY NO on mention markets. Trump: 0-24h before event. NCAA: live games only (0-2h after start, 5-25c). All others: 0-1.5h before event.

---

## Parameters

| Parameter | Value |
|---|---|
| Side | BUY NO |
| NO price range | 5-30c default; NCAA live: 5-25c |
| Entry window | Trump: 0-24h before event; NCAA: 0-2h after event start (live only); All others: 0-1.5h before event (enforced via milestones API) |
| Bet size (current) | $3/bet flat |
| Max per event | $10 (spread across tickers) |
| Max concurrent positions | 40 |
| Max resting orders | 10 |
| Order rest time | 10 min (cancel if unfilled) |
| Cooldown | 24h per ticker |
| Hold | Until settlement (no early exit) |
| Scan interval | Every 2 min (mention), every 5 min (reversion) |

---

## Included Series

Bot dynamically discovers mention series via the Kalshi API (any series with "MENTION" in ticker or mention-type keywords in title). Categories are then filtered by the exclusion list below.

| Category | Series | Backtest ROI (0-1.5h pre-event, 5-30c) | t-stat |
|---|---|---|---|
| **NFL** | KXNFLMENTION, KXSNFMENTION, KXTNFMENTION, KXCFBMENTION, KXSBMENTION | +80% | sig |
| **NCAA** | KXNCAAMENTION, KXNCAABMENTION | +114% live 0-2h, 5-25c (232 mkts) | +4.40 |
| **MLB** | KXMLBMENTION | +34.7% | limited data |
| **Trump** | KXTRUMPMENTION, KXTRUMPMENTIONB | +88% at 0-24h (761 mkts) | +8.15 |
| **Governor** | KXGOVERNORMENTION, KXHOCHULMENTION | +55% | sig |
| **Mamdani** | KXMAMDANIMENTION | +14.6% (test) | sig |
| **Media** | KXMADDOWMENTION, KXSNLMENTION, KXROGANMENTION, KXCOOPERMENTION, KXCOLBERTMENTION, KXKIMMELMENTION | Maddow +175% | sig |
| **Other** | KXVANCEMENTION + any new series discovered dynamically | varies | |

### Dynamically Discovered Series (live bot picks these up automatically)
KXPSAKIMENTION, KXMELANIAMENTION, KXFOXNEWSMENTION, KXNEWSOMMENTION, KXBERNIEMENTION, KXBESSENTMTPMENTION, KXPOLITICSMENTION, KXNYCMAYORDEBATEMENTION, KXWOMENTION, KXSNOOPMENTION, and others as Kalshi adds them.

## Excluded Categories

| Category | Reason |
|---|---|
| **Earnings** | No event milestones available, can't time entry |
| **Fight/UFC** | No milestones, tiny sample (9 markets in 90 days), thin edge |
| **SEC Press / Leavitt** | Press briefings efficiently priced (+2% ROI) |
| **NBA** | Pre-event edge not significant (t=1.43, small N). Strong edge during live games (+92% ROI 1-2h after tipoff, t=5.16) but small N. Revisit with live-game timing. |

---

## Backtest Results

### All Categories Combined (Last 3 Months: Nov 2025 - Feb 2026)

**Headline Numbers:**
- **1,506 markets**, 28.4% win rate
- At $25/bet flat: **+$11,871 PnL, +36.8% ROI, $129/day**
- At $50/bet flat: **+$20,681 PnL, +35.5% ROI, $225/day**
- At $100/bet flat: **+$32,418 PnL, +32.3% ROI, $352/day**

### Scaling Analysis (bet = min(liquidity, cap), no floor)

| Cap | $/week | $/month | ROI |
|---|---|---|---|
| $25 | $903 | $3,871 | +36.8% |
| $50 | $1,574 | $6,744 | +35.5% |
| $75 | $2,071 | $8,877 | +33.8% |
| $100 | $2,467 | $10,571 | +32.3% |
| $150 | $3,166 | $13,569 | +31.2% |
| $200 | $3,731 | $15,991 | +30.4% |
| $500 | $6,123 | $26,243 | +31.1% |

### Monthly Breakdown (cap $100)

| Month | Markets | WR | PnL | ROI | $/day |
|---|---|---|---|---|---|
| 2025-11 | 111 | 26.1% | +$960 | +14.4% | $120 |
| 2025-12 | 373 | 23.3% | +$8,786 | +33.5% | $303 |
| 2026-01 | 643 | 30.8% | +$16,897 | +36.6% | $603 |
| 2026-02 | 379 | 29.8% | +$5,776 | +27.2% | $321 |

### Edge Trend (Monthly ROI, all-time)
Edge is **increasing** over time — more mention series being added by Kalshi:
- Mar 2025: -39%
- Jun 2025: +28%
- Sep 2025: +52%
- Dec 2025: +68%
- Jan 2026: +109%
- Rolling 3-month ROI trending up: 59% -> 68% -> 79%

### NCAA Backtest — Live Games (0-2h after event start, per-market, last 90 days)

| NO Price Bucket | N | WR% | ROI% | t-stat |
|---|---|---|---|---|
| 3-5c | 66 | 6.1% | +49% | +0.66 |
| 6-8c | 50 | 20.0% | +162% | +2.13 |
| 9-11c | 22 | 31.8% | +210% | +2.12 |
| 12-15c | 39 | 23.1% | +66% | +1.37 |
| 16-20c | 60 | 33.3% | +76% | +2.37 |
| 21-25c | 42 | 47.6% | +99% | +3.04 |
| 26-30c | 62 | 37.1% | +33% | +1.50 |
| **5-25c (bot range)** | **232** | **29.3%** | **+114%** | **+4.40** |

67 unique NCAA events. Edge is much stronger during live games than pre-event.

### NCAA Time Window Comparison (5-30c NO)

| Window | N | WR% | ROI% | t-stat |
|---|---|---|---|---|
| 3h to 2h before | 254 | 29.5% | +99% | +4.04 |
| 2h to 1h before | 283 | 30.0% | +104% | +4.34 |
| 1h to start | 337 | 28.2% | +79% | +3.98 |
| **Live 0-1h after** | **661** | **35.9%** | **+124%** | **+8.83** |
| **Live 1-2h after** | **413** | **44.1%** | **+153%** | **+9.23** |
| Live 2-3h after | 225 | 50.7% | +199% | +7.92 |

Pre-event edge is real but live-game edge is 2-3x stronger with higher statistical significance.

### NBA Analysis
- **Pre-event (0-1.5h before tipoff):** 5-30c +15.4%, t=1.43 — NOT significant
- **During game (0-1h after tipoff):** 5-30c +34.1%, t=3.08
- **During game (1-2h after tipoff):** 5-30c +91.8%, t=5.16
- **During game (2-3h after tipoff):** 5-30c +142.9%, t=4.21
- Edge increases deeper into the game, but sample sizes are small
- Currently excluded; revisit with live-game timing window

### Theories Tested and Rejected
- **Earnings mentions:** -12.1% ROI at 5-30c, t=-0.77 (noise)
- **Pre-event momentum (6h->1h):** Moves are smart money, not fadeable. Following also thin (+7-10% ROI)
- **Pre-event momentum (3h->1h):** Same result, no alpha
- **Golf surge fade (Fri/Sat):** No edge with full 7,557 market dataset. Markets efficiently priced
- **Golf surge follow:** Also negative. No edge in either direction
- **Fight mentions:** No milestones, only 9 markets at 5-30c in 90 days, not backtestable

### Market Liquidity Distribution
Median market has **$72** of NO-side volume in the 1h pre-event window (NO 5-30c).

| Bet size | % of markets fillable |
|---|---|
| $2 | 94% |
| $25 | 72% |
| $50 | 59% |
| $100 | 42% |
| $200 | 25% |

~118 markets/week in recent months.

---

## Live Bot Results (as of 2026-02-21)

### Bot Trades — Strategy Parameters Only (<$4 cost, 5-30c NO)
- **30 settled trades**, 13W/17L, 43% WR
- **PnL: +$44.08, +83.3% ROI**
- Running at $3/bet flat (bumped from $2 on 2026-02-21)

### All Bot Trades (including bugs/out-of-range)
- 78 total settled mention trades, 30W/48L
- PnL: -$270 (dominated by buggy oversized bets now fixed)
- Bugs fixed: buy_price +1 cap, dynamic discovery betting on unvetted series at wrong sizes

### Account
- Balance: ~$70.50
- 68 open mention positions, ~$133 exposure

### Weather Trades (manual, NOT bot strategy)
- 42 settled, 2W/40L, **-$462.74, -93% ROI**
- These are manual bets, not the bot strategy. Weather markets are efficiently priced.

---

## Deployment

### Railway
- **Project:** `attractive-courage` (ID: `778ad27c-ba00-4aa2-96ba-ae31db2155d8`)
- **Service:** `polymarket-signal-bot` (ID: `404236e1-9e40-45c8-8b98-229770d34bed`)
- **Environment:** `production`

### Environment Variables
| Var | Status |
|---|---|
| `KALSHI_API_KEY_ID` | SET |
| `KALSHI_PRIVATE_KEY_B64` | SET (2237 chars, RSA key) |
| `TELEGRAM_BOT_TOKEN` | SET |
| `TELEGRAM_CHAT_ID` | SET |
| `DRY_RUN` | `true` (artifact — bot IS live, placing real orders) |

### Key Files
| File | Purpose |
|---|---|
| `realtime_scanner/kalshi_reversion_scanner.py` | Main bot — both strategies (reversion + mention) |
| `mention_markets_cache.json` | Cached market metadata for backtests |
| `mention_trade_cache.json` | Cached trade data for backtests |
| `mention_milestones_cache.json` | Event start times for backtests + bot timing filter |
| `backtest_mention_buy_no.py` | Main BUY NO backtest with train/test split |
| `backtest_nba_mentions.py` | NBA-specific price bucket analysis |

### Bot Architecture
- **Strategy 1 (Reversion):** Fade retail surges, $3 max, 24h hold
- **Strategy 2 (Mention):** BUY NO, $3/bet, per-category timing + price, ex Earnings/Fight/Press/NBA
- **Event start timing:** Milestones fetched from Kalshi API (cached 10 min). Per-category: Trump 0-24h pre-event 5-30c, NCAA live 0-2h after start 5-25c, others 0-1.5h pre-event 5-30c. Markets without milestones are skipped.
- **Dynamic series discovery:** Bot discovers new mention series automatically via API. Excluded categories are filtered after discovery.
- Limit orders rest on book, canceled after **10 minutes** if unfilled
- Duplicate trade protection: seeds cooldown from API positions on startup (survives redeploys)
- Auth: RSA-PSS signing with SHA256

### Railway CLI Commands
```bash
# Login
railway login

# Link project (one-time)
railway link -p 778ad27c-ba00-4aa2-96ba-ae31db2155d8

# Set service
railway service polymarket-signal-bot

# Pull logs
railway logs -n 100

# Get env vars
railway variables --json

# Deploy (push to Railway)
railway up
```

### Kalshi API Endpoints Used
```
GET /trade-api/v2/portfolio/fills        — trade history
GET /trade-api/v2/portfolio/settlements  — settled positions
GET /trade-api/v2/portfolio/positions    — open positions
GET /trade-api/v2/portfolio/balance      — account balance
GET /trade-api/v2/events?with_milestones=true — event start times
```

---

## Key Findings & Decisions

### Why BUY NO works on mention markets
- People overestimate that specific words will be said during events
- Cheap NO (5-30c) means YES is priced 70-95c — very overconfident
- ~28% of the time YES hits, but you only pay 5-30c for NO, so the math works

### Entry window per category
- **Trump (0-24h):** Edge persists across entire 24h pre-event window. 0-24h: +88% ROI (t=8.15, 761 mkts). Edge is strongest 8-12h out (+88% ROI) and stays positive through 24h. Marginal ROI positive at every layer. Reason: Trump markets are listed well in advance and NO stays mispriced for longer. At 18-24h, 79% of markets have zero trades — the ones that do trade early are popular tickers with strong edge.
- **NCAA (live 0-2h after start, 5-25c):** Pre-event edge exists (+79-104% ROI) but live-game edge is far stronger: +114% ROI (t=4.40) at 5-25c. WR climbs from 29% to 44% deeper into the game. Edge dies above 25c during live games. Switched to live-only on 2026-02-21.
- **All others (0-1.5h):** Backtest showed 0-1.5h pre-event has strong ROI across non-Trump categories. Earlier entry on governor/media categories doesn't add significant edge.
- Enforced via milestones API — bot fetches event_start and applies per-category window

### Why exclude Earnings/Fight/Press/NBA
- **Earnings:** No event milestone data on Kalshi API, can't time entry
- **Fight/UFC:** No milestones, tiny sample (9 markets at 5-30c in 90 days)
- **SEC Press / Leavitt:** Press briefings efficiently priced (+2% ROI)
- **NBA:** Pre-event not significant (t=1.43). Live-game edge is strong but small N. Revisit later.

### NCAA pricing
- Tested special NCAA floors (10c, 20c) and ranges (20-69c)
- Pre-event 5-30c: +74% ROI (t=4.48), but live 5-25c is better: +114% ROI (t=4.40)
- Dropping 5c floor to 6c barely changes ROI (+114.0% vs +113.7%), not worth it
- Edge dies above 25c during live games (26-30c only +33% ROI)
- 67 unique events in 90 days, ~3-4 eligible tickers per game at 5-25c live
- Kalshi only creates mention markets for nationally-televised/high-profile games

### Category performance (per-category timing windows)
- **NCAA (live 0-2h, 5-25c):** 232 markets, +114% ROI, t=4.40
- **Trump (0-24h, 5-30c):** Highest volume, +88% ROI, t=8.15
- **Governor/Political (0-1.5h, 5-30c):** +55% ROI, lower volume
- **Maddow (0-1.5h, 5-30c):** +175% ROI, small sample

### Liquidity-scaled sizing
- At flat $3, every market gets equal weight — optimal for ROI
- Scaling to liquidity over-allocates to efficiently-priced markets
- Marginal ROI drops from 37% (first $25) to ~27% ($100-200 layer) — still positive

### Conservative income estimate
- **$50 cap:** ~$6,700/month backtest, ~$4,000-4,700 after 30-40% friction haircut
- **$75 cap:** ~$8,900/month backtest, ~$5,300+ after haircut
- **$5k/month is achievable at $75 cap** with comfortable margin for slow months

### Polymarket
- Does NOT have mention markets — this strategy is Kalshi-specific

---

## Scaling Plan

Bot is LIVE at $3/bet. Gradual scaling based on settled trade count:

| Milestone | Bet Size | Capital Needed | Expected $/month |
|---|---|---|---|
| Now (26 settled) | $3/bet | ~$150 | ~$800 |
| 100 settled | $5/bet | $625 | ~$1,350 |
| 200+ settled | $10/bet | $1,250 | ~$2,700 |
| Confidence established | $25-50/bet | $3,000-6,200 | ~$5,700-10,000 |

Statistical significance: need ~105 trades for 95% confidence.

Capital math: ~68 concurrent positions x bet size x 1.3 buffer.

## Bugs Fixed
1. **Buy price +1 cap:** `buy_price = min(best_no_ask + 1, 65)` pushed orders above 30c. Fixed to `min(best_no_ask, max_cents)` where max_cents = 30.
2. **NCAA 20-69c deployment:** Brief 7-min window where NCAA was set to 20-69c created out-of-range positions. Reverted to 5-30c.
3. **Dynamic discovery without filtering:** Bot discovered new series (Snoop, WO, Bernie, etc.) and bet on them at wrong sizes. Category exclusion filter now catches these.

## Next Steps
1. ~~Turn off DRY_RUN to go live~~ — DONE
2. ~~Fix duplicate trade bug~~ — DONE (API position seeding on startup)
3. ~~Remove NCAA price floor~~ — DONE (5-30c same as all categories)
4. ~~Add event_start timing filter~~ — DONE (0-1.5h pre-event via milestones API)
5. ~~Exclude NBA~~ — DONE (pre-event not significant)
6. ~~Bump to $3/bet~~ — DONE (26 settled, +114% ROI)
7. ~~Fix buy_price cap bug~~ — DONE
8. ~~Switch NCAA to live-game only (0-2h after start, 5-25c)~~ — DONE (+114% ROI vs +74% pre-event)
9. Hit 100 settled trades -> bump to $5/bet, deposit to ~$625
10. Validate at $5 -> bump to $10/bet, deposit to ~$1,250
11. Revisit NBA with live-game timing window (bet during game instead of before)
11. Optionally implement liquidity-scaled sizing
12. Monitor live fill rates — backtest assumes 100% fill, reality will be lower
13. Track Strategy 1 (reversion) performance once enough trades settle
14. Find additional signal types to stack toward $25k/month target
15. Explore market making on mention markets (top #3 mention trader made $33k/week)
