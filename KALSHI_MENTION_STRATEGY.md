# Kalshi Mention Market Strategy — Full Documentation

## Strategy Overview

**Edge:** "What will X say during Y?" markets systematically overprice YES outcomes. People expect mentions that don't happen. We BUY NO cheaply and hold to settlement.

**Action:** BUY NO on mention markets. Trump: 0-24h before event. NCAA: live games only (0.5-1.5h after start, 6-25c). NBA: live games only (0.5-2h after tipoff, 9-25c). All others: 0-1.5h before event.

---

## Parameters

| Parameter | Value |
|---|---|
| Side | BUY NO |
| NO price range | 5-30c default; NCAA live: 6-25c; NBA live: 9-25c |
| Entry window | Trump: 0-24h before event; NCAA: 0.5-1.5h after event start (live only); NBA: 0.5-2h after tipoff (live only); All others: 0-1.5h before event (enforced via milestones API) |
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
| **NCAA** | KXNCAAMENTION, KXNCAABMENTION | +151% live 0.5-1.5h, 6-25c (484 mkts, 67 events) | +9.13 |
| **NBA** | KXNBAMENTION | +113% live 0.5-2h, 9-25c (1,006 mkts, 132 events) | +11.79 |
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

### NCAA Backtest — Live Games (0.5-1.5h after event start, 6-25c, last 90 days)

| Window | N | Events | WR% | ROI% | t-stat |
|---|---|---|---|---|---|
| 0-0.5h | 474 | 67 | 33.3% | +121.2% | +7.49 |
| **0.5-1h (bot)** | **359** | **67** | **36.8%** | **+147.6%** | **+7.56** |
| **1-1.5h (bot)** | **257** | **64** | **36.6%** | **+136.6%** | **+6.28** |
| 1.5-2h | 218 | 65 | 43.1% | +176.7% | +7.20 |
| **0.5-1.5h (bot range)** | **484** | **67** | **37.4%** | **+151.0%** | **+9.13** |
| 0-2h (previous) | 749 | 67 | 36.3% | +145.5% | +11.04 |

67 unique NCAA events. Bot uses 0.5-1.5h after start, 6-25c. All half-hour slices are strongly profitable (t > 6).

### NBA Backtest — Live Games (0.5-2h after tipoff, 9-25c, last 90 days)

| Window | N | Events | WR% | ROI% | t-stat |
|---|---|---|---|---|---|
| 3h-2h before | 525 | 132 | 17.9% | +19.0% | +1.51 |
| 1h-start | 660 | 132 | 19.5% | +13.8% | +1.36 |
| **Live 0-1h** | **1,228** | **134** | **28.9%** | **+79.6%** | **+9.01** |
| **Live 0-2h** | **1,412** | **134** | **30.7%** | **+92.4%** | **+10.92** |
| **Live 1-2h** | **667** | **130** | **37.8%** | **+134.6%** | **+10.43** |
| Live 0-3h | 1,470 | 134 | 31.7% | +101.0% | +11.90 |

**NBA Live 0-2h — By Price Bucket (dedup at 9-25c, then bucket):**

| Bucket | N | WR% | ROI% | t-stat |
|---|---|---|---|---|
| 9-11c | 192 | 26.6% | +157.6% | +5.08 |
| 12-15c | 215 | 27.0% | +97.5% | +4.40 |
| 16-20c | 433 | 38.3% | +105.8% | +8.41 |
| 21-25c | 313 | 41.9% | +76.5% | +6.55 |
| **9-25c (bot range)** | **1,006** | **36.0%** | **+113.3%** | **+11.79** |

132 unique NBA events. Bot uses 0.5-2h after tipoff (skips first 30 min — edge is weaker in early game). 9-25c range: edge below 9c is noise, dies above 25c. Win/loss: avg win $5.00, avg loss $0.95, 5.24x ratio.

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
- **Strategy 2 (Mention):** BUY NO, $3/bet, per-category timing + price, ex Earnings/Fight/Press
- **Event start timing:** Milestones fetched from Kalshi API (cached 10 min). Per-category: Trump 0-24h pre-event 5-30c, NCAA live 0.5-1.5h after start 6-25c, NBA live 0.5-2h after tipoff 9-25c, others 0-1.5h pre-event 5-30c. Markets without milestones are skipped.
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
- **NCAA (live 0.5-1.5h after start, 6-25c):** Pre-event edge exists (+79-104% ROI) but live-game edge is far stronger: +151% ROI (t=9.13) at 6-25c. Every half-hour slice from 0-2h is profitable (t > 6), but 0.5-1.5h is the sweet spot. Switched to live-only on 2026-02-21, tightened to 0.5-1.5h same day.
- **NBA (live 0.5-2h after tipoff, 9-25c):** Pre-event not significant (t=1.43). Live-game edge is very strong: +113% ROI (t=11.79) at 9-25c, 1,006 markets across 132 events. First 30 min skipped (edge weaker in early game). Edge below 9c is noise, dies above 25c. Added live-game on 2026-02-21.
- **All others (0-1.5h):** Backtest showed 0-1.5h pre-event has strong ROI across non-Trump categories. Earlier entry on governor/media categories doesn't add significant edge.
- Enforced via milestones API — bot fetches event_start and applies per-category window

### Why exclude Earnings/Fight/Press
- **Earnings:** No event milestone data on Kalshi API, can't time entry
- **Fight/UFC:** No milestones, tiny sample (9 markets at 5-30c in 90 days)
- **SEC Press / Leavitt:** Press briefings efficiently priced (+2% ROI)

### NCAA pricing
- Tested special NCAA floors (10c, 20c) and ranges (20-69c)
- Pre-event 5-30c: +74% ROI (t=4.48), but live 0.5-1.5h 6-25c is better: +151% ROI (t=9.13)
- Bumped floor from 5c to 6c — marginally better ROI (+114.0% vs +113.7%) and avoids noise at 5c
- Edge dies above 25c during live games (26-30c only +33% ROI)
- 67 unique events in 90 days, ~7 eligible tickers per game at 6-25c in 0.5-1.5h window
- Kalshi only creates mention markets for nationally-televised/high-profile games

### Category performance (per-category timing windows)
- **NBA (live 0.5-2h, 9-25c):** 1,006 markets, +113% ROI, t=11.79 — highest volume and significance
- **NCAA (live 0.5-1.5h, 6-25c):** 484 markets, +151% ROI, t=9.13
- **Trump (0-24h, 5-30c):** 761 markets, +88% ROI, t=8.15
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
5. ~~Exclude NBA~~ — Re-included with live-game timing (0.5-2h after tipoff, 9-25c)
6. ~~Bump to $3/bet~~ — DONE (26 settled, +114% ROI)
7. ~~Fix buy_price cap bug~~ — DONE
8. ~~Switch NCAA to live-game only (0.5-1.5h after start, 6-25c)~~ — DONE (+151% ROI vs +74% pre-event)
9. Hit 100 settled trades -> bump to $5/bet, deposit to ~$625
10. Validate at $5 -> bump to $10/bet, deposit to ~$1,250
11. ~~Revisit NBA with live-game timing window~~ — DONE (0.5-2h after tipoff, 9-25c, +113% ROI, t=11.79)
11. Optionally implement liquidity-scaled sizing
12. Monitor live fill rates — backtest assumes 100% fill, reality will be lower
13. Track Strategy 1 (reversion) performance once enough trades settle
14. Find additional signal types to stack toward $25k/month target
15. Explore market making on mention markets (top #3 mention trader made $33k/week)
